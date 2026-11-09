"""本站账号。密码只存哈希，和教务系统的密码不是一回事。"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import timedelta

from campus_agent.config import now_local
from campus_agent.db import connect

STUDENT_NO = re.compile(r"^[A-Za-z0-9]{4,32}$")


def register(student_no: str, password: str, display_name: str = "") -> dict:
    student_no = student_no.strip()
    display_name = display_name.strip() or student_no
    _check_password(password)
    if not STUDENT_NO.fullmatch(student_no):
        raise ValueError("学号请用 4 到 32 位字母或数字")
    if len(display_name) > 32:
        raise ValueError("姓名太长")
    salt = secrets.token_hex(16)
    digest = _hash(password, salt)
    conn = connect()
    try:
        exists = conn.execute("SELECT id FROM users WHERE student_no = ?", (student_no,)).fetchone()
        if exists:
            raise ValueError("这个学号已经注册过，直接登录即可")
        cursor = conn.execute(
            "INSERT INTO users(student_no, display_name, password_hash, salt, created_at) VALUES(?, ?, ?, ?, ?)",
            (student_no, display_name, digest, salt, now_local().isoformat(timespec="seconds")),
        )
        conn.commit()
        user_id = int(cursor.lastrowid)
    finally:
        conn.close()
    token = _issue(user_id)
    return {"ok": True, "token": token, "user": {"id": user_id, "student_no": student_no, "display_name": display_name}}


def login(student_no: str, password: str) -> dict:
    student_no = student_no.strip()
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE student_no = ?", (student_no,)).fetchone()
    finally:
        conn.close()
    if row is None or not secrets.compare_digest(_hash(password, row["salt"]), row["password_hash"]):
        raise ValueError("学号或密码不对")
    token = _issue(int(row["id"]))
    return {
        "ok": True,
        "token": token,
        "user": {"id": int(row["id"]), "student_no": row["student_no"], "display_name": row["display_name"]},
    }


def logout(token: str) -> None:
    if not token:
        return
    conn = connect()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def user_from_token(token: str) -> dict | None:
    if not token:
        return None
    conn = connect()
    try:
        row = conn.execute(
            """
            SELECT users.id, users.student_no, users.display_name, sessions.expires_at
            FROM sessions JOIN users ON users.id = sessions.user_id
            WHERE sessions.token = ?
            """,
            (token,),
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] < now_local().isoformat(timespec="seconds"):
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            return None
        return {"id": int(row["id"]), "student_no": row["student_no"], "display_name": row["display_name"]}
    finally:
        conn.close()


def _issue(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    expires = (now_local() + timedelta(days=14)).isoformat(timespec="seconds")
    conn = connect()
    try:
        conn.execute("INSERT INTO sessions(token, user_id, expires_at) VALUES(?, ?, ?)", (token, user_id, expires))
        conn.commit()
    finally:
        conn.close()
    return token


def _check_password(password: str) -> None:
    if len(password) < 6 or len(password) > 72:
        raise ValueError("密码需要 6 到 72 位")


def _hash(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()
