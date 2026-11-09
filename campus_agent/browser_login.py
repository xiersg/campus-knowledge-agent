"""打开学校登录窗口，等用户自己登录后读取这一次的教务 Cookie。

密码由用户在学校页面里输入。这里不填写密码，也不保存 Cookie。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from campus_agent.gdut import LOGIN_PAGE, fetch_personal_timetable

JXFW_URL = "https://jxfw.gdut.edu.cn/"


def find_browser() -> Path:
    candidates = [
        os.environ.get("PROGRAMFILES(X86)", "") + r"\Microsoft\Edge\Application\msedge.exe",
        os.environ.get("PROGRAMFILES", "") + r"\Microsoft\Edge\Application\msedge.exe",
        os.environ.get("PROGRAMFILES", "") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("PROGRAMFILES(X86)", "") + r"\Google\Chrome\Application\chrome.exe",
        os.environ.get("LOCALAPPDATA", "") + r"\Google\Chrome\Application\chrome.exe",
    ]
    for raw in candidates:
        path = Path(raw)
        if raw and path.exists():
            return path
    raise ValueError("没有找到 Edge 或 Chrome，无法打开教务登录窗口")


def format_cookie_header(cookies: list[dict]) -> str:
    parts = []
    for item in cookies:
        domain = str(item.get("domain") or "")
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if "jxfw.gdut.edu.cn" not in domain or not name:
            continue
        parts.append(f"{name}={value}")
    return ";".join(parts)


def capture_schedule_cookie(semester: str, timeout: float = 180, on_status=None) -> str:
    """阻塞到用户在弹出的窗口里登录成功，返回只用于本次请求的 Cookie。"""
    browser = find_browser()
    profile = Path(tempfile.mkdtemp(prefix="gdut-login-"))
    port = _free_port()
    process = None
    try:
        _notify(on_status, "opening", "正在打开教务登录窗口")
        process = subprocess.Popen(
            [
                str(browser),
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1",
                "--remote-allow-origins=*",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "--new-window",
                LOGIN_PAGE,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if not _wait_port(port, 20):
            raise ValueError("登录窗口没有启动。请确认 Edge 或 Chrome 可以打开")
        _notify(on_status, "waiting", "请在弹出的学校页面登录。登录成功后会自动导入课表")
        deadline = time.time() + timeout
        misses = 0
        while time.time() < deadline:
            if not _port_open(port):
                misses += 1
                if misses >= 2:
                    raise ValueError("登录窗口已关闭，还没有完成教务登录")
            else:
                misses = 0
                cookie = _cookie_if_logged_in(port)
                if not cookie:
                    continue
                try:
                    fetch_personal_timetable(cookie, semester)
                except ValueError:
                    continue
                return cookie
            time.sleep(1)
        raise ValueError("等待登录超时。请在弹出的窗口里完成教务登录后再试")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        shutil.rmtree(profile, ignore_errors=True)


def _notify(callback, state: str, message: str) -> None:
    if callback is not None:
        callback(state, message)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_open(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _wait_port(port: int, seconds: float) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _port_open(port):
            return True
        time.sleep(0.3)
    return False


def _cookie_if_logged_in(port: int) -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
            targets = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return ""
    for target in targets:
        if target.get("type") != "page" or not target.get("webSocketDebuggerUrl"):
            continue
        try:
            client = DevtoolsClient(target["webSocketDebuggerUrl"])
            result = client.call("Network.getCookies", {"urls": [JXFW_URL]})
            client.close()
        except (OSError, RuntimeError, json.JSONDecodeError, TimeoutError):
            continue
        header = format_cookie_header(result.get("cookies") or [])
        if "JSESSIONID=" in header or "SESSION=" in header.upper():
            return header
    return ""


class DevtoolsClient:
    def __init__(self, ws_url: str) -> None:
        parsed = urlparse(ws_url)
        self._sock = socket.create_connection((parsed.hostname, parsed.port or 80), timeout=5)
        self._sock.settimeout(5)
        key = os.urandom(16)
        import base64

        encoded = base64.b64encode(key).decode()
        host = f"{parsed.hostname}:{parsed.port}"
        request = (
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {encoded}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._sock.sendall(request.encode("ascii"))
        handshake = b""
        while b"\r\n\r\n" not in handshake:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("调试连接没有建立")
            handshake += chunk
        if b" 101 " not in handshake.split(b"\r\n", 1)[0]:
            raise ConnectionError("浏览器拒绝了调试连接")
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        message = {"id": self._id, "method": method, "params": params or {}}
        self._send_text(json.dumps(message))
        while True:
            payload = json.loads(self._recv_text())
            if payload.get("id") != self._id:
                continue
            if "error" in payload:
                raise RuntimeError(payload["error"].get("message") or "读取 Cookie 失败")
            return payload.get("result") or {}

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def _send_text(self, text: str) -> None:
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        header = bytearray([0x81])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        self._sock.sendall(bytes(header) + mask + masked)

    def _recv_text(self) -> str:
        chunks: list[bytes] = []
        while True:
            fin, opcode, payload = self._recv_frame()
            if opcode == 8:
                raise ConnectionError("调试连接已关闭")
            if opcode == 9:
                continue
            if opcode in {0, 1}:
                chunks.append(payload)
                if fin:
                    return b"".join(chunks).decode("utf-8")

    def _recv_frame(self) -> tuple[bool, int, bytes]:
        first, second = self._read(2)
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read(8))[0]
        if second & 0x80:
            mask = self._read(4)
            raw = self._read(length)
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(raw))
        else:
            payload = self._read(length)
        return fin, opcode, payload

    def _read(self, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            piece = self._sock.recv(size - len(chunks))
            if not piece:
                raise ConnectionError("调试连接中断")
            chunks.extend(piece)
        return bytes(chunks)
