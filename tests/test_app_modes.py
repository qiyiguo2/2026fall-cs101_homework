"""两种运行模式的行为断言（config.APP_MODE）。

    server —— 要登录，成绩与录音进服务器；
    local  —— 不登录，成绩与录音只进用户浏览器的 IndexedDB，服务器一个字都不写。

这里只能验后端那一半：页面能不能打开、写数据的接口有没有被挡住、数据库有没有被偷偷写。
浏览器那一半（IndexedDB 真的存下了、导出导入能对上）必须在真浏览器里跑，
清单见 docs/deployment.md 第 8.2 节「切完怎么确认」。

跑法（需要先有一份开发数据库，里面至少有一个用户和一点题库）：

    venv/Scripts/python.exe -m unittest discover -s tests -v

数据库这条线是安全的：测试把 data/app.db **复制**到临时目录再跑，
所有写入都落在副本上，原库只读；scripts/init_db.py --reset 那种事情这里绝不会发生。
"""

import importlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid

from werkzeug.security import generate_password_hash

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 和 scripts/ 里那些脚本一样自己把项目根加进 sys.path：
# 这样从仓库根跑 `python -m unittest discover -s tests` 时，
# unittest 把 tests/ 当顶层目录也不会让 import config 失败。
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import config
import db
import app as app_module

LOGIN_FREE_PAGES = [
    "/practice",
    "/practice/vowel",
    "/practice/read-translate",
    "/practice/listen-spell",
    "/settings",
    "/account",
    "/result/1",
]
WRITE_APIS = ["/api/practice/session", "/api/practice/finish", "/api/recording"]


def table_count(path, table):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def issued_answers(path, session_id):
    """读出某场练习登记在服务器上的标准答案 {题号: 答案}。

    服务器模式的近似音出题接口不再下发答案，测试要知道「选哪个算对」只能看库里的快照。
    """
    import json
    conn = sqlite3.connect(path)
    try:
        raw = conn.execute("SELECT questions_json FROM practice_sessions WHERE id = ?",
                           (session_id,)).fetchone()[0]
    finally:
        conn.close()
    return {q["seq"]: q.get("answer") for q in json.loads(raw)}


class ModeTestBase(unittest.TestCase):
    """把 db.DB_PATH 指到副本上，测试怎么写都不会碰开发库。"""

    @classmethod
    def setUpClass(cls):
        source = os.path.join(BASE_DIR, "data", "app.db")
        if not os.path.exists(source):
            raise unittest.SkipTest(
                "没有 data/app.db，请先跑 scripts/init_db.py 建开发数据库")
        cls.tmp_dir = tempfile.mkdtemp(prefix="ep-mode-test-")
        cls.db_path = os.path.join(cls.tmp_dir, "app.db")
        shutil.copy(source, cls.db_path)
        cls.saved_db_path = db.DB_PATH
        db.DB_PATH = cls.db_path            # db.connect() 每次调用都读这个全局

    @classmethod
    def tearDownClass(cls):
        db.DB_PATH = cls.saved_db_path
        shutil.rmtree(cls.tmp_dir, ignore_errors=True)

    def setUp(self):
        self.saved_mode = config.APP_MODE
        self.client = app_module.app.test_client()
        self.recordings_dir = app_module.recording_dir()

    def tearDown(self):
        config.APP_MODE = self.saved_mode

    def as_server(self):
        config.APP_MODE = "server"

    def as_local(self):
        config.APP_MODE = "local"

    def sign_in(self):
        """在副本里造一个用户并把会话塞进 cookie，返回 (client, user_id)。"""
        username = f"mode-test-{uuid.uuid4().hex[:8]}"
        conn = db.connect()
        try:
            cur = conn.execute(
                "INSERT INTO users(username, password_hash, nickname, default_count, time_limit) "
                "VALUES(?,?,?,?,?)",
                (username, generate_password_hash("secret-pass-1"), "测试账号", 12, 2.5),
            )
            conn.commit()
            uid = cur.lastrowid
        finally:
            conn.close()
        with self.client.session_transaction() as jar:
            jar["user_id"] = uid
        return uid


class TestAppModeParsing(ModeTestBase):
    """开关本身的取值：写错了必须退回 server，不能退回「谁都不用登录」。"""

    def test_only_two_recognised_values(self):
        cases = [
            ("server", "server"), ("SERVER", "server"), ("local", "local"),
            ("Local", "local"), ("  local  ", "local"),
            # 下面这些都是「填错了」：退回 server = 要登录，是更安全的那一边
            ("ture", "server"), ("loca", "server"), ("localmode", "server"),
            ("none", "server"), ("", "server"), ("off", "server"),
        ]
        saved = os.environ.get("APP_MODE")
        try:
            for raw, expected in cases:
                os.environ["APP_MODE"] = raw
                importlib.reload(config)     # reload 是就地更新同一个模块对象，app 那边跟着变
                self.assertEqual(config.APP_MODE, expected, f"APP_MODE={raw!r}")
        finally:
            if saved is None:
                os.environ.pop("APP_MODE", None)
            else:
                os.environ["APP_MODE"] = saved
            importlib.reload(config)


class TestServerMode(ModeTestBase):
    """默认那档：线上 240 人正在用的行为，一条都不能退化。"""

    def test_pages_and_apis_require_login(self):
        self.as_server()
        for path in LOGIN_FREE_PAGES:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertIn("/login", response.headers["Location"], path)

        for path in WRITE_APIS + ["/api/vowel/questions", "/api/words"]:
            response = self.client.post(path, json={})
            self.assertEqual(response.status_code, 401, path)
        # 词表那条是 GET-only 的接口（只给「音频导入」匹配文件名用），同样要挡
        self.assertEqual(self.client.get("/api/words/all").status_code, 401)

        upload = self.client.post(
            "/api/recording",
            data={"audio": (io.BytesIO(b"fake"), "r.webm")},
            content_type="multipart/form-data",
        )
        self.assertEqual(upload.status_code, 401)

    def test_login_page_is_the_form(self):
        self.as_server()
        body = self.client.get("/login").get_data().decode()
        self.assertIn('name="password"', body)

    def test_finish_actually_writes_the_database(self):
        uid = self.sign_in()
        created = self.client.post("/api/practice/session",
                                   json={"practice_type": "vowel", "params": {"count": 2}})
        self.assertEqual(created.status_code, 200)
        session_id = created.get_json()["session_id"]
        with app_module.app.app_context():
            from services import practice
            group = practice.list_vowel_groups()[0]["group_key"]
        issued = self.client.post("/api/vowel/questions", json={
            "group_key": group, "count": 2, "session_id": session_id})
        self.assertEqual(issued.status_code, 200, issued.get_json())
        first, second = issued.get_json()["questions"]
        self.assertNotIn("answer", first, "服务器模式下作答前不能下发答案")
        answers = issued_answers(self.db_path, session_id)
        first["answer"], second["answer"] = answers[1], answers[2]

        # 第一题选对、第二题选错；成绩以后端按出题快照判的为准
        records = [
            {"seq": 1, "type": "vowel", "question": {"type": "vowel"},
             "user_answer": first["answer"], "is_correct": True},
            {"seq": 2, "type": "vowel", "question": {"type": "vowel"},
             "user_answer": 3 - second["answer"], "is_correct": False},
        ]
        done = self.client.post("/api/practice/finish",
                                json={"session_id": session_id, "records": records})
        self.assertEqual(done.status_code, 200)
        payload = done.get_json()
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["correct"], 1)
        self.assertIn(f"/result/{session_id}", payload["result_url"])

        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute("SELECT user_id, total, correct_count, wrong_count, finished "
                               "FROM practice_sessions WHERE id = ?", (session_id,)).fetchone()
            self.assertEqual(row, (uid, 2, 1, 1, 1))
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM practice_records WHERE session_id = ?",
                             (session_id,)).fetchone()[0], 2)
        finally:
            conn.close()

        result = self.client.get(payload["result_url"])
        self.assertEqual(result.status_code, 200)
        self.assertIn("逐题回看", result.get_data().decode())


class TestLocalMode(ModeTestBase):
    """本地模式：门都开得开，但服务器一个字的用户数据都不写。"""

    def test_pages_open_without_any_session(self):
        self.as_local()
        for path in LOGIN_FREE_PAGES:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            self.assertIn('data-mode="local"', response.get_data().decode(), path)

    def test_read_only_question_apis_still_work(self):
        # 出题靠的是服务器上的词库和题库：本地模式照样要用，只读不写
        self.as_local()
        with app_module.app.app_context():      # list_vowel_groups 要连库，测试里得自己给上下文
            from services import practice
            group = practice.list_vowel_groups()[0]["group_key"]
        response = self.client.post("/api/vowel/questions",
                                    json={"group_key": group, "count": 3})
        self.assertEqual(response.status_code, 200, response.get_data())
        data = response.get_json()
        self.assertEqual(len(data["questions"]), 3)
        self.assertTrue(data["questions"][0]["audio_url"].startswith("/static/"))
        # 本地模式成绩只存在学生自己的浏览器，前端就地判分，答案照常下发
        self.assertIn(data["questions"][0]["answer"], (1, 2))

        # 本地模式的 session_id 是浏览器 IndexedDB 的键，后端忽略它，不登记也不报错
        with_sid = self.client.post("/api/vowel/questions",
                                    json={"group_key": group, "count": 2, "session_id": 1})
        self.assertEqual(with_sid.status_code, 200, with_sid.get_data())

        words = self.client.post("/api/words", json={"count": 4, "session_id": 1})
        self.assertEqual(words.status_code, 200)
        self.assertEqual(len(words.get_json()["words"]), 4)
        self.assertEqual(self.client.get("/api/words/all").status_code, 200)

    def test_write_apis_are_refused_and_nothing_reaches_the_database(self):
        self.as_local()
        before_sessions = table_count(self.db_path, "practice_sessions")
        before_records = table_count(self.db_path, "practice_records")
        os.makedirs(self.recordings_dir, exist_ok=True)
        before_files = set(os.listdir(self.recordings_dir))

        answers = [{"seq": 1, "type": "vowel", "question": {"type": "vowel"},
                    "user_answer": 1, "is_correct": True}]
        responses = {
            "/api/practice/session": self.client.post(
                "/api/practice/session",
                json={"practice_type": "vowel", "params": {"count": 1}}),
            "/api/practice/finish": self.client.post(
                "/api/practice/finish", json={"session_id": 1, "records": answers}),
            "/api/recording": self.client.post(
                "/api/recording",
                data={"audio": (io.BytesIO(b"fake-audio"), "r.webm")},
                content_type="multipart/form-data"),
        }
        for path, response in responses.items():
            self.assertEqual(response.status_code, 409, path)
            payload = response.get_json()
            self.assertEqual(payload["mode"], "local", path)
            self.assertIn("本地模式", payload["error"], path)

        self.assertEqual(table_count(self.db_path, "practice_sessions"), before_sessions)
        self.assertEqual(table_count(self.db_path, "practice_records"), before_records)
        self.assertEqual(set(os.listdir(self.recordings_dir)), before_files)

    def test_login_register_logout_show_the_notice(self):
        self.as_local()
        for path in ["/login", "/register", "/logout"]:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            body = response.get_data().decode()
            self.assertIn("本地模式", body, path)
            self.assertNotIn('name="password"', body, path)
        # POST 也一样只给说明页，不接受表单
        response = self.client.post("/register",
                                    data={"username": "x", "password": "y", "confirm": "y"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("注册成功", response.get_data().decode())

    def test_navigation_offers_no_login_or_signup(self):
        self.as_local()
        body = self.client.get("/").get_data().decode()
        self.assertNotIn('href="/login"', body)
        self.assertNotIn('href="/register"', body)
        self.assertIn("我的数据", body)
        self.assertIn("本地模式", body)

    def test_result_page_is_the_local_shell_and_leaks_nothing(self):
        """本地模式的结果页绝不能去查库：万一查了，别人的会话号就能在这台机器上读出内容。"""
        uid = self.sign_in()
        conn = sqlite3.connect(self.db_path)
        try:
            marker = f"LEAK-MARKER-{uuid.uuid4().hex[:8]}"
            cur = conn.execute(
                "INSERT INTO practice_sessions(user_id, practice_type, params_json, total, "
                "correct_count, wrong_count, finished) VALUES(?,?,?,?,?,?,?)",
                (uid, "vowel", '{"count": 9}', 9, 8, 1, 1))
            session_id = cur.lastrowid
            conn.execute(
                "INSERT INTO practice_records(session_id, seq, question_json, user_answer, "
                "is_correct) VALUES(?,?,?,?,1)",
                (session_id, 1, f'{{"type": "vowel", "marker": "{marker}"}}', 1))
            conn.commit()
        finally:
            conn.close()

        self.as_local()
        response = self.client.get(f"/result/{session_id}")
        self.assertEqual(response.status_code, 200)
        body = response.get_data().decode()
        self.assertNotIn(marker, body)
        self.assertIn("local-result.js", body)
        self.assertIn("只存在这台设备", body)

    def test_settings_post_changes_no_user_row(self):
        uid = self.sign_in()
        self.as_local()
        response = self.client.post("/settings", data={
            "default_count": "99", "time_limit": "30",
            "round1_seconds": "20", "round2_seconds": "60", "round_gap": "3",
        })
        self.assertEqual(response.status_code, 200)
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute("SELECT default_count, time_limit FROM users WHERE id = ?",
                               (uid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(row, (12, 2.5))      # 没被动过

    def test_api_me_reports_local_and_no_identity(self):
        self.as_local()
        self.sign_in()                        # 就算浏览器还留着切模式之前的 cookie
        payload = self.client.get("/api/me").get_json()
        self.assertEqual(payload["mode"], "local")
        self.assertFalse(payload["logged_in"])
        self.assertIsNone(payload["username"])
        # 默认值照样给得出来，而且用的是 config 那一套
        self.assertEqual(payload["settings"]["default_count"], config.DEFAULT_QUESTION_COUNT)

    def test_recordings_route_serves_nothing(self):
        """服务器上没有本地模式的录音文件，这条路由也不该吐出任何人的音频。"""
        self.as_local()
        name = os.listdir(self.recordings_dir)[0] if os.path.isdir(
            self.recordings_dir) and os.listdir(self.recordings_dir) else "u1_deadbeefcafe.webm"
        self.assertEqual(self.client.get(f"/recordings/{name}").status_code, 404)

    def test_local_store_script_is_only_loaded_in_local_mode(self):
        self.as_local()
        self.assertIn("local-store.js", self.client.get("/practice/vowel").get_data().decode())
        self.as_server()
        uid = self.sign_in()
        body = self.client.get("/practice/vowel").get_data().decode()
        self.assertNotIn("local-store.js", body)
        self.assertIn("practice-store.js", body)   # 门面两种模式都要有
        self.assertIsNotNone(uid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
