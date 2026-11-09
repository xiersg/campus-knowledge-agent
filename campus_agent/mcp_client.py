"""同步 MCP 客户端。对话和网页通过它调用工具进程。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections import deque

from campus_agent.config import PROJECT_ROOT, db_path
from campus_agent.identity import current_user_id
from campus_agent.mcp_server import read_message, write_message


class McpClient:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._next_id = 1
        self._ready = False
        self._stderr: deque[str] = deque(maxlen=40)

    def start(self) -> None:
        with self._lock:
            if self._ready and self._proc is not None and self._proc.poll() is None:
                return
            self._close_unlocked()
            env = os.environ.copy()
            env["CAMPUS_DB_PATH"] = str(db_path())
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            self._proc = subprocess.Popen(
                [sys.executable, "-m", "campus_agent.mcp_server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
                env=env,
            )
            threading.Thread(target=self._drain_stderr, daemon=True).start()
            try:
                self._rpc(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "campus-knowledge-agent", "version": "1.0.0"},
                    },
                )
                self._notify("notifications/initialized")
                self._ready = True
            except Exception:
                self._close_unlocked()
                raise

    def list_tools(self) -> list[dict]:
        self.start()
        result = self._rpc("tools/list", {})
        return result.get("tools", [])

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        self.start()
        payload = dict(arguments or {})
        payload["user_id"] = int(current_user_id.get())
        result = self._rpc("tools/call", {"name": name, "arguments": payload})
        content = result.get("content") or []
        text = content[0].get("text", "") if content else ""
        try:
            data = json.loads(text) if text else {"ok": False, "error": "工具没有返回内容"}
        except json.JSONDecodeError:
            data = {"ok": False, "error": text or "工具返回了无法解析的内容"}
        if result.get("isError") and data.get("ok", True):
            data["ok"] = False
        return data

    def close(self) -> None:
        with self._lock:
            self._close_unlocked()

    def _close_unlocked(self) -> None:
        proc = self._proc
        self._proc = None
        self._ready = False
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is None:
                continue
            try:
                stream.close()
            except OSError:
                pass

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr.append(line.decode("utf-8", "replace").rstrip())

    def _notify(self, method: str) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        write_message(self._proc.stdin, {"jsonrpc": "2.0", "method": method})

    def _rpc(self, method: str, params: dict) -> dict:
        with self._lock:
            if self._proc is None or self._proc.poll() is not None or self._proc.stdin is None or self._proc.stdout is None:
                raise RuntimeError("MCP 服务没有在运行")
            request_id = self._next_id
            self._next_id += 1
            write_message(
                self._proc.stdin,
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            )
            while True:
                message = read_message(self._proc.stdout)
                if message is None:
                    detail = "；".join(self._stderr) or "没有错误输出"
                    raise RuntimeError(f"MCP 服务已退出：{detail}")
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise RuntimeError(message["error"].get("message", "MCP 调用失败"))
                return message.get("result") or {}
