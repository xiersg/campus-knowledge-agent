"""当前请求属于哪个本站账号。MCP 子进程靠工具参数里的 user_id 设置它。"""

from __future__ import annotations

from contextvars import ContextVar

current_user_id: ContextVar[int] = ContextVar("current_user_id", default=1)
