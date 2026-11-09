"""路径、学期时间和模型配置。"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = Path(__file__).resolve().parent
SAMPLE_PATH = PROJECT_ROOT / "samples" / "schedule.json"
TZ = timezone(timedelta(hours=8))
DEFAULT_SEMESTER_START = "2026-09-07"
WEEKDAY_NAMES = ["", "一", "二", "三", "四", "五", "六", "日"]


def load_dotenv() -> None:
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def db_path() -> Path:
    raw = os.environ.get("CAMPUS_DB_PATH", "").strip()
    if raw:
        return Path(raw)
    return PROJECT_ROOT / "data" / "campus.db"


def now_local() -> datetime:
    return datetime.now(TZ)


def today() -> date:
    return now_local().date()


def weekday_name(weekday: int) -> str:
    if 1 <= weekday <= 7:
        return f"周{WEEKDAY_NAMES[weekday]}"
    return f"星期{weekday}"


MAX_HISTORY_MESSAGES = 8
MAX_HISTORY_CHARS = 6000
MAX_REQUEST_CHARS = 12000
CHAT_PAGE_SIZE = 12
MAX_STORED_MESSAGES = 36
KEEP_MESSAGES = 18


def llm_settings() -> dict[str, str]:
    load_dotenv()
    key = os.environ.get("OPENAI_API_KEY", "").strip() or os.environ.get("DEEPSEEK_API_KEY", "").strip()
    placeholders = {"", "your-key", "sk-xxx", "changeme"}
    if key.lower() in placeholders:
        key = ""
    base = os.environ.get("OPENAI_BASE_URL", "https://api.deepseek.com/v1").strip().rstrip("/")
    model = os.environ.get("OPENAI_MODEL", "deepseek-chat").strip() or "deepseek-chat"
    return {"api_key": key, "base_url": base, "model": model}


def chat_completions_url(base_url: str) -> str:
    if base_url.endswith("/chat/completions"):
        return base_url
    return base_url + "/chat/completions"
