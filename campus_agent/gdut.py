"""从广东工业大学教务系统读取当前登录学生自己的课表。

日在广工的小程序接口不对外开放。这里走学校教务的个人课表，
和日在广工看的是同一份数据。只接受调用方当场提供的 Cookie，
不保存教务密码，也不提供选课或抢课。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from campus_agent.schedule import compact_weeks, parse_weeks

JXFW = "https://jxfw.gdut.edu.cn"
LOGIN_PAGE = (
    "https://authserver.gdut.edu.cn/authserver/login"
    "?service=https%3A%2F%2Fjxfw.gdut.edu.cn%2Fnew%2FssoLogin"
)
PERIODS = {
    1: ("08:30", "09:15"),
    2: ("09:20", "10:05"),
    3: ("10:25", "11:10"),
    4: ("11:15", "12:00"),
    5: ("14:00", "14:45"),
    6: ("14:50", "15:35"),
    7: ("15:55", "16:40"),
    8: ("16:45", "17:30"),
    9: ("19:00", "19:45"),
    10: ("19:50", "20:35"),
    11: ("20:45", "21:30"),
    12: ("21:35", "22:20"),
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def semester_label(code: str) -> str:
    year = int(code[:4])
    term = "第一学期" if code.endswith("01") else "第二学期" if code.endswith("02") else f"学期{code[4:]}"
    return f"{year}-{year + 1}学年{term}"


def rows_to_courses(rows: list[dict]) -> list[dict]:
    courses = []
    for row in rows:
        name = str(row.get("kcmc") or "").strip()
        if not name:
            continue
        try:
            start_time, end_time = _clock(str(row.get("jcdm2") or row.get("jcdm") or ""))
            weeks = _weeks(str(row.get("zcs") or row.get("zc") or ""))
            weekday = int(str(row.get("xq") or "").strip() or "0")
        except (ValueError, TypeError):
            continue
        if not 1 <= weekday <= 7 or not weeks:
            continue
        teacher = str(row.get("teaxms") or row.get("teaxm") or "待定").strip() or "待定"
        classroom = str(row.get("jxcdmcs") or row.get("jxcdmc") or "待定").strip() or "待定"
        content = " ".join(
            part
            for part in (str(row.get("jxhjmc") or "").strip(), str(row.get("sknr") or "").strip())
            if part
        )
        courses.append(
            {
                "name": name,
                "teacher": teacher,
                "classroom": classroom,
                "weekday": weekday,
                "start_time": start_time,
                "end_time": end_time,
                "weeks": compact_weeks(weeks),
                "content": content,
            }
        )
    return courses


def rows_to_grades(rows: list[dict]) -> list[dict]:
    items = []
    for row in rows:
        name = str(row.get("kcmc") or row.get("courseName") or "").strip()
        if not name:
            continue
        raw_total = row.get("zcj")
        if raw_total in (None, ""):
            raw_total = row.get("cj")
        raw_total = "" if raw_total is None else str(raw_total).strip()
        total, full = _score_pair(raw_total)
        detail: dict = {"_source": "jxfw"}
        if raw_total:
            detail["成绩"] = raw_total
        for key, label in (
            ("pscj", "平时"),
            ("qzcj", "期中"),
            ("qmcj", "期末"),
            ("sycj", "实验"),
        ):
            piece = row.get(key)
            if piece in (None, ""):
                continue
            text = str(piece).strip()
            score, part_full = _score_pair(text)
            detail[label] = {"score": score, "full": part_full} if score is not None else text
        nested = row.get("cjxm") or row.get("xmbl") or []
        if isinstance(nested, list):
            for part in nested:
                if not isinstance(part, dict):
                    continue
                label = str(part.get("xmmc") or part.get("xmblmc") or "").strip()
                value = part.get("xmcj")
                if value in (None, ""):
                    value = part.get("cj")
                if not label or value in (None, ""):
                    continue
                text = str(value).strip()
                score, part_full = _score_pair(text)
                detail[label] = {"score": score, "full": part_full} if score is not None else text
        credit = str(row.get("xf") or "").strip()
        if credit:
            detail["学分"] = credit
        point = str(row.get("cjjd") or row.get("jd") or "").strip()
        if point:
            detail["绩点"] = point
        term = str(row.get("xnxqmc") or row.get("xnxqdm") or "").strip()
        if term:
            detail["学期"] = term
        items.append(
            {
                "course_name": name,
                "title": "总评",
                "total": total if total is not None else 0,
                "full_mark": full if total is not None else 0,
                "detail": detail,
                "exam_at": "",
            }
        )
    return items


def fetch_personal_grades(cookie: str, semester: str = "") -> list[dict]:
    cookie = _clean_cookie(cookie)
    semester = semester.strip()
    rows = _fetch_grade_rows(cookie, "")
    if not rows and semester:
        rows = _fetch_grade_rows(cookie, semester)
    return rows_to_grades(rows)


def fetch_personal_timetable(cookie: str, semester: str) -> dict:
    cookie = _clean_cookie(cookie)
    semester = semester.strip()
    if not re.fullmatch(r"20\d{2}0[12]", semester):
        raise ValueError("学期代码形如 202601，表示 2026-2027 学年第一学期")
    rows = _fetch_rows(cookie, semester)
    courses = rows_to_courses(rows)
    if not courses:
        raise ValueError("这个学期没有解析到课程。请确认学期代码，并重新登录教务后再复制 Cookie")
    start = _fetch_monday(cookie, semester)
    payload = {
        "semester_name": semester_label(semester),
        "courses": courses,
    }
    if start:
        payload["semester_start"] = start
    return payload


def _clean_cookie(cookie: str) -> str:
    text = cookie.strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1]
    text = "".join(text.split())
    if len(text) < 8 or "=" not in text or len(text) > 4096:
        raise ValueError("请粘贴你自己登录教务后的 Cookie，只在这次同步时使用")
    return text


def _fetch_rows(cookie: str, semester: str) -> list[dict]:
    form = urllib.parse.urlencode(
        {"xnxqdm": semester, "zc": "", "page": "1", "rows": "200", "sort": "kxh", "order": "asc"}
    ).encode()
    try:
        body = _open(f"{JXFW}/xsgrkbcx!getDataList.action", cookie, form)
    except ValueError:
        body = _open(f"{JXFW}/xsgrkbcx!xsAllKbList.action?xnxqdm={semester}", cookie, None)
    return _parse_body(body)


def _fetch_monday(cookie: str, semester: str) -> str:
    try:
        body = _open(f"{JXFW}/xsgrkbcx!getKbRq.action?zc=1&xnxqdm={semester}", cookie, None)
        data = json.loads(body)
    except (ValueError, json.JSONDecodeError):
        return ""
    days = data[1] if isinstance(data, list) and len(data) > 1 else data
    if not isinstance(days, list):
        return ""
    for item in days:
        if isinstance(item, dict) and str(item.get("xqmc")) in {"1", "一", "周一", "星期一"}:
            text = str(item.get("rq") or "")[:10]
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
                day = datetime.strptime(text, "%Y-%m-%d").date()
                return (day - timedelta(days=day.weekday())).isoformat()
    return ""


def _fetch_grade_rows(cookie: str, semester: str) -> list[dict]:
    form = urllib.parse.urlencode(
        {"xnxqdm": semester, "page": "1", "rows": "500", "sort": "xnxqdm", "order": "desc"}
    ).encode()
    body = _open(f"{JXFW}/xskccjxx!getDataList.action", cookie, form)
    return _parse_body(body, "成绩")


def _score_pair(text: str) -> tuple[float | None, float | None]:
    matched = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(?:\s*/\s*([0-9]+(?:\.[0-9]+)?))?", text.strip())
    if not matched:
        return None, None
    score = float(matched.group(1))
    full = float(matched.group(2)) if matched.group(2) else (100.0 if score <= 100 else score)
    return score, full


def _parse_body(body: str, label: str = "课表") -> list[dict]:
    match = re.search(r"var\s+kbxx\s*=\s*(\[.*?\]);", body, re.S)
    raw = match.group(1) if match else body
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        if "登录" in body or "login" in body.lower():
            raise ValueError("教务会话已失效。请重新登录后再复制 Cookie") from exc
        raise ValueError(f"教务没有返回{label}数据") from exc
    if isinstance(data, dict):
        rows = data.get("rows") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    if not isinstance(rows, list):
        raise ValueError("教务返回的课表格式无法识别")
    return [row for row in rows if isinstance(row, dict)]


def _open(url: str, cookie: str, data: bytes | None) -> str:
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0",
            "Referer": f"{JXFW}/xsgrkbcx!getXsgrbkList.action",
            "Accept": "application/json,text/javascript,*/*",
        },
    )
    if data is not None:
        request.add_header("Content-Type", "application/x-www-form-urlencoded")
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=25) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308}:
            raise ValueError("教务会话已失效。请重新登录后再复制 Cookie") from exc
        raise ValueError(f"教务系统返回了 {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError("连不上教务系统，请检查校园网或 VPN") from exc


def _clock(section: str) -> tuple[str, str]:
    numbers = [int(piece) for piece in re.findall(r"\d{2}", section)] or [
        int(piece) for piece in re.findall(r"\d+", section)
    ]
    if not numbers or any(number not in PERIODS for number in numbers):
        raise ValueError(f"无法识别节次：{section}")
    return PERIODS[min(numbers)][0], PERIODS[max(numbers)][1]


def _weeks(value: str) -> list[int]:
    text = value.strip()
    if re.fullmatch(r"[01]{8,}", text):
        return [index for index, flag in enumerate(text, start=1) if flag == "1"]
    return parse_weeks(text or "1-16")

