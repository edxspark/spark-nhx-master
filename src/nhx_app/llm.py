"""倪师 AI · LLM 客户端

面向 OpenAI 兼容协议的流式对话客户端，零第三方依赖（只用标准库）。

为什么不用 SDK：部署环境可能是离线内网、Python 版本受限、或不允许装包。
一个 200 行的 stdlib 客户端比一个依赖树可靠。

⚠️ 代理陷阱（实测踩到，务必保留这段逻辑）
------------------------------------------
`urllib.request.urlopen` 默认会读取**操作系统级代理设置**。
macOS 上如果装了 Clash / Surge / V2Ray 之类工具，`getproxies()` 会返回
`http://127.0.0.1:1082`，于是**所有** HTTP 请求（包括访问 127.0.0.1 本地服务）
都被塞进那个代理，表现为莫名其妙的 `RemoteDisconnected`，
而 curl 与 http.client 却正常 —— 极难排查。

更严重的是安全问题：LLM 请求头里带着 **API Key**，
默认走系统代理等于把密钥交给一个第三方本地进程。

因此本客户端改用 `http.client` —— 它**不读系统代理设置**，行为可预测。
需要代理时用 `NHX_LLM_PROXY` 显式指定（走 CONNECT 隧道）。

支持：
  - 流式（SSE）与非流式
  - 超时、失败重试（指数退避）
  - 明确的错误分类（认证失败 / 限流 / 网络 / 协议）
"""

import http.client
import json
import os
import socket
import ssl
import time
import urllib.error
import urllib.parse
from typing import Any, Dict, Iterator, List, Optional


def parse_base_url(base_url: str):
    """把 `https://host:port/v1` 拆成连接所需的三元组。

    用 http.client 而非 urllib，就是为了**彻底绕开代理**——
    http.client 不读系统代理设置（实测：macOS 系统代理下 urllib 会静默走代理，
    连 127.0.0.1 都被劫持），行为可预测。
    """
    u = urllib.parse.urlsplit(base_url or "")
    if u.scheme not in ("http", "https"):
        raise LLMError("NHX_LLM_BASE_URL 必须以 http:// 或 https:// 开头：%r" % base_url)
    host = u.hostname
    if not host:
        raise LLMError("NHX_LLM_BASE_URL 缺少主机名：%r" % base_url)
    port = u.port or (443 if u.scheme == "https" else 80)
    path = (u.path or "").rstrip("/")
    return u.scheme, host, port, path


class LLMError(Exception):
    """LLM 调用失败。`retryable` 指示调用方是否值得重试。"""

    def __init__(self, message: str, retryable: bool = False, status: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status = status


class LLMNotConfigured(LLMError):
    """未配置 API Key。"""


class LLMClient:
    def __init__(self, base_url: str, api_key: str, model: str,
                 timeout: int = 120, max_tokens: int = 2048,
                 temperature: float = 0.3, max_retries: int = 2,
                 proxy: Optional[str] = None):
        self.base_url = (base_url or "").rstrip("/")
        self.scheme, self.host, self.port, self.base_path = parse_base_url(self.base_url)
        self.api_key = api_key or ""
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.max_retries = max_retries
        # 显式代理为可选能力；默认直连（http.client 本身就不会走代理）
        self.proxy = (proxy if proxy is not None
                      else os.environ.get("NHX_LLM_PROXY", "").strip()) or None

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _require_key(self) -> None:
        if not self.api_key:
            raise LLMNotConfigured(
                "未配置 LLM API Key。请设置环境变量 NHX_LLM_API_KEY 后重启服务。"
            )

    def _connect(self):
        """建立连接。默认直连；显式配置代理时经代理的 CONNECT 隧道。"""
        if self.scheme == "https":
            ctx = ssl.create_default_context()
            if self.proxy:
                phost, pport = _split_proxy(self.proxy)
                raw = socket.create_connection((phost, pport), timeout=self.timeout)
                conn = http.client.HTTPSConnection(self.host, self.port,
                                                   timeout=self.timeout, context=ctx)
                conn.sock = ctx.wrap_socket(raw, server_hostname=self.host)
                return conn
            return http.client.HTTPSConnection(self.host, self.port,
                                               timeout=self.timeout, context=ctx)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def _payload(self, messages: List[Dict[str, str]], stream: bool) -> bytes:
        return json.dumps({
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }, ensure_ascii=False).encode("utf-8")

    def _headers(self, stream: bool) -> Dict[str, str]:
        return {
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": "Bearer " + self.api_key,
            "Accept": "text/event-stream" if stream else "application/json",
            "User-Agent": "nishi-ai/1.0",
            "Connection": "close",
        }

    @staticmethod
    def _classify(exc: Exception) -> LLMError:
        if isinstance(exc, socket.timeout):
            return LLMError("LLM 请求超时，请稍后重试或调大 NHX_LLM_TIMEOUT。", retryable=True)
        if isinstance(exc, ssl.SSLError):
            return LLMError("LLM TLS 握手失败：%s。请检查网络或证书环境。" % exc,
                            retryable=False)
        if isinstance(exc, (ConnectionError, OSError, http.client.HTTPException)):
            return LLMError(
                "无法连接 LLM 服务：%s。请检查网络与 NHX_LLM_BASE_URL。" % exc,
                retryable=True)
        return LLMError("LLM 调用异常：%s" % exc, retryable=False)

    @staticmethod
    def _http_error(status: int, detail: str) -> LLMError:
        if status == 401:
            return LLMError("LLM 认证失败（401）：API Key 无效或已过期。%s" % detail,
                            retryable=False, status=status)
        if status == 402:
            return LLMError("LLM 账户余额不足（402）。%s" % detail,
                            retryable=False, status=status)
        if status == 429:
            return LLMError("LLM 触发限流（429），请稍后重试。%s" % detail,
                            retryable=True, status=status)
        if 500 <= status < 600:
            return LLMError("LLM 服务端错误（%d），可重试。%s" % (status, detail),
                            retryable=True, status=status)
        return LLMError("LLM 请求被拒（%d）：%s" % (status, detail),
                        retryable=False, status=status)

    # ── 流式 ────────────────────────────────────────────────────────────────

    def stream_chat(self, messages: List[Dict[str, str]]) -> Iterator[str]:
        """逐段产出文本增量。失败时抛 LLMError。"""
        self._require_key()
        attempt = 0
        while True:
            conn = None
            try:
                conn = self._connect()
                conn.request("POST", self.base_path + "/chat/completions",
                             body=self._payload(messages, True),
                             headers=self._headers(True))
                resp = conn.getresponse()
                if resp.status != 200:
                    detail = resp.read().decode("utf-8", "replace")[:300]
                    raise self._http_error(resp.status, detail)
                yield from self._iter_sse(resp)
                return
            except LLMError:
                raise
            except Exception as exc:  # noqa: BLE001
                err = self._classify(exc)
                attempt += 1
                if err.retryable and attempt <= self.max_retries:
                    time.sleep(0.8 * (2 ** (attempt - 1)))
                    continue
                raise err
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass

    @staticmethod
    def _iter_sse(resp) -> Iterator[str]:
        """解析 OpenAI 兼容的 SSE 流。"""
        buf = b""
        while True:
            chunk = resp.read(1)
            if not chunk:
                break
            if chunk != b"\n":
                buf += chunk
                continue
            line = buf.decode("utf-8", "replace").strip()
            buf = b""
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                return
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            piece = (choices[0].get("delta") or {}).get("content")
            if piece:
                yield piece

    # ── 非流式 ──────────────────────────────────────────────────────────────

    def chat(self, messages: List[Dict[str, str]]) -> str:
        self._require_key()
        attempt = 0
        while True:
            conn = None
            try:
                conn = self._connect()
                conn.request("POST", self.base_path + "/chat/completions",
                             body=self._payload(messages, False),
                             headers=self._headers(False))
                resp = conn.getresponse()
                raw = resp.read().decode("utf-8", "replace")
                if resp.status != 200:
                    raise self._http_error(resp.status, raw[:300])
                data = json.loads(raw)
                choices = data.get("choices") or []
                if not choices:
                    raise LLMError("LLM 返回为空，未包含 choices。", retryable=True)
                return (choices[0].get("message") or {}).get("content", "") or ""
            except LLMError:
                raise
            except Exception as exc:  # noqa: BLE001
                err = self._classify(exc)
                attempt += 1
                if err.retryable and attempt <= self.max_retries:
                    time.sleep(0.8 * (2 ** (attempt - 1)))
                    continue
                raise err
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass


def _split_proxy(proxy: str):
    u = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
    return u.hostname or "127.0.0.1", u.port or 8080
