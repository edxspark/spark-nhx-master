"""倪师 AI · 会话存储

按会话 ID 存 JSON 文件。为什么不用数据库：
- 单机部署时几十万条会话用文件完全够用
- 零依赖（不需要装 sqlite 之外的任何东西，且文件可 grep 可人工修复）
- 便于审计与取证：一次对话就是一个文件

并发安全：进程内用锁；写用「临时文件 + rename」保证原子性。
"""

import json
import os
import re
import secrets
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

_SID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

MAX_MESSAGE_CHARS = 20000


def new_session_id() -> str:
    return "s-" + secrets.token_urlsafe(16)


class SessionStore:
    def __init__(self, root: str, ttl_sec: int = 7 * 24 * 3600):
        self.root = root
        self.ttl_sec = ttl_sec
        self._lock = threading.Lock()
        os.makedirs(root, exist_ok=True)

    # ── 内部 ────────────────────────────────────────────────────────────────

    def _path(self, sid: str) -> str:
        if not _SID_RE.match(sid or ""):
            raise ValueError("会话 ID 格式非法")
        return os.path.join(self.root, "%s.json" % sid)

    @staticmethod
    def _write_atomic(path: str, data: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ── 公共 API ────────────────────────────────────────────────────────────

    def create(self, user_ref: str = "anonymous", layer_hint: str = "") -> Dict[str, Any]:
        sid = new_session_id()
        now = time.time()
        doc = {
            "session_id": sid,
            "user_ref": user_ref,
            "created_at": now,
            "updated_at": now,
            "messages": [],
            "layer_hint": layer_hint,
        }
        with self._lock:
            self._write_atomic(self._path(sid), doc)
        return doc

    def get(self, sid: str) -> Optional[Dict[str, Any]]:
        path = self._path(sid)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - doc.get("updated_at", 0) > self.ttl_sec:
            return None
        return doc

    def append_message(self, sid: str, role: str, content: str,
                       meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        content = (content or "")[:MAX_MESSAGE_CHARS]
        with self._lock:
            doc = self.get(sid)
            if doc is None:
                raise FileNotFoundError("会话不存在或已过期")
            doc["messages"].append({
                "role": role,
                "content": content,
                "at": time.time(),
                "meta": meta or {},
            })
            # 单会话最多保留 200 轮，防文件无限增长
            if len(doc["messages"]) > 400:
                doc["messages"] = doc["messages"][-400:]
            doc["updated_at"] = time.time()
            self._write_atomic(self._path(sid), doc)
        return doc

    def history_for_llm(self, sid: str, limit: int = 20) -> List[Dict[str, str]]:
        doc = self.get(sid)
        if not doc:
            return []
        msgs = [{"role": m["role"], "content": m["content"]}
                for m in doc.get("messages", [])
                if m.get("role") in ("user", "assistant") and m.get("content")]
        return msgs[-limit:]

    def delete(self, sid: str) -> bool:
        path = self._path(sid)
        if os.path.exists(path):
            os.remove(path)
            return True
        return False

    def purge_expired(self) -> int:
        """清理过期会话。由服务定时调用。"""
        now = time.time()
        n = 0
        for fn in os.listdir(self.root):
            if not fn.endswith(".json"):
                continue
            path = os.path.join(self.root, fn)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    doc = json.load(fh)
                if now - doc.get("updated_at", 0) > self.ttl_sec:
                    os.remove(path)
                    n += 1
            except (OSError, json.JSONDecodeError):
                continue
        return n

    def stats(self) -> Dict[str, Any]:
        total = 0
        messages = 0
        for fn in os.listdir(self.root):
            if not fn.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.root, fn), "r", encoding="utf-8") as fh:
                    doc = json.load(fh)
                total += 1
                messages += len(doc.get("messages", []))
            except (OSError, json.JSONDecodeError):
                continue
        return {"sessions": total, "messages": messages}
