"""本机网页：门户、账号和课表导入。"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from campus_agent.accounts import login, logout, register, user_from_token
from campus_agent.browser_login import capture_schedule_cookie
from campus_agent.agent import Agent, list_chat
from campus_agent.config import PACKAGE_ROOT, llm_settings
from campus_agent.db import connect, get_user_meta, set_user_meta
from campus_agent.gdut import LOGIN_PAGE, fetch_personal_grades, fetch_personal_timetable
from campus_agent.identity import current_user_id
from campus_agent.mcp_client import McpClient
from campus_agent.tools import import_schedule, import_school_grades, seed_if_empty

STATIC = PACKAGE_ROOT / "static" / "index.html"
MAX_BODY = 1_000_000


class App:
    def __init__(self) -> None:
        seed_if_empty()
        self.mcp = McpClient()
        self.agent = Agent(self.mcp)
        self.browser_sync = {"state": "idle", "message": "", "courses": 0, "user_id": 0}
        self._sync_guard = threading.Lock()

    def close(self) -> None:
        self.mcp.close()

    def start_browser_sync(self, user_id: int, semester: str) -> dict:
        with self._sync_guard:
            if self.browser_sync["state"] in {"opening", "waiting", "importing"}:
                return {"ok": False, "error": "已经有一个教务登录窗口在等待"}
            self.browser_sync = {"state": "opening", "message": "正在打开教务登录窗口", "courses": 0, "user_id": user_id}
        threading.Thread(target=self._browser_sync, args=(user_id, semester), daemon=True).start()
        return {"ok": True, "state": "opening", "message": "正在打开教务登录窗口"}

    def _browser_sync(self, user_id: int, semester: str) -> None:
        def update(state: str, message: str) -> None:
            self.browser_sync = {"state": state, "message": message, "courses": 0, "user_id": user_id}

        try:
            cookie = capture_schedule_cookie(semester, on_status=update)
            update("importing", "已登录，正在读取课表和成绩")
            timetable = fetch_personal_timetable(cookie, semester)
            _remember_cookie(user_id, cookie)
            result = _store_official(user_id, cookie, semester, timetable)
            self.browser_sync = {
                "state": "done",
                "message": _imported_message(result),
                "courses": int(result.get("courses") or 0),
                "user_id": user_id,
            }
        except ValueError as exc:
            self.browser_sync = {"state": "error", "message": str(exc), "courses": 0, "user_id": user_id}
        except Exception:
            self.browser_sync = {"state": "error", "message": "同步没有完成，请重新打开教务登录", "courses": 0, "user_id": user_id}


class Handler(BaseHTTPRequestHandler):
    server: "CampusServer"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, STATIC.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/health":
            settings = llm_settings()
            self._json(200, {"ok": True, "model_configured": bool(settings["api_key"]), "model": settings["model"]})
            return
        if path == "/api/me":
            user = self._user()
            self._json(200, {"ok": True, "user": user, "jxfw_login": LOGIN_PAGE})
            return
        if path == "/api/gdut-browser-sync":
            user = self._require_user()
            if user is None:
                return
            status = self.server.app.browser_sync
            if status["user_id"] not in {0, int(user["id"])}:
                self._json(200, {"ok": True, "state": "idle", "message": "", "courses": 0})
                return
            self._json(200, {"ok": True, "state": status["state"], "message": status["message"], "courses": status["courses"]})
            return
        if path == "/api/messages":
            user = self._require_user()
            if user is None:
                return
            query = parse_qs(urlparse(self.path).query)
            session_id = f"u{user['id']}:{(query.get('session_id') or ['default'])[0]}"
            try:
                before = int((query.get("before") or ["0"])[0])
                limit = int((query.get("limit") or ["12"])[0])
            except ValueError:
                self._json(400, {"ok": False, "error": "消息分页参数无效"})
                return
            self._json(200, self._as_user(user, lambda: list_chat(session_id, before, limit)))
            return
        if path == "/api/grades":
            user = self._require_user()
            if user is None:
                return
            keyword = (parse_qs(urlparse(self.path).query).get("keyword") or [""])[0]
            self._json(200, self._as_user(user, lambda: self.server.app.mcp.call_tool("list_grades", {"keyword": keyword})))
            return
        if path == "/api/grade-analysis":
            user = self._require_user()
            if user is None:
                return
            self._json(200, self._as_user(user, lambda: self.server.app.mcp.call_tool("analyze_grades")))
            return
        if path == "/api/evaluations":
            user = self._require_user()
            if user is None:
                return
            self._json(200, self._as_user(user, lambda: self.server.app.mcp.call_tool("list_evaluations")))
            return
        if path == "/api/week":
            user = self._require_user()
            if user is None:
                return
            try:
                offset = int((parse_qs(urlparse(self.path).query).get("offset") or ["0"])[0])
            except ValueError:
                self._json(400, {"ok": False, "error": "周次偏移无效"})
                return
            self._json(200, self._as_user(user, lambda: self.server.app.mcp.call_tool("list_courses", {"scope": "week", "week_offset": offset})))
            return
        if path == "/api/overview":
            user = self._require_user()
            if user is None:
                return
            self._json(200, self._as_user(user, lambda: self.server.app.mcp.call_tool("campus_brief")))
            return
        self._json(404, {"ok": False, "error": "没有这个页面"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        if path == "/api/register":
            self._auth_result(lambda: register(
                str(payload.get("student_no") or ""),
                str(payload.get("password") or ""),
                str(payload.get("display_name") or ""),
            ))
            return
        if path == "/api/login":
            self._auth_result(lambda: login(str(payload.get("student_no") or ""), str(payload.get("password") or "")))
            return
        if path == "/api/logout":
            logout(self._token())
            self._json(200, {"ok": True}, clear_cookie=True)
            return
        user = self._require_user()
        if user is None:
            return
        app = self.server.app
        if path == "/api/chat":
            session_id = f"u{user['id']}:{payload.get('session_id') or 'default'}"
            message = str(payload.get("message") or "")
            mode = str(payload.get("mode") or "campus")
            topic = str(payload.get("topic") or "")[:40]
            result = self._as_user(user, lambda: app.agent.chat(session_id, message, mode, topic))
            result["ok"] = True
            self._json(200, result)
            return
        if path == "/api/import":
            content = str(payload.get("content") or "")
            self._json(200, self._as_user(user, lambda: app.mcp.call_tool("import_schedule", {"content": content, "replace": True})))
            return
        if path == "/api/sample":
            self._json(200, self._as_user(user, lambda: app.mcp.call_tool("load_sample_schedule")))
            return
        if path == "/api/gdut-sync":
            self._json(200, self._sync_gdut(user, payload))
            return
        if path == "/api/gdut-refresh":
            self._json(200, self._refresh_saved(user, str(payload.get("semester") or "202601")))
            return
        if path == "/api/evaluations":
            try:
                score = int(payload.get("rating") or 0)
            except (TypeError, ValueError):
                score = 0
            self._json(200, self._as_user(user, lambda: app.mcp.call_tool("add_evaluation", {
                "course_name": str(payload.get("course_name") or ""),
                "teacher": str(payload.get("teacher") or ""),
                "rating": score,
                "comment": str(payload.get("comment") or ""),
            })))
            return
        if path == "/api/gdut-browser-sync":
            self._json(200, app.start_browser_sync(int(user["id"]), str(payload.get("semester") or "202601")))
            return
        self._json(404, {"ok": False, "error": "没有这个接口"})

    def log_message(self, fmt: str, *args) -> None:
        return

    def _sync_gdut(self, user: dict, payload: dict) -> dict:
        try:
            cookie = str(payload.get("cookie") or "")
            semester = str(payload.get("semester") or "202601")
            timetable = fetch_personal_timetable(cookie, semester)
            _remember_cookie(int(user["id"]), cookie)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return _store_official(int(user["id"]), cookie, semester, timetable)

    def _refresh_saved(self, user: dict, semester: str) -> dict:
        conn = connect()
        try:
            cookie = get_user_meta(conn, int(user["id"]), "jxfw_cookie", "")
        finally:
            conn.close()
        if not cookie:
            return {"ok": False, "need_login": True, "error": "还没有保存的教务登录，请先登录"}
        try:
            timetable = fetch_personal_timetable(cookie, semester)
        except ValueError as exc:
            return {"ok": False, "need_login": True, "error": str(exc)}
        return _store_official(int(user["id"]), cookie, semester, timetable)

    def _auth_result(self, action) -> None:
        try:
            result = action()
        except ValueError as exc:
            self._json(400, {"ok": False, "error": str(exc)})
            return
        token = result.pop("token")
        self._json(200, result, set_cookie=token)

    def _require_user(self) -> dict | None:
        user = self._user()
        if user is None:
            self._json(401, {"ok": False, "error": "请先登录本站账号"})
            return None
        return user

    def _user(self) -> dict | None:
        return user_from_token(self._token())

    def _token(self) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "campus_session":
                return value
        return ""

    def _as_user(self, user: dict, action):
        token = current_user_id.set(int(user["id"]))
        try:
            return action()
        finally:
            current_user_id.reset(token)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length < 0 or length > MAX_BODY:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("请求不是 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("请求必须是 JSON 对象")
        return data

    def _json(self, status: int, payload: dict, set_cookie: str = "", clear_cookie: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if set_cookie:
            self.send_header(
                "Set-Cookie",
                f"campus_session={set_cookie}; HttpOnly; Path=/; Max-Age=1209600; SameSite=Lax",
            )
        if clear_cookie:
            self.send_header("Set-Cookie", "campus_session=; HttpOnly; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CampusServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], app: App) -> None:
        self.app = app
        super().__init__(address, Handler)


def make_server(host: str, port: int, app: App | None = None) -> CampusServer:
    return CampusServer((host, port), app or App())


def _store_official(user_id: int, cookie: str, semester: str, timetable: dict) -> dict:
    grade_rows: list[dict] = []
    grade_error = ""
    try:
        grade_rows = fetch_personal_grades(cookie, semester)
    except ValueError as exc:
        grade_error = str(exc)
    token = current_user_id.set(int(user_id))
    try:
        result = import_schedule(json.dumps(timetable, ensure_ascii=False), replace=True)
        graded = import_school_grades(grade_rows)
    finally:
        current_user_id.reset(token)
    result.pop("source", None)
    result["grades"] = int(graded.get("grades") or 0)
    if grade_error:
        result["grades_error"] = grade_error
    return result


def _imported_message(result: dict) -> str:
    text = f"已导入 {result.get('courses', 0)} 门课"
    if result.get("grades"):
        text += f"，成绩 {result['grades']} 条"
    elif result.get("grades_error"):
        text += "。成绩暂时没有读到"
    return text


def _remember_cookie(user_id: int, cookie: str) -> None:
    if not cookie:
        return
    conn = connect()
    try:
        set_user_meta(conn, int(user_id), "jxfw_cookie", cookie)
        conn.commit()
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="校园知识咨询")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    app = App()
    server = None
    port = args.port
    for candidate in range(port, port + 10):
        try:
            server = make_server(args.host, candidate, app)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        app.close()
        raise SystemExit(f"端口 {args.port} 到 {args.port + 9} 都被占用")
    print(f"校园知识咨询已打开：http://{args.host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        server.server_close()
        app.close()
