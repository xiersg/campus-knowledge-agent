"""对话循环：有模型时由模型选工具，没有密钥时按问题直接查课表。"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import timedelta

from campus_agent.config import (
    CHAT_PAGE_SIZE,
    KEEP_MESSAGES,
    MAX_HISTORY_CHARS,
    MAX_HISTORY_MESSAGES,
    MAX_REQUEST_CHARS,
    MAX_STORED_MESSAGES,
    chat_completions_url,
    llm_settings,
    now_local,
    today,
    weekday_name,
)
from campus_agent.db import connect, get_user_meta, rows_as_dicts, set_user_meta
from campus_agent.identity import current_user_id
from campus_agent.memory import capture_memory, list_memories, remember
from campus_agent.rag import search
from campus_agent.tools import TOOL_SPECS, dispatch

SYSTEM = """你是广工校园知识咨询助手，只根据工具返回的课表、考试、课堂复盘和课程评价回答。
规则：
1. 教师、课室、上课时间、教学内容、考试时间和复盘要点都必须来自工具，不要凭记忆编造。
2. 问今天、明天或本周课表时用 list_courses；问某门课的老师、课室或讲什么时用 course_detail。
3. 问考试、测验、答辩时用 upcoming_exams。需要新增考试时用 add_exam。
4. 问某次课讲了什么、怎么复习时用 list_reviews；用户明确要求记下复盘时用 add_review。
5. 问课程评价、老师评语时用 list_evaluations 或 search_knowledge。用户要记下评价时用 add_evaluation。评价按课程和教师归类，不要编造评分。
6. 问成绩、分数、得分时用 list_grades。用户要分析成绩时用 analyze_grades。成绩只来自教务导入，不要编造分数，也不要调用工具去手记成绩。
7. 用户要推荐课程、公开课或 B 站视频时用 recommend_courses，优先给出 B 站标题和链接。不要编造不存在的视频。
8. 用户给出 CSV 或 JSON 课表时用 import_schedule。用户要示例数据时用 load_sample_schedule。
9. 先给结论，再补一句复习建议。工具失败时说明原因，不要假装已经完成。
10. 课表、考试、复盘、评价、成绩和推荐课程用 Markdown 表格回答。课表列：日期、时间、课程、教师、课室、教学内容。评价要分别给出按课程、按教师两张表。成绩列：课程、项目、总分、满分、分项。推荐列：课程、UP主、播放、链接。
11. 教学内容、复盘、评价、成绩或用户说过要记住的事，先用 search_knowledge 查索引，再按需要调用对应工具核对。
12. 用简洁中文回答。不要复述过长的历史。
"""

KNOWLEDGE_SYSTEM = """你是课程知识问答助手。这个会话只讲课程知识，不处理课表导入和选课。
规则：
1. 讲概念前先用 search_knowledge、course_detail、list_reviews 查本机已有的教学内容和复盘，不要把没查到的内容说成老师讲过。
2. 用户要推荐课程、公开课、入门视频，或本机没有足够材料时，用 recommend_courses，优先搜索 B 站，给出标题和链接。不要编造视频。
3. 用户要分析成绩时用 analyze_grades。不要编造成绩，也不要手记成绩。
4. 先讲清楚知识点，再给一个可以马上做的复习动作。
5. 用简洁中文。推荐结果用 Markdown 表格。
"""


class ToolGateway:
    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        return dispatch(name, arguments or {})


class Agent:
    def __init__(self, gateway: ToolGateway | None = None) -> None:
        self.gateway = gateway or ToolGateway()

    def chat(self, session_id: str, user_text: str, mode: str = "campus", topic: str = "") -> dict:
        text = user_text.strip()
        self.mode = mode if mode in {"campus", "knowledge"} else "campus"
        self.topic = topic.strip()[:40] if self.mode == "knowledge" else ""
        if not text:
            return {"reply": "请先输入问题。例如：明天软件工程在哪上课？", "traces": []}
        user_message_id = self._save(session_id, "user", text)
        capture_memory(text)
        settings = llm_settings()
        traces: list[dict] = []
        if settings["api_key"]:
            try:
                reply, traces = self._llm_chat(session_id, text, settings)
            except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
                reply, traces = self._fallback(text)
                reply = f"模型接口暂时不可用（{exc}）。已改用本机资料直接回答。\n\n{reply}"
        else:
            reply, traces = self._fallback(text)
        assistant_message_id = self._save(session_id, "assistant", reply)
        return {
            "reply": reply,
            "traces": traces,
            "user_message_id": user_message_id,
            "assistant_message_id": assistant_message_id,
        }

    def _llm_chat(self, session_id: str, user_text: str, settings: dict) -> tuple[str, list[dict]]:
        moment = now_local()
        memories = list_memories(8)
        hits = search(user_text, 4)
        extra = ""
        if memories:
            extra += "\n\n用户长期记忆（只作背景，课表事实以工具为准）：\n" + "\n".join(f"- {item}" for item in memories)
        if hits:
            extra += "\n\n检索索引：\n" + "\n".join(f"- [{item['source']}] {item['text']}" for item in hits)
        messages: list[dict] = [
            {
                "role": "system",
                "content": (
                    f"{KNOWLEDGE_SYSTEM if getattr(self, 'mode', 'campus') == 'knowledge' else SYSTEM}"
                    f"{_topic_clause(getattr(self, 'topic', ''))}\n"
                    f"当前时间：{moment.strftime('%Y-%m-%d %H:%M')} "
                    f"{weekday_name(moment.date().weekday() + 1)}。{extra}"
                ),
            }
        ]
        history = self._history(session_id)[:-1]
        kept, dropped = trim_history(history)
        self._fold(session_id, dropped)
        for item in kept:
            messages.append({"role": item["role"], "content": item["content"][:2000]})
        messages.append({"role": "user", "content": user_text})
        tools = [_openai_tool(spec) for spec in TOOL_SPECS]
        traces: list[dict] = []
        tables: list[str] = []
        for _ in range(5):
            response = _complete(settings, _fit_messages(messages), tools)
            choice = response["choices"][0]["message"]
            tool_calls = choice.get("tool_calls") or []
            if not tool_calls:
                reply = (choice.get("content") or "我还没有形成回答。").strip()
                if tables and "|" not in reply:
                    reply = "\n\n".join(tables) + "\n\n" + reply
                return reply, traces
            messages.append(
                {
                    "role": "assistant",
                    "content": choice.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )
            for call in tool_calls:
                name = call.get("function", {}).get("name", "")
                raw_args = call.get("function", {}).get("arguments") or "{}"
                try:
                    arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                result = self.gateway.call_tool(name, arguments)
                traces.append({"tool": name, "arguments": arguments, "ok": bool(result.get("ok", False))})
                table = _table_from_tool(name, result)
                if table:
                    tables.append(table)
                content = json.dumps(result, ensure_ascii=False)
                if len(content) > 2000:
                    content = content[:2000] + "…（已截断）"
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id") or name,
                        "content": content,
                    }
                )
        return "这个问题连续查了几次仍没有收束。可以换一种问法，例如直接说课程名。", traces

    def _fallback(self, text: str) -> tuple[str, list[dict]]:
        traces: list[dict] = []

        def use(name: str, arguments: dict | None = None) -> dict:
            result = self.gateway.call_tool(name, arguments or {})
            traces.append({"tool": name, "arguments": arguments or {}, "ok": bool(result.get("ok", False))})
            return result

        if _looks_like_table(text) or ("导入" in text and ("课程" in text or "," in text or "{" in text)):
            content = text
            if "导入" in text and not _looks_like_table(text):
                return "把 CSV 或 JSON 课表贴到输入框。CSV 需要表头：课程,教师,课室,星期,开始时间,结束时间,周次,教学内容。", traces
            result = use("import_schedule", {"content": content, "replace": True})
            if not result.get("ok"):
                return f"导入没有成功：{result.get('error', '未知原因')}", traces
            exam_part = f"、{result['exams']} 场考试" if result.get("exams") else ""
            review_part = f"、{result['reviews']} 条复盘" if result.get("reviews") else ""
            return (
                f"已导入 {result['courses']} 门课{exam_part}{review_part}。"
                "可以继续问明天的课室，或最近的考试。"
            ), traces
        if "示例" in text and ("课表" in text or "导入" in text or "载入" in text):
            result = use("load_sample_schedule")
            if not result.get("ok"):
                return f"示例课表没有载入：{result.get('error', '')}", traces
            return "已载入示例课表。明天可以问软件工程和操作系统的课室、教师。", traces
        if getattr(self, "mode", "campus") == "knowledge":
            return self._fallback_knowledge(text, use), traces
        if _wants_recommendation(text):
            result = use("recommend_courses", {"topic": _topic_from_text(text)})
            return _format_recommendations(result), traces
        if "分析" in text and "成绩" in text:
            return _format_analysis(use("analyze_grades", {})), traces
        if any(word in text for word in ("成绩", "分数", "得分")) and "评价" not in text:
            if any(word in text for word in ("记录", "添加", "记下", "录入")):
                return "成绩从教务导入，不能在这里手记。登录后点「导入刷新」，再打开考试成绩查看。", traces
            result = use("list_grades", {"keyword": _known_course(text)})
            return _format_grades(result), traces
        if "评价" in text:
            if any(word in text for word in ("记录", "添加", "打分", "评一下")) or ("评语" in text and "分" in text):
                result = use("add_evaluation", _evaluation_from_text(text))
                if not result.get("ok"):
                    return f"评价没有记下：{result.get('error', '')}。可以写成：记录评价 软件工程 李老师 4分 评语：用例讲得清楚。", traces
                listed = use("list_evaluations", {"keyword": result.get("course_name", "")})
                return f"已记下 {result['course_name']} / {result['teacher']} 的 {result['rating']} 分评价。\n\n" + _format_evaluations(listed), traces
            result = use("list_evaluations", {"keyword": _known_course(text)})
            return _format_evaluations(result), traces
        if "记录" in text and "复盘" in text:
            result = use("add_review", _review_from_text(text))
            if not result.get("ok"):
                return f"复盘没有记下：{result.get('error', '')}。可以写成：记录复盘 软件工程 主题：用例图 要点：参与者和扩展关系。", traces
            return f"已记下 {result['course_name']} 在 {result['session_date']} 的复盘：{result['topic']}。", traces
        if any(word in text for word in ("考试", "测验", "答辩", "提醒")) and "复盘" not in text:
            if text.startswith("添加考试") or text.startswith("新增考试"):
                result = use("add_exam", _exam_from_text(text))
                if not result.get("ok"):
                    return f"考试没有加上：{result.get('error', '')}", traces
                return f"已添加 {result['course_name']}，时间 {result['exam_at']}，地点 {result['location']}。", traces
            result = use("upcoming_exams", {"within_days": 45})
            return _format_exams(result), traces
        course = _known_course(text)
        asks_place = any(word in text for word in ("课室", "教室", "在哪", "哪里", "老师", "教师", "谁上", "教学内容", "讲什么"))
        asks_when = any(word in text for word in ("课表", "今天", "明天", "后天", "本周", "这周", "有什么课", "有课"))
        if any(word in text for word in ("复盘", "上节课", "上次课", "讲了什么", "复习")):
            result = use("list_reviews", {"course_name": course, "limit": 3})
            detail = use("course_detail", {"name": course}) if course else {"ok": True, "matches": []}
            return _format_review(result, detail, course), traces
        if asks_place and course and not asks_when:
            result = use("course_detail", {"name": course})
            return _format_detail(result), traces
        if asks_when or (asks_place and not course):
            if "后天" in text:
                scope = "week"
            elif "明天" in text:
                scope = "tomorrow"
            elif "本周" in text or "这周" in text:
                scope = "week"
            elif "全部" in text:
                scope = "all"
            elif "今天" in text:
                scope = "today"
            else:
                scope = "tomorrow" if asks_place else "today"
            result = use("list_courses", {"scope": scope, "keyword": course})
            if "后天" in text:
                target = (today() + timedelta(days=2)).isoformat()
                result["courses"] = [item for item in result.get("courses", []) if item.get("date") == target]
            answer = _format_courses(result)
            if asks_place and not course and not asks_when:
                answer = "没有指定课程时，按时间列出课室和教师。\n\n" + answer
            return answer, traces
        brief = use("campus_brief")
        return (
            "我可以查课表、课室和教师，提醒考试，也帮你复盘教学内容。\n\n" + _format_brief(brief)
        ), traces

    def _fallback_knowledge(self, text: str, use) -> str:
        course = getattr(self, "topic", "") or _known_course(text)
        if _wants_recommendation(text):
            result = use("recommend_courses", {"topic": course or _topic_from_text(text)})
            return _format_recommendations(result)
        if "分析" in text and "成绩" in text:
            return _format_analysis(use("analyze_grades", {}))
        detail = use("course_detail", {"name": course}) if course else {"ok": True, "matches": []}
        reviews = use("list_reviews", {"course_name": course, "limit": 3}) if course else {"ok": True, "reviews": []}
        found = use("search_knowledge", {"query": f"{course} {text}".strip()})
        parts = [f"课程问答 · {course}：" if course else "课程知识问答："]
        content = []
        for item in detail.get("matches") or []:
            if item.get("content"):
                content.append(f"{item.get('name') or course}的教学内容：{item['content']}")
        for item in reviews.get("reviews") or []:
            content.append(f"{item['session_date']} {item['topic']}：{item['key_points']}")
        for item in (found.get("hits") or [])[:3]:
            content.append(item.get("text") or "")
        if content:
            parts.append("\n".join(piece for piece in content if piece))
            parts.append("要看公开课可以说：推荐一门" + (course or "相关") + "课程。")
        else:
            parts.append("本机课表和复盘里还没有这段内容。下面优先给 B 站上的课。")
            parts.append(_format_recommendations(use("recommend_courses", {"topic": _topic_from_text(text)})))
        return "\n\n".join(parts)

    def _history(self, session_id: str) -> list[dict]:
        conn = connect()
        try:
            rows = rows_as_dicts(
                conn.execute(
                    "SELECT id, role, content FROM messages WHERE user_id = ? AND session_id = ? ORDER BY id DESC LIMIT 40",
                    (int(current_user_id.get()), session_id),
                ).fetchall()
            )
        finally:
            conn.close()
        rows.reverse()
        return rows

    def _fold(self, session_id: str, dropped: list[dict]) -> None:
        if not dropped or "id" not in dropped[-1]:
            return
        last_id = int(dropped[-1]["id"])
        user_id = int(current_user_id.get())
        key = "fold:" + session_id[:80]
        conn = connect()
        try:
            previous = int(get_user_meta(conn, user_id, key, "0") or "0")
            if previous >= last_id:
                return
            texts = [item["content"].replace("\n", " ")[:80] for item in dropped if item["role"] == "user"]
            if texts:
                remember("较早对话：" + "；".join(texts[-4:]))
            set_user_meta(conn, user_id, key, str(last_id))
            conn.commit()
        finally:
            conn.close()

    def _save(self, session_id: str, role: str, content: str) -> int:
        conn = connect()
        try:
            cursor = conn.execute(
                "INSERT INTO messages(user_id, session_id, role, content, created_at) VALUES(?, ?, ?, ?, ?)",
                (int(current_user_id.get()), session_id, role, content, now_local().isoformat(timespec="seconds")),
            )
            row_id = int(cursor.lastrowid)
            conn.commit()
        finally:
            conn.close()
        compact_chat(session_id)
        return row_id


def _openai_tool(spec: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec["inputSchema"],
        },
    }


def list_chat(session_id: str, before: int = 0, limit: int = CHAT_PAGE_SIZE) -> dict:
    size = max(1, min(int(limit or CHAT_PAGE_SIZE), 30))
    uid = int(current_user_id.get())
    conn = connect()
    try:
        if before:
            rows = rows_as_dicts(
                conn.execute(
                    """
                    SELECT id, role, content, created_at FROM messages
                    WHERE user_id = ? AND session_id = ? AND id < ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (uid, session_id, int(before), size),
                ).fetchall()
            )
        else:
            rows = rows_as_dicts(
                conn.execute(
                    """
                    SELECT id, role, content, created_at FROM messages
                    WHERE user_id = ? AND session_id = ?
                    ORDER BY id DESC LIMIT ?
                    """,
                    (uid, session_id, size),
                ).fetchall()
            )
        rows.reverse()
        oldest = rows[0]["id"] if rows else 0
        has_more = False
        if oldest:
            has_more = (
                conn.execute(
                    "SELECT 1 FROM messages WHERE user_id = ? AND session_id = ? AND id < ? LIMIT 1",
                    (uid, session_id, oldest),
                ).fetchone()
                is not None
            )
    finally:
        conn.close()
    return {"ok": True, "messages": rows, "has_more": has_more}


def compact_chat(session_id: str) -> None:
    uid = int(current_user_id.get())
    conn = connect()
    try:
        count = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE user_id = ? AND session_id = ?",
                (uid, session_id),
            ).fetchone()["n"]
        )
        if count <= MAX_STORED_MESSAGES:
            return
        extra = count - KEEP_MESSAGES
        rows = rows_as_dicts(
            conn.execute(
                """
                SELECT id, role, content FROM messages
                WHERE user_id = ? AND session_id = ?
                ORDER BY id ASC LIMIT ?
                """,
                (uid, session_id, extra),
            ).fetchall()
        )
        if not rows:
            return
        ids = [row["id"] for row in rows]
        conn.execute(
            f"DELETE FROM messages WHERE user_id = ? AND id IN ({','.join('?' for _ in ids)})",
            [uid, *ids],
        )
        conn.commit()
    finally:
        conn.close()
    bits = []
    for row in rows:
        who = "问" if row["role"] == "user" else "答"
        bits.append(who + ":" + row["content"].replace("\n", " ")[:40])
    remember("较早对话已压缩：" + "；".join(bits[:12]))


def trim_history(
    rows: list[dict],
    max_messages: int = MAX_HISTORY_MESSAGES,
    max_chars: int = MAX_HISTORY_CHARS,
) -> tuple[list[dict], list[dict]]:
    kept: list[dict] = []
    used = 0
    for item in reversed(rows):
        size = len(item.get("content") or "")
        if kept and (len(kept) >= max_messages or used + size > max_chars):
            break
        if not kept and size > max_chars:
            kept.append({**item, "content": item["content"][-max_chars:]})
            break
        kept.append(item)
        used += size
    kept.reverse()
    return kept, rows[: len(rows) - len(kept)]


def _fit_messages(messages: list[dict]) -> list[dict]:
    if sum(len(str(item.get("content") or "")) for item in messages) <= MAX_REQUEST_CHARS:
        return messages
    system = messages[:1]
    rest = messages[1:]
    kept: list[dict] = []
    used = len(str(system[0].get("content") or "")) if system else 0
    for item in reversed(rest):
        size = len(str(item.get("content") or ""))
        if kept and used + size > MAX_REQUEST_CHARS:
            break
        kept.append(item)
        used += size
    kept.reverse()
    return system + kept


def _complete(settings: dict, messages: list[dict], tools: list[dict]) -> dict:
    payload = {
        "model": settings["model"],
        "messages": messages,
        "tools": tools,
        "temperature": 0.2,
    }
    request = urllib.request.Request(
        chat_completions_url(settings["base_url"]),
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {settings['api_key']}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _looks_like_table(text: str) -> bool:
    stripped = text.lstrip()
    return stripped.startswith("{") or stripped.startswith("[") or stripped.startswith("课程,") or stripped.startswith("name,")


def _known_course(text: str) -> str:
    result = dispatch("list_courses", {"scope": "all"})
    names = sorted({item["name"] for item in result.get("courses", [])}, key=len, reverse=True)
    for name in names:
        if name and name in text:
            return name
    return ""


def _review_from_text(text: str) -> dict:
    def grab(label: str) -> str:
        match = re.search(label + r"[：:]\s*(.+?)(?=(?:主题|要点|问题|课程)[：:]|$)", text)
        return match.group(1).strip(" ，。") if match else ""

    date_match = re.search(r"\d{4}-\d{2}-\d{2}", text)
    course = _known_course(text) or grab("课程") or "未命名课程"
    return {
        "course_name": course,
        "session_date": date_match.group(0) if date_match else "",
        "topic": grab("主题") or "课堂复盘",
        "key_points": grab("要点") or text,
        "questions": grab("问题"),
    }


def _exam_from_text(text: str) -> dict:
    match = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})", text)
    course = _known_course(text) or "未命名课程"
    location = "待定"
    location_match = re.search(r"(?:地点|课室|教室)[：:\s]*([^\s，。]+(?:\s+[^\s，。]+)?)", text)
    if location_match:
        location = location_match.group(1).strip()
    return {
        "course_name": course,
        "exam_at": f"{match.group(1)} {match.group(2)}" if match else "",
        "location": location,
        "note": "",
    }


def _evaluation_from_text(text: str) -> dict:
    course = _known_course(text) or "未命名课程"
    catalog = dispatch("list_courses", {"scope": "all"})
    teachers = sorted(
        {item["teacher"] for item in catalog.get("courses", []) if item.get("teacher")},
        key=len,
        reverse=True,
    )
    teacher = next((name for name in teachers if name in text), "")
    if not teacher:
        matched = re.search(r"([\u4e00-\u9fff]{1,8})老师", text)
        teacher = (matched.group(1) + "老师") if matched else "未填教师"
    score = re.search(r"([1-5])\s*分", text)
    comment = re.search(r"评语[：:]\s*(.+)$", text)
    return {
        "course_name": course,
        "teacher": teacher,
        "rating": int(score.group(1)) if score else 5,
        "comment": comment.group(1).strip() if comment else "",
    }


def _cell(value: object) -> str:
    return str(value or "").replace("|", "/").replace("\n", " ").strip()


def _md_table(headers: list[str], rows: list[list[object]]) -> str:
    head = "| " + " | ".join(_cell(item) for item in headers) + " |"
    rule = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(_cell(item) for item in row) + " |" for row in rows]
    return "\n".join([head, rule, *body])


def _table_from_tool(name: str, result: dict) -> str:
    if name == "list_courses":
        return _format_courses(result) if result.get("courses") else ""
    if name == "course_detail":
        return _format_detail(result) if result.get("matches") else ""
    if name == "upcoming_exams":
        return _format_exams(result) if result.get("exams") else ""
    if name == "list_reviews":
        return _format_review(result, {"matches": []}, "") if result.get("reviews") else ""
    if name == "campus_brief":
        return _format_brief(result) if result.get("ok") else ""
    if name == "list_evaluations":
        return _format_evaluations(result) if result.get("items") else ""
    if name == "list_grades":
        return _format_grades(result) if result.get("items") else ""
    if name == "analyze_grades":
        return _format_analysis(result) if result.get("ok") else ""
    if name == "recommend_courses":
        return _format_recommendations(result) if result.get("ok") else ""
    return ""


def _format_courses(result: dict) -> str:
    if not result.get("ok"):
        return f"课表查询失败：{result.get('error', '')}"
    courses = result.get("courses") or []
    if not courses:
        scope = {"today": "今天", "tomorrow": "明天", "week": "本教学周", "all": "已导入的课表"}.get(result.get("scope"), "这个范围")
        return f"{scope}没有对应课程。可以点右上角登录后导入课表。"
    rows = []
    for item in courses:
        when = f"{item.get('date', '')} {item['weekday_name']}".strip()
        rows.append(
            [
                when,
                f"{item['start_time']}-{item['end_time']}",
                item["name"],
                item["teacher"],
                item["classroom"],
                item["content"] or "课表里还没写教学内容。",
            ]
        )
    return _md_table(["日期", "时间", "课程", "教师", "课室", "教学内容"], rows)


def _format_detail(result: dict) -> str:
    if not result.get("ok"):
        return f"课程查询失败：{result.get('error', '')}"
    matches = result.get("matches") or []
    if not matches:
        return f"课表里没有找到「{result.get('keyword', '')}」。"
    rows = []
    notes = []
    for item in matches:
        for session in item["sessions"]:
            rows.append(
                [
                    item["name"],
                    f"{session['weekday_name']} {session['start_time']}-{session['end_time']}",
                    session["teacher"],
                    session["classroom"],
                    item["content"] or "还没有填写。",
                ]
            )
        review = item.get("latest_review")
        if review:
            notes.append(
                [
                    item["name"],
                    review["session_date"],
                    review["topic"],
                    review["key_points"],
                    review.get("questions") or "",
                ]
            )
    text = _md_table(["课程", "时间", "教师", "课室", "教学内容"], rows)
    if notes:
        text += "\n\n" + _md_table(["课程", "日期", "主题", "要点", "问题"], notes)
    return text


def _format_exams(result: dict) -> str:
    if not result.get("ok"):
        return f"考试查询失败：{result.get('error', '')}"
    exams = result.get("exams") or []
    if not exams:
        return "未来 45 天里没有已登记的考试。可以用「添加考试 课程 2026-11-12 19:00 地点：课室」补一条。"
    rows = []
    for item in exams:
        flag = "该提醒了" if item["should_remind"] else "先记在日历上"
        rows.append([item["course_name"], item["exam_at"], item["location"], item["days_left"], f"{flag} {item['note']}".strip()])
    return "近期考试：\n\n" + _md_table(["课程", "时间", "地点", "还有几天", "备注"], rows)


def _format_review(result: dict, detail: dict, course: str) -> str:
    if not result.get("ok"):
        return f"复盘查询失败：{result.get('error', '')}"
    reviews = result.get("reviews") or []
    if not reviews:
        target = course or "这些课"
        return f"还没有 {target} 的课堂复盘。下课后可以说：记录复盘 {course or '课程名'} 主题：…… 要点：……"
    rows = [
        [item["course_name"], item["session_date"], item["topic"], item["key_points"], item["questions"] or "用自己的话把要点讲一遍。"]
        for item in reviews
    ]
    text = "教学内容复盘：\n\n" + _md_table(["课程", "日期", "主题", "要点", "问题"], rows)
    for item in detail.get("matches") or []:
        if item.get("content"):
            text += f"\n\n这门课的整体内容是：{item['content']}"
            break
    return text


def _grade_from_text(text: str) -> dict:
    title = "期末"
    for name in ("期中", "期末", "平时", "测验", "作业"):
        if name in text:
            title = name
            break
    total = re.search(r"(?:总分|得分|成绩)[：:\s]*([0-9]+(?:\.[0-9]+)?)", text)
    full = re.search(r"满分[：:\s]*([0-9]+(?:\.[0-9]+)?)", text)
    detail = ""
    matched = re.search(r"((?:选择|填空|大题|简答|论述).+)", text)
    if matched:
        detail = matched.group(1)
    return {
        "course_name": _known_course(text) or "未命名课程",
        "title": title,
        "total": float(total.group(1)) if total else 0,
        "full_mark": float(full.group(1)) if full else 100,
        "detail": detail,
    }


def _topic_clause(topic: str) -> str:
    if not topic:
        return ""
    return f"\n当前主题是《{topic}》。只讲这门课，检索、复盘和 B 站推荐都围绕它，不要串到其他课。"


def _wants_recommendation(text: str) -> bool:
    return any(word in text for word in ("推荐", "公开课", "b站", "B站", "哔哩"))


def _topic_from_text(text: str) -> str:
    course = _known_course(text)
    if course:
        return course
    cleaned = re.sub(r"推荐|一门|课程|公开课|b站|B站|哔哩哔哩|哔哩|视频|入门", "", text)
    cleaned = cleaned.strip(" ，。？?的")
    return cleaned[:40] or "大学课程"


def _format_analysis(result: dict) -> str:
    if not result.get("ok"):
        return f"成绩分析失败：{result.get('error', '')}"
    if not result.get("count"):
        return result.get("summary") or "还没有可分析的成绩。"
    lines = [result.get("summary") or "", ""]
    weak = result.get("weak") or []
    strong = result.get("strong") or []
    if weak:
        rows = [[item["course_name"], item["title"], item["percent"]] for item in weak]
        lines.append("偏低的课：\n\n" + _md_table(["课程", "项目", "百分制"], rows))
    if strong:
        rows = [[item["course_name"], item["title"], item["percent"]] for item in strong]
        lines.append("较好的课：\n\n" + _md_table(["课程", "项目", "百分制"], rows))
    if weak:
        lines.append(f"可以到课程问答里继续问「{weak[0]['course_name']}」，并让我推荐 B 站课程。")
    return "\n\n".join(line for line in lines if line)


def _format_recommendations(result: dict) -> str:
    topic = result.get("topic") or "课程"
    videos = result.get("videos") or []
    link = result.get("search_url") or ""
    if not videos:
        return f"这次没有解析到「{topic}」的 B 站视频。可以直接打开搜索：{link}"
    rows = [[item["title"], item["author"], item["play"], item["url"]] for item in videos]
    return f"优先在 B 站找「{topic}」：\n\n" + _md_table(["课程", "UP主", "播放", "链接"], rows) + f"\n\n更多结果：{link}"


def _format_grades(result: dict) -> str:
    items = result.get("items") or []
    if not items:
        return "还没有记下成绩。"
    lines = ["| 课程 | 项目 | 总分 | 满分 | 分项 |", "| --- | --- | --- | --- | --- |"]
    for item in items:
        shown = item["total"] if item.get("full_mark") else (item.get("detail") or {}).get("成绩", item["total"])
        full = item["full_mark"] if item.get("full_mark") else ""
        lines.append(
            f"| {item['course_name']} | {item['title']} | {shown} | {full} | {item.get('detail_text') or ''} |"
        )
    return "\n".join(lines)


def _format_evaluations(result: dict) -> str:
    if not result.get("ok"):
        return f"评价查询失败：{result.get('error', '')}"
    items = result.get("items") or []
    if not items:
        return "还没有课程评价。可以说：记录评价 软件工程 李老师 4分 评语：用例讲得清楚。"
    by_course = [
        [item["course_name"], item["teacher"], item["rating"], item["comment"], item["created_at"][:16]]
        for item in items
    ]
    by_teacher = [
        [item["teacher"], item["course_name"], item["rating"], item["comment"]]
        for item in sorted(items, key=lambda row: (row["teacher"], row["course_name"]))
    ]
    return (
        "按课程：\n\n"
        + _md_table(["课程", "教师", "评分", "评语", "时间"], by_course)
        + "\n\n按老师：\n\n"
        + _md_table(["教师", "课程", "评分", "评语"], by_teacher)
    )


def _format_brief(result: dict) -> str:
    if not result.get("ok"):
        return f"暂时读不到课表：{result.get('error', '')}"
    today_info = result["today"]
    tomorrow = result["tomorrow"]
    lines = [
        f"{result.get('semester_name', '')}，今天是 {today_info['date']} {today_info['weekday_name']}，第 {today_info['week']} 教学周。"
    ]
    if today_info["courses"]:
        lines.append("今天有课：\n" + _format_courses({"ok": True, "scope": "today", "courses": today_info["courses"]}))
    else:
        lines.append("今天没有课。")
    if tomorrow["courses"]:
        lines.append("明天：\n" + _format_courses({"ok": True, "scope": "tomorrow", "courses": tomorrow["courses"]}))
    reminders = result.get("reminders") or []
    if reminders:
        lines.append("考试提醒：\n" + _format_exams({"ok": True, "exams": reminders}).replace("近期考试：\n\n", ""))
    return "\n\n".join(lines)

