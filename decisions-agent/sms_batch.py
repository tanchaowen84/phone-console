"""Send the rows of a CSV individually through iPhone Messages."""

import argparse
from contextlib import closing
import csv
from hashlib import sha256
from io import StringIO
from pathlib import Path
import sqlite3
from typing import NamedTuple

from messages_task import MessagesApp, phone_number, report
from phone_runtime import PhoneTaskError, WDAError, phone_task
from sms_vision import VisionError


class Message(NamedTuple):
    task_id: str
    phone: str
    text: str

    @property
    def fingerprint(self):
        return sha256((self.phone + "\0" + self.text).encode()).hexdigest()


def read_messages(path, expected_sha256=None):
    tasks = {}
    if expected_sha256:
        content = path.read_bytes()
        if sha256(content).hexdigest() != expected_sha256:
            raise PhoneTaskError("短信名单在预览后已更改，请重新规划")
        stream = StringIO(content.decode("utf-8-sig"), newline="")
    else:
        stream = path.open(encoding="utf-8-sig", newline="")
    with stream:
        rows = csv.DictReader(stream)
        if not {"task_id", "phone", "message"}.issubset(rows.fieldnames or ()):
            raise PhoneTaskError("CSV 需要 task_id、phone、message 三列")
        for row in rows:
            ident = (row["task_id"] or "").strip()
            number = phone_number(row["phone"] or "")
            body = row["message"] or ""
            if not ident or ident in tasks or not number or not body.strip() or len(body.splitlines()) != 1 or "\n" in body or "\r" in body or "\0" in body:
                raise PhoneTaskError(f"CSV 第 {rows.line_num} 行需要唯一 task_id、有效手机号和单行正文")
            tasks[ident] = Message(ident, number, body)
    return list(tasks.values())


def run_batch(tasks, journal, app=None, send=False):
    journal.execute("CREATE TABLE IF NOT EXISTS outcomes (id TEXT PRIMARY KEY, fingerprint TEXT, status TEXT, reason TEXT)")
    existing = {r[0]: r[1:] for r in journal.execute("SELECT id, fingerprint, status, reason FROM outcomes")}
    unresolved = False
    for task in tasks:
        if task.task_id in existing and existing[task.task_id][0] != task.fingerprint:
            raise PhoneTaskError(f"{task.task_id} 的号码或正文已变化，请使用新 task_id")
    for task in tasks:
        if task.task_id in existing:
            _, status, reason = existing[task.task_id]
            report(task, "待核对" if status == "发送中" else status, reason)
            unresolved |= status != "本机已发"
            continue
        if app is None:
            report(task, "待处理")
            continue
        claimed = False

        def claim():
            nonlocal claimed
            row = journal.execute("INSERT OR IGNORE INTO outcomes VALUES (?, ?, '发送中', NULL)",
                                  (task.task_id, task.fingerprint))
            journal.commit()
            if row.rowcount != 1:
                raise sqlite3.IntegrityError(f"{task.task_id} 已被另一个进程处理，请重新查看进度")
            claimed = True

        status, reason = app.execute(task, send=send, before_send=claim if send else None)
        if not send:
            report(task, status, reason)
            return int(status != "草稿已核对")
        if claimed:
            journal.execute("UPDATE outcomes SET status=?, reason=? WHERE id=?", (status, reason, task.task_id))
        else:
            journal.execute("INSERT OR IGNORE INTO outcomes VALUES (?, ?, ?, ?)",
                            (task.task_id, task.fingerprint, status, reason))
        journal.commit()
        report(task, status, reason)
        unresolved |= status != "本机已发"
    return int(send and unresolved)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="phone_agent.py sms", description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--wda-state-dir", type=Path)
    parser.add_argument("--expected-sha256", help=argparse.SUPPRESS)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--draft", action="store_true", help="在手机准备并核对下一条草稿")
    action.add_argument("--send", action="store_true", help="发送名单中尚未处理的短信")
    args = parser.parse_args(argv)
    journal = args.csv.resolve().with_suffix(".sms.sqlite3")
    try:
        tasks = read_messages(args.csv, args.expected_sha256)
        with closing(sqlite3.connect(journal if args.send or journal.exists() else ":memory:")) as db:
            if not (args.draft or args.send):
                return run_batch(tasks, db)
            with phone_task(args.wda_state_dir) as (phone, _, _):
                return run_batch(tasks, db, MessagesApp(phone), args.send)
    except (OSError, sqlite3.Error, PhoneTaskError, VisionError, WDAError) as error:
        parser.exit(1, f"{error}\n")
