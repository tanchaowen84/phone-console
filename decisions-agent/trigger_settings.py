"""Loopback UI and persisted rules for the phone trigger service."""

from contextlib import closing
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3

from phone_runtime import PhoneTaskError
from sms_batch import phone_number

RULE_FIELDS = {"messages_preview": "sender", "app_snapshot": "bundle_id", "sms_draft": "csv"}
RULE_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def normalize_action(name, task, base):
    if not isinstance(task, dict):
        raise PhoneTaskError(f"{name} 的任务格式无效")
    action = task.get("action")
    field = RULE_FIELDS.get(action) if isinstance(action, str) else None
    if field is None or set(task) != {"action", field} or not isinstance(task[field], str):
        raise PhoneTaskError(f"{name} 的任务或参数无效")
    value = task[field].strip()
    if field == "sender":
        value = phone_number(value)
    elif field == "bundle_id":
        if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+", value):
            value = None
    elif value:
        value = str((base / Path(value).expanduser()).resolve())
    if not value:
        raise PhoneTaskError(f"{name} 的 {field} 无效")
    return {"action": action, field: value}


def normalize_condition(condition):
    kind = condition.get("type") if isinstance(condition, dict) else None
    if kind == "webhook":
        return {"type": kind}
    if kind == "daily":
        when = condition.get("time")
        if isinstance(when, str) and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", when):
            return {"type": kind, "time": when}
    if kind == "message":
        sender = condition.get("sender")
        if isinstance(sender, str) and (number := phone_number(sender)):
            return {"type": kind, "sender": number}
    raise PhoneTaskError("请填写有效的触发条件")


class RuleStore:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS trigger_rules (
                name TEXT PRIMARY KEY, condition TEXT NOT NULL, task TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0
            )""")

    def all(self):
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute("SELECT name, condition, task, enabled FROM trigger_rules ORDER BY rowid").fetchall()
        return [{"name": name, "condition": json.loads(condition), "task": json.loads(task),
                 "enabled": bool(enabled)} for name, condition, task, enabled in rows]

    def save(self, name, data, create):
        if not RULE_NAME.fullmatch(name):
            raise PhoneTaskError("规则名称需要 1 至 64 位字母、数字、短横线或下划线")
        condition = normalize_condition(data.get("condition"))
        task = normalize_action(name, data.get("task"), Path.cwd())
        values = (json.dumps(condition), json.dumps(task), name)
        with closing(sqlite3.connect(self.path)) as db, db:
            if create:
                if db.execute("INSERT OR IGNORE INTO trigger_rules (condition, task, name) VALUES (?, ?, ?)",
                              values).rowcount != 1:
                    raise PhoneTaskError("该规则名称已存在")
            elif db.execute("UPDATE trigger_rules SET condition=?, task=? WHERE name=?", values).rowcount != 1:
                raise PhoneTaskError("找不到这条规则")

    def enable(self, name, enabled):
        if type(enabled) is not bool:
            raise PhoneTaskError("启用状态必须为 true 或 false")
        with closing(sqlite3.connect(self.path)) as db, db:
            if db.execute("UPDATE trigger_rules SET enabled=? WHERE name=?", (enabled, name)).rowcount != 1:
                raise PhoneTaskError("找不到这条规则")


def load_or_create_token(path):
    if path.is_file():
        return path.read_text().strip()
    token = secrets.token_urlsafe(32)
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as stream:
        stream.write(token + "\n")
    return token


def settings_handler(store, event_base, token, console=None):
    root = Path(__file__).parent
    pages = {"/triggers": (root / "trigger_settings.html").read_bytes()}
    pages["/"] = (root / "phone_console.html").read_bytes() if console else pages["/triggers"]
    css = (root / "phone_console.css").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def trusted(self, write=False):
            host = f"127.0.0.1:{self.server.server_port}"
            local = self.client_address[0] == "127.0.0.1" and self.headers.get("Host") == host
            origin = self.headers.get("Origin")
            return local and (not write or origin == f"http://{host}")

        def respond(self, code, data, content_type="application/json"):
            body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if content_type == "text/html":
                self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self.trusted():
                self.send_error(403)
            elif self.path in pages:
                self.respond(200, pages[self.path], "text/html")
            elif self.path == "/console.css":
                self.respond(200, css, "text/css")
            elif console and self.path == "/api/device":
                self.respond(200, console.device_status())
            elif console and self.path == "/api/run":
                self.respond(200, {"run": console.status()})
            elif self.path == "/api/rules":
                self.respond(200, {"rules": store.all(), "event_base": event_base})
            elif self.path == "/api/token":
                self.respond(200, {"token": token})
            else:
                self.send_error(404)

        def write(self, method):
            if not self.trusted(write=True):
                self.send_error(403)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 4096:
                    raise PhoneTaskError("配置请求长度无效")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise PhoneTaskError("配置格式无效")
                if console and self.path == "/api/run":
                    console.start(data)
                elif console and self.path == "/api/run/stop":
                    console.stop()
                else:
                    name = data.get("name", "") if method == "POST" else self.path.removeprefix("/api/rules/")
                    if not isinstance(name, str) or not RULE_NAME.fullmatch(name):
                        raise PhoneTaskError("请输入有效的规则名称")
                    if method == "PATCH":
                        store.enable(name, data.get("enabled"))
                    else:
                        store.save(name, data, create=method == "POST")
            except (PhoneTaskError, ValueError, OSError) as error:
                self.respond(400, {"error": str(error)})
                return
            self.respond(200, {"ok": True})

        def do_POST(self):
            if self.path == "/api/rules" or (console and self.path in ("/api/run", "/api/run/stop")):
                self.write("POST")
            else:
                self.send_error(404)

        def do_PUT(self):
            if self.path.startswith("/api/rules/"):
                self.write("PUT")
            else:
                self.send_error(404)

        def do_PATCH(self):
            if self.path.startswith("/api/rules/"):
                self.write("PATCH")
            else:
                self.send_error(404)

    return Handler
