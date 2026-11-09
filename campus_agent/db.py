"""SQLite 存储：课表、考试、课堂复盘和对话。"""

from __future__ import annotations

import secrets
import sqlite3
from pathlib import Path

from campus_agent.config import DEFAULT_SEMESTER_START, db_path, now_local

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    student_no TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_meta (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS courses (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    teacher TEXT NOT NULL,
    classroom TEXT NOT NULL,
    weekday INTEGER NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    weeks TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS exams (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
    course_name TEXT NOT NULL,
    exam_at TEXT NOT NULL,
    location TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    remind_days INTEGER NOT NULL DEFAULT 7
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
    course_name TEXT NOT NULL,
    session_date TEXT NOT NULL,
    topic TEXT NOT NULL,
    key_points TEXT NOT NULL,
    questions TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL DEFAULT 1,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS grades (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    course_name TEXT NOT NULL,
    title TEXT NOT NULL,
    total REAL NOT NULL,
    full_mark REAL NOT NULL DEFAULT 100,
    detail TEXT NOT NULL DEFAULT '',
    exam_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    course_name TEXT NOT NULL,
    teacher TEXT NOT NULL,
    rating INTEGER NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    ref TEXT NOT NULL,
    text TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for table in ("courses", "exams", "reviews", "messages"):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "user_id" not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
    conn.commit()
    if conn.execute("SELECT 1 FROM users WHERE id = 1").fetchone() is None:
        conn.execute(
            "INSERT INTO users(id, student_no, display_name, password_hash, salt, created_at) VALUES(1, ?, ?, ?, ?, ?)",
            ("sample", "示例课表", secrets.token_hex(32), secrets.token_hex(16), now_local().isoformat(timespec="seconds")),
        )
        conn.commit()
    if get_meta(conn, "semester_start", "") == "":
        set_meta(conn, "semester_start", DEFAULT_SEMESTER_START)
        set_meta(conn, "semester_name", "2026-2027学年第一学期")
    return conn


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_user_meta(conn: sqlite3.Connection, user_id: int, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM user_meta WHERE user_id = ? AND key = ?", (user_id, key)).fetchone()
    return row["value"] if row else default


def set_user_meta(conn: sqlite3.Connection, user_id: int, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO user_meta(user_id, key, value) VALUES(?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
        """,
        (user_id, key, value),
    )


def rows_as_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


def database_file() -> Path:
    return db_path()
