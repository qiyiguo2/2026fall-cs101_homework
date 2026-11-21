"""账号、邀请码闸门、录音归属与建库迁移。

和 test_app_modes.py 不同，这里**不依赖开发库 data/app.db**：每个用例自己用
scripts/init_db.py 的 SCHEMA 在临时目录建一个空库，干净 clone 或 CI 里也能跑，
不会因为缺库整体 SkipTest。

    python -m unittest tests.test_accounts_and_invites -v
"""

import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import config  # noqa: E402
import db  # noqa: E402
import app as app_module  # noqa: E402
from services import auth, registration  # noqa: E402
from scripts import init_db  # noqa: E402

PASSWORD = "secret-pass-1"


class FreshDbTestCase(unittest.TestCase):
    """每个用例一个只有表结构的新库 + 一个临时录音目录，服务器模式。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-accounts-test-")
        self.db_path = os.path.join(self.tmp, "app.db")
        conn = db.connect(self.db_path)
        conn.executescript(init_db.SCHEMA)
        conn.commit()
        conn.close()
        self.patches = [
            mock.patch.object(db, "DB_PATH", self.db_path),
            mock.patch.object(config, "APP_MODE", "server"),
            mock.patch.object(config, "REGISTER_MODE", "invite"),
            mock.patch.object(config, "RECORDING_DIR", os.path.join(self.tmp, "recordings")),
        ]
        for p in self.patches:
            p.start()
        self.app = app_module.app
        self.client = self.app.test_client()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- 小工具 -------------------------------------------------------
    def add_code(self, code, max_uses=None):
        with self.app.app_context():
            return registration.add(code, max_uses=max_uses)

    def revoke_code(self, code):
        with self.app.app_context():
            return registration.revoke(code)

    def query(self, sql, *args):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, args).fetchall()
        finally:
            conn.close()

    def register(self, username, code="", client=None):
        return (client or self.client).post("/register", data={
            "username": username, "password": PASSWORD, "confirm": PASSWORD,
            "nickname": "", "invite_code": code,
        })

    def user_count(self):
        return self.query("SELECT COUNT(*) FROM users")[0][0]


class TestInviteGate(FreshDbTestCase):

    def test_valid_code_registers_and_is_recorded(self):
        self.add_code("class-a")
        res = self.register("alice", "class-a")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(self.user_count(), 1)
        uses = self.query("SELECT c.code FROM invite_uses u JOIN invite_codes c ON c.id = u.code_id")
        self.assertEqual(uses, [("class-a",)])

    def test_code_is_case_and_space_insensitive(self):
        """iOS 会把首字母自动大写，学生还常带上空格。"""
        self.add_code("pku2026")
        self.assertEqual(self.register("alice", "  Pku2026 ").status_code, 302)

    def test_wrong_code_is_refused(self):
        self.add_code("class-a")
        res = self.register("alice", "class-b")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.user_count(), 0)

    def test_no_codes_means_registration_is_closed(self):
        res = self.register("alice", "anything")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.user_count(), 0)

    def test_single_use_code_works_once(self):
        self.add_code("only-one", max_uses=1)
        self.assertEqual(self.register("alice", "only-one").status_code, 302)
        other = self.app.test_client()
        self.assertEqual(self.register("bob", "only-one", client=other).status_code, 400)
        self.assertEqual(self.user_count(), 1)

    def test_revoked_code_is_refused(self):
        self.add_code("leaked")
        self.revoke_code("leaked")
        self.add_code("still-good")       # 让注册页保持开放，单测「被吊销的码」这一条
        self.assertEqual(self.register("alice", "leaked").status_code, 400)
        self.assertEqual(self.user_count(), 0)

    def test_closed_mode_refuses_even_a_valid_code(self):
        self.add_code("class-a")
        with mock.patch.object(config, "REGISTER_MODE", "closed"):
            self.assertEqual(self.register("alice", "class-a").status_code, 403)
        self.assertEqual(self.user_count(), 0)

    def test_open_mode_needs_no_code(self):
        with mock.patch.object(config, "REGISTER_MODE", "open"):
            self.assertEqual(self.register("alice").status_code, 302)
        self.assertEqual(self.user_count(), 1)

    def test_unknown_mode_falls_back_to_invite(self):
        for raw in ["opne", "OPEN ", "", "true"]:
            with mock.patch.object(config, "REGISTER_MODE", raw):
                expected = "open" if raw.strip().lower() == "open" else "invite"
                self.assertEqual(registration.mode(), expected, repr(raw))

    def test_code_is_checked_before_the_password_is_hashed(self):
        """一次 scrypt 很贵：错码必须在哈希之前就被挡下，不能先算再拒。"""
        self.add_code("class-a")
        with self.app.test_request_context(), \
                mock.patch.object(auth, "hash_password", side_effect=AssertionError("不该哈希")) as hashed:
            with self.assertRaises(auth.AuthError):
                auth.register("alice", PASSWORD, None, "wrong-code")
            hashed.assert_not_called()

    def test_duplicate_username_does_not_burn_a_single_use_code(self):
        self.add_code("first", max_uses=None)
        self.add_code("only-one", max_uses=1)
        self.assertEqual(self.register("alice", "first").status_code, 302)
        self.assertEqual(self.register("alice", "only-one", client=self.app.test_client()).status_code, 400)
        used = self.query("SELECT use_count FROM invite_codes WHERE code = 'only-one'")[0][0]
        self.assertEqual(used, 0, "用户名重复被拒，一次性码却被扣掉了")


class TestLoginAndPassword(FreshDbTestCase):

    def setUp(self):
        super().setUp()
        with mock.patch.object(config, "REGISTER_MODE", "open"):
            self.assertEqual(self.register("alice").status_code, 302)
        self.client = self.app.test_client()       # 换一个没有登录 cookie 的客户端

    def login(self, username="alice", password=PASSWORD):
        return self.client.post("/login", data={"username": username, "password": password})

    def test_correct_password_logs_in(self):
        self.assertEqual(self.login().status_code, 302)
        self.assertTrue(self.client.get("/api/me").get_json()["logged_in"])

    def test_wrong_password_and_unknown_user_look_the_same(self):
        wrong = self.login(password="not-the-password")
        unknown = self.login(username="nobody")
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(unknown.status_code, 401)
        self.assertFalse(self.client.get("/api/me").get_json()["logged_in"])

    def test_change_password_requires_the_old_one(self):
        self.login()
        self.client.post("/account", data={
            "action": "password", "old_password": "guess-123", "new_password": "brand-new-pass"})
        fresh = self.app.test_client()
        self.assertEqual(fresh.post("/login", data={
            "username": "alice", "password": "brand-new-pass"}).status_code, 401)

        self.client.post("/account", data={
            "action": "password", "old_password": PASSWORD, "new_password": "brand-new-pass"})
        self.assertEqual(fresh.post("/login", data={
            "username": "alice", "password": "brand-new-pass"}).status_code, 302)

    def test_practice_pages_and_apis_require_login(self):
        self.assertEqual(self.client.get("/practice/vowel").status_code, 302)
        self.assertEqual(self.client.post("/api/recording").status_code, 401)


class TestRecordings(FreshDbTestCase):

    def signed_in_client(self, username):
        client = self.app.test_client()
        with mock.patch.object(config, "REGISTER_MODE", "open"):
            self.assertEqual(self.register(username, client=client).status_code, 302)
        return client

    def upload(self, client, payload=b"RIFF....WAVE", name="a.wav"):
        return client.post("/api/recording", data={"audio": (io.BytesIO(payload), name)},
                           content_type="multipart/form-data")

    def test_owner_can_play_back_and_others_get_404(self):
        alice = self.signed_in_client("alice")
        bob = self.signed_in_client("bob")
        url = self.upload(alice).get_json()["url"]
        res = alice.get(url)
        self.assertEqual(res.status_code, 200)
        res.close()                           # send_from_directory 返回的是打开的文件
        self.assertEqual(bob.get(url).status_code, 404)
        self.assertEqual(self.app.test_client().get(url).status_code, 404)

    def test_unknown_extension_is_stored_as_webm(self):
        alice = self.signed_in_client("alice")
        path = self.upload(alice, name="evil.php").get_json()["path"]
        self.assertTrue(path.endswith(".webm"), path)

    def test_oversized_upload_is_refused_with_413(self):
        alice = self.signed_in_client("alice")
        with mock.patch.dict(self.app.config, {"MAX_CONTENT_LENGTH": 1024}):
            res = self.upload(alice, payload=b"x" * 4096)
        self.assertEqual(res.status_code, 413)
        self.assertIn("error", res.get_json())
        self.assertFalse(os.listdir(os.path.join(self.tmp, "recordings"))
                         if os.path.isdir(os.path.join(self.tmp, "recordings")) else [])

    def test_finish_cannot_claim_someone_elses_recording(self):
        alice = self.signed_in_client("alice")
        bob = self.signed_in_client("bob")
        alice_path = self.upload(alice).get_json()["path"]
        sid = bob.post("/api/practice/session",
                       json={"practice_type": "read-translate", "params": {}}).get_json()["session_id"]
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO words(word, meaning_cn) VALUES('abandon', '放弃')")
        issued = bob.post("/api/words", json={"count": 1, "session_id": sid})
        self.assertEqual(issued.status_code, 200, issued.get_json())
        res = bob.post("/api/practice/finish", json={"session_id": sid, "records": [
            {"seq": 1, "question": {}, "user_answer": "x", "is_correct": False,
             "recording_path": alice_path}]})
        self.assertEqual(res.status_code, 200)
        stored = self.query("SELECT recording_path FROM practice_records WHERE session_id = ?", sid)
        self.assertEqual(stored, [(None,)])

    def test_cannot_finish_someone_elses_session(self):
        alice = self.signed_in_client("alice")
        bob = self.signed_in_client("bob")
        sid = alice.post("/api/practice/session",
                         json={"practice_type": "vowel", "params": {}}).get_json()["session_id"]
        res = bob.post("/api/practice/finish", json={"session_id": sid, "records": []})
        self.assertEqual(res.status_code, 404)


class TestSchemaMigrations(unittest.TestCase):
    """init_db 对老库要幂等：CREATE TABLE IF NOT EXISTS 不补列，补列全靠 migrate_*。"""

    OLD_SCHEMA = """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL, nickname TEXT,
        default_count INTEGER NOT NULL DEFAULT 20, time_limit REAL NOT NULL DEFAULT 1,
        voice TEXT NOT NULL DEFAULT 'f', created_at TIMESTAMP);
    CREATE TABLE words (
        id INTEGER PRIMARY KEY AUTOINCREMENT, word TEXT NOT NULL UNIQUE, phonetic TEXT,
        meaning_cn TEXT, example TEXT, audio_path TEXT, category TEXT);
    INSERT INTO users(username, password_hash) VALUES('old-student', 'x');
    INSERT INTO words(word, meaning_cn) VALUES('abandon', '放弃');
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ep-migrate-test-")
        self.conn = sqlite3.connect(os.path.join(self.tmp, "app.db"))   # 故意不设 row_factory
        self.conn.executescript(self.OLD_SCHEMA)

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def migrate(self):
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            self.conn.executescript(init_db.SCHEMA)
            init_db.migrate_words_voice_columns(self.conn)
            init_db.migrate_users_rhythm_columns(self.conn)
        self.conn.commit()

    def columns(self, table):
        return {row[1] for row in self.conn.execute("PRAGMA table_info(%s)" % table)}

    def test_old_database_gets_the_new_columns_and_keeps_its_rows(self):
        self.migrate()
        self.assertTrue({"audio_f", "audio_m"} <= self.columns("words"))
        self.assertTrue({"round1_seconds", "round2_seconds", "round_gap_seconds",
                         "overlap_record"} <= self.columns("users"))
        self.assertEqual(self.conn.execute(
            "SELECT username, round1_seconds, overlap_record FROM users").fetchall(),
            [("old-student", 1.5, 1)])
        self.assertEqual(self.conn.execute("SELECT word FROM words").fetchall(), [("abandon",)])
        # 后来加的表（邀请码）也要能建出来
        tables = {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"invite_codes", "invite_uses", "practice_sessions"} <= tables)

    def test_running_twice_is_harmless(self):
        self.migrate()
        self.migrate()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
