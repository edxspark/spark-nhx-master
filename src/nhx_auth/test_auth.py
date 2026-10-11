"""认证层测试。

重点验证**安全属性**，而不只是功能是否可用：
每条安全断言都有一个「尝试绕过」的反例。

运行：python3 src/nhx_media/test_auth.py
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth  # noqa: E402  (同目录)
from auth import AuthError  # noqa: E402


class AuthTestCase(unittest.TestCase):
    def setUp(self):
        self._orig = auth.AUTH_FILE
        self.tmp = tempfile.mkdtemp(prefix="nhx-auth-test-")
        auth.AUTH_FILE = os.path.join(self.tmp, "auth.json")
        # 默认不设环境变量，让配置文件生效；
        # 只有 test_env_overrides_config 会在测试内临时设置。
        self._orig_env = os.environ.pop("NHX_AUTH_MODE", None)

    def tearDown(self):
        auth.AUTH_FILE = self._orig
        os.environ.pop("NHX_AUTH_MODE", None)
        if self._orig_env is not None:
            os.environ["NHX_AUTH_MODE"] = self._orig_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _user(self, name="alice", pw="secret123", role="admin"):
        return auth.add_user(name, pw, role)

    # ── 口令哈希 ────────────────────────────────────────────────────────
    def test_password_not_stored_in_plaintext(self):
        self._user(pw="secret123")
        with open(auth.AUTH_FILE, "r", encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("secret123", raw, "口令绝不能以明文落盘")
        self.assertIn("pbkdf2_sha256", raw)

    def test_same_password_different_hash(self):
        """加盐：相同口令产生不同哈希。"""
        h1 = auth.hash_password("secret123")
        h2 = auth.hash_password("secret123")
        self.assertNotEqual(h1, h2)
        self.assertTrue(auth.verify_password("secret123", h1))
        self.assertTrue(auth.verify_password("secret123", h2))

    def test_wrong_password_rejected(self):
        h = auth.hash_password("secret123")
        self.assertFalse(auth.verify_password("secret124", h))
        self.assertFalse(auth.verify_password("", h))

    def test_malformed_hash_does_not_crash(self):
        for bad in ["", "garbage", "pbkdf2_sha256$notanint$a$b", "md5$1$2$3"]:
            with self.subTest(bad=bad):
                self.assertFalse(auth.verify_password("x", bad))

    def test_short_password_rejected(self):
        with self.assertRaises(ValueError):
            auth.hash_password("12345")

    # ── 用户管理 ────────────────────────────────────────────────────────
    def test_invalid_username_rejected(self):
        for bad in ["ab", "", "has space", "a/b", "x" * 40]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    auth.add_user(bad, "secret123")

    def test_duplicate_user_rejected(self):
        self._user()
        with self.assertRaises(ValueError):
            self._user()

    def test_list_users_hides_hash(self):
        self._user()
        for u in auth.list_users():
            self.assertNotIn("password_hash", u)

    def test_remove_user_revokes_sessions(self):
        self._user()
        s = auth.login("alice", "secret123")
        self.assertIsNotNone(auth.resolve_token(s["token"]))
        auth.remove_user("alice")
        self.assertIsNone(auth.resolve_token(s["token"]), "删用户后其会话必须失效")

    def test_set_password_revokes_sessions(self):
        self._user()
        s = auth.login("alice", "secret123")
        auth.set_password("alice", "newsecret456")
        self.assertIsNone(auth.resolve_token(s["token"]), "改密后旧会话必须失效")
        self.assertIsNotNone(auth.login("alice", "newsecret456"))

    # ── 登录 ────────────────────────────────────────────────────────────
    def test_login_success(self):
        self._user()
        s = auth.login("alice", "secret123")
        self.assertIn("token", s)
        self.assertEqual(s["username"], "alice")
        self.assertEqual(s["role"], "admin")

    def test_login_wrong_password(self):
        self._user()
        with self.assertRaises(AuthError):
            auth.login("alice", "wrong")

    def test_no_user_enumeration(self):
        """用户不存在与口令错误必须返回同一条错误信息。"""
        self._user()
        try:
            auth.login("alice", "wrong")
            m1 = None
        except AuthError as e:
            m1 = e.message
        try:
            auth.login("nobody", "wrong")
            m2 = None
        except AuthError as e:
            m2 = e.message
        self.assertEqual(m1, m2, "错误信息不同会泄露用户名是否存在")

    def test_raw_token_not_stored(self):
        """只存 token 哈希——文件泄漏也拿不到可用 token。"""
        self._user()
        s = auth.login("alice", "secret123")
        with open(auth.AUTH_FILE, "r", encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn(s["token"], raw, "原始 token 绝不能落盘")

    # ── 防爆破 ──────────────────────────────────────────────────────────
    def test_lockout_after_repeated_failures(self):
        self._user()
        for _ in range(auth.MAX_LOGIN_FAILURES):
            try:
                auth.login("alice", "wrong")
            except AuthError:
                pass
        # 即使口令正确也应被锁定
        with self.assertRaises(AuthError) as ctx:
            auth.login("alice", "secret123")
        self.assertEqual(ctx.exception.status, 429)
        self.assertIn("频繁", ctx.exception.message)

    def test_successful_login_clears_failures(self):
        self._user()
        for _ in range(auth.MAX_LOGIN_FAILURES - 1):
            try:
                auth.login("alice", "wrong")
            except AuthError:
                pass
        auth.login("alice", "secret123")  # 成功
        # 计数已清零，再来一次失败不该锁
        try:
            auth.login("alice", "wrong")
        except AuthError as e:
            self.assertNotEqual(e.status, 429)

    # ── 会话 ────────────────────────────────────────────────────────────
    def test_resolve_valid_token(self):
        self._user()
        s = auth.login("alice", "secret123")
        ident = auth.resolve_token(s["token"])
        self.assertEqual(ident["username"], "alice")
        self.assertEqual(ident["role"], "admin")

    def test_resolve_invalid_token(self):
        for bad in ["", "garbage", "a" * 50, None]:
            with self.subTest(bad=bad):
                self.assertIsNone(auth.resolve_token(bad))

    def test_expired_token_rejected(self):
        self._user()
        s = auth.login("alice", "secret123")
        # 手工把会话改成已过期
        data = auth._load()
        for sess in data["sessions"]:
            sess["expires_at"] = time.time() - 1
        auth._save(data)
        self.assertIsNone(auth.resolve_token(s["token"]), "过期 token 必须失效")

    def test_logout_invalidates_token(self):
        self._user()
        s = auth.login("alice", "secret123")
        self.assertTrue(auth.logout(s["token"]))
        self.assertIsNone(auth.resolve_token(s["token"]))

    def test_logout_all(self):
        self._user()
        a = auth.login("alice", "secret123")
        b = auth.login("alice", "secret123")
        self.assertEqual(auth.logout_all(), 2)
        self.assertIsNone(auth.resolve_token(a["token"]))
        self.assertIsNone(auth.resolve_token(b["token"]))

    # ── 模式与授权 ──────────────────────────────────────────────────────
    def test_default_mode_is_open(self):
        self.assertEqual(auth.get_mode(), "open")

    def test_open_mode_allows_everything(self):
        """默认免登录：读写都不需要 token。"""
        self.assertIsNone(auth.check("GET", None))
        self.assertIsNone(auth.check("POST", None))
        self.assertIsNone(auth.check("DELETE", None))

    def test_protected_mode_allows_read_blocks_write(self):
        self._user()
        auth.set_mode("protected")
        self.assertIsNone(auth.check("GET", None), "读操作应免登录")
        with self.assertRaises(AuthError) as ctx:
            auth.check("POST", None)
        self.assertEqual(ctx.exception.status, 401)

    def test_protected_mode_allows_write_with_token(self):
        self._user()
        auth.set_mode("protected")
        s = auth.login("alice", "secret123")
        ident = auth.check("POST", s["token"])
        self.assertEqual(ident["username"], "alice")

    def test_strict_mode_blocks_read_too(self):
        self._user()
        auth.set_mode("strict")
        with self.assertRaises(AuthError):
            auth.check("GET", None)
        s = auth.login("alice", "secret123")
        self.assertIsNotNone(auth.check("GET", s["token"]))

    def test_bogus_token_rejected_in_protected_mode(self):
        """伪造 token 不能提权。"""
        self._user()
        auth.set_mode("protected")
        for bad in ["Bearer x", "garbage", "a" * 60]:
            with self.subTest(bad=bad):
                with self.assertRaises(AuthError):
                    auth.check("POST", bad)

    def test_case_insensitive_method(self):
        """`post` 小写也应按写操作处理，不能绕过。"""
        self._user()
        auth.set_mode("protected")
        with self.assertRaises(AuthError):
            auth.check("post", None)

    # ── 防自锁 ──────────────────────────────────────────────────────────
    def test_cannot_enable_auth_without_users(self):
        """没有用户时切到需登录模式必须被拒——否则把自己锁在门外。"""
        with self.assertRaises(ValueError) as ctx:
            auth.set_mode("protected")
        self.assertIn("没有任何用户", str(ctx.exception))
        self.assertEqual(auth.get_mode(), "open")

    def test_can_enable_after_first_user(self):
        self._user()
        self.assertEqual(auth.set_mode("protected"), "protected")

    def test_invalid_mode_rejected(self):
        with self.assertRaises(ValueError):
            auth.set_mode("nonsense")

    def test_switching_to_open_clears_sessions(self):
        self._user()
        auth.set_mode("protected")
        s = auth.login("alice", "secret123")
        auth.set_mode("open")
        self.assertIsNone(auth.resolve_token(s["token"]),
                          "切回免登录时应清理残留会话")

    def test_env_overrides_config(self):
        self._user()
        auth.set_mode("protected")
        os.environ["NHX_AUTH_MODE"] = "open"
        self.assertEqual(auth.get_mode(), "open")

    def test_set_mode_refuses_when_env_overrides(self):
        """回归：环境变量在时切换模式必须报错，而不是静默无效。"""
        self._user()
        os.environ["NHX_AUTH_MODE"] = "open"
        with self.assertRaises(ValueError) as ctx:
            auth.set_mode("protected")
        self.assertIn("NHX_AUTH_MODE", str(ctx.exception))
        os.environ.pop("NHX_AUTH_MODE", None)
        # 取消环境变量后应可正常切换
        self.assertEqual(auth.set_mode("protected"), "protected")

    # ── status ──────────────────────────────────────────────────────────
    def test_status_shape(self):
        st = auth.status(None)
        self.assertEqual(st["mode"], "open")
        self.assertFalse(st["authenticated"])
        self.assertFalse(st["has_users"])
        self._user()
        s = auth.login("alice", "secret123")
        st = auth.status(s["token"])
        self.assertTrue(st["authenticated"])
        self.assertEqual(st["user"]["username"], "alice")
        self.assertTrue(st["has_users"])

    def test_status_with_bad_token_is_anonymous(self):
        st = auth.status("garbage")
        self.assertFalse(st["authenticated"])

    # ── 文件权限 ────────────────────────────────────────────────────────
    def test_auth_file_permissions_are_restrictive(self):
        self._user()
        mode = os.stat(auth.AUTH_FILE).st_mode & 0o777
        self.assertEqual(mode, 0o600, "认证文件权限应为 600，实际 %o" % mode)


if __name__ == "__main__":
    unittest.main(verbosity=2)
