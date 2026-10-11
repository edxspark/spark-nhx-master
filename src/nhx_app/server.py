"""倪师 AI · Web 服务

独立可部署版本：不依赖 DSH / SparkWork 运行时，标准库即可跑起来。

启动：
    export NHX_LLM_API_KEY=sk-xxxx
    python3 src/nhx_app/server.py --host 0.0.0.0 --port 8790

接口：
    GET  /                    聊天界面
    GET  /api/health          健康检查（供反代 / 监控探活）
    GET  /api/config          前端可见配置（不含密钥）
    GET  /api/kb/stats        知识库索引概况
    POST /api/session         新建会话
    GET  /api/session/<id>    读取会话
    DELETE /api/session/<id>  删除会话
    POST /api/chat            SSE 流式对话
    POST /api/triage          仅分诊（调试 / 集成用）
    POST /api/auth/login      登录
    POST /api/auth/logout     登出
    GET  /api/auth/status     认证状态

部署要点：
  - 前面挂 Nginx 并 **关闭 proxy_buffering**，否则 SSE 会被缓冲成一次性输出。
  - 生产环境务必设置 NHX_AUTH_MODE=protected 或 strict，并配置 HTTPS。
"""

import argparse
import json
import os
import sys
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)
sys.path.insert(0, os.path.join(APP_DIR, ".."))

from nhx_auth import auth as auth_mod  # noqa: E402
from config import Config, REPO_ROOT  # noqa: E402
from kb import KnowledgeBase  # noqa: E402
from llm import LLMClient, LLMError  # noqa: E402
from pipeline import Pipeline  # noqa: E402
from sessions import SessionStore  # noqa: E402

STATIC_DIR = os.path.join(APP_DIR, "static")

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
}

# 免登录也允许访问的路径（认证自身）
_AUTH_OPEN = {
    ("GET", "/api/health"),
    ("GET", "/api/config"),
    ("GET", "/api/auth/status"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
}


class App:
    """全局单例：配置、LLM、KB、会话、管线。"""

    def __init__(self, config: Config):
        self.config = config
        auth_mod.AUTH_FILE = config.auth_file
        os.makedirs(config.data_dir, exist_ok=True)
        # 数据目录含会话（用户健康信息）与认证数据，收紧为仅属主可访问
        try:
            os.chmod(config.data_dir, 0o700)
            os.makedirs(config.sessions_dir, exist_ok=True)
            os.chmod(config.sessions_dir, 0o700)
        except OSError:
            pass

        self.llm = LLMClient(
            base_url=config.llm_base_url,
            api_key=config.llm_api_key,
            model=config.llm_model,
            timeout=config.llm_timeout,
            max_tokens=config.llm_max_tokens,
            temperature=config.llm_temperature,
        )
        self.kb = KnowledgeBase(config.knowledge_dir, config.kb_max_snippets)
        self.sessions = SessionStore(config.sessions_dir, config.session_ttl_sec)
        self.pipeline = Pipeline(config, llm=self.llm, kb=self.kb)


APP: App = None  # 由 main() 注入


class Handler(BaseHTTPRequestHandler):
    server_version = "NiShiAI/1.0"
    protocol_version = "HTTP/1.1"

    # ── 响应工具 ────────────────────────────────────────────────────────────

    def _json(self, status, payload, extra=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, data):
        self._json(200, {"ok": True, "data": data})

    def _fail(self, status, message):
        self._json(status, {"ok": False, "error": message})

    def log_message(self, fmt, *args):
        sys.stderr.write("[nhx] %s %s\n" % (self.address_string(), fmt % args))

    # ── 请求体 / 令牌 ───────────────────────────────────────────────────────

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("Content-Length 非法")
        if length <= 0:
            return {}
        if length > 4 * 1024 * 1024:
            raise ValueError("请求体过大（上限 4 MB）")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体不是合法 JSON：%s" % exc)

    def _token(self):
        h = self.headers.get("Authorization") or ""
        return h[7:].strip() if h.startswith("Bearer ") else None

    def _drain_body(self) -> None:
        """读完请求体再回响应。

        **不做这一步会出严重问题**：若在读取 body 之前就返回 401/403，
        未读的字节会留在 socket 缓冲里。浏览器复用同一条 keep-alive 连接
        发下一个请求时，服务器先读到的是**上一个请求的残留 body**，
        请求行被拼成 `{"text":"..."}POST /api/auth/login HTTP/1.1`，
        于是返回 501，前端拿到 HTML 而不是 JSON，报
        `Unexpected token '<', "<!DOCTYPE "... is not valid JSON`。

        实测踩到过：未登录点发送 → 401 → 弹登录框 → **登录必失败**。
        """
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return
        if length <= 0 or length > 8 * 1024 * 1024:
            return
        try:
            self.rfile.read(length)
        except (OSError, ValueError):
            pass

    # ── 路由 ────────────────────────────────────────────────────────────────

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Allow", "GET, POST, DELETE, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        try:
            if path.startswith("/api"):
                if (method, path) not in _AUTH_OPEN:
                    auth_mod.check(method, self._token(), mode=APP.config.auth_mode)
                return self._api(method, path, query)
            if method == "GET":
                return self._static(path)
            self._fail(405, "方法不允许")
        except auth_mod.AuthError as exc:
            self._drain_body()   # 关键：先读完再回，否则残体污染同连接的下一个请求
            self._fail(exc.status, exc.message)
        except ValueError as exc:
            self._drain_body()
            self._fail(400, str(exc))
        except FileNotFoundError as exc:
            self._drain_body()
            self._fail(404, str(exc))
        except BrokenPipeError:
            pass  # 客户端断开（SSE 常见），静默
        except Exception:
            self._drain_body()
            traceback.print_exc()
            self._fail(500, "服务内部错误，详见服务端日志")

    # ── API ─────────────────────────────────────────────────────────────────

    def _api(self, method, path, query):
        cfg = APP.config

        if path == "/api/health":
            return self._ok({
                "status": "ok",
                "model": cfg.llm_model,
                "llm_ready": cfg.llm_ready(),
                "auth_mode": cfg.auth_mode,
                "uptime_hint": "process alive",
            })

        if path == "/api/config":
            return self._ok(cfg.as_public_dict())

        if path == "/api/kb/stats":
            return self._ok(APP.kb.stats())

        # ── 认证 ────────────────────────────────────────────────────────────
        if path == "/api/auth/status":
            return self._ok(auth_mod.status(self._token()))

        if path == "/api/auth/login":
            body = self._body()
            try:
                session = auth_mod.login(body.get("username", ""), body.get("password", ""))
            except auth_mod.AuthError as exc:
                return self._fail(exc.status, exc.message)
            return self._ok(session)

        if path == "/api/auth/logout":
            auth_mod.logout(self._token())
            return self._ok({"logged_out": True})

        # ── 会话 ────────────────────────────────────────────────────────────
        if path == "/api/session" and method == "POST":
            ident = auth_mod.resolve_token(self._token())
            doc = APP.sessions.create(user_ref=(ident or {}).get("username", "anonymous"))
            return self._ok({"session_id": doc["session_id"], "created_at": doc["created_at"]})

        if path.startswith("/api/session/"):
            sid = path[len("/api/session/"):]
            if method == "GET":
                doc = APP.sessions.get(sid)
                if not doc:
                    return self._fail(404, "会话不存在或已过期")
                return self._ok({
                    "session_id": doc["session_id"],
                    "messages": doc.get("messages", []),
                    "updated_at": doc.get("updated_at"),
                })
            if method == "DELETE":
                return self._ok({"deleted": APP.sessions.delete(sid)})

        # ── 仅分诊 ──────────────────────────────────────────────────────────
        if path == "/api/triage" and method == "POST":
            body = self._body()
            return self._ok(APP.pipeline.triage_only(body.get("text", "")))

        # ── 对话（SSE）──────────────────────────────────────────────────────
        if path == "/api/chat" and method == "POST":
            body = self._body()
            return self._chat(body)

        raise FileNotFoundError("未知 API 路径：%s" % path)

    # ── SSE 对话 ────────────────────────────────────────────────────────────

    def _sse_open(self):
        """开启 SSE 流。

        关键点：**不用 chunked、改用 `Connection: close`**。
        我们的模型是「一次请求 = 一条流，写完即结束」，让客户端以 EOF 判定结束
        最简单可靠。若保持 keep-alive 又不发 Content-Length，客户端无法判断
        响应何时结束，会一直挂着（实测 curl 卡到 60s 超时）。
        """
        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")   # 关键：告诉 Nginx 不要缓冲
        self.end_headers()

    def _sse_send(self, event, data):
        payload = "event: %s\ndata: %s\n\n" % (
            event, json.dumps(data, ensure_ascii=False))
        self.wfile.write(payload.encode("utf-8"))
        self.wfile.flush()

    def _chat(self, body):
        text = (body.get("text") or "").strip()
        if not text:
            return self._fail(400, "缺少 `text`")
        if len(text) > 20000:
            return self._fail(413, "输入过长（上限 20000 字符）")

        sid = body.get("session_id")
        if not sid:
            ident = auth_mod.resolve_token(self._token())
            doc = APP.sessions.create(user_ref=(ident or {}).get("username", "anonymous"))
            sid = doc["session_id"]
        elif APP.sessions.get(sid) is None:
            return self._fail(404, "会话不存在或已过期")

        history = APP.sessions.history_for_llm(sid, APP.config.max_history_messages)
        APP.sessions.append_message(sid, "user", text)

        self._sse_open()
        self._sse_send("session", {"session_id": sid})

        final_text = ""
        layer = ""
        emergency = False
        guard_info = {}

        try:
            for ev in APP.pipeline.run(text, history=history, session_id=sid):
                name = ev["event"]
                data = ev["data"]
                if name == "done":
                    final_text = data.get("text", "")
                    layer = data.get("layer", "")
                    emergency = bool(data.get("emergency"))
                elif name == "guard":
                    guard_info = data
                self._sse_send(name, data)
        except BrokenPipeError:
            return
        except Exception as exc:  # noqa: BLE001
            try:
                self._sse_send("error", {"message": str(exc)})
            except Exception:
                pass
            return

        if final_text:
            APP.sessions.append_message(
                sid, "assistant", final_text,
                meta={"layer": layer, "emergency": emergency,
                      "guard": guard_info.get("verdict")})
        self._sse_send("end", {"session_id": sid})

    # ── 静态文件 ────────────────────────────────────────────────────────────

    def _static(self, path):
        rel = "index.html" if path == "/" else path.lstrip("/")
        target = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not target.startswith(os.path.normpath(STATIC_DIR)):
            return self._fail(403, "禁止访问")
        if not os.path.isfile(target):
            # SPA 回退
            target = os.path.join(STATIC_DIR, "index.html")
            if not os.path.isfile(target):
                return self._fail(404, "资源不存在")
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", _MIME.get(ext, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


def _lan_ip() -> str:
    """取本机在**局域网**中的出口 IP。

    注意：装了 Clash / Surge 之类工具时，`connect("8.8.8.8")` 可能返回
    TUN 网卡地址（如 198.18.0.1，属 RFC2544 基准测试段），
    那个地址在浏览器里打不开，对用户毫无意义。因此做一次范围过滤。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()

    # 198.18.0.0/15 = 代理工具 TUN；169.254/16 = link-local；都不是可访问的局域网地址
    if ip.startswith(("198.18.", "198.19.", "169.254.")):
        return ""
    return ip


def browsable_urls(host: str, port: int) -> list:
    """把**绑定地址**翻译成**浏览器可访问的地址**。

    为什么需要这个：`0.0.0.0` 是「监听所有网卡」的绑定地址，**不是**可访问地址。
    Chrome 128+ 出于防 DNS rebinding 的考虑，直接屏蔽对 `0.0.0.0` 的导航与
    fetch —— 表现是页面全白、`document.body` 为空、连 title 都没有，
    但服务器日志显示 200，极难排查。

    因此绑定 0.0.0.0 / :: 时，必须告诉用户 127.0.0.1（本机）与局域网 IP。
    """
    wildcards = ("0.0.0.0", "::", "", "*")
    if host not in wildcards:
        return ["http://%s:%d" % (host, port)]

    urls = ["http://127.0.0.1:%d" % port]
    ip = _lan_ip()
    if ip and ip != "127.0.0.1":
        urls.append("http://%s:%d" % (ip, port))
    return urls


def main():
    global APP

    ap = argparse.ArgumentParser(description="倪师 AI Web 服务")
    ap.add_argument("--host", help="绑定地址（默认取 NHX_HOST，127.0.0.1）")
    ap.add_argument("--port", type=int, help="端口（默认取 NHX_PORT，8790）")
    ap.add_argument("--check", action="store_true", help="仅自检配置后退出")
    args = ap.parse_args()

    config = Config()
    if args.host:
        config.host = args.host
    if args.port:
        config.port = args.port

    APP = App(config)

    n_docs = APP.kb.build()
    urls = browsable_urls(config.host, config.port)

    print("倪师 AI · Web 服务")
    print("  监听：%s:%d%s" % (config.host, config.port,
                             "（所有网卡）" if config.host in ("0.0.0.0", "::") else ""))
    print("  ★ 请在浏览器打开：%s" % urls[0])
    for extra in urls[1:]:
        print("    （同一局域网的其他设备可用）%s" % extra)
    if config.host in ("0.0.0.0", "::"):
        print("    ⚠️ 不要访问 http://%s:%d —— 那是绑定地址，"
              "现代浏览器会直接屏蔽（页面全白）" % (config.host, config.port))
    print("  模型：%s @ %s" % (config.llm_model, config.llm_base_url))
    key_src = config.source_of("NHX_LLM_API_KEY")
    print("  API Key：%s" % (
        ("已配置（来自 .env）" if key_src == "dotenv" else "已配置（来自环境变量）")
        if config.llm_ready() else "❌ 未配置（对话将不可用）"))
    print("  配置来源：%s" % config.describe_source())
    print("  知识库：%d 篇文档（%s）" % (n_docs, config.knowledge_dir))
    print("  认证：%s（%d 个账号）" % (config.auth_mode, len(auth_mod.list_users())))
    print("  守门：%s（action=%s）" % (
        "开启" if config.guard_enabled else "关闭", config.guard_action))
    print("  契约：%s（%d 字符）" % (
        config.contract_path, len(APP.pipeline.contract)))
    print()

    # 防呆（AGENT.md §6.5）：需要登录却没有任何账号 = 把自己锁在门外
    lockout = (config.auth_mode in ("protected", "strict")
               and not auth_mod.list_users())

    if args.check:
        problems = []
        if not config.llm_ready():
            problems.append("未配置 NHX_LLM_API_KEY（当前配置来源：%s）" % config.describe_source())
        if lockout:
            problems.append("认证模式 %s 需要账号，但一个都没有 —— 启动后将无人能登录"
                            % config.auth_mode)
        if not APP.pipeline.contract:
            problems.append("未能从 AGENT.md 读取到行为契约")
        if n_docs == 0:
            problems.append("知识库为空（%s）" % config.knowledge_dir)
        for line in config.dotenv.get("invalid", []):
            problems.append(".env 里有无法解析的行：%r" % line)
        if problems:
            for p in problems:
                print("  ⚠️  %s" % p)
            return 1
        print("  ✅ 自检通过")
        return 0

    if lockout:
        print("  ❌ 认证模式 %s 需要账号，但当前一个都没有 —— 启动后没人能登录。" % config.auth_mode)
        print("     先建账号： python3 src/nhx_auth/auth.py adduser <用户名>")
        print("     或改回免登录：在 .env 里设 NHX_AUTH_MODE=open")
        return 1

    if not config.llm_ready():
        print("  ⚠️  未配置 NHX_LLM_API_KEY，服务可启动但对话会报错。")
        print("     ① 写进文件：%s" % os.path.join(REPO_ROOT, ".env"))
        print("        内容形如：NHX_LLM_API_KEY=sk-xxxx（顶格、半角等号、不要中文冒号）")
        print("     ② 或临时导出：export NHX_LLM_API_KEY=sk-xxxx")
        print("     ③ 排查：python3 src/nhx_app/server.py --check")
        print()

    try:
        httpd = ThreadingHTTPServer((config.host, config.port), Handler)
    except OSError as exc:
        # 端口占用是最常见的启动失败，给出可操作的指引而不是裸 traceback
        if getattr(exc, "errno", None) in (48, 98):      # macOS 48 / Linux 98
            print("  ❌ 端口 %d 已被占用，无法启动。" % config.port)
            print("     查占用：lsof -nP -iTCP:%d -sTCP:LISTEN" % config.port)
            print("     结束它：kill $(lsof -t -iTCP:%d -sTCP:LISTEN)" % config.port)
            print("     或换端口：在 .env 里改 NHX_PORT，或 python3 src/nhx_app/server.py --port 8791")
        elif getattr(exc, "errno", None) in (13, 1):     # 权限不足（<1024 端口）
            print("  ❌ 无权限绑定 %s:%d —— 1024 以下端口需要 root。" % (config.host, config.port))
            print("     换高位端口，或用 sudo 运行。")
        else:
            print("  ❌ 无法绑定 %s:%d：%s" % (config.host, config.port, exc))
        return 1
    httpd.daemon_threads = True
    httpd.allow_reuse_address = True

    def _purge_loop():
        while True:
            time.sleep(3600)
            try:
                APP.sessions.purge_expired()
            except Exception:
                pass

    threading.Thread(target=_purge_loop, daemon=True).start()

    print("  停止：Ctrl+C")
    print()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
