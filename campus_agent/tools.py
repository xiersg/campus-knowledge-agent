"""课表、考试和复盘工具。网页和 MCP 都调用这一层。"""

from __future__ import annotations

import inspect
import json
import re
import urllib.error
from datetime import date, datetime, timedelta
from typing import Any

from campus_agent.bilibili import search_bilibili, search_url
from campus_agent.config import SAMPLE_PATH, now_local, today, weekday_name
from campus_agent.db import connect, get_meta, get_user_meta, rows_as_dicts, set_meta, set_user_meta
from campus_agent.identity import current_user_id
from campus_agent.schedule import (
    compact_weeks,
    parse_exam_at,
    parse_import,
    parse_semester_start,
    semester_week,
    teaching_week_dates,
)

TOOL_SPECS = [
    {
        "name": "campus_brief",
        "description": "查看当前学期、今天和明天的课，以及需要提醒的考试。没有参数。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_courses",
        "description": "按今天、明天、教学周或全部范围列出课程，可按课程名、教师或课室筛选。week_offset 用于翻周，0 是本周。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["today", "tomorrow", "week", "all"],
                    "description": "today 今天，tomorrow 明天，week 教学周，all 全部已导入课程",
                },
                "keyword": {"type": "string", "description": "可选，匹配课程、教师、课室或教学内容"},
                "week_offset": {"type": "integer", "description": "相对本周的偏移，0 本周，1 下一周，-1 上一周"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "course_detail",
        "description": "查询某门课的教师、课室、上课时间和教学内容，并附上最近一次课堂复盘。",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "课程名，可以是一部分"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "upcoming_exams",
        "description": "列出即将到来的考试和答辩，标出是否已进入提醒窗口。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "within_days": {"type": "integer", "description": "只看未来多少天内，默认 30"}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "add_exam",
        "description": "新增一场考试或答辩提醒。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "course_name": {"type": "string"},
                "exam_at": {"type": "string", "description": "YYYY-MM-DD HH:MM"},
                "location": {"type": "string"},
                "note": {"type": "string"},
                "remind_days": {"type": "integer", "description": "提前几天开始提醒，默认 7"},
            },
            "required": ["course_name", "exam_at", "location"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_reviews",
        "description": "查看课堂教学复盘。可按课程名或关键词筛选，默认返回最近的记录。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "course_name": {"type": "string"},
                "keyword": {"type": "string", "description": "匹配主题、要点或留下的问题"},
                "limit": {"type": "integer"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "add_review",
        "description": "把某一次课的主题、要点和未解决问题记成教学复盘。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "course_name": {"type": "string"},
                "session_date": {"type": "string", "description": "YYYY-MM-DD，不填则用今天"},
                "topic": {"type": "string"},
                "key_points": {"type": "string"},
                "questions": {"type": "string", "description": "课后还没解决的问题，可空"},
            },
            "required": ["course_name", "topic", "key_points"],
            "additionalProperties": False,
        },
    },
    {
        "name": "import_schedule",
        "description": "导入课表。content 可以是 CSV 文本，或包含 courses/exams/reviews 的 JSON。replace 为 true 时覆盖已有课程。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "replace": {"type": "boolean"},
            },
            "required": ["content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "load_sample_schedule",
        "description": "载入项目自带的示例课表、考试和课堂复盘，会覆盖当前课程数据。没有参数。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "search_knowledge",
        "description": "从课表、考试、复盘、课程评价和用户记忆的检索索引里找与问题最相关的片段。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "要检索的问题或关键词"},
                "limit": {"type": "integer", "description": "返回条数，默认 4"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_evaluation",
        "description": "添加一条课程评价，按课程和教师保存。评分为 1 到 5。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "course_name": {"type": "string"},
                "teacher": {"type": "string"},
                "rating": {"type": "integer", "description": "1 到 5 分"},
                "comment": {"type": "string", "description": "评语，可空"},
            },
            "required": ["course_name", "teacher", "rating"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_evaluations",
        "description": "查看课程评价，结果按课程和按教师分别归类，可按关键词筛选。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "匹配课程、教师或评语"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_grades",
        "description": "查看已记录的考试成绩和分项分数，可按课程筛选。",
        "inputSchema": {
            "type": "object",
            "properties": {"keyword": {"type": "string"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "analyze_grades",
        "description": "根据已导入的教务成绩做分析，标出偏低和较好的课程。没有参数。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "recommend_courses",
        "description": "按主题推荐课程，优先搜索 B 站视频，返回标题、UP 主和链接。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "课程或知识点，例如 操作系统"},
            },
            "required": ["topic"],
            "additionalProperties": False,
        },
    },
]


def _course_public(row: dict, day: date | None = None) -> dict:
    weeks = json.loads(row["weeks"])
    item = {
        "name": row["name"],
        "teacher": row["teacher"],
        "classroom": row["classroom"],
        "weekday": row["weekday"],
        "weekday_name": weekday_name(row["weekday"]),
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "weeks": compact_weeks(weeks),
        "content": row["content"],
    }
    if day is not None:
        item["date"] = day.isoformat()
    return item


def _touch_index() -> None:
    from campus_agent.rag import reindex_current

    try:
        reindex_current()
    except Exception:
        return


def search_knowledge(query: str, limit: int = 4) -> dict:
    from campus_agent.rag import search

    query = query.strip()
    if not query:
        raise ValueError("检索需要一段问题")
    return {"ok": True, "hits": search(query, limit)}


def _uid() -> int:
    return int(current_user_id.get())


def _semester_start(conn) -> date:
    raw = get_user_meta(conn, _uid(), "semester_start") or get_meta(conn, "semester_start")
    return parse_semester_start(raw)


def _load_courses(conn) -> list[dict]:
    return rows_as_dicts(
        conn.execute(
            "SELECT * FROM courses WHERE user_id = ? ORDER BY weekday, start_time, name",
            (_uid(),),
        ).fetchall()
    )


def _courses_on(conn, day: date, keyword: str = "") -> list[dict]:
    week = semester_week(day, _semester_start(conn))
    found = []
    for row in _load_courses(conn):
        weeks = json.loads(row["weeks"])
        if row["weekday"] != day.weekday() + 1 or week not in weeks:
            continue
        if keyword and not _hit(row, keyword):
            continue
        found.append(_course_public(row, day))
    return found


def _hit(row: dict, keyword: str) -> bool:
    blob = " ".join([row["name"], row["teacher"], row["classroom"], row["content"]])
    return keyword.strip().lower() in blob.lower()


def _exam_public(row: dict, moment: datetime) -> dict:
    exam_at = datetime.strptime(row["exam_at"], "%Y-%m-%d %H:%M")
    exam_at = exam_at.replace(tzinfo=moment.tzinfo)
    days_left = (exam_at.date() - moment.date()).days
    return {
        "course_name": row["course_name"],
        "exam_at": row["exam_at"],
        "location": row["location"],
        "note": row["note"],
        "remind_days": row["remind_days"],
        "days_left": days_left,
        "should_remind": 0 <= days_left <= row["remind_days"],
    }


def campus_brief() -> dict:
    conn = connect()
    try:
        moment = now_local()
        day = moment.date()
        exams = [
            _exam_public(row, moment)
            for row in rows_as_dicts(
                conn.execute("SELECT * FROM exams WHERE user_id = ? ORDER BY exam_at", (_uid(),)).fetchall()
            )
        ]
        upcoming = [item for item in exams if 0 <= item["days_left"] <= 45]
        catalog = []
        seen = set()
        for row in rows_as_dicts(conn.execute("SELECT name, teacher FROM courses WHERE user_id = ?", (_uid(),)).fetchall()):
            key = (row["name"], row["teacher"])
            if key in seen:
                continue
            seen.add(key)
            catalog.append({"name": row["name"], "teacher": row["teacher"]})
        return {
            "ok": True,
            "semester_name": get_user_meta(conn, _uid(), "semester_name") or get_meta(conn, "semester_name"),
            "semester_start": get_user_meta(conn, _uid(), "semester_start") or get_meta(conn, "semester_start"),
            "today": {
                "date": day.isoformat(),
                "week": semester_week(day, _semester_start(conn)),
                "weekday_name": weekday_name(day.weekday() + 1),
                "courses": _courses_on(conn, day),
            },
            "tomorrow": {
                "date": (day + timedelta(days=1)).isoformat(),
                "courses": _courses_on(conn, day + timedelta(days=1)),
            },
            "reminders": [item for item in upcoming if item["should_remind"]],
            "upcoming_exams": upcoming,
            "catalog": catalog,
        }
    finally:
        conn.close()


def list_courses(scope: str = "today", keyword: str = "", week_offset: int = 0) -> dict:
    allowed = {"today", "tomorrow", "week", "all"}
    if scope not in allowed:
        raise ValueError("scope 只能是 today、tomorrow、week 或 all")
    try:
        offset = int(week_offset or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("周次偏移无效") from exc
    if offset < -20 or offset > 20:
        raise ValueError("翻页超出范围")
    conn = connect()
    try:
        day = today()
        start = _semester_start(conn)
        if scope == "all":
            courses = []
            for row in _load_courses(conn):
                if keyword and not _hit(row, keyword):
                    continue
                courses.append(_course_public(row))
            return {"ok": True, "scope": scope, "keyword": keyword, "courses": courses}
        anchor = day + timedelta(days=offset * 7) if scope == "week" else day
        if scope == "today":
            dates = [day]
        elif scope == "tomorrow":
            dates = [day + timedelta(days=1)]
        else:
            dates = teaching_week_dates(anchor, start)
        days = [
            {
                "date": current.isoformat(),
                "weekday_name": weekday_name(current.weekday() + 1),
                "courses": _courses_on(conn, current, keyword),
            }
            for current in dates
        ]
        courses = [item for block in days for item in block["courses"]]
        payload = {
            "ok": True,
            "scope": scope,
            "keyword": keyword,
            "week": semester_week(anchor if scope == "week" else day, start),
            "courses": courses,
        }
        if scope == "week":
            payload.update(
                {
                    "week_offset": offset,
                    "week_start": dates[0].isoformat() if dates else "",
                    "week_end": dates[-1].isoformat() if dates else "",
                    "today": day.isoformat(),
                    "days": days,
                }
            )
        return payload
    finally:
        conn.close()


def course_detail(name: str) -> dict:
    keyword = name.strip()
    if not keyword:
        raise ValueError("请提供课程名")
    conn = connect()
    try:
        matched = [row for row in _load_courses(conn) if keyword.lower() in row["name"].lower()]
        exact = [row for row in matched if row["name"].lower() == keyword.lower()]
        if exact:
            matched = exact
        matched.sort(key=lambda row: (row["name"] != keyword, row["weekday"], row["start_time"]))
        reviews = rows_as_dicts(
            conn.execute(
                "SELECT * FROM reviews WHERE user_id = ? AND course_name LIKE ? ORDER BY session_date DESC, id DESC",
                (_uid(), f"%{keyword}%"),
            ).fetchall()
        )
        grouped: dict[str, dict] = {}
        for row in matched:
            item = grouped.setdefault(
                row["name"],
                {"name": row["name"], "content": row["content"], "sessions": [], "latest_review": None},
            )
            item["sessions"].append(_course_public(row))
            if not item["content"] and row["content"]:
                item["content"] = row["content"]
        for item in grouped.values():
            related = [review for review in reviews if review["course_name"] == item["name"]]
            if related:
                latest = related[0]
                item["latest_review"] = {
                    "session_date": latest["session_date"],
                    "topic": latest["topic"],
                    "key_points": latest["key_points"],
                    "questions": latest["questions"],
                }
        return {"ok": True, "keyword": keyword, "matches": list(grouped.values())}
    finally:
        conn.close()


def upcoming_exams(within_days: int = 30) -> dict:
    if within_days < 0 or within_days > 365:
        raise ValueError("within_days 应在 0 到 365 之间")
    conn = connect()
    try:
        moment = now_local()
        exams = []
        for row in rows_as_dicts(
            conn.execute("SELECT * FROM exams WHERE user_id = ? ORDER BY exam_at", (_uid(),)).fetchall()
        ):
            item = _exam_public(row, moment)
            if 0 <= item["days_left"] <= within_days:
                exams.append(item)
        return {"ok": True, "as_of": moment.strftime("%Y-%m-%d %H:%M"), "exams": exams}
    finally:
        conn.close()


def add_exam(course_name: str, exam_at: str, location: str, note: str = "", remind_days: int = 7) -> dict:
    course_name = course_name.strip()
    location = location.strip()
    if not course_name or not location:
        raise ValueError("考试需要课程名和地点")
    if not 0 <= int(remind_days) <= 60:
        raise ValueError("提醒天数应在 0 到 60 之间")
    when = parse_exam_at(exam_at)
    conn = connect()
    try:
        cursor = conn.execute(
            "INSERT INTO exams(user_id, course_name, exam_at, location, note, remind_days) VALUES(?, ?, ?, ?, ?, ?)",
            (_uid(), course_name, when, location, note.strip(), int(remind_days)),
        )
        row_id = int(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()
    _touch_index()
    return {"ok": True, "id": row_id, "course_name": course_name, "exam_at": when, "location": location}


def list_reviews(course_name: str = "", keyword: str = "", limit: int = 5) -> dict:
    limit = max(1, min(int(limit), 20))
    conn = connect()
    try:
        rows = rows_as_dicts(
            conn.execute(
                "SELECT * FROM reviews WHERE user_id = ? ORDER BY session_date DESC, id DESC",
                (_uid(),),
            ).fetchall()
        )
        if course_name.strip():
            needle = course_name.strip().lower()
            exact = [row for row in rows if row["course_name"].lower() == needle]
            rows = exact or [row for row in rows if needle in row["course_name"].lower()]
        if keyword.strip():
            needle = keyword.strip().lower()
            rows = [
                row
                for row in rows
                if needle in " ".join([row["topic"], row["key_points"], row["questions"]]).lower()
            ]
        trimmed = []
        for row in rows[:limit]:
            trimmed.append(
                {
                    "course_name": row["course_name"],
                    "session_date": row["session_date"],
                    "topic": row["topic"],
                    "key_points": row["key_points"],
                    "questions": row["questions"],
                }
            )
        return {"ok": True, "reviews": trimmed}
    finally:
        conn.close()


def add_review(
    course_name: str,
    topic: str,
    key_points: str,
    session_date: str = "",
    questions: str = "",
) -> dict:
    course_name = course_name.strip()
    topic = topic.strip()
    key_points = key_points.strip()
    if not course_name or not topic or not key_points:
        raise ValueError("复盘需要课程、主题和要点")
    if session_date.strip():
        try:
            datetime.strptime(session_date.strip(), "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("上课日期应为 YYYY-MM-DD") from exc
        when = session_date.strip()
    else:
        when = today().isoformat()
    conn = connect()
    try:
        cursor = conn.execute(
            """
            INSERT INTO reviews(user_id, course_name, session_date, topic, key_points, questions, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (_uid(), course_name, when, topic, key_points, questions.strip(), now_local().isoformat(timespec="minutes")),
        )
        row_id = int(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()
    _touch_index()
    return {"ok": True, "id": row_id, "course_name": course_name, "session_date": when, "topic": topic}


def import_schedule(content: str, replace: bool = True) -> dict:
    parsed = parse_import(content)
    conn = connect()
    try:
        user_id = _uid()
        if parsed["semester_start"]:
            set_user_meta(conn, user_id, "semester_start", parse_semester_start(parsed["semester_start"]).isoformat())
        if parsed["semester_name"]:
            set_user_meta(conn, user_id, "semester_name", parsed["semester_name"])
        if replace and parsed["courses"]:
            conn.execute("DELETE FROM courses WHERE user_id = ?", (user_id,))
        for course in parsed["courses"]:
            conn.execute(
                """
                INSERT INTO courses(user_id, name, teacher, classroom, weekday, start_time, end_time, weeks, content)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    course["name"],
                    course["teacher"],
                    course["classroom"],
                    course["weekday"],
                    course["start_time"],
                    course["end_time"],
                    json.dumps(course["weeks"]),
                    course["content"],
                ),
            )
        if parsed["exams"] and replace:
            conn.execute("DELETE FROM exams WHERE user_id = ?", (user_id,))
        for exam in parsed["exams"]:
            conn.execute(
                "INSERT INTO exams(user_id, course_name, exam_at, location, note, remind_days) VALUES(?, ?, ?, ?, ?, ?)",
                (user_id, exam["course_name"], exam["exam_at"], exam["location"], exam["note"], exam["remind_days"]),
            )
        if parsed["reviews"] and replace:
            conn.execute("DELETE FROM reviews WHERE user_id = ?", (user_id,))
        created = now_local().isoformat(timespec="minutes")
        for review in parsed["reviews"]:
            conn.execute(
                """
                INSERT INTO reviews(user_id, course_name, session_date, topic, key_points, questions, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    review["course_name"],
                    review["session_date"],
                    review["topic"],
                    review["key_points"],
                    review["questions"],
                    created,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    _touch_index()
    return {
        "ok": True,
        "courses": len(parsed["courses"]),
        "exams": len(parsed["exams"]),
        "reviews": len(parsed["reviews"]),
        "replaced": bool(replace),
    }


def add_evaluation(course_name: str, teacher: str, rating: int = 5, comment: str = "") -> dict:
    course_name = course_name.strip()
    teacher = teacher.strip()
    comment = comment.strip()
    if not course_name or not teacher:
        raise ValueError("评价需要课程名和教师")
    score = int(rating)
    if score < 1 or score > 5:
        raise ValueError("评分要在 1 到 5 之间")
    if len(comment) > 500:
        raise ValueError("评语太长")
    conn = connect()
    try:
        cursor = conn.execute(
            """
            INSERT INTO evaluations(user_id, course_name, teacher, rating, comment, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (_uid(), course_name, teacher, score, comment, now_local().isoformat(timespec="seconds")),
        )
        row_id = int(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()
    _touch_index()
    return {"ok": True, "id": row_id, "course_name": course_name, "teacher": teacher, "rating": score, "comment": comment}


def list_evaluations(keyword: str = "") -> dict:
    conn = connect()
    try:
        rows = rows_as_dicts(
            conn.execute(
                "SELECT * FROM evaluations WHERE user_id = ? ORDER BY id DESC",
                (_uid(),),
            ).fetchall()
        )
    finally:
        conn.close()
    needle = keyword.strip().lower()
    if needle:
        rows = [
            row
            for row in rows
            if needle in " ".join([row["course_name"], row["teacher"], row["comment"]]).lower()
        ]
    items = [
        {
            "id": row["id"],
            "course_name": row["course_name"],
            "teacher": row["teacher"],
            "rating": row["rating"],
            "comment": row["comment"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]
    by_course: dict[str, list[dict]] = {}
    by_teacher: dict[str, list[dict]] = {}
    for item in items:
        by_course.setdefault(item["course_name"], []).append(item)
        by_teacher.setdefault(item["teacher"], []).append(item)
    return {"ok": True, "items": items, "by_course": by_course, "by_teacher": by_teacher}


def _parse_detail(detail: Any) -> dict:
    if isinstance(detail, dict):
        return detail
    text = str(detail or "").strip()
    if not text:
        return {}
    if text.startswith("{"):
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("分项分数需要是键值")
        return data
    parts: dict[str, dict] = {}
    for piece in re.split(r"[，,;；]", text):
        matched = re.match(r"\s*(.+?)[：:]\s*([0-9]+(?:\.[0-9]+)?)(?:\s*/\s*([0-9]+(?:\.[0-9]+)?))?\s*$", piece)
        if not matched:
            continue
        parts[matched.group(1).strip()] = {
            "score": float(matched.group(2)),
            "full": float(matched.group(3)) if matched.group(3) else None,
        }
    return parts


def _detail_text(detail: dict) -> str:
    bits = []
    for name, value in detail.items():
        if str(name).startswith("_"):
            continue
        if isinstance(value, dict):
            score = value.get("score")
            full = value.get("full")
            bits.append(f"{name}:{score}" + (f"/{full}" if full else ""))
        else:
            bits.append(f"{name}:{value}")
    return "，".join(bits)


def import_school_grades(items: list[dict]) -> dict:
    uid = _uid()
    conn = connect()
    try:
        existing = rows_as_dicts(conn.execute("SELECT id, detail FROM grades WHERE user_id = ?", (uid,)).fetchall())
        drop = []
        for row in existing:
            try:
                parsed = json.loads(row["detail"] or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and parsed.get("_source") == "jxfw":
                drop.append(row["id"])
        if drop:
            conn.execute(
                f"DELETE FROM grades WHERE user_id = ? AND id IN ({','.join('?' for _ in drop)})",
                [uid, *drop],
            )
        stamp = now_local().isoformat(timespec="seconds")
        for item in items:
            conn.execute(
                """
                INSERT INTO grades (user_id, course_name, title, total, full_mark, detail, exam_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    uid,
                    item["course_name"],
                    item.get("title") or "总评",
                    float(item.get("total") or 0),
                    float(item.get("full_mark") or 0),
                    json.dumps(item.get("detail") or {}, ensure_ascii=False),
                    str(item.get("exam_at") or ""),
                    stamp,
                ),
            )
        conn.commit()
    finally:
        conn.close()
    _touch_index()
    return {"ok": True, "grades": len(items)}


def add_grade(
    course_name: str,
    title: str,
    total: float,
    full_mark: float = 100,
    detail: Any = "",
    exam_at: str = "",
) -> dict:
    name = course_name.strip()
    label = title.strip()
    if not name or not label:
        raise ValueError("课程和成绩名称不能为空")
    score = float(total)
    full = float(full_mark or 100)
    if full <= 0 or score < 0 or score > full:
        raise ValueError("总分需要在 0 和满分之间")
    parts = _parse_detail(detail)
    when = exam_at.strip()
    if when:
        datetime.strptime(when, "%Y-%m-%d")
    uid = _uid()
    conn = connect()
    try:
        cursor = conn.execute(
            """
            INSERT INTO grades (user_id, course_name, title, total, full_mark, detail, exam_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (uid, name, label, score, full, json.dumps(parts, ensure_ascii=False), when, now_local().isoformat(timespec="seconds")),
        )
        row_id = int(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()
    _touch_index()
    return {"ok": True, "id": row_id, "course_name": name, "title": label, "total": score, "full_mark": full, "detail": parts}


def list_grades(keyword: str = "") -> dict:
    uid = _uid()
    conn = connect()
    try:
        rows = rows_as_dicts(conn.execute("SELECT * FROM grades WHERE user_id = ? ORDER BY id DESC", (uid,)).fetchall())
    finally:
        conn.close()
    needle = keyword.strip().lower()
    items = []
    for row in rows:
        parts = json.loads(row["detail"] or "{}")
        text = " ".join([row["course_name"], row["title"], _detail_text(parts)]).lower()
        if needle and needle not in text:
            continue
        items.append(
            {
                "id": row["id"],
                "course_name": row["course_name"],
                "title": row["title"],
                "total": row["total"],
                "full_mark": row["full_mark"],
                "detail": parts,
                "detail_text": _detail_text(parts),
                "exam_at": row["exam_at"],
                "created_at": row["created_at"],
            }
        )
    return {"ok": True, "items": items}


def analyze_grades() -> dict:
    items = list_grades().get("items") or []
    scored = []
    for item in items:
        full = float(item.get("full_mark") or 0)
        if full <= 0:
            continue
        percent = round(float(item["total"]) / full * 100, 1)
        scored.append(
            {
                "course_name": item["course_name"],
                "title": item["title"],
                "total": item["total"],
                "full_mark": full,
                "percent": percent,
                "detail_text": item.get("detail_text") or "",
            }
        )
    if not scored:
        return {
            "ok": True,
            "count": 0,
            "average": None,
            "weak": [],
            "strong": [],
            "summary": "还没有可分析的百分制成绩。请先登录教务并导入刷新。",
        }
    average = round(sum(item["percent"] for item in scored) / len(scored), 1)
    weak = sorted((item for item in scored if item["percent"] < 75), key=lambda item: item["percent"])
    strong = sorted((item for item in scored if item["percent"] >= 85), key=lambda item: item["percent"], reverse=True)
    if any(item["percent"] < 60 for item in weak):
        summary = f"已分析 {len(scored)} 门百分制成绩，平均 {average} 分。有课程低于 60，先补这几门。"
    elif weak:
        summary = f"已分析 {len(scored)} 门百分制成绩，平均 {average} 分。75 分以下的课建议先复习。"
    else:
        summary = f"已分析 {len(scored)} 门百分制成绩，平均 {average} 分。没有低于 75 分的课。"
    return {"ok": True, "count": len(scored), "average": average, "weak": weak, "strong": strong, "summary": summary}


def recommend_courses(topic: str, limit: int = 5) -> dict:
    text = topic.strip() or "大学课程"
    size = max(1, min(int(limit or 5), 8))
    try:
        videos = search_bilibili(text, size)
        error = ""
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        videos = []
        error = str(exc)
    return {"ok": True, "topic": text, "videos": videos, "search_url": search_url(text), "error": error}


def load_sample_schedule() -> dict:
    if not SAMPLE_PATH.exists():
        raise ValueError("找不到示例课表 samples/schedule.json")
    result = import_schedule(SAMPLE_PATH.read_text(encoding="utf-8"), replace=True)
    result["source"] = "samples/schedule.json"
    return result


def seed_if_empty() -> None:
    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM courses WHERE user_id = 1").fetchone()["n"]
    finally:
        conn.close()
    if count == 0 and SAMPLE_PATH.exists():
        load_sample_schedule()


HANDLERS = {
    "campus_brief": campus_brief,
    "list_courses": list_courses,
    "course_detail": course_detail,
    "upcoming_exams": upcoming_exams,
    "add_exam": add_exam,
    "list_reviews": list_reviews,
    "add_review": add_review,
    "import_schedule": import_schedule,
    "load_sample_schedule": load_sample_schedule,
    "search_knowledge": search_knowledge,
    "add_evaluation": add_evaluation,
    "list_evaluations": list_evaluations,
    "add_grade": add_grade,
    "list_grades": list_grades,
    "analyze_grades": analyze_grades,
    "recommend_courses": recommend_courses,
}


def dispatch(name: str, arguments: dict | None) -> dict:
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return {"ok": False, "error": "参数必须是对象"}
    handler = HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "error": f"没有这个工具：{name}"}
    accepted = {key: value for key, value in arguments.items() if key in inspect.signature(handler).parameters}
    try:
        return handler(**accepted)
    except (TypeError, ValueError, json.JSONDecodeError, KeyError) as exc:
        return {"ok": False, "error": str(exc)}
