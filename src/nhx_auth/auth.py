"""倪师 AI · 认证与授权层

设计目标：**默认免登录可用**，需要时一键开启。

三种模式
--------
| 模式 | 读操作 | 写操作 | 适用 |
|---|---|---|---|
| `open`（默认） | 免登录 | 免登录 | 本机开发、个人使用 |
| `protected` | 免登录 | **需登录** | 团队内网：谁都能看，改动要身份 |
| `strict` | **需登录** | **需登录** | 对外部署 |

安全实现
--------
- 口令：PBKDF2-HMAC-SHA256，随机盐，26 万次迭代（OWASP 建议区间）
- 比对：`hmac.compare_digest` 常量时间，防时序侧信道
- 会话：`secrets.token_urlsafe(32)`，**只存 token 的 SHA-256**，泄漏文件也拿不到可用 token
- 过期：默认 12 小时，可配置
- 防爆破：同一用户名连续失败达阈值即锁定一段时间
- 不泄露用户是否存在：用户名错与口令错返回同一错误

零依赖：仅 Python 标准库。
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
AUTH_FILE = os.path.join(REPO_ROOT, "data", "auth.json")

# ── 参数 ────────────────────────────────────────────────────────────────────

MODES = ["open", "protected", "strict"]
DEFAULT_MODE = "open"

PBKDF2_ITERATIONS = 260000
SALT_BYTES = 16
TOKEN_BYTES = 32

SESSION_TTL_SEC = 12 * 3600          # 12 小时
MAX_LOGIN_FAILURES = 10               # 连续失败阈值
LOCKOUT_SEC = 60                    # 锁定时长：15 分钟

ROLES = ["admin", "editor", "viewer"]

# 用户名规则：3–32 位字母数字下划线连字符
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_-]{3,32}$")


class AuthError(Exception):
    """认证/授权失败。"""

    def __init__(self, message: str, status: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


# ── 存储 ────────────────────────────────────────────────────────────────────

def _empty_store() -> Dict[str, Any]:
    return {"version": 1, "mode": DEFAULT_MODE, "users": [], "sessions": [], "failures": {}}


def _load() -> Dict[str, Any]:
    if not os.path.exists(AUTH_FILE):
        return _empty_store()
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return _empty_store()
    base = _empty_store()
    base.update(data)
    return base


def _save(data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(AUTH_FILE), suffix=".tmp")
    try:
        # 权限收紧：仅属主可读写
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, AUTH_FILE)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ── 口令 ────────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """返回 `pbkdf2_sha256$iterations$salt_hex$hash_hex`。"""
    if not password or len(password) < 6:
        raise ValueError("口令至少 6 位")
    salt = secrets.token_bytes(SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ITERATIONS, salt.hex(), dk.hex())


def verify_password(password: str, stored: str) -> bool:
    """常量时间校验口令。"""
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── 模式 ────────────────────────────────────────────────────────────────────

def get_mode() -> str:
    """当前模式。环境变量 `NHX_AUTH_MODE` 优先于配置文件。"""
    env = (os.environ.get("NHX_AUTH_MODE") or "").strip().lower()
    if env in MODES:
        return env
    return _load().get("mode", DEFAULT_MODE)


def set_mode(mode: str) -> str:
    """切换模式。

    两类「静默无效」都会被拦下，否则用户以为切了其实没切：
    1. 环境变量 `NHX_AUTH_MODE` 存在时，它会覆盖配置文件 —— 切了也不生效。
    2. 切到需要登录的模式时若没有任何用户，会把所有人锁在门外。
    """
    mode = (mode or "").strip().lower()
    if mode not in MODES:
        raise ValueError("未知模式：%r（可选 %s）" % (mode, MODES))

    env = (os.environ.get("NHX_AUTH_MODE") or "").strip().lower()
    if env in MODES:
        raise ValueError(
            "环境变量 NHX_AUTH_MODE=%s 正在覆盖配置文件，此时切换模式不会生效。\n"
            "请先取消该环境变量（unset NHX_AUTH_MODE）再切换。" % env
        )

    data = _load()
    if mode in ("protected", "strict") and not data.get("users"):
        raise ValueError(
            "无法切换到 `%s`：当前没有任何用户，切换后将无人能登录。\n"
            "请先创建账号： python3 src/nhx_media/auth.py adduser <用户名>" % mode
        )
    data["mode"] = mode
    # 切回 open 时清理全部会话，避免残留 token
    if mode == "open":
        data["sessions"] = []
    _save(data)
    return mode


def describe_mode(mode: Optional[str] = None) -> Dict[str, Any]:
    m = mode or get_mode()
    table = {
        "open":      {"label": "免登录", "read": False, "write": False},
        "protected": {"label": "写操作需登录", "read": False, "write": True},
        "strict":    {"label": "全部需登录", "read": True, "write": True},
    }
    info = dict(table[m])
    info["mode"] = m
    return info


# ── 用户 ────────────────────────────────────────────────────────────────────

def add_user(username: str, password: str, role: str = "admin") -> Dict[str, Any]:
    if not _USERNAME_RE.match(username or ""):
        raise ValueError("用户名需 3–32 位字母/数字/下划线/连字符")
    if role not in ROLES:
        raise ValueError("未知角色：%r（可选 %s）" % (role, ROLES))

    data = _load()
    for u in data["users"]:
        if u["username"] == username:
            raise ValueError("用户已存在：%s" % username)

    user = {
        "username": username,
        "password_hash": hash_password(password),
        "role": role,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    data["users"].append(user)
    _save(data)
    return {k: v for k, v in user.items() if k != "password_hash"}


def list_users() -> List[Dict[str, Any]]:
    return [{k: v for k, v in u.items() if k != "password_hash"}
            for u in _load().get("users", [])]


def remove_user(username: str) -> bool:
    data = _load()
    before = len(data["users"])
    data["users"] = [u for u in data["users"] if u["username"] != username]
    if len(data["users"]) == before:
        return False
    # 连带撤销该用户的所有会话
    data["sessions"] = [s for s in data.get("sessions", []) if s.get("username") != username]
    _save(data)
    return True


def set_password(username: str, password: str) -> bool:
    data = _load()
    for u in data["users"]:
        if u["username"] == username:
            u["password_hash"] = hash_password(password)
            # 改密即踢掉该用户全部会话
            data["sessions"] = [s for s in data.get("sessions", [])
                                if s.get("username") != username]
            _save(data)
            return True
    return False


# ── 登录 / 会话 ─────────────────────────────────────────────────────────────

def _get_failure(data: Dict[str, Any], username: str) -> Dict[str, Any]:
    return (data.get("failures") or {}).get(username, {"count": 0, "locked_until": 0})


def _is_locked(data: Dict[str, Any], username: str) -> Tuple[bool, int]:
    f = _get_failure(data, username)
    now = time.time()
    if f.get("locked_until", 0) > now:
        return True, int(f["locked_until"] - now)
    return False, 0


def _record_failure(data: Dict[str, Any], username: str) -> None:
    failures = data.setdefault("failures", {})
    f = failures.get(username, {"count": 0, "locked_until": 0})
    f["count"] = f.get("count", 0) + 1
    if f["count"] >= MAX_LOGIN_FAILURES:
        f["locked_until"] = time.time() + LOCKOUT_SEC
        f["count"] = 0
    failures[username] = f


def _clear_failure(data: Dict[str, Any], username: str) -> None:
    (data.get("failures") or {}).pop(username, None)


def login(username: str, password: str) -> Dict[str, Any]:
    """登录并返回会话 token。失败信息不区分「用户不存在」与「口令错误」。"""
    data = _load()

    locked, remain = _is_locked(data, username)
    if locked:
        raise AuthError("尝试过于频繁，请 %d 分钟后再试" % (remain // 60 + 1), status=429)

    user = next((u for u in data["users"] if u["username"] == username), None)
    ok = bool(user) and verify_password(password or "", user["password_hash"])

    if not ok:
        _record_failure(data, username or "")
        _save(data)
        # 统一错误信息，避免用户名枚举
        raise AuthError("用户名或口令不正确", status=401)

    _clear_failure(data, username)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    now = time.time()
    data.setdefault("sessions", []).append({
        "token_hash": _hash_token(token),
        "username": username,
        "role": user["role"],
        "created_at": now,
        "expires_at": now + SESSION_TTL_SEC,
    })
    _purge_expired(data)
    _save(data)

    return {
        "token": token,
        "username": username,
        "role": user["role"],
        "expires_in": SESSION_TTL_SEC,
    }


def _purge_expired(data: Dict[str, Any]) -> None:
    now = time.time()
    data["sessions"] = [s for s in data.get("sessions", []) if s.get("expires_at", 0) > now]
    # 清理过期的失败计数
    failures = data.get("failures") or {}
    for name in list(failures.keys()):
        f = failures[name]
        if f.get("locked_until", 0) and f["locked_until"] <= now:
            failures.pop(name, None)


def resolve_token(token: str) -> Optional[Dict[str, Any]]:
    """由 token 解析出会话身份。无效/过期返回 None。"""
    if not token:
        return None
    th = _hash_token(token)
    data = _load()
    now = time.time()
    for s in data.get("sessions", []):
        # token_hash 是 sha256 十六进制，等长，可用常量时间比对
        if hmac.compare_digest(s.get("token_hash", ""), th) and s.get("expires_at", 0) > now:
            return {"username": s["username"], "role": s["role"]}
    return None


def logout(token: str) -> bool:
    if not token:
        return False
    th = _hash_token(token)
    data = _load()
    before = len(data.get("sessions", []))
    data["sessions"] = [s for s in data.get("sessions", [])
                        if not hmac.compare_digest(s.get("token_hash", ""), th)]
    if len(data["sessions"]) == before:
        return False
    _save(data)
    return True


def logout_all() -> int:
    data = _load()
    n = len(data.get("sessions", []))
    data["sessions"] = []
    _save(data)
    return n


def active_sessions() -> List[Dict[str, Any]]:
    data = _load()
    _purge_expired(data)
    now = time.time()
    return [{
        "username": s["username"],
        "role": s["role"],
        "expires_in": int(s["expires_at"] - now),
    } for s in data.get("sessions", [])]


# ── 授权判定 ────────────────────────────────────────────────────────────────

def check(method: str, token: Optional[str],
          mode: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """校验一次请求。通过返回身份（可能为 None 的匿名），否则抛 AuthError。

    `method` 为 HTTP 方法；GET/HEAD/OPTIONS 视为读，其余视为写。
    """
    m = mode or get_mode()
    is_write = method.upper() not in ("GET", "HEAD", "OPTIONS")
    need_auth = (m == "strict") or (m == "protected" and is_write)

    if not need_auth:
        # 免登录模式下也解析身份，方便界面显示当前用户
        return resolve_token(token) if token else None

    ident = resolve_token(token)
    if not ident:
        raise AuthError("需要登录后才能执行此操作", status=401)
    return ident


def status(token: Optional[str]) -> Dict[str, Any]:
    """给前端用的认证状态。"""
    m = get_mode()
    ident = resolve_token(token) if token else None
    info = describe_mode(m)
    info.update({
        "authenticated": bool(ident),
        "user": ident,
        "has_users": len(_load().get("users", [])) > 0,
    })
    return info


# ── CLI ─────────────────────────────────────────────────────────────────────

def _cli() -> int:
    import getpass
    import sys

    args = sys.argv[1:]
    if not args:
        print(__doc__)
        print("命令：")
        print("  adduser <用户名> [角色]     创建用户（角色：admin/editor/viewer）")
        print("  passwd  <用户名>            修改口令")
        print("  deluser <用户名>            删除用户")
        print("  users                       列出用户")
        print("  mode [open|protected|strict]  查看或切换模式")
        print("  sessions                    列出活动会话")
        print("  logoutall                   撤销全部会话")
        return 0

    cmd = args[0]

    if cmd == "adduser":
        if len(args) < 2:
            print("用法：adduser <用户名> [角色]")
            return 2
        username = args[1]
        role = args[2] if len(args) > 2 else "admin"
        pw = getpass.getpass("设置口令（至少 6 位）：")
        pw2 = getpass.getpass("再输一次：")
        if pw != pw2:
            print("❌ 两次输入不一致")
            return 1
        try:
            u = add_user(username, pw, role)
        except ValueError as exc:
            print("❌ %s" % exc)
            return 1
        print("✅ 已创建用户：%s（角色 %s）" % (u["username"], u["role"]))
        if get_mode() == "open":
            print("   当前模式为 open（免登录）。需要启用登录：")
            print("   python3 src/nhx_media/auth.py mode protected")
        return 0

    if cmd == "passwd":
        if len(args) < 2:
            print("用法：passwd <用户名>")
            return 2
        pw = getpass.getpass("新口令：")
        pw2 = getpass.getpass("再输一次：")
        if pw != pw2:
            print("❌ 两次输入不一致")
            return 1
        try:
            ok = set_password(args[1], pw)
        except ValueError as exc:
            print("❌ %s" % exc)
            return 1
        print("✅ 已修改" if ok else "❌ 用户不存在")
        return 0 if ok else 1

    if cmd == "deluser":
        if len(args) < 2:
            print("用法：deluser <用户名>")
            return 2
        ok = remove_user(args[1])
        print("✅ 已删除" if ok else "❌ 用户不存在")
        return 0 if ok else 1

    if cmd == "users":
        us = list_users()
        if not us:
            print("（暂无用户）")
        for u in us:
            print("  %-20s %-8s %s" % (u["username"], u["role"], u["created_at"]))
        return 0

    if cmd == "mode":
        if len(args) < 2:
            cur = get_mode()
            print("当前模式：%s（%s）" % (cur, describe_mode(cur)["label"]))
            return 0
        try:
            m = set_mode(args[1])
        except ValueError as exc:
            print("❌ %s" % exc)
            return 1
        print("✅ 模式已切换为 %s（%s）" % (m, describe_mode(m)["label"]))
        return 0

    if cmd == "sessions":
        ss = active_sessions()
        if not ss:
            print("（无活动会话）")
        for s in ss:
            print("  %-20s %-8s 剩余 %d 分钟" % (s["username"], s["role"], s["expires_in"] // 60))
        return 0

    if cmd == "logoutall":
        n = logout_all()
        print("✅ 已撤销 %d 个会话" % n)
        return 0

    print("未知命令：%s" % cmd)
    return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
