"""课表解析、咨询回答和 MCP 往返。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from campus_agent.accounts import login, register, user_from_token
from campus_agent.browser_login import find_browser, format_cookie_header
from campus_agent.agent import Agent, _format_courses, list_chat, trim_history
from campus_agent.bilibili import videos_from_payload
from campus_agent.memory import list_memories
from campus_agent.rag import search
from campus_agent.config import today
from campus_agent.db import connect, set_meta
from campus_agent.gdut import rows_to_courses, rows_to_grades
from campus_agent.identity import current_user_id
from campus_agent.mcp_client import McpClient
from campus_agent.schedule import compact_weeks, parse_weeks, semester_week
from campus_agent import tools as tool_module
from campus_agent.tools import add_exam, course_detail, dispatch, import_school_grades, list_courses, load_sample_schedule, upcoming_exams
from campus_agent.web import make_server


class CampusTest(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._old_db = os.environ.get("CAMPUS_DB_PATH")
        self._old_key = os.environ.get("OPENAI_API_KEY")
        os.environ["CAMPUS_DB_PATH"] = str(Path(self._dir.name) / "campus.db")
        os.environ["OPENAI_API_KEY"] = ""

    def tearDown(self):
        if self._old_db is None:
            os.environ.pop("CAMPUS_DB_PATH", None)
        else:
            os.environ["CAMPUS_DB_PATH"] = self._old_db
        if self._old_key is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = self._old_key
        self._dir.cleanup()

    def pin_this_week(self):
        load_sample_schedule()
        monday = today() - timedelta(days=today().weekday())
        conn = connect()
        try:
            set_meta(conn, "semester_start", monday.isoformat())
        finally:
            conn.close()

    def test_week_page_turns(self):
        load_sample_schedule()
        current = list_courses("week")
        nxt = list_courses("week", week_offset=1)
        self.assertGreater(current["week"], 0)
        self.assertEqual(len(current["days"]), 7)
        start = today().__class__.fromisoformat(current["week_start"])
        self.assertEqual(today().__class__.fromisoformat(nxt["week_start"]), start + timedelta(days=7))
        self.assertEqual(nxt["week"], current["week"] + 1)

    def test_weeks_and_semester(self):
        self.assertEqual(parse_weeks("1-3,5"), [1, 2, 3, 5])
        self.assertEqual(parse_weeks("1-8单"), [1, 3, 5, 7])
        self.assertEqual(compact_weeks([1, 2, 3, 5]), "1-3,5")
        self.assertEqual(semester_week(today().__class__.fromisoformat("2026-11-08"), today().__class__.fromisoformat("2026-09-07")), 9)

    def test_course_teacher_and_review(self):
        load_sample_schedule()
        detail = course_detail("操作系统")
        self.assertEqual(detail["matches"][0]["sessions"][0]["teacher"], "周老师")
        self.assertEqual(detail["matches"][0]["sessions"][0]["classroom"], "工学2号馆 214")
        reply = Agent().chat("s1", "复盘一下操作系统上次课")["reply"]
        self.assertIn("进程", reply)
        self.assertIn("信号量", reply)

    def test_tomorrow_classes_follow_weekday(self):
        self.pin_this_week()
        expected = {
            1: {"软件工程", "操作系统"},
            2: {"软件工程实验"},
            3: {"计算机网络"},
            4: {"数据库系统"},
            5: {"编译原理"},
            6: set(),
            7: set(),
        }
        tomorrow = today() + timedelta(days=1)
        result = list_courses("tomorrow")
        names = {item["name"] for item in result["courses"]}
        self.assertEqual(names, expected[tomorrow.weekday() + 1])

    def test_exam_reminder_and_import(self):
        self.pin_this_week()
        when = today() + timedelta(days=2)
        added = add_exam("编译原理", when.strftime("%Y-%m-%d") + " 09:30", "工学2号馆 108", "闭卷", 7)
        self.assertTrue(added["ok"])
        found = [item for item in upcoming_exams(30)["exams"] if item["exam_at"].startswith(when.isoformat())]
        self.assertEqual(found[0]["days_left"], 2)
        self.assertTrue(found[0]["should_remind"])
        csv_text = "课程,教师,课室,星期,开始时间,结束时间,周次,教学内容\n形势与政策,吴老师,工学1号馆 101,周一,19:00,20:35,1-8,时政专题\n"
        imported = dispatch("import_schedule", {"content": csv_text, "replace": True})
        self.assertEqual(imported["courses"], 1)
        self.assertEqual(course_detail("形势与政策")["matches"][0]["sessions"][0]["teacher"], "吴老师")

    def test_record_review_roundtrip(self):
        load_sample_schedule()
        reply = Agent().chat("s2", "记录复盘 数据库系统 主题：事务 要点：原子性和隔离性要一起看 问题：脏读怎么避免")["reply"]
        self.assertIn("事务", reply)
        follow = Agent().chat("s2", "数据库系统上次课讲了什么")["reply"]
        self.assertIn("原子性", follow)

    def test_mcp_course_lookup(self):
        client = McpClient()
        try:
            tools = client.list_tools()
            self.assertIn("course_detail", [item["name"] for item in tools])
            result = client.call_tool("course_detail", {"name": "软件工程"})
        finally:
            client.close()
        self.assertTrue(result["ok"])
        self.assertEqual(result["matches"][0]["sessions"][0]["teacher"], "李老师")
        self.assertIn("工学1号馆 301", result["matches"][0]["sessions"][0]["classroom"])

    def test_cookie_header_only_keeps_jxfw(self):
        header = format_cookie_header(
            [
                {"name": "JSESSIONID", "value": "abc", "domain": "jxfw.gdut.edu.cn"},
                {"name": "route", "value": "no", "domain": "authserver.gdut.edu.cn"},
            ]
        )
        self.assertEqual(header, "JSESSIONID=abc")
        self.assertTrue(find_browser().exists())

    def test_gdut_row_parser(self):
        courses = rows_to_courses(
            [
                {
                    "kcmc": "软件工程",
                    "teaxms": "李老师",
                    "jxcdmcs": "工学1号馆 301",
                    "xq": "1",
                    "jcdm2": "01,02",
                    "zcs": "111111111111111100000000",
                    "jxhjmc": "理论",
                }
            ]
        )
        self.assertEqual(courses[0]["teacher"], "李老师")
        self.assertEqual(courses[0]["start_time"], "08:30")
        self.assertEqual(courses[0]["end_time"], "10:05")
        self.assertEqual(courses[0]["weeks"], "1-16")

    def test_school_grades_replace_only_official_rows(self):
        parsed = rows_to_grades(
            [
                {
                    "kcmc": "操作系统",
                    "zcj": "86",
                    "xf": "3",
                    "cjjd": "3.7",
                    "pscj": "90",
                    "qmcj": "82",
                    "xnxqmc": "2026-2027学年第一学期",
                },
                {"kcmc": "形势与政策", "zcj": "优秀", "xf": "0.5"},
            ]
        )
        self.assertEqual(parsed[0]["total"], 86.0)
        self.assertEqual(parsed[0]["detail"]["平时"]["score"], 90.0)
        self.assertEqual(parsed[1]["detail"]["成绩"], "优秀")
        self.assertEqual(parsed[1]["full_mark"], 0)
        dispatch(
            "add_grade",
            {"course_name": "软件工程", "title": "期末", "total": 90, "full_mark": 100, "detail": "大题:60/70"},
        )
        import_school_grades(parsed)
        import_school_grades(parsed[:1])
        listed = dispatch("list_grades", {})
        names = [item["course_name"] for item in listed["items"]]
        self.assertEqual(names.count("操作系统"), 1)
        self.assertNotIn("形势与政策", names)
        self.assertIn("软件工程", names)
        school = next(item for item in listed["items"] if item["course_name"] == "操作系统")
        self.assertIn("平时:90", school["detail_text"])
        self.assertNotIn("_source", school["detail_text"])

    def test_accounts_do_not_share_schedules(self):
        load_sample_schedule()
        created = register("3124000001", "secret12", "测试")
        self.assertTrue(created["ok"])
        self.assertIsNotNone(user_from_token(created["token"]))
        with self.assertRaises(ValueError):
            login("3124000001", "wrong-password")
        token = current_user_id.set(created["user"]["id"])
        try:
            self.assertEqual(course_detail("软件工程")["matches"], [])
        finally:
            current_user_id.reset(token)
        self.assertEqual(course_detail("软件工程")["matches"][0]["sessions"][0]["teacher"], "李老师")

    def test_http_chat(self):
        app_server = make_server("127.0.0.1", 0)
        thread = threading.Thread(target=app_server.serve_forever, daemon=True)
        thread.start()
        try:
            port = app_server.server_address[1]
            base = f"http://127.0.0.1:{port}"
            denied = urllib.request.Request(
                base + "/api/chat",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(denied, timeout=20)
            self.assertEqual(caught.exception.code, 401)
            caught.exception.close()
            registered = self._post(base, "/api/register", {"student_no": "3124000002", "password": "secret12", "display_name": "同学"})
            cookie = registered[1]
            self._post(base, "/api/sample", {}, cookie)
            body, _ = self._post(base, "/api/chat", {"session_id": "web", "message": "操作系统的老师和课室是什么？"}, cookie)
            self.assertIn("周老师", body["reply"])
            self.assertIn("工学2号馆 214", body["reply"])
            self.assertTrue(body["traces"])
            page = urllib.request.urlopen(base + "/", timeout=10).read().decode("utf-8")
            self.assertIn("广工资讯", page)
            self.assertIn("导入刷新", page)
            self.assertIn("打开学校登录页", page)
            self.assertIn("用 Cookie 登录", page)
            self.assertIn("loginDialog", page)
            self.assertIn("课程评价", page)
            self.assertIn("scheduleDialog", page)
            self.assertIn("本周课表", page)
            self.assertIn("上一周", page)
            self.assertIn("下一周", page)
            self.assertIn("考试成绩", page)
            self.assertIn("成绩分析", page)
            self.assertIn("课程问答", page)
            self.assertIn('id="topics"', page)
            self.assertIn("查看更早的对话", page)
            self.assertIn("Max-Age=1209600", registered[1])
        finally:
            app_server.shutdown()
            app_server.server_close()
            app_server.app.close()

    def test_table_memory_context_and_index(self):
        load_sample_schedule()
        text = _format_courses(
            {
                "ok": True,
                "scope": "today",
                "courses": [
                    {
                        "date": "2026-11-09",
                        "weekday_name": "周一",
                        "start_time": "08:30",
                        "end_time": "10:05",
                        "name": "软件工程",
                        "teacher": "李老师",
                        "classroom": "301",
                        "content": "用例",
                    }
                ],
            }
        )
        self.assertIn("| 课程 |", text)
        self.assertIn("李老师", text)
        Agent().chat("mem", "请记住我周三晚上不排实验课")
        self.assertTrue(any("周三" in item for item in list_memories()))
        hits = search("操作系统课室")
        self.assertTrue(any("周老师" in item["text"] for item in hits))
        rows = [{"id": index, "role": "user", "content": "课" * 500} for index in range(20)]
        kept, dropped = trim_history(rows)
        self.assertLessEqual(len(kept), 8)
        self.assertLessEqual(sum(len(item["content"]) for item in kept), 6000)
        self.assertEqual(len(kept) + len(dropped), 20)

    def test_evaluations_group_and_search(self):
        load_sample_schedule()
        added = dispatch("add_evaluation", {"course_name": "软件工程", "teacher": "李老师", "rating": 4, "comment": "用例讲得很清楚"})
        self.assertTrue(added["ok"])
        listed = dispatch("list_evaluations", {})
        self.assertEqual(listed["by_course"]["软件工程"][0]["teacher"], "李老师")
        self.assertEqual(listed["by_teacher"]["李老师"][0]["course_name"], "软件工程")
        hits = search("用例讲得很清楚")
        self.assertTrue(any(item["source"] == "evaluation" and "李老师" in item["text"] for item in hits))
        reply = Agent().chat("ev", "软件工程的评价怎么样")["reply"]
        self.assertIn("用例讲得很清楚", reply)
        self.assertIn("按老师", reply)
        saved = Agent().chat("ev2", "记录评价 操作系统 周老师 5分 评语：调度讲得明白")["reply"]
        self.assertIn("周老师", saved)
        self.assertIn("调度讲得明白", saved)

    def test_chat_window_and_grade_detail(self):
        load_sample_schedule()
        agent = Agent()
        for index in range(20):
            agent._save("page", "user", f"问题{index} 操作系统课室")
            agent._save("page", "assistant", f"回答{index}")
        page = list_chat("page", 0, 5)
        self.assertEqual(len(page["messages"]), 5)
        self.assertTrue(page["has_more"])
        self.assertEqual(page["messages"][-1]["content"], "回答19")
        older = list_chat("page", page["messages"][0]["id"], 5)
        self.assertLess(older["messages"][-1]["id"], page["messages"][0]["id"])
        conn = connect()
        try:
            left = int(conn.execute("SELECT COUNT(*) AS n FROM messages WHERE session_id = 'page'").fetchone()["n"])
        finally:
            conn.close()
        self.assertLessEqual(left, 36)
        self.assertTrue(any("问题0" in item for item in list_memories(30)))
        self.assertTrue(any(item["source"] == "memory" and "问题0" in item["text"] for item in search("问题0", 8)))
        added = dispatch(
            "add_grade",
            {
                "course_name": "操作系统",
                "title": "期中",
                "total": 86,
                "full_mark": 100,
                "detail": "选择:28/30，填空:18/20，大题:40/50",
            },
        )
        self.assertEqual(added["detail"]["选择"]["score"], 28.0)
        reply = Agent().chat("grades", "操作系统的成绩是多少")["reply"]
        self.assertIn("86", reply)
        self.assertIn("选择:28", reply)
        refused = Agent().chat("grades2", "记录成绩 软件工程 期末 总分90 满分100 选择:30/30，大题:60/70")["reply"]
        self.assertIn("教务", refused)
        listed = dispatch("list_grades", {})
        self.assertFalse(any(item["course_name"] == "软件工程" and item["total"] == 90 for item in listed["items"]))

    def test_grade_analysis_and_bilibili_session(self):
        load_sample_schedule()
        import_school_grades(
            [
                {"course_name": "操作系统", "title": "总评", "total": 58, "full_mark": 100, "detail": {"_source": "jxfw", "成绩": "58"}},
                {"course_name": "软件工程", "title": "总评", "total": 92, "full_mark": 100, "detail": {"_source": "jxfw", "成绩": "92"}},
            ]
        )
        analysis = dispatch("analyze_grades", {})
        self.assertEqual(analysis["weak"][0]["course_name"], "操作系统")
        self.assertEqual(analysis["strong"][0]["course_name"], "软件工程")
        report = Agent().chat("analysis", "分析一下我的成绩")["reply"]
        self.assertIn("58", report)
        parsed = videos_from_payload(
            {
                "data": {
                    "result": [
                        {
                            "title": "<em>操作系统</em>精讲",
                            "author": "某UP",
                            "bvid": "BV1test",
                            "play": 1000,
                            "arcurl": "https://www.bilibili.com/video/BV1test",
                        }
                    ]
                }
            }
        )
        self.assertEqual(parsed[0]["title"], "操作系统精讲")
        original = tool_module.search_bilibili
        tool_module.search_bilibili = lambda keyword, limit=5: parsed
        try:
            recommended = Agent().chat("rec", "推荐一门操作系统公开课", "knowledge")["reply"]
            explained = Agent().chat("know", "什么是管程", "knowledge")["reply"]
        finally:
            tool_module.search_bilibili = original
        self.assertIn("https://www.bilibili.com/video/BV1test", recommended)
        self.assertIn("课程知识问答", explained)
        os_reply = Agent().chat("k-os", "进程是什么", "knowledge", "操作系统")["reply"]
        se_reply = Agent().chat("k-se", "讲讲用例", "knowledge", "软件工程")["reply"]
        self.assertIn("课程问答 · 操作系统", os_reply)
        self.assertIn("课程问答 · 软件工程", se_reply)
        self.assertNotEqual(list_chat("k-os")["messages"][0]["id"], list_chat("k-se")["messages"][0]["id"])
        campus = list_chat("rec", 0, 10)
        knowledge = list_chat("know", 0, 10)
        self.assertTrue(campus["messages"])
        self.assertTrue(knowledge["messages"])
        self.assertNotEqual(campus["messages"][0]["id"], knowledge["messages"][0]["id"])

    def _post(self, base, path, payload, cookie=""):
        request = urllib.request.Request(
            base + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Cookie": cookie},
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            header = response.headers.get("Set-Cookie") or cookie
            token = header
            return json.loads(response.read().decode("utf-8")), token


if __name__ == "__main__":
    unittest.main()
