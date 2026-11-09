"""按用户保存的长期记忆。对话原文仍在 messages 表。"""

from __future__ import annotations

from campus_agent.config import now_local
from campus_agent.db import connect
from campus_agent.identity import current_user_id

TRIGGERS = ("记住", "请记得", "别忘了", "以后都", "我喜欢", "我不喜欢")
MAX_MEMORIES = 30


def list_memories(limit: int = 12) -> list[str]:
    limit = max(1, min(int(limit), MAX_MEMORIES))
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT content FROM memories WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (int(current_user_id.get()), limit),
        ).fetchall()
    finally:
        conn.close()
    return [row["content"] for row in rows]


def remember(content: str) -> None:
    text = " ".join(content.split())[:200]
    if len(text) < 2:
        return
    user_id = int(current_user_id.get())
    conn = connect()
    try:
        existing = conn.execute("SELECT id FROM memories WHERE user_id = ? AND content = ?", (user_id, text)).fetchone()
        if existing:
            return
        conn.execute(
            "INSERT INTO memories(user_id, content, created_at) VALUES(?, ?, ?)",
            (user_id, text, now_local().isoformat(timespec="seconds")),
        )
        extra = conn.execute(
            "SELECT id FROM memories WHERE user_id = ? ORDER BY id DESC",
            (user_id,),
        ).fetchall()
        if len(extra) > MAX_MEMORIES:
            stale = [row["id"] for row in extra[MAX_MEMORIES:]]
            conn.execute(
                f"DELETE FROM memories WHERE id IN ({','.join('?' for _ in stale)})",
                stale,
            )
        conn.commit()
    finally:
        conn.close()
    from campus_agent.rag import reindex_current

    reindex_current()


def capture_memory(text: str) -> None:
    if any(trigger in text for trigger in TRIGGERS):
        remember(text)
