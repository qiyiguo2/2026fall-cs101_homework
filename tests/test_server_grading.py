"""后端判分（services/grading.py）：服务器模式下成绩只以后端重算的为准。

不依赖开发库：照 test_accounts_and_invites.py 的 FreshDbTestCase 在临时目录建空库，
自己塞一组近似音题和几个单词。

    python -m unittest tests.test_server_grading -v

最后一组用例拿同一批输入同时跑 Python 的 judge_chinese 和 tworounds.js 的 judgeChinese，
要求两边结论一致；机器上没有 node 时跳过。
"""

import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_accounts_and_invites import BASE_DIR, FreshDbTestCase, config  # noqa: E402
from scripts import init_db  # noqa: E402
from services import grading  # noqa: E402

WORDS = [
    ("abandon", "vt. 放弃；抛弃"),
    ("answer", "n. 回答；答案"),
    ("firewall", "n. [计] 防火墙"),
]


class GradingTestCase(FreshDbTestCase):

    def setUp(self):
        super().setUp()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("INSERT INTO vowel_groups(group_key, label, pair_count) "
                         "VALUES('sheep-ship', '/iː/ 与 /ɪ/', 1)")
            for option_no, word in ((1, "sheep"), (2, "ship")):
                conn.execute(
                    "INSERT INTO vowel_items(group_id, item_no, option_no, word, audio_f, audio_m) "
                    "VALUES(1, 1, ?, ?, ?, ?)",
                    (option_no, word, f"static/audio/{word}.f.wav", f"static/audio/{word}.m.wav"))
            conn.executemany("INSERT INTO words(word, meaning_cn) VALUES(?, ?)", WORDS)
        conn.close()
        self.alice = self.signed_in_client("alice")

    def signed_in_client(self, username):
        client = self.app.test_client()
        with mock.patch.object(config, "REGISTER_MODE", "open"):
            self.assertEqual(self.register(username, client=client).status_code, 302)
        return client

    def new_session(self, practice_type, client=None):
        res = (client or self.alice).post("/api/practice/session",
                                          json={"practice_type": practice_type, "params": {}})
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()["session_id"]

    def vowel_questions(self, sid, count=4, client=None):
        """出题，并从库里的快照把标准答案补回题目上（接口本身不下发答案）。"""
        res = (client or self.alice).post("/api/vowel/questions", json={
            "group_key": "sheep-ship", "count": count, "session_id": sid})
        self.assertEqual(res.status_code, 200, res.get_json())
        questions = res.get_json()["questions"]
        answers = {q["seq"]: q["answer"] for q in json.loads(self.query(
            "SELECT questions_json FROM practice_sessions WHERE id = ?", sid)[0][0])}
        for q in questions:
            self.assertNotIn("answer", q, "作答前不能下发答案")
            q["answer"] = answers[q["seq"]]
        return questions

    def check(self, sid, seq, choice, client=None):
        return (client or self.alice).post("/api/vowel/answer",
                                           json={"session_id": sid, "seq": seq, "choice": choice})

    def words(self, sid, count=3, client=None):
        res = (client or self.alice).post("/api/words", json={"count": count, "session_id": sid})
        self.assertEqual(res.status_code, 200, res.get_json())
        return res.get_json()["words"]

    def finish(self, sid, records, client=None):
        return (client or self.alice).post("/api/practice/finish",
                                           json={"session_id": sid, "records": records})

    def session_row(self, sid):
        return self.query("SELECT total, correct_count, wrong_count, finished "
                          "FROM practice_sessions WHERE id = ?", sid)[0]


class TestVowelGrading(GradingTestCase):

    def test_forged_is_correct_is_ignored(self):
        sid = self.new_session("vowel")
        questions = self.vowel_questions(sid)
        # 每题都选错，却声称答对
        records = [{"seq": q["seq"], "user_answer": 3 - q["answer"], "is_correct": True}
                   for q in questions]
        res = self.finish(sid, records)
        self.assertEqual(res.status_code, 200, res.get_json())
        self.assertEqual(res.get_json()["correct"], 0)
        self.assertEqual(self.session_row(sid), (4, 0, 4, 1))
        self.assertEqual({r[0] for r in self.query(
            "SELECT is_correct FROM practice_records WHERE session_id = ?", sid)}, {0})

    def test_right_answers_are_counted_even_if_flagged_wrong(self):
        sid = self.new_session("vowel")
        questions = self.vowel_questions(sid)
        records = [{"seq": q["seq"], "user_answer": str(q["answer"]), "is_correct": False}
                   for q in questions]
        self.assertEqual(self.finish(sid, records).get_json()["correct"], 4)

    def test_leaving_out_wrong_answers_does_not_raise_the_score(self):
        sid = self.new_session("vowel")
        questions = self.vowel_questions(sid)
        only_right = [{"seq": questions[0]["seq"], "user_answer": questions[0]["answer"]}]
        res = self.finish(sid, only_right).get_json()
        self.assertEqual((res["total"], res["correct"], res["wrong"]), (4, 1, 3))
        answers = self.query("SELECT seq, user_answer FROM practice_records "
                             "WHERE session_id = ? ORDER BY seq", sid)
        self.assertEqual([a for _, a in answers[1:]], [None, None, None])

    def test_timeout_and_junk_answers_are_wrong(self):
        sid = self.new_session("vowel")
        questions = self.vowel_questions(sid, count=3)
        records = [{"seq": questions[0]["seq"], "user_answer": None, "is_correct": True},
                   {"seq": questions[1]["seq"], "user_answer": True, "is_correct": True},
                   {"seq": questions[2]["seq"], "user_answer": "一", "is_correct": True}]
        self.assertEqual(self.finish(sid, records).get_json()["correct"], 0)

    def test_stored_question_comes_from_the_server(self):
        sid = self.new_session("vowel")
        q = self.vowel_questions(sid, count=1)[0]
        self.finish(sid, [{"seq": 1, "user_answer": q["answer"],
                           "question": {"type": "vowel", "answer": 9, "options": []}}])
        stored = json.loads(self.query(
            "SELECT question_json FROM practice_records WHERE session_id = ?", sid)[0][0])
        self.assertEqual(stored["answer"], q["answer"])
        self.assertEqual([o["word"] for o in stored["options"]], [o["word"] for o in q["options"]])
        self.assertEqual(self.alice.get(f"/result/{sid}").status_code, 200)


class TestWordGrading(GradingTestCase):

    def answer_by_word(self, sid, answers):
        words = self.words(sid)
        return self.finish(sid, [
            {"seq": i, "user_answer": answers[w["word"]], "is_correct": True}
            for i, w in enumerate(words, start=1)])

    def test_read_translate_is_judged_on_the_server(self):
        sid = self.new_session("read-translate")
        res = self.answer_by_word(sid, {"abandon": "放弃", "answer": ".", "firewall": "防火墙"})
        self.assertEqual(res.get_json()["correct"], 2)

    def test_empty_answer_placeholder_is_not_matched_as_text(self):
        """「无回答」是前端的占位文字；「answer」的释义里有「回答」，不能被它蹭对。"""
        self.assertTrue(grading.judge_chinese("n. 回答；答案", "无回答"),
                        "前提：按字面比较「无回答」确实会命中「回答」")
        sid = self.new_session("read-translate")
        res = self.answer_by_word(sid, {"abandon": "无回答", "answer": "无回答", "firewall": "无回答"})
        self.assertEqual(res.get_json()["correct"], 0)

    def test_listen_spell_ignores_case_and_spaces(self):
        sid = self.new_session("listen-spell")
        res = self.answer_by_word(sid, {"abandon": "  Abandon ", "answer": "anser", "firewall": "FIREWALL"})
        self.assertEqual(res.get_json()["correct"], 2)
        stored = json.loads(self.query("SELECT question_json FROM practice_records "
                                       "WHERE session_id = ? AND seq = 1", sid)[0][0])
        self.assertEqual(stored["type"], "listen-spell", "题型取会话自己的类型")


class TestIssuingAndRefusals(GradingTestCase):

    def test_finish_without_issued_questions_is_refused(self):
        """旧版脚本出题时不带 session_id：交卷回 409 要求刷新，会话也不被标成已交卷。"""
        sid = self.new_session("read-translate")
        self.alice.post("/api/words", json={"count": 2})
        res = self.finish(sid, [{"seq": 1, "user_answer": "放弃", "is_correct": True}])
        self.assertEqual(res.status_code, 409)
        self.assertIn("刷新", res.get_json()["error"])
        self.assertNotIn("result_url", res.get_json(), "不能被前端当成「已交过卷」直接跳结果页")
        self.assertEqual(self.session_row(sid)[3], 0)

    def test_unknown_and_duplicate_seq_are_refused(self):
        sid = self.new_session("vowel")
        questions = self.vowel_questions(sid, count=2)
        self.assertEqual(self.finish(sid, [{"seq": 99, "user_answer": 1}]).status_code, 400)
        dup = [{"seq": 1, "user_answer": questions[0]["answer"]}] * 2
        self.assertEqual(self.finish(sid, dup).status_code, 400)
        self.assertEqual(self.session_row(sid)[3], 0, "校验失败不能把会话烧掉")

    def test_questions_are_issued_only_once_per_session(self):
        sid = self.new_session("vowel")
        self.vowel_questions(sid)
        again = self.alice.post("/api/vowel/questions", json={
            "group_key": "sheep-ship", "count": 4, "session_id": sid})
        self.assertEqual(again.status_code, 409)

    def test_question_kind_must_match_the_session(self):
        vowel_sid = self.new_session("vowel")
        res = self.alice.post("/api/words", json={"count": 2, "session_id": vowel_sid})
        self.assertEqual(res.status_code, 400)
        word_sid = self.new_session("read-translate")
        res = self.alice.post("/api/vowel/questions", json={
            "group_key": "sheep-ship", "count": 2, "session_id": word_sid})
        self.assertEqual(res.status_code, 400)

    def test_cannot_issue_into_someone_elses_session(self):
        bob = self.signed_in_client("bob")
        sid = self.new_session("vowel", client=bob)
        res = self.alice.post("/api/vowel/questions", json={
            "group_key": "sheep-ship", "count": 2, "session_id": sid})
        self.assertEqual(res.status_code, 404)
        self.assertIsNone(self.query(
            "SELECT questions_json FROM practice_sessions WHERE id = ?", sid)[0][0])

    def test_cannot_issue_into_a_finished_session(self):
        sid = self.new_session("vowel")
        questions = self.vowel_questions(sid, count=1)
        self.finish(sid, [{"seq": 1, "user_answer": questions[0]["answer"]}])
        res = self.alice.post("/api/words", json={"count": 1, "session_id": sid})
        self.assertIn(res.status_code, (400, 409))


class TestVowelAnswerHidden(GradingTestCase):
    """服务器模式下近似音作答前拿不到答案：出题不带答案，音频地址不带单词，逐题判分锁定作答。"""

    def setUp(self):
        super().setUp()
        self.static_dir = os.path.join(self.tmp, "static")
        os.makedirs(os.path.join(self.static_dir, "audio"))
        for word in ("sheep", "ship"):
            for voice in ("f", "m"):
                with open(os.path.join(self.static_dir, "audio", f"{word}.{voice}.wav"), "wb") as fh:
                    fh.write(f"RIFF-{word}-{voice}".encode())
        # static_folder 是 Flask 的属性（没有 deleter），mock.patch 还原不了，手动换回
        saved = self.app.static_folder
        self.app.static_folder = self.static_dir
        self.addCleanup(setattr, self.app, "static_folder", saved)

    def test_questions_carry_no_answer_and_no_word_in_audio_url(self):
        sid = self.new_session("vowel")
        res = self.alice.post("/api/vowel/questions", json={
            "group_key": "sheep-ship", "count": 3, "session_id": sid}).get_json()
        for q in res["questions"]:
            self.assertNotIn("answer", q)
            self.assertEqual(q["audio_url"], f"/api/practice/audio/{sid}/{q['seq']}")
            self.assertNotIn("sheep", q["audio_url"])
            self.assertNotIn("ship", q["audio_url"])

    def test_server_mode_requires_a_session(self):
        res = self.alice.post("/api/vowel/questions", json={"group_key": "sheep-ship", "count": 2})
        self.assertEqual(res.status_code, 400)
        self.assertIn("刷新", res.get_json()["error"])

    def test_audio_is_served_by_seq_to_the_owner_only(self):
        sid = self.new_session("vowel")
        q = self.vowel_questions(sid, count=1)[0]
        stored = json.loads(self.query(
            "SELECT questions_json FROM practice_sessions WHERE id = ?", sid)[0][0])[0]
        res = self.alice.get(q["audio_url"])
        self.assertEqual(res.status_code, 200)
        expected = stored["audio_url"].rsplit("/", 1)[-1]          # 如 sheep.f.wav
        word, voice, _ = expected.split(".")
        self.assertEqual(res.get_data(), f"RIFF-{word}-{voice}".encode())
        self.assertIn("private", res.headers["Cache-Control"])
        res.close()
        bob = self.signed_in_client("bob")
        self.assertEqual(bob.get(q["audio_url"]).status_code, 404)
        self.assertEqual(self.alice.get(f"/api/practice/audio/{sid}/99").status_code, 404)

    def test_check_returns_verdict_and_answer(self):
        sid = self.new_session("vowel")
        q = self.vowel_questions(sid, count=1)[0]
        res = self.check(sid, 1, q["answer"]).get_json()
        self.assertEqual(res, {"seq": 1, "choice": q["answer"], "correct": True, "answer": q["answer"]})

    def test_first_choice_is_locked(self):
        """先交错的选项换出答案，再交对的那个：这一题仍然按第一次判，交卷时也改不了。"""
        sid = self.new_session("vowel")
        q = self.vowel_questions(sid, count=1)[0]
        wrong = 3 - q["answer"]
        self.assertFalse(self.check(sid, 1, wrong).get_json()["correct"])
        again = self.check(sid, 1, q["answer"]).get_json()
        self.assertEqual((again["choice"], again["correct"]), (wrong, False))
        res = self.finish(sid, [{"seq": 1, "user_answer": q["answer"], "is_correct": True}])
        self.assertEqual(res.get_json()["correct"], 0)
        self.assertEqual(self.query("SELECT user_answer FROM practice_records WHERE session_id = ?",
                                    sid), [(str(wrong),)])

    def test_timeout_is_locked_too(self):
        sid = self.new_session("vowel")
        q = self.vowel_questions(sid, count=1)[0]
        self.assertEqual(self.check(sid, 1, None).get_json()["choice"], None)
        self.assertEqual(self.finish(sid, [{"seq": 1, "user_answer": q["answer"]}])
                         .get_json()["correct"], 0)

    def test_unchecked_question_takes_the_submitted_choice(self):
        """判分请求没发出去的题，服务器从没给过答案，交卷时照收前端的选项。"""
        sid = self.new_session("vowel")
        questions = self.vowel_questions(sid, count=2)
        self.check(sid, 1, questions[0]["answer"])
        res = self.finish(sid, [{"seq": 1, "user_answer": questions[0]["answer"]},
                                {"seq": 2, "user_answer": questions[1]["answer"]}])
        self.assertEqual(res.get_json()["correct"], 2)

    def test_check_refusals(self):
        sid = self.new_session("vowel")
        self.vowel_questions(sid, count=1)
        self.assertEqual(self.check(sid, 5, 1).status_code, 404)
        self.assertEqual(self.check(sid, "x", 1).status_code, 400)
        bob = self.signed_in_client("bob")
        self.assertEqual(self.check(sid, 1, 1, client=bob).status_code, 404)
        word_sid = self.new_session("read-translate")
        self.words(word_sid)
        self.assertEqual(self.check(word_sid, 1, 1).status_code, 400)
        self.finish(sid, [])
        self.assertEqual(self.check(sid, 1, 1).status_code, 409)
        self.assertEqual(self.query("SELECT COUNT(*) FROM practice_answers WHERE session_id = ?",
                                    sid), [(0,)], "被拒的判分请求不能锁定作答")


class TestMigration(unittest.TestCase):

    def test_old_sessions_table_gets_questions_column(self):
        tmp = tempfile.mkdtemp(prefix="ep-grading-migrate-")
        try:
            conn = sqlite3.connect(os.path.join(tmp, "app.db"))
            conn.executescript("""
                CREATE TABLE practice_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                    practice_type TEXT NOT NULL, params_json TEXT,
                    total INTEGER NOT NULL DEFAULT 0, correct_count INTEGER NOT NULL DEFAULT 0,
                    wrong_count INTEGER NOT NULL DEFAULT 0, finished INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP);
                INSERT INTO practice_sessions(user_id, practice_type, finished) VALUES(1, 'vowel', 1);
            """)
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                init_db.migrate_sessions_questions_column(conn)
                init_db.migrate_sessions_questions_column(conn)     # 第二次什么都不做
            columns = {row[1] for row in conn.execute("PRAGMA table_info(practice_sessions)")}
            self.assertIn("questions_json", columns)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM practice_sessions").fetchone()[0], 1)
            conn.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# 两边都应成立的看读译用例：(释义, 作答, 应判对与否)
CHINESE_CASES = [
    ("vt. 放弃；抛弃", "放弃", True),
    ("vt. 放弃；抛弃", "放弃了", True),
    ("vt. 放弃；抛弃", "我不知道放弃", False),
    ("vt. 放弃；抛弃", "弃", False),
    ("n. [计] 防火墙", "防火墙", True),
    ("n. [计] 防火墙", ".", False),
    ("n. [计] 防火墙", "n", False),
    ("n. [计] 防火墙", "的", False),
    ("a. 满意的（感到）", "满意", True),
    ("给…穿衣", "给穿衣", True),
    ("n. 扫除, 打扫；vt. 掸去", "打扫", True),
    ("n. 扫除, 打扫；vt. 掸去", "vt.", False),
    ("", "什么", False),
    ("放弃", "  放 弃 ", True),
]


class TestChineseRule(unittest.TestCase):

    def test_cases(self):
        for meaning, answer, expected in CHINESE_CASES:
            self.assertEqual(grading.judge_chinese(meaning, answer), expected, (meaning, answer))

    def test_same_verdict_as_tworounds_js(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("没有 node，跳过与 tworounds.js 的对照")
        with open(os.path.join(BASE_DIR, "scripts", "tworounds.js"), encoding="utf-8") as fh:
            source = fh.read()
        match = re.search(r"  const POS_PREFIX = .*?\n  function judgeChinese\(word, answer\) \{.*?\n  \}\n",
                          source, re.S)
        self.assertIsNotNone(match, "tworounds.js 里找不到 judgeChinese，判分规则是不是改名了？")
        cases = [[m, a] for m, a, _ in CHINESE_CASES]
        script = (match.group(0) +
                  "const cases = JSON.parse(process.argv[1]);\n"
                  "console.log(JSON.stringify(cases.map(([m, a]) => judgeChinese({meaning_cn: m}, a))));\n")
        out = subprocess.run([node, "-e", script, json.dumps(cases)],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(out), [grading.judge_chinese(m, a) for m, a in cases])


if __name__ == "__main__":
    unittest.main(verbosity=2)
