"""课表、考试、复盘和记忆的检索索引。用词元重叠打分，不依赖额外安装包。"""

from __future__ import annotations

import math
import re

from campus_agent.config import now_local, weekday_name
from campus_agent.db import connect, rows_as_dicts
from campus_agent.identity import current_user_id

_HAN = re.compile(r"[\u4e00-\u9fff]")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    han: list[str] = []
    latin: list[str] = []

    def flush_han() -> None:
        if not han:
            return
        tokens.extend(han)
        tokens.extend(han[index] + han[index + 1] for index in range(len(han) - 1))
        han.clear()

    def flush_latin() -> None:
        if latin:
            tokens.append("".join(latin))
            latin.clear()

    for char in text.lower():
        if _HAN.fullmatch(char):
            flush_latin()
            han.append(char)
        elif char.isalnum():
            flush_han()
            latin.append(char)
        else:
            flush_han()
            flush_latin()
    flush_han()
    flush_latin()
    return tokens


def reindex_current() -> int:
    return reindex_user(int(current_user_id.get()))


def reindex_user(user_id: int) -> int:
    conn = connect()
    try:
        courses = rows_as_dicts(conn.execute("SELECT * FROM courses WHERE user_id = ?", (user_id,)).fetchall())
        exams = rows_as_dicts(conn.execute("SELECT * FROM exams WHERE user_id = ?", (user_id,)).fetchall())
        reviews = rows_as_dicts(conn.execute("SELECT * FROM reviews WHERE user_id = ?", (user_id,)).fetchall())
        grades = rows_as_dicts(conn.execute("SELECT * FROM grades WHERE user_id = ? ORDER BY id", (user_id,)).fetchall())
        evaluations = rows_as_dicts(
            conn.execute("SELECT * FROM evaluations WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
        )
        memories = rows_as_dicts(
            conn.execute("SELECT * FROM memories WHERE user_id = ? ORDER BY id", (user_id,)).fetchall()
        )
        chunks: list[tuple[str, str, str]] = []
        for course in courses:
            chunks.append(
                (
                    "course",
                    course["name"],
                    " ".join(
                        [
                            "课程",
                            course["name"],
                            "教师",
                            course["teacher"],
                            "课室",
                            course["classroom"],
                            weekday_name(int(course["weekday"])),
                            f"{course['start_time']}-{course['end_time']}",
                            "周次",
                            course["weeks"],
                            "教学内容",
                            course["content"],
                        ]
                    ),
                )
            )
        for exam in exams:
            chunks.append(
                (
                    "exam",
                    exam["course_name"],
                    f"考试 {exam['course_name']} 时间 {exam['exam_at']} 地点 {exam['location']} 备注 {exam['note']}",
                )
            )
        for review in reviews:
            chunks.append(
                (
                    "review",
                    review["course_name"],
                    " ".join(
                        [
                            "复盘",
                            review["course_name"],
                            review["session_date"],
                            review["topic"],
                            review["key_points"],
                            review["questions"],
                        ]
                    ),
                )
            )
        for grade in grades:
            chunks.append(
                (
                    "grade",
                    grade["course_name"],
                    " ".join(
                        [
                            "成绩",
                            grade["course_name"],
                            grade["title"],
                            "总分",
                            str(grade["total"]),
                            "满分",
                            str(grade["full_mark"]),
                            "分项",
                            grade["detail"],
                        ]
                    ),
                )
            )
        for item in evaluations:
            chunks.append(
                (
                    "evaluation",
                    f"{item['course_name']}|{item['teacher']}",
                    " ".join(
                        [
                            "评价",
                            "课程",
                            item["course_name"],
                            "教师",
                            item["teacher"],
                            "评分",
                            str(item["rating"]),
                            "评语",
                            item["comment"],
                        ]
                    ),
                )
            )
        for memory in memories:
            chunks.append(("memory", str(memory["id"]), f"记忆 {memory['content']}"))
        conn.execute("DELETE FROM chunks WHERE user_id = ?", (user_id,))
        stamp = now_local().isoformat(timespec="seconds")
        conn.executemany(
            "INSERT INTO chunks(user_id, source, ref, text, updated_at) VALUES(?, ?, ?, ?, ?)",
            [(user_id, source, ref, text, stamp) for source, ref, text in chunks],
        )
        conn.commit()
        return len(chunks)
    finally:
        conn.close()


def search(query: str, limit: int = 4, user_id: int | None = None) -> list[dict]:
    uid = int(current_user_id.get() if user_id is None else user_id)
    limit = max(1, min(int(limit), 8))
    rows = _load(uid)
    if not rows:
        reindex_user(uid)
        rows = _load(uid)
    query_terms = tokenize(query)
    if not rows or not query_terms:
        return []
    documents = [tokenize(row["text"]) for row in rows]
    average = sum(len(doc) for doc in documents) / len(documents)
    df: dict[str, int] = {}
    for doc in documents:
        for term in set(doc):
            df[term] = df.get(term, 0) + 1
    total = len(documents)
    scored: list[tuple[float, dict]] = []
    for row, doc in zip(rows, documents):
        score = _bm25(query_terms, doc, df, total, average)
        if score > 0:
            scored.append((score, {"source": row["source"], "ref": row["ref"], "text": row["text"], "score": round(score, 3)}))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored[:limit]]


def _load(user_id: int) -> list[dict]:
    conn = connect()
    try:
        return rows_as_dicts(
            conn.execute("SELECT source, ref, text FROM chunks WHERE user_id = ?", (user_id,)).fetchall()
        )
    finally:
        conn.close()


def _bm25(query_terms: list[str], doc: list[str], df: dict[str, int], total: int, average: float) -> float:
    if not doc or average <= 0:
        return 0.0
    counts: dict[str, int] = {}
    for term in doc:
        counts[term] = counts.get(term, 0) + 1
    k1 = 1.2
    b = 0.75
    score = 0.0
    for term in set(query_terms):
        freq = counts.get(term, 0)
        if not freq:
            continue
        idf = math.log(1 + (total - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
        denom = freq + k1 * (1 - b + b * len(doc) / average)
        score += idf * (freq * (k1 + 1)) / denom
    return score
