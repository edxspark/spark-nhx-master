"""倪师 AI · `.env` 读取（纯标准库，零依赖）

**为什么需要这个模块**

Python 不会自动读取 `.env`。曾经的真实故障：

    cp .env.example .env   # 填好 NHX_LLM_API_KEY
    python3 src/nhx_app/server.py
    #   → API Key：❌ 未配置（对话将不可用）

配置只读 `os.environ`，而 `.env` 只有 Docker 的 `env_file:` 与 systemd 的
`EnvironmentFile=` 会注入。**本地直接 `python3 ...` 跑时，文件被完全忽略**，
用户看到的是「明明填了却说没填」。

**规则**（对齐常见 dotenv 实现，但不引入第三方库 —— AGENT.md §6.8）

* 只认 `KEY=VALUE`；空行与 `#` 开头的行是注释；支持 `export KEY=VALUE`
* 值可用单引号 / 双引号包裹；双引号内支持 `\\n` `\\t` `\\r` `\\"` `\\\\`
* 不加引号的值，会去掉行尾 ` # 注释`
* 键名必须是 `[A-Za-z_][A-Za-z0-9_]*`，否则跳过并计入 `invalid`
* **真实环境变量优先，绝不覆盖**（12-factor：真环境 > .env）
* 文件不存在就静默跳过 —— `.env` 是可选的

**查找顺序**：`NHX_ENV_FILE` 指定的路径 → 仓库根 `.env` → 当前工作目录 `.env`
**关闭开关**：`NHX_NO_DOTENV=1` 只关「自动发现」；显式传 path 时照读
（测试 / CI 用，保证环境干净）
"""

import os
import re

__all__ = ["parse_env_text", "find_env_file", "load_dotenv", "dotenv_info"]

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DISABLE_ENV = "NHX_NO_DOTENV"
_PATH_ENV = "NHX_ENV_FILE"

# 本次进程内最近一次加载的结果（便于启动横幅展示来源；测试可重置）
_STATE = {"done": False, "path": None, "loaded": {}, "skipped": [], "invalid": [], "disabled": False}


def _unescape_double(value):
    out = []
    i = 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            mapping = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "'": "'"}
            if nxt in mapping:
                out.append(mapping[nxt])
                i += 2
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def parse_env_text(text):
    """把 `.env` 文本解析成 dict。解析不了的键写进第二个返回值的列表。"""
    values, invalid = {}, []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            invalid.append(raw_line.strip())
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not _KEY_RE.match(key):
            invalid.append(raw_line.strip())
            continue
        # 引号包裹：原样取用（双引号再做转义）
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            if value[0] == '"':
                value = _unescape_double(value[1:-1])
            else:
                value = value[1:-1]
        else:
            # 去掉行尾注释：` #` 之后的内容
            cut = re.split(r"\s+#", value, maxsplit=1)
            value = cut[0].rstrip()
        values[key] = value
    return values, invalid


def find_env_file():
    """定位 .env：NHX_ENV_FILE > 仓库根 > 当前工作目录。找不到返回 None。"""
    explicit = (os.environ.get(_PATH_ENV) or "").strip()
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    for cand in (os.path.join(REPO_ROOT, ".env"), os.path.join(os.getcwd(), ".env")):
        if os.path.isfile(cand):
            return cand
    return None


def load_dotenv(path=None, override=False, force=False):
    """读取 `.env` 并写入 `os.environ`（已有变量不覆盖）。

    幂等：同一进程内重复调用不会再读盘，除非 `force=True`（测试用）。
    返回 `{"path", "loaded", "skipped", "invalid", "disabled"}`，
    其中 `loaded` 是本次**真正生效**的键值（不含被真实环境覆盖的）。
    """
    if not force and _STATE["done"]:
        return dict(_STATE)
    _STATE.update({"done": True, "path": None, "loaded": {}, "skipped": [], "invalid": [],
                   "disabled": False})

    # NHX_NO_DOTENV=1 只关「自动发现」；显式传入 path 时照读（测试与工具需要）
    if path is None and (os.environ.get(_DISABLE_ENV) or "").strip().lower() in (
            "1", "true", "yes", "on"):
        _STATE["disabled"] = True
        return dict(_STATE)

    target = path or find_env_file()
    if not target or not os.path.isfile(target):
        return dict(_STATE)

    try:
        with open(target, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return dict(_STATE)

    values, invalid = parse_env_text(text)
    loaded, skipped = {}, []
    for key, value in values.items():
        if key in os.environ and not override:
            skipped.append(key)          # 真实环境优先
            continue
        os.environ[key] = value
        loaded[key] = value

    _STATE.update({"path": os.path.abspath(target), "loaded": loaded,
                   "skipped": skipped, "invalid": invalid})
    return dict(_STATE)


def dotenv_info():
    """当前进程的 .env 加载情况（只暴露键名，不暴露值）。"""
    return {
        "path": _STATE["path"],
        "keys": sorted(_STATE["loaded"].keys()),
        "skipped": sorted(_STATE["skipped"]),
        "invalid": list(_STATE["invalid"]),
        "disabled": _STATE["disabled"],
    }
