"""课表字段解析：星期、周次、时间和导入文本。"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta

WEEKDAY_TEXT = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
    "天": 7,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
}

HEADER_ALIASES = {
    "name": ["name", "课程", "课程名", "课程名称"],
    "teacher": ["teacher", "教师", "老师", "任课教师"],
    "classroom": ["classroom", "课室", "教室", "上课地点", "地点"],
    "weekday": ["weekday", "星期", "周几", "上课日"],
    "start_time": ["start_time", "开始", "开始时间", "上课时间"],
    "end_time": ["end_time", "结束", "结束时间", "下课时间"],
    "weeks": ["weeks", "周次", "上课周"],
    "content": ["content", "教学内容", "内容", "简介"],
}


def parse_weekday(value: object) -> int:
    text = str(value).strip().replace("星期", "").replace("周", "")
    if text.isdigit():
        number = int(text)
        if 1 <= number <= 7:
            return number
    if text[:1] in WEEKDAY_TEXT:
        return WEEKDAY_TEXT[text[:1]]
    raise ValueError(f"无法识别星期：{value}")


def parse_weeks(value: object) -> list[int]:
    text = str(value or "1-16").strip()
    if not text:
        text = "1-16"
    odd = "单" in text
    even = "双" in text
    cleaned = text.replace("周", "").replace("单", "").replace("双", "").replace("，", ",")
    numbers: list[int] = []
    for part in cleaned.split(","):
        piece = part.strip()
        if not piece:
            continue
        if "-" in piece:
            left, right = piece.split("-", 1)
            start, end = int(left), int(right)
            if end < start or start < 1 or end > 30:
                raise ValueError(f"周次范围不合法：{piece}")
            numbers.extend(range(start, end + 1))
        else:
            number = int(piece)
            if not 1 <= number <= 30:
                raise ValueError(f"周次不合法：{piece}")
            numbers.append(number)
    if odd:
        numbers = [n for n in numbers if n % 2 == 1]
    if even:
        numbers = [n for n in numbers if n % 2 == 0]
    if not numbers:
        raise ValueError(f"周次为空：{value}")
    return sorted(set(numbers))


def compact_weeks(weeks: list[int]) -> str:
    if not weeks:
        return ""
    ordered = sorted(set(weeks))
    ranges: list[str] = []
    start = previous = ordered[0]
    for number in ordered[1:]:
        if number == previous + 1:
            previous = number
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def parse_clock(value: str) -> str:
    text = value.strip().replace("：", ":")
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if not match:
        raise ValueError(f"时间格式应为 HH:MM：{value}")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"时间不合法：{value}")
    return f"{hour:02d}:{minute:02d}"


def split_time_range(start_value: str, end_value: str) -> tuple[str, str]:
    start_text = str(start_value or "").strip()
    end_text = str(end_value or "").strip()
    if start_text and not end_text and re.search(r"[-~到至]", start_text):
        left, right = re.split(r"[-~到至]", start_text, maxsplit=1)
        return parse_clock(left), parse_clock(right)
    return parse_clock(start_text), parse_clock(end_text)


def parse_exam_at(value: str) -> str:
    text = value.strip().replace("T", " ").replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%Y-%m-%d":
                parsed = parsed.replace(hour=9, minute=0)
            return parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue
    raise ValueError(f"考试时间无法识别：{value}")


def semester_week(day: date, semester_start: date) -> int:
    if day < semester_start:
        return 0
    return (day - semester_start).days // 7 + 1


def teaching_week_dates(day: date, semester_start: date) -> list[date]:
    week = semester_week(day, semester_start)
    if week <= 0:
        return []
    monday = semester_start + timedelta(days=(week - 1) * 7)
    return [monday + timedelta(days=offset) for offset in range(7)]


def parse_semester_start(value: str) -> date:
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("学期开始日期应为 YYYY-MM-DD，且必须是周一") from exc
    if parsed.weekday() != 0:
        raise ValueError("学期开始日期必须是周一")
    return parsed


def _pick(record: dict, field: str, default: str = "") -> str:
    aliases = HEADER_ALIASES[field]
    lowered = {str(key).strip().lower(): value for key, value in record.items()}
    for alias in aliases:
        if alias.lower() in lowered and str(lowered[alias.lower()]).strip():
            return str(lowered[alias.lower()]).strip()
    return default


def normalize_course(record: dict) -> dict:
    name = _pick(record, "name")
    teacher = _pick(record, "teacher")
    classroom = _pick(record, "classroom")
    if not name or not teacher or not classroom:
        raise ValueError("每门课都需要课程、教师和课室")
    start_time, end_time = split_time_range(_pick(record, "start_time"), _pick(record, "end_time"))
    weeks = parse_weeks(_pick(record, "weeks", "1-16"))
    return {
        "name": name,
        "teacher": teacher,
        "classroom": classroom,
        "weekday": parse_weekday(_pick(record, "weekday")),
        "start_time": start_time,
        "end_time": end_time,
        "weeks": weeks,
        "content": _pick(record, "content"),
    }


def _read_csv(text: str) -> list[dict]:
    sample = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(sample))
    if not reader.fieldnames:
        raise ValueError("CSV 缺少表头")
    return [dict(row) for row in reader if any(str(value or "").strip() for value in row.values())]


def parse_import(content: str) -> dict:
    text = content.strip()
    if not text:
        raise ValueError("导入内容是空的")
    exams: list[dict] = []
    reviews: list[dict] = []
    semester_start = ""
    semester_name = ""
    if text[0] in "{[":
        payload = json.loads(text)
        if isinstance(payload, list):
            courses = [normalize_course(item) for item in payload]
        elif isinstance(payload, dict):
            semester_start = str(payload.get("semester_start") or "")
            semester_name = str(payload.get("semester_name") or "")
            courses = [normalize_course(item) for item in payload.get("courses", [])]
            for item in payload.get("exams", []):
                exams.append(
                    {
                        "course_name": str(item["course_name"]).strip(),
                        "exam_at": parse_exam_at(str(item["exam_at"])),
                        "location": str(item.get("location") or "待定").strip(),
                        "note": str(item.get("note") or "").strip(),
                        "remind_days": int(item.get("remind_days") or 7),
                    }
                )
            for item in payload.get("reviews", []):
                reviews.append(
                    {
                        "course_name": str(item["course_name"]).strip(),
                        "session_date": str(item["session_date"]).strip(),
                        "topic": str(item["topic"]).strip(),
                        "key_points": str(item["key_points"]).strip(),
                        "questions": str(item.get("questions") or "").strip(),
                    }
                )
        else:
            raise ValueError("JSON 应该是课程数组，或包含 courses 的对象")
    else:
        courses = [normalize_course(row) for row in _read_csv(text)]
    if not courses and not exams and not reviews:
        raise ValueError("没有解析到课程、考试或复盘")
    return {
        "courses": courses,
        "exams": exams,
        "reviews": reviews,
        "semester_start": semester_start,
        "semester_name": semester_name,
    }
