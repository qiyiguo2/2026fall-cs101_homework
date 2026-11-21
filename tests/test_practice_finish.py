"""交卷接口 /api/practice/finish：一场练习只能交一次。

以前对已交卷的会话再提交，会把整场成绩删掉重写；现在第二次提交回 409，
附上结果页地址，库里的成绩和逐题记录原样不动。

跑法同 test_app_modes.py（依赖开发库 data/app.db，测试只写它的副本）：

    python -m unittest tests.test_practice_finish -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_app_modes import ModeTestBase, app_module, issued_answers, table_count  # noqa: E402  复用「库副本 + 造用户」


class TestFinishOnlyOnce(ModeTestBase):

    def start_session(self, count=3):
        """建会话并按后端判分的流程出 count 道近似音题，返回会话 id。"""
        self.as_server()
        self.sign_in()
        res = self.client.post("/api/practice/session",
                               json={"practice_type": "vowel", "params": {}})
        self.assertEqual(res.status_code, 200, res.get_json())
        sid = res.get_json()["session_id"]
        with app_module.app.app_context():
            from services import practice
            group = practice.list_vowel_groups()[0]["group_key"]
        res = self.client.post("/api/vowel/questions",
                               json={"group_key": group, "count": count, "session_id": sid})
        self.assertEqual(res.status_code, 200, res.get_json())
        self.questions = res.get_json()["questions"]
        answers = issued_answers(self.db_path, sid)
        for q in self.questions:
            q["answer"] = answers[q["seq"]]
        return sid

    def finish(self, sid, results):
        """results[i] 为 True 时第 i+1 题选标准答案，否则选另一个；只交 results 那么多题。"""
        records = [{"seq": q["seq"], "question": {"word": "前端写的不算"},
                    "user_answer": q["answer"] if ok else 3 - q["answer"],
                    "is_correct": ok} for q, ok in zip(self.questions, results)]
        return self.client.post("/api/practice/finish",
                                json={"session_id": sid, "records": records})

    def session_row(self, sid):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(
                "SELECT total, correct_count, wrong_count, finished "
                "FROM practice_sessions WHERE id = ?", (sid,)).fetchone()
        finally:
            conn.close()

    def test_first_finish_is_recorded(self):
        sid = self.start_session()
        res = self.finish(sid, [True, False, True])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()["correct"], 2)
        self.assertEqual(self.session_row(sid), (3, 2, 1, 1))

    def test_second_finish_is_refused_and_changes_nothing(self):
        sid = self.start_session()
        self.assertEqual(self.finish(sid, [False, False, False]).status_code, 200)
        records_before = table_count(self.db_path, "practice_records")

        res = self.finish(sid, [True] * 10)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.get_json()["result_url"], f"/result/{sid}")
        self.assertEqual(self.session_row(sid), (3, 0, 3, 1), "成绩被第二次提交改写了")
        self.assertEqual(table_count(self.db_path, "practice_records"), records_before)

    def test_bad_records_do_not_burn_the_session(self):
        """校验不通过的提交不能把会话标成已交卷，否则改好之后再也交不上。"""
        sid = self.start_session()
        bad = self.client.post("/api/practice/finish",
                               json={"session_id": sid, "records": ["不是对象"]})
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(self.session_row(sid)[3], 0)
        self.assertEqual(self.finish(sid, [True]).status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
