"""倪师 AI · 配置

全部通过环境变量配置，便于部署（systemd / Docker / 宝塔等）。
所有配置都有合理默认值：**不填 API Key 也能启动**，只是对话功能会明确报错，
而不是启动即崩 —— 便于先验证部署链路。

关于 `.env`
-----------
`Config()` 构造时会先调用 `env.load_dotenv()`，把仓库根目录的 `.env` 读进
`os.environ`（**真实环境变量优先，不覆盖**）。原因见 `env.py` 的说明：
Python 不会自动读 `.env`，本地直跑 `python3 src/nhx_app/server.py` 时，
只有显式加载才能让 `.env` 生效。
测试 / CI 可设 `NHX_NO_DOTENV=1` 关闭，保证环境干净。
"""

import os

try:                                    # 脚本方式运行（server.py 把 APP_DIR 加进 sys.path）
    import env as env_mod
except ImportError:                     # 包方式导入（from nhx_app.config import Config）
    from . import env as env_mod

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def _env(name, default=None):
    v = os.environ.get(name)
    if v is None:
        return default
    v = v.strip()
    return v if v else default


def _env_bool(name, default=False):
    v = _env(name)
    if v is None:
        return default
    return v.lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    v = _env(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _env_float(name, default):
    v = _env(name)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _env_path(name, default):
    """路径型配置：相对路径按**仓库根**解析，而不是当前工作目录。

    `.env` 里写 `NHX_DATA_DIR=./data` 时，如果按 cwd 解析，换个目录启动
    就会把会话写到别处（甚至 `cd /` 后写到 `/data` 而权限失败）。
    默认值本来就是仓库根下的绝对路径，这里保持语义一致。
    """
    v = _env(name)
    if v is None:
        return default
    v = os.path.expanduser(v)
    return v if os.path.isabs(v) else os.path.abspath(os.path.join(REPO_ROOT, v))


class Config:
    """运行时配置。每次实例化都重新读环境变量，便于测试。"""

    def __init__(self, use_dotenv=True):
        # 先把仓库根的 .env 读进 os.environ（幂等；真实环境变量优先）
        self.dotenv = env_mod.load_dotenv() if use_dotenv else env_mod.dotenv_info()

        # ── 服务 ────────────────────────────────────────────────────────────
        self.host = _env("NHX_HOST", "127.0.0.1")
        self.port = _env_int("NHX_PORT", 8790)

        # ── 数据目录 ────────────────────────────────────────────────────────
        self.data_dir = _env_path("NHX_DATA_DIR", os.path.join(REPO_ROOT, "data"))
        self.knowledge_dir = _env_path(
            "NHX_KNOWLEDGE_DIR", os.path.join(REPO_ROOT, "knowledge"))

        # ── LLM ─────────────────────────────────────────────────────────────
        # 默认指向 DeepSeek（OpenAI 兼容协议）；换供应商只改 base_url 与 model。
        self.llm_base_url = _env("NHX_LLM_BASE_URL", "https://api.deepseek.com/v1")
        self.llm_api_key = _env("NHX_LLM_API_KEY", "")
        self.llm_model = _env("NHX_LLM_MODEL", "deepseek-chat")
        self.llm_timeout = _env_int("NHX_LLM_TIMEOUT", 120)
        self.llm_max_tokens = _env_int("NHX_LLM_MAX_TOKENS", 2048)
        self.llm_temperature = _env_float("NHX_LLM_TEMPERATURE", 0.3)

        # ── 会话 ────────────────────────────────────────────────────────────
        self.session_ttl_sec = _env_int("NHX_SESSION_TTL", 7 * 24 * 3600)
        self.max_history_messages = _env_int("NHX_MAX_HISTORY", 20)

        # ── 认证 ────────────────────────────────────────────────────────────
        # open = 免登录（默认）；protected = 对话需登录；strict = 全部需登录
        self.auth_mode = _env("NHX_AUTH_MODE", "open")

        # ── 安全 ────────────────────────────────────────────────────────────
        # 输出守门开关。默认开启：即便模型失守，也会被后置校验拦下。
        self.guard_enabled = _env_bool("NHX_GUARD_ENABLED", True)
        # 守门发现剂量违规时：block = 拦下并改写；warn = 仅记录
        self.guard_action = _env("NHX_GUARD_ACTION", "block")

        # ── 知识库检索 ──────────────────────────────────────────────────────
        self.kb_enabled = _env_bool("NHX_KB_ENABLED", True)
        self.kb_max_snippets = _env_int("NHX_KB_MAX_SNIPPETS", 5)

    # ── 派生 ────────────────────────────────────────────────────────────────

    @property
    def sessions_dir(self):
        return os.path.join(self.data_dir, "sessions")

    @property
    def auth_file(self):
        return os.path.join(self.data_dir, "auth.json")

    @property
    def contract_path(self):
        """行为契约（AGENT.md）——成为系统提示词的来源。"""
        return os.path.join(REPO_ROOT, "AGENT.md")

    def llm_ready(self):
        return bool(self.llm_api_key)

    # ── 配置来源（排障用） ──────────────────────────────────────────────────

    def source_of(self, name):
        """某个键的取值来源：`env`（真实环境变量）/ `dotenv`（.env）/ `default`。"""
        if os.environ.get(name):
            return "dotenv" if name in self.dotenv.get("loaded", {}) else "env"
        return "default"

    def describe_source(self):
        """启动横幅用的一句话描述。"""
        if self.dotenv.get("disabled"):
            return "系统环境变量（NHX_NO_DOTENV=1，已跳过 .env）"
        path = self.dotenv.get("path")
        n = len(self.dotenv.get("loaded", {}))
        if not path:
            return "系统环境变量（未找到 .env）"
        if n == 0:
            return "%s（未生效：全部被真实环境变量覆盖）" % path
        return "%s（已加载 %d 项）" % (path, n)

    def as_public_dict(self):
        """可安全暴露给前端的配置（不含密钥）。"""
        return {
            "model": self.llm_model,
            "llm_ready": self.llm_ready(),
            "auth_mode": self.auth_mode,
            "guard_enabled": self.guard_enabled,
            "kb_enabled": self.kb_enabled,
        }
