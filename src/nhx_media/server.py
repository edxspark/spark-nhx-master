"""倪师 AI · 媒体资源管理服务

零依赖 HTTP 服务：REST API + 管理界面。

启动：
    python3 src/nhx_media/server.py                 # 默认 http://127.0.0.1:8788
    python3 src/nhx_media/server.py --port 9000
    python3 src/nhx_media/server.py --host 0.0.0.0  # 局域网可访问（慎用）

安全说明
--------
默认只绑定 127.0.0.1。本服务**无鉴权**，可直接改写索引文件，
因此**不要**暴露到公网；需要远程访问请置于反向代理 + 鉴权之后。
"""

import argparse
import json
import os
import sys
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'nhx_app'))

from nhx_auth import auth  # noqa: E402
import env as env_mod  # noqa: E402  （与 Web 应用共用同一个 .env 加载器）
import store  # noqa: E402

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
}

# 免登录也允许访问的 API（认证自身 + 只读元信息）
_AUTH_OPEN_PATHS = {
    ("GET", "/api/auth/status"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


class Handler(BaseHTTPRequestHandler):
    server_version = "NHXMedia/0.2"

    # ── 基础响应 ────────────────────────────────────────────────────────────

    def _send(self, status, body, content_type="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _ok(self, data, extra=None):
        self._send(200, {"ok": True, "data": data}, extra=extra)

    def _fail(self, status, message):
        self._send(status, {"ok": False, "error": message})

    def log_message(self, fmt, *args):
        sys.stderr.write("[nhx-media] %s - %s\n" % (self.address_string(), fmt % args))

    # ── 认证辅助 ────────────────────────────────────────────────────────────

    def _token(self):
        h = self.headers.get("Authorization") or ""
        if h.startswith("Bearer "):
            return h[7:].strip()
        return None

    def _drain_body(self) -> None:
        """读完请求体再回响应。

        **不做这一步会出严重问题**：若在读取 body 之前就返回 401/403，
        未读字节会留在 socket 缓冲里；浏览器复用同一条 keep-alive 连接时，
        服务器会把残留 body 当成下一个请求的请求行，返回 501，
        前端拿到 HTML 而非 JSON。（实测踩到，详见 nhx_app/server.py 同名方法。）
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

    def _authorize(self, method, path):
        """按当前模式校验权限。返回身份（可能为 None）。"""
        if (method, path) in _AUTH_OPEN_PATHS:
            return None
        return auth.check(method, self._token())

    # ── 请求体 ──────────────────────────────────────────────────────────────

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Content-Length 非法")
        if length <= 0:
            return {}
        if length > 8 * 1024 * 1024:
            raise ApiError(413, "请求体过大（上限 8 MB）")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, "请求体不是合法 JSON：%s" % exc)

    # ── 路由 ────────────────────────────────────────────────────────────────

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        try:
            if path.startswith("/api"):
                self._authorize(method, path)
                return self._api(method, path, query)
            if method == "GET":
                return self._static(path)
            self._fail(405, "方法不允许")
        except auth.AuthError as exc:
            self._drain_body()   # 关键：先读完再回，否则残体污染同连接的下一个请求
            self._fail(exc.status, exc.message)
        except ApiError as exc:
            self._drain_body()
            self._fail(exc.status, exc.message)
        except FileNotFoundError as exc:
            self._drain_body()
            self._fail(404, str(exc))
        except ValueError as exc:
            self._drain_body()
            self._fail(400, str(exc))
        except Exception:
            self._drain_body()
            traceback.print_exc()
            self._fail(500, "服务内部错误，详见服务端日志")

    # ── API ─────────────────────────────────────────────────────────────────

    def _api(self, method, path, query):
        parts = [p for p in path.split("/") if p]  # ['api', ...]

        # ── 认证 ────────────────────────────────────────────────────────────
        if method == "GET" and parts == ["api", "auth", "status"]:
            return self._ok(auth.status(self._token()))

        if method == "POST" and parts == ["api", "auth", "login"]:
            body = self._body()
            try:
                session = auth.login(body.get("username", ""), body.get("password", ""))
            except auth.AuthError as exc:
                return self._fail(exc.status, exc.message)
            return self._ok(session)

        if method == "POST" and parts == ["api", "auth", "logout"]:
            auth.logout(self._token())
            return self._ok({"logged_out": True})

        # GET /api/meta
        if method == "GET" and parts == ["api", "meta"]:
            return self._ok({
                "statuses": store.STATUSES,
                "status_labels": store.STATUS_LABELS,
                "license_statuses": store.LICENSE_STATUSES,
                "source_types": store.SOURCE_TYPES,
                "kinds": list(store.KIND_DIRS.keys()),
                "bitrate_presets": list(store.BITRATE_PRESETS_BYTES_PER_SEC.keys()),
                "auth": auth.status(self._token()),
            })

        # GET /api/series
        if method == "GET" and parts == ["api", "series"]:
            kind = query.get("kind", [None])[0]
            return self._ok(store.list_series(kind))

        # GET /api/stats
        if method == "GET" and parts == ["api", "stats"]:
            bitrate = query.get("bitrate", [store.DEFAULT_BITRATE])[0]
            return self._ok(store.stats(bitrate=bitrate))

        # GET /api/validate
        if method == "GET" and parts == ["api", "validate"]:
            return self._ok(store.validate_all())

        # GET /api/items  |  POST /api/items
        if parts == ["api", "items"]:
            if method == "GET":
                items = store.list_items(
                    kind=query.get("kind", [None])[0],
                    series_id=query.get("series", [None])[0],
                    status=query.get("status", [None])[0],
                    query=query.get("q", [None])[0],
                )
                try:
                    limit = int(query.get("limit", ["100"])[0])
                    offset = int(query.get("offset", ["0"])[0])
                except ValueError:
                    raise ApiError(400, "limit/offset 必须是整数")
                total = len(items)
                return self._ok({
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                    "items": items[offset:offset + limit],
                })
            if method == "POST":
                body = self._body()
                kind = body.get("kind")
                series_id = body.get("series_id")
                item = body.get("item") or {}
                if not kind or not series_id:
                    raise ApiError(400, "缺少 `kind` 或 `series_id`")
                return self._ok(store.upsert_item(kind, series_id, item))

        # /api/items/{id}[/action]
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "items":
            item_id = parts[2]

            if len(parts) == 3 and method == "GET":
                return self._ok(store.find_item(item_id))
            if len(parts) == 3 and method == "DELETE":
                return self._ok(store.delete_item(item_id))

            if len(parts) == 4 and method == "POST":
                action = parts[3]
                body = self._body()
                if action == "status":
                    status = body.get("status")
                    if not status:
                        raise ApiError(400, "缺少 `status`")
                    return self._ok(store.set_status(item_id, status))
                if action == "segments":
                    return self._ok(store.set_segments(item_id, body.get("segments") or []))
                raise ApiError(404, "未知操作：%s" % action)

        # POST /api/offline
        if method == "POST" and parts == ["api", "offline"]:
            body = self._body()
            return self._ok(store.bulk_offline(
                license_status=body.get("license_status"),
                source_type=body.get("source_type"),
            ))

        raise ApiError(404, "未知 API 路径：%s" % path)

    # ── 静态文件 ────────────────────────────────────────────────────────────

    def _static(self, path):
        rel = "index.html" if path == "/" else path.lstrip("/")
        # 防目录穿越
        target = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not target.startswith(os.path.normpath(STATIC_DIR)):
            return self._fail(403, "禁止访问")
        if not os.path.isfile(target):
            return self._fail(404, "资源不存在：%s" % rel)
        ext = os.path.splitext(target)[1].lower()
        with open(target, "rb") as fh:
            body = fh.read()
        self._send(200, body, _MIME.get(ext, "application/octet-stream"))


def main():
    # 与 Web 应用一致：先把仓库根 .env 读进环境（真实环境变量优先）。
    # 否则 .env 里设的 NHX_AUTH_MODE 对本服务不生效，两个服务的认证模式会不一致。
    env_mod.load_dotenv()

    ap = argparse.ArgumentParser(description="倪师 AI 媒体资源管理服务")
    ap.add_argument("--host", default="127.0.0.1",
                    help="绑定地址（默认 127.0.0.1；本服务默认无鉴权，勿暴露公网）")
    ap.add_argument("--port", type=int, default=8788, help="端口（默认 8788）")
    ap.add_argument("--mode", choices=auth.MODES,
                    help="覆盖认证模式（默认读配置；open=免登录，protected=写需登录，strict=全需登录）")
    args = ap.parse_args()

    if args.mode:
        try:
            auth.set_mode(args.mode)
        except ValueError as exc:
            print("❌ %s" % exc)
            return 1

    store.ensure_dirs()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)

    mode = auth.get_mode()
    info = auth.describe_mode(mode)
    has_users = len(auth.list_users()) > 0

    url = "http://%s:%d" % (args.host, args.port)
    print("倪师 AI · 媒体资源管理服务")
    print("  地址：%s" % url)
    print("  索引：%s" % os.path.relpath(store.MEDIA_ROOT, store.REPO_ROOT))
    print("  认证：%s（%s）%s" % (mode, info["label"], "" if has_users else " — 尚无用户"))
    if mode == "open":
        print("        默认免登录，开箱即用。需要登录时：")
        print("        python3 src/nhx_media/auth.py adduser <用户名>")
        print("        python3 src/nhx_media/auth.py mode protected")
    print("  停止：Ctrl+C")
    if args.host not in ("127.0.0.1", "localhost") and mode == "open":
        print("  ⚠️  已绑定非本机地址且为免登录模式——任何能访问该端口的人都能改写索引。")
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
