"""倪师 AI · `.env` 配置加载测试

回归目标（真实故障）：用户 `cp .env.example .env` 并填好 `NHX_LLM_API_KEY`，
再 `python3 src/nhx_app/server.py`，横幅仍显示「API Key：❌ 未配置」——
因为 Python 不会自动读 `.env`，配置只读 `os.environ`。

因此这里的断言分两层：
* **解析层**：`parse_env_text` 认不认各种写法（引号、注释、export、非法行）
* **生效层**：`load_dotenv` / `Config` 真的把值用上了，且**真实环境变量优先**

运行：python3 src/nhx_app/test_env.py
"""

import os
import sys
import tempfile
import unittest

APP = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(APP, "..", ".."))
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, ".."))

import env as env_mod  # noqa: E402
from config import Config  # noqa: E402

# 注意：测试期间一律用 NHX_NO_DOTENV 关掉「自动发现仓库根 .env」，
# 否则用例结果会取决于开发机上 .env 里写了什么。需要读文件时显式传路径。


class EnvGuard(unittest.TestCase):
    """每个用例前后都清掉本用例碰过的环境变量与模块缓存。"""

    KEYS = ("NHX_LLM_API_KEY", "NHX_LLM_MODEL", "NHX_PORT", "NHX_DATA_DIR",
            "NHX_AUTH_MODE", env_mod._PATH_ENV, env_mod._DISABLE_ENV)

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in self.KEYS}
        os.environ[env_mod._DISABLE_ENV] = "1"   # 先禁自动发现，再清缓存
        env_mod.load_dotenv(force=True)
        self.tmp = tempfile.mkdtemp(prefix="nhx-env-")

    def tearDown(self):
        for k in self.KEYS:
            os.environ.pop(k, None)
        os.environ[env_mod._DISABLE_ENV] = "1"
        env_mod.load_dotenv(force=True)          # 清缓存，且不读仓库 .env
        for k in self.KEYS:
            os.environ.pop(k, None)
            if self._saved.get(k) is not None:
                os.environ[k] = self._saved[k]
        env_mod.load_dotenv(force=True)

    def write_env(self, text, name=".env"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path


# ── 解析层 ──────────────────────────────────────────────────────────────────

class TestParse(EnvGuard):

    def test_basic_and_comments(self):
        vals, bad = env_mod.parse_env_text(
            "# 注释\n\nNHX_LLM_API_KEY=sk-abc\n   \nNHX_PORT=18790\n")
        self.assertEqual(vals, {"NHX_LLM_API_KEY": "sk-abc", "NHX_PORT": "18790"})
        self.assertEqual(bad, [])

    def test_export_prefix_and_spaces(self):
        vals, _ = env_mod.parse_env_text("  export   NHX_LLM_MODEL = deepseek-chat  \n")
        self.assertEqual(vals["NHX_LLM_MODEL"], "deepseek-chat")

    def test_quotes_and_escapes(self):
        vals, _ = env_mod.parse_env_text(
            'A="sk-1"\n'
            "B='sk-2'\n"
            'C="line1\\nline2"\n'
            'D="say \\"hi\\""\n'
        )
        self.assertEqual(vals["A"], "sk-1")
        self.assertEqual(vals["B"], "sk-2")
        self.assertEqual(vals["C"], "line1\nline2")
        self.assertEqual(vals["D"], 'say "hi"')

    def test_inline_comment_stripped_only_when_unquoted(self):
        vals, _ = env_mod.parse_env_text('A=sk-abc # 这是我的 key\nB="sk-abc # 保留"\n')
        self.assertEqual(vals["A"], "sk-abc")
        self.assertEqual(vals["B"], "sk-abc # 保留")

    def test_value_may_contain_equals(self):
        vals, _ = env_mod.parse_env_text("NHX_LLM_BASE_URL=https://x/v1?a=b\n")
        self.assertEqual(vals["NHX_LLM_BASE_URL"], "https://x/v1?a=b")

    def test_empty_value_kept(self):
        vals, _ = env_mod.parse_env_text("NHX_LLM_PROXY=\n")
        self.assertEqual(vals["NHX_LLM_PROXY"], "")

    def test_invalid_lines_reported_not_crashing(self):
        vals, bad = env_mod.parse_env_text(
            "这行没有等号\n"
            "中文键=值\n"
            "1BAD=x\n"
            "GOOD=y\n"
        )
        self.assertEqual(vals, {"GOOD": "y"})
        self.assertEqual(len(bad), 3)


# ── 生效层 ──────────────────────────────────────────────────────────────────

class TestLoad(EnvGuard):

    def test_loads_into_environ(self):
        path = self.write_env("NHX_LLM_API_KEY=sk-from-file\nNHX_LLM_MODEL=m1\n")
        info = env_mod.load_dotenv(path, force=True)
        self.assertEqual(os.environ["NHX_LLM_API_KEY"], "sk-from-file")
        self.assertEqual(info["path"], os.path.abspath(path))
        self.assertEqual(sorted(info["loaded"]), ["NHX_LLM_API_KEY", "NHX_LLM_MODEL"])

    def test_real_env_wins(self):
        """真实环境变量优先级高于 .env（12-factor）。"""
        path = self.write_env("NHX_LLM_API_KEY=sk-from-file\n")
        os.environ["NHX_LLM_API_KEY"] = "sk-from-shell"
        info = env_mod.load_dotenv(path, force=True)
        self.assertEqual(os.environ["NHX_LLM_API_KEY"], "sk-from-shell")
        self.assertIn("NHX_LLM_API_KEY", info["skipped"])
        self.assertEqual(info["loaded"], {})

    def test_override_true(self):
        path = self.write_env("NHX_LLM_API_KEY=sk-from-file\n")
        os.environ["NHX_LLM_API_KEY"] = "sk-from-shell"
        env_mod.load_dotenv(path, override=True, force=True)
        self.assertEqual(os.environ["NHX_LLM_API_KEY"], "sk-from-file")

    def test_missing_file_is_silent(self):
        info = env_mod.load_dotenv(os.path.join(self.tmp, "nope.env"), force=True)
        self.assertIsNone(info["path"])
        self.assertEqual(info["loaded"], {})

    def test_disable_switch_blocks_auto_discovery(self):
        """NHX_NO_DOTENV=1 只关「自动发现」，显式指定路径仍照读。"""
        path = self.write_env("NHX_LLM_API_KEY=sk-from-file\n")
        os.environ[env_mod._PATH_ENV] = path
        os.environ[env_mod._DISABLE_ENV] = "1"
        info = env_mod.load_dotenv(force=True)          # 不传路径 = 自动发现
        self.assertTrue(info["disabled"])
        self.assertNotIn("NHX_LLM_API_KEY", os.environ)

        info = env_mod.load_dotenv(path, force=True)    # 显式路径 = 照读
        self.assertEqual(os.environ["NHX_LLM_API_KEY"], "sk-from-file")
        self.assertFalse(info["disabled"])

    def test_nhx_env_file_points_to_custom_path(self):
        path = self.write_env("NHX_LLM_API_KEY=sk-custom\n", name="custom.env")
        os.environ.pop(env_mod._DISABLE_ENV, None)
        os.environ[env_mod._PATH_ENV] = path
        env_mod.load_dotenv(force=True)
        self.assertEqual(os.environ["NHX_LLM_API_KEY"], "sk-custom")

    def test_idempotent_until_forced(self):
        """同一进程内重复调用不会反复读盘（force 除外）。"""
        path = self.write_env("NHX_LLM_API_KEY=sk-v1\n")
        env_mod.load_dotenv(path, force=True)
        self.write_env("NHX_LLM_API_KEY=sk-v2\n")
        env_mod.load_dotenv(path)                       # 命中缓存，不重读文件
        self.assertEqual(os.environ["NHX_LLM_API_KEY"], "sk-v1",
                         "非 force 调用不应重读文件")
        os.environ.pop("NHX_LLM_API_KEY", None)
        env_mod.load_dotenv(path, force=True)           # 强制重读 → 拿到新值
        self.assertEqual(os.environ["NHX_LLM_API_KEY"], "sk-v2")


# ── 与 Config 的集成（真实故障路径） ────────────────────────────────────────

class TestConfigUsesDotenv(EnvGuard):

    def test_config_reads_dotenv(self):
        """回归：写进 .env 的 Key / 模型 / 端口必须被 Config 读到。"""
        path = self.write_env(
            "NHX_LLM_API_KEY=sk-1234567890\n"
            "NHX_LLM_MODEL=deepseek-v4-flash\n"
            "NHX_PORT=18790\n"
        )
        os.environ.pop(env_mod._DISABLE_ENV, None)
        os.environ[env_mod._PATH_ENV] = path
        env_mod.load_dotenv(force=True)

        cfg = Config()
        self.assertTrue(cfg.llm_ready())
        self.assertEqual(cfg.llm_model, "deepseek-v4-flash")
        self.assertEqual(cfg.port, 18790, "端口没从 .env 生效就是当初那个 bug")
        self.assertEqual(cfg.source_of("NHX_LLM_API_KEY"), "dotenv")
        self.assertIn(".env", cfg.describe_source())

    def test_env_var_beats_dotenv_in_config(self):
        path = self.write_env("NHX_LLM_MODEL=from-file\n")
        os.environ.pop(env_mod._DISABLE_ENV, None)
        os.environ[env_mod._PATH_ENV] = path
        os.environ["NHX_LLM_MODEL"] = "from-shell"
        env_mod.load_dotenv(force=True)
        self.assertEqual(Config().llm_model, "from-shell")

    def test_use_dotenv_false_is_hermetic(self):
        """测试/CI 可以完全不碰 .env。"""
        path = self.write_env("NHX_LLM_API_KEY=sk-hidden\n")
        os.environ[env_mod._PATH_ENV] = path
        cfg = Config(use_dotenv=False)
        self.assertFalse(cfg.llm_ready())
        self.assertNotIn("NHX_LLM_API_KEY", os.environ)

    def test_relative_paths_resolve_against_repo_root(self):
        """`.env` 里写 ./data 不该随 cwd 漂移。"""
        path = self.write_env("NHX_DATA_DIR=./data\n")
        os.environ.pop(env_mod._DISABLE_ENV, None)
        os.environ[env_mod._PATH_ENV] = path
        env_mod.load_dotenv(force=True)
        cfg = Config()
        self.assertTrue(cfg.data_dir.startswith(REPO), cfg.data_dir)
        self.assertEqual(os.path.basename(cfg.data_dir), "data")
        self.assertTrue(os.path.isabs(cfg.data_dir))

    def test_defaults_when_no_dotenv(self):
        os.environ[env_mod._DISABLE_ENV] = "1"
        os.environ[env_mod._PATH_ENV] = os.path.join(self.tmp, "nope.env")
        cfg = Config()
        self.assertEqual(cfg.port, 8790)
        self.assertEqual(cfg.llm_model, "deepseek-chat")
        self.assertFalse(cfg.llm_ready())
        self.assertIn("系统环境变量", cfg.describe_source())


if __name__ == "__main__":
    unittest.main(verbosity=2)
