"""Queue configured iPhone tasks when Shortcuts or another caller posts an event."""

import argparse
from contextlib import ExitStack, closing
from datetime import datetime
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
from threading import Event, Thread
import time

from phone_console import PhoneConsole
from phone_runtime import PhoneBusy, PhoneDevice, PhoneTaskError, WDAError
from messages_task import MESSAGES, MessagesApp, phone_number, point
from sms_batch import read_messages, run_batch
from trigger_settings import RULE_NAME, RuleStore, load_or_create_token, normalize_action, settings_handler


def load_rules(path):
    rules = json.loads(path.read_text())
    if not isinstance(rules, dict) or not rules:
        raise PhoneTaskError("触发器配置需要至少一条规则")
    for name, rule in rules.items():
        if not RULE_NAME.fullmatch(name):
            raise PhoneTaskError(f"触发器名称或规则格式无效：{name}")
        rules[name] = normalize_action(name, rule, path.parent)
    return rules


class InboxQueue:
    def __init__(self, path, managed=False):
        self.path = path
        self.managed = managed
        self.store = RuleStore(path)
        with closing(sqlite3.connect(path)) as db:
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, sender TEXT NOT NULL, return_app TEXT,
                result TEXT, done INTEGER NOT NULL DEFAULT 0, rule TEXT, rule_id TEXT
            )""")
            columns = {column[1] for column in db.execute("PRAGMA table_info(events)")}
            for column in ("rule", "rule_id"):
                if column not in columns:
                    db.execute(f"ALTER TABLE events ADD COLUMN {column} TEXT")
            db.commit()

    def enqueue(self, event_id, sender, rule=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute("INSERT OR IGNORE INTO events (id, sender, rule) VALUES (?, ?, ?)",
                              (event_id, sender, json.dumps(rule) if rule else None)).rowcount == 1

    def next_event(self):
        with closing(sqlite3.connect(self.path)) as db:
            return db.execute("""SELECT id, sender, return_app, result, rule FROM events
                WHERE done = 0 AND (rule_id IS NULL OR (? AND (
                    return_app IS NOT NULL OR EXISTS (
                        SELECT 1 FROM trigger_rules WHERE name = events.rule_id AND enabled = 1
                    )))) ORDER BY rowid LIMIT 1""", (self.managed,)).fetchone()

    def enqueue_managed(self, name, event_id, sender=None, scheduled_time=None):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT condition, task, enabled FROM trigger_rules WHERE name = ?", (name,)).fetchone()
            if row is None:
                return "missing"
            condition, task, enabled = row
            if not enabled:
                return "disabled"
            condition = json.loads(condition)
            kind = condition["type"]
            if kind == "daily" or scheduled_time is not None:
                if kind != "daily" or condition["time"] != scheduled_time:
                    return "condition"
            if kind == "message":
                number = phone_number(sender) if isinstance(sender, str) else None
                if not number or number.lstrip("+") != condition["sender"].lstrip("+"):
                    return "condition"
            key = f"ui/{name}/{event_id}" if scheduled_time is None else f"ui/{name}/daily/{event_id}"
            added = db.execute("INSERT OR IGNORE INTO events (id, sender, rule, rule_id) VALUES (?, '', ?, ?)",
                               (key, task, name)).rowcount
            return "queued" if added else "duplicate"

    def enqueue_due(self, now):
        minute = now.strftime("%H:%M")
        for rule in self.store.all():
            if rule["enabled"] and rule["condition"] == {"type": "daily", "time": minute}:
                self.enqueue_managed(rule["name"], now.date().isoformat(), scheduled_time=minute)

    def save_return_app(self, event_id, bundle_id):
        with closing(sqlite3.connect(self.path)) as db, db:
            return db.execute("""UPDATE events SET return_app = ? WHERE id = ? AND (
                rule_id IS NULL OR EXISTS (SELECT 1 FROM trigger_rules WHERE name = events.rule_id AND enabled = 1)
                )""", (bundle_id, event_id)).rowcount == 1

    def save_result(self, event_id, result):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE events SET result = ? WHERE id = ?", (json.dumps(result, ensure_ascii=False), event_id))

    def complete(self, event_id):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE events SET done = 1 WHERE id = ?", (event_id,))


def request_handler(queue, token, rules):
    expected = f"Bearer {token}".encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            name = self.path.removeprefix("/events/") if self.path.startswith("/events/") else None
            if not name or not RULE_NAME.fullmatch(name) or (rules is not None and name not in rules):
                self.send_error(404)
                return
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected):
                self.send_error(401)
                return
            size = self.headers.get("Content-Length", "")
            if not re.fullmatch(r"[0-9]{1,4}", size) or not 0 < int(size) <= 2048:
                self.send_error(400)
                return
            try:
                body = json.loads(self.rfile.read(int(size)))
            except (ValueError, UnicodeError):
                self.send_error(400)
                return
            event_id = body.get("event_id") if isinstance(body, dict) else None
            if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", event_id):
                self.send_error(400)
                return
            if rules is None:
                status = queue.enqueue_managed(name, event_id, body.get("sender"))
                if status in ("missing", "disabled", "condition"):
                    self.send_error({"missing": 404, "disabled": 409, "condition": 422}[status])
                    return
                added = status == "queued"
            else:
                rule = rules[name]
                key = event_id if name == "messages" else f"{name}/{event_id}"
                added = queue.enqueue(key, rule.get("sender", ""), rule)
            payload = json.dumps({"trigger": name, "event_id": event_id, "queued": added, "duplicate": not added}).encode()
            self.send_response(202 if added else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def inbox_preview(phone, sender, current_app):
    if current_app != MESSAGES:
        phone.launch_app(MESSAGES)
    nodes, _ = phone.tree()
    if (phone._source_app or phone.active_app()) != MESSAGES:
        raise PhoneTaskError("前台未核对到信息 App")
    back = [node for node in nodes if node.get("name") == "BackButton"]
    if len(back) == 1 and any(node.get("name") == "TranscriptCollectionView" for node in nodes):
        phone.post("/wda/tap", point(back[0]))
        time.sleep(.5)
        nodes, _ = phone.tree()
    if any(node.get("name") == "messageBodyField" for node in nodes):
        raise PhoneTaskError("信息 App 停在编辑页，请核对会话列表")
    if not any(node.get("name") == "ConversationList" for node in nodes):
        raise PhoneTaskError("未核对到信息会话列表")
    previews = set()
    for node in nodes:
        label = node.get("label") or ""
        number = phone_number(label.partition(",")[0].partition("，")[0])
        if node["type"] == "XCUIElementTypeCell" and number and number.lstrip("+") == sender.lstrip("+"):
            previews.add(label.strip())
    if len(previews) != 1:
        raise PhoneTaskError("未核对到该号码的唯一可见会话")
    return previews.pop()


def execute_rule(phone, rule, current_app):
    action = rule["action"]
    if action == "messages_preview":
        preview = inbox_preview(phone, rule["sender"], current_app)
        return {"status": "已核对会话", "preview": preview}
    if action == "app_snapshot":
        target = rule["bundle_id"]
        if current_app != target:
            try:
                phone.launch_app(target, verify=True)
            except WDAError as error:
                if error.code != "postcondition_failed":
                    raise
                raise PhoneTaskError(f"{target} 未切到前台，请检查手机上的系统提示") from error
        nodes, _ = phone.tree()
        if (phone._source_app or phone.active_app()) != target:
            raise PhoneTaskError("前台未核对到配置中的 App")
        labels = dict.fromkeys(node["label"] for node in nodes if node["type"] == "XCUIElementTypeStaticText"
                               and node.get("label"))
        return {"status": "已记录页面", "bundle_id": target, "text": list(labels)}
    if action == "sms_draft":
        csv = Path(rule["csv"])
        journal = csv.with_suffix(".sms.sqlite3")
        try:
            tasks = read_messages(csv)
        except UnicodeDecodeError as error:
            raise PhoneTaskError("短信名单需要保存为 UTF-8 编码的 CSV") from error
        with closing(sqlite3.connect(journal if journal.exists() else ":memory:")) as db:
            if run_batch(tasks, db, MessagesApp(phone)):
                raise PhoneTaskError("未核对到短信草稿")
        return {"status": "短信草稿任务已执行"}
    raise PhoneTaskError(f"未知的触发器动作：{action}")


def process_event(queue, device):
    if queue.next_event() is None:
        return False
    try:
        with device.acquire() as phone:
            event = queue.next_event()
            if event is None:
                return False
            event_id, sender, previous, stored, saved_rule = event
            rule = json.loads(saved_rule) if saved_rule else {"action": "messages_preview", "sender": sender}
            target = rule.get("bundle_id", MESSAGES)
            if phone.client.request("GET", "/wda/locked")["value"] is not False:
                return False
            if stored is None:
                current = phone.active_app()
                if previous is None:
                    previous = current
                    if not queue.save_return_app(event_id, previous):
                        return False
                try:
                    result = execute_rule(phone, rule, current)
                except (PhoneTaskError, OSError, sqlite3.Error) as error:
                    result = {"status": "待核对", "reason": str(error)}
                queue.save_result(event_id, result)
            else:
                result = json.loads(stored)
            if previous == "com.apple.springboard":
                phone.press_button("home", verify=True)
            elif previous != target:
                phone.launch_app(previous, verify=True)
            queue.complete(event_id)
            print(json.dumps({"event_id": event_id, "status": result["status"]}, ensure_ascii=False), flush=True)
    except PhoneBusy:
        return False
    except WDAError as error:
        if error.code not in ("pua_unreachable", "pua_timeout", "action_uncertain", "not_ready"):
            raise
        return False
    return True


def schedule_daily(queue, stop):
    while not stop.is_set():
        queue.enqueue_due(datetime.now())
        stop.wait(1)


def interrupt_for_shutdown(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    parser = argparse.ArgumentParser(prog="phone_agent.py trigger", description=__doc__)
    settings = parser.add_mutually_exclusive_group()
    settings.add_argument("--sender", help="快捷指令对应的发件人手机号")
    settings.add_argument("--config", type=Path, help="自定义触发规则 JSON")
    parser.add_argument("--bind", default="127.0.0.1", help="手机访问时填写 Mac 的局域网 IP")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--ui-port", type=int, default=8767, help="Mac 本地控制台端口")
    parser.add_argument("--wda-state-dir", type=Path)
    args = parser.parse_args(argv)
    sender = phone_number(args.sender or "")
    token = os.environ.get("PHONE_TRIGGER_TOKEN")
    managed = args.config is None and args.sender is None
    if not managed and (not token or (args.sender is not None and not sender)):
        parser.error("需要 PHONE_TRIGGER_TOKEN，使用 --sender 时请填写有效手机号")
    try:
        rules = None
        if args.config:
            rules = load_rules(args.config)
        elif args.sender is not None:
            rules = {"messages": {"action": "messages_preview", "sender": sender}}
        device = PhoneDevice(args.wda_state_dir)
        with device.exclusive("trigger"), ExitStack() as stack:
            if managed:
                for shutdown_signal in (signal.SIGTERM, signal.SIGHUP):
                    previous = signal.signal(shutdown_signal, interrupt_for_shutdown)
                    stack.callback(signal.signal, shutdown_signal, previous)
            queue = InboxQueue(device.storage / f"{device.udid}-trigger.sqlite3", managed=managed)
            token = token or load_or_create_token(device.storage / f"{device.udid}-trigger.token")
            events = stack.enter_context(ThreadingHTTPServer((args.bind, args.port), request_handler(queue, token, rules)))
            base = f"http://{args.bind}:{events.server_port}/events"
            servers = [events]
            if managed:
                console = PhoneConsole(device)
                stack.callback(console.close)
                admin = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", args.ui_port),
                                                                 settings_handler(queue.store, base, token, console)))
                servers.append(admin)
                print(f"手机 Agent 控制台 http://127.0.0.1:{admin.server_port}/", flush=True)
            else:
                for name in rules:
                    print(f"事件入口 {base}/{name}", flush=True)
            for server in servers:
                worker = Thread(target=server.serve_forever, daemon=True)
                worker.start()
                stack.callback(worker.join)
                stack.callback(server.shutdown)
            if managed:
                stop = Event()
                scheduler = Thread(target=schedule_daily, args=(queue, stop), daemon=True)
                scheduler.start()
                stack.callback(scheduler.join)
                stack.callback(stop.set)
            while True:
                if not process_event(queue, device):
                    time.sleep(1)
    except KeyboardInterrupt:
        return 0
    except (OSError, PhoneTaskError, WDAError, json.JSONDecodeError) as error:
        parser.exit(1, f"{error}\n")
