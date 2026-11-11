"""Long-running WeChat support plugin for the Mac phone agent."""

import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from desk_models import DeskModels
from phone_runtime import PhoneBusy, PhoneDevice, PhoneTaskError, WDAError
from wechat_tasks import WeChatTasks, normalized_name


def is_sent_preview(preview, reply):
    preview, reply = normalized_name(preview), normalized_name(reply)
    return preview == reply or (preview.endswith("...") and reply.startswith(preview[:-3]))


class Desk:
    def __init__(self, args, models, output):
        self.args, self.models, self.output = args, models, output
        self.checkpoint = output.with_suffix(".watch.json")
        state = json.loads(self.checkpoint.read_text()) if self.checkpoint.is_file() else None
        self.previews = {name: tuple(value) for name, value in state["previews"].items()} if state else {}
        self.sent_previews = state["sent_previews"] if state else {}
        self.baselined = state is not None
        self.status = None
        self.faq = json.loads(args.faq.read_text())
        if not isinstance(self.faq, dict) or any(
                not question.strip() or not isinstance(reply, str) or not reply.strip()
                for question, reply in self.faq.items()):
            raise ValueError("知识库必须是「主题：已审核事实」的 JSON 对象")

    def choose(self, stage, provider, evidence, choices, tasks, title):
        return self.models.choose(stage, provider, tasks.row_image(title), evidence, choices)

    def record(self, contact, status, **detail):
        with open(self.output, "a", encoding="utf-8", opener=private_open) as stream:
            stream.write(json.dumps({"contact": contact, "status": status, "mode": self.args.mode, **detail},
                                    ensure_ascii=False) + "\n")
        print(json.dumps({"event": status, "contact": contact}, ensure_ascii=False), flush=True)

    def history(self, contact):
        if not self.output.is_file():
            return [], False
        turns, pending = [], False
        with self.output.open(encoding="utf-8") as stream:
            for line in stream:
                entry = json.loads(line)
                if normalized_name(entry["contact"]) != normalized_name(contact):
                    continue
                status = entry["status"]
                if status in ("incoming", "sent"):
                    turns.append({"role": "customer" if status == "incoming" else "service", "text": entry["text"]})
                if status in ("send_pending", "send_uncertain", "sent"):
                    pending = status != "sent"
        return turns[-12:], pending

    def handle_message(self, tasks, contact, preview, title):
        choice = self.choose("open_chat", self.args.mode, {"contact": contact, "preview": preview}, {
            "open": "Open the exact contact only if the expected row is visible and matches.",
            "handoff": "The exact contact is uncertain or the screen is obstructed.",
        }, tasks, title)
        if choice == "handoff":
            self.record(contact, "handoff", reason="目标会话无法确认", preview=preview)
            return True

        incoming = tasks.read_message(contact, preview)
        if isinstance(incoming, str):
            self.handle_reply(tasks, contact, incoming)
        elif incoming.status == "changed":
            return False
        else:
            self.record(contact, "handoff", locator="ocr", preview=preview, reason=incoming.reason)
            if incoming.status == "wrong_chat":
                return False
        if not tasks.return_to_list(contact):
            self.report_status("waiting_for_wechat_list")
        return True

    def handle_reply(self, tasks, contact, text):
        turns, pending = self.history(contact)
        incoming = {"role": "customer", "text": text}
        self.record(contact, "incoming", locator="ocr", text=text)
        print(json.dumps({"event": "reply_context", "prior_turns": len(turns),
                          "prior_sent": sum(turn["role"] == "service" for turn in turns)}), flush=True)
        kind, reply = self.models.draft_reply([*turns, incoming], self.faq)
        if kind == "handoff":
            self.record(contact, "handoff", reason=reply)
        elif self.args.send_to and normalized_name(self.args.send_to) == normalized_name(contact) and not pending:
            result = tasks.reply(contact, text, reply, lambda: self.record(contact, "send_pending", text=reply))
            if result.status == "sent":
                self.record(contact, "sent", text=reply)
                self.sent_previews[contact] = reply
            elif result.status == "send_uncertain":
                self.record(contact, "send_uncertain", text=reply, reason=result.reason)
            else:
                self.record(contact, "draft", reply=reply, reason=result.reason)
        else:
            self.record(contact, "draft", locator="ocr", question=text, reply=reply)

    def save_checkpoint(self):
        temporary = self.checkpoint.with_suffix(".tmp")
        with open(temporary, "w", encoding="utf-8", opener=private_open) as stream:
            json.dump({"previews": self.previews, "sent_previews": self.sent_previews}, stream, ensure_ascii=False)
        temporary.replace(self.checkpoint)

    def scan(self, tasks):
        chats = tasks.scan(self.args.contact)
        if chats is None:
            self.report_status("waiting_for_wechat_list")
            return
        self.report_status("watching" if chats else "waiting_for_visible_chat")
        if not self.baselined and chats:
            self.previews.update({contact: (preview, badge) for contact, (_, preview, badge) in chats.items()})
            self.baselined = True
            self.save_checkpoint()
            return
        previous = self.previews.copy()
        for contact, (title, preview, badge) in chats.items():
            before = self.previews.get(contact)
            current = (preview, badge)
            outgoing = self.sent_previews.get(contact)
            if outgoing and not badge and is_sent_preview(preview, outgoing):
                self.previews[contact] = current
                continue
            if (before and preview != before[0]) or (badge and (not before or badge != before[1])):
                verdict = self.choose("new_message", "decisions", {
                    "contact": contact, "previous": before, "preview": preview, "possible_unread_badge": badge,
                }, {
                    "new": "The changed chat preview is evidence of a new incoming message.",
                    "unrelated": "The change is unrelated to an incoming message, such as typing or an outgoing message.",
                    "uncertain": "The change might be incoming but the screenshot and OCR do not establish it.",
                }, tasks, title)
                if verdict == "new":
                    self.report_status("processing")
                    if self.handle_message(tasks, contact, preview, title):
                        self.previews[contact] = current
                        self.save_checkpoint()
                    return  # A tap can reorder the list. Never reuse its earlier screenshot.
                if verdict == "uncertain":
                    self.record(contact, "handoff", preview=preview, reason="无法确认是否为新来信")
            self.previews[contact] = current
        if self.previews != previous:
            self.save_checkpoint()

    def report_status(self, status):
        if status != self.status:
            self.status = status
            print(json.dumps({"event": status}), flush=True)


def private_open(path, flags):
    return os.open(path, flags, 0o600)


def watch_once(desk, device, scratch):
    try:
        with device.acquire() as phone:
            try:
                desk.scan(WeChatTasks(phone, scratch))
            except WDAError as exc:
                if exc.code not in ("pua_unreachable", "pua_timeout"):
                    raise
                route = next((row["endpoint"] for row in reversed(phone.client.records) if row["error"]), "")
                desk.report_status(f"wda_read_interrupted:{exc.code}:{route}")
    except PhoneBusy:
        desk.report_status("waiting_for_phone")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["terra", "decisions"])
    parser.add_argument("--contact", default=[], action="append", help="可重复指定；省略时监控可见会话")
    parser.add_argument("--knowledge", "--faq", dest="faq", required=True, type=Path, help="已审核知识的 JSON 文件")
    parser.add_argument("--send-to", help="只对这个唯一测试会话启用实发")
    parser.add_argument("--wda-state-dir", type=Path, help="手机对应的 iPhone Use 状态目录")
    parser.add_argument("--poll", type=float, default=3, help="每次扫描后等待的秒数")
    args = parser.parse_args(argv)
    if not 0 < args.poll < float("inf"):
        parser.error("--poll 必须是有限正数")
    key = os.environ.pop("OPENAI_API_KEY", None)
    if not key:
        parser.error("当前进程需要已授权的 OPENAI_API_KEY")
    models = DeskModels(key, lambda row: print(json.dumps(row, ensure_ascii=False), flush=True))
    try:
        device = PhoneDevice(args.wda_state_dir)
        with device.exclusive("watch"), TemporaryDirectory(prefix="wechat-desk-") as tmp:
            output = device.storage / f"{device.udid}-dialogue.jsonl"
            desk = Desk(args, models, output)
            scratch = Path(tmp) / "screen.png"
            print(f"值守输出：{output}", flush=True)
            while True:
                watch_once(desk, device, scratch)
                time.sleep(args.poll)
    except KeyboardInterrupt:
        pass
    except PhoneTaskError as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
