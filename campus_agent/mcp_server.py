"""MCP stdio 服务：Content-Length 帧 + JSON-RPC 2.0。"""

from __future__ import annotations

import json
import sys

from campus_agent import __version__
from campus_agent.identity import current_user_id
from campus_agent.tools import TOOL_SPECS, dispatch, seed_if_empty

PROTOCOL = "2024-11-05"
MAX_BYTES = 2_000_000


def read_exact(stream, size: int) -> bytes:
    buffer = bytearray()
    while len(buffer) < size:
        chunk = stream.read(size - len(buffer))
        if not chunk:
            raise EOFError
        buffer.extend(chunk)
    return bytes(buffer)


def read_message(stream) -> dict | None:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if line == b"":
            return None
        if line in (b"\r\n", b"\n"):
            break
        text = line.decode("ascii", "replace")
        if ":" in text:
            key, value = text.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0 or length > MAX_BYTES:
        return None
    return json.loads(read_exact(stream, length).decode("utf-8"))


def write_message(stream, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stream.flush()


def handle(message: dict) -> dict | None:
    method = message.get("method", "")
    request_id = message.get("id")
    params = message.get("params") or {}
    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "campus-knowledge", "version": __version__},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOL_SPECS}
    elif method == "tools/call":
        name = params.get("name", "")
        arguments = dict(params.get("arguments") or {})
        user_token = None
        if "user_id" in arguments:
            user_token = current_user_id.set(int(arguments.pop("user_id")))
        try:
            data = dispatch(name, arguments)
        finally:
            if user_token is not None:
                current_user_id.reset(user_token)
        text = json.dumps(data, ensure_ascii=False)
        result = {"content": [{"type": "text", "text": text}], "isError": not data.get("ok", False)}
    elif request_id is None:
        return None
    else:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"不支持的方法：{method}"},
        }
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def serve() -> None:
    seed_if_empty()
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        try:
            message = read_message(stdin)
        except EOFError:
            break
        if message is None:
            break
        response = handle(message)
        if response is not None:
            write_message(stdout, response)


if __name__ == "__main__":
    serve()
