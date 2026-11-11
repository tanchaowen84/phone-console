"""Messages app actions and screen checks for one recipient."""

import json
import re
import time

from phone_core import Result, Step
from phone_plugins import wda_core
from phone_runtime import PhoneTaskError, WDAError, fill_focused
from sms_vision import VisionError, decide

MESSAGES = "com.apple.MobileSMS"
EDITORS = {"XCUIElementTypeTextView", "XCUIElementTypeTextField"}
SEND_LABELS = {"发送", "Send", "sendButton"}
FAILURE_BUTTONS = {"发送失败", "Send Failure", "Not Delivered"}
SEND_AS_SMS = {"作为短信发送", "Send as Text Message"}
RECIPIENT_LABELS = {"To:", "To", "收件人", "收件人:", "收件人："}
EMPTY_COMPOSER = {"", "信息", "iMessage信息", "信息 · RCS", "信息 · 短信", "iMessage", "Text Message"}


class DeliveryError(PhoneTaskError):
    pass


def phone_number(value):
    compact = re.sub(r"[\s().-]", "", value)
    return compact if re.fullmatch(r"\+?[0-9]{7,15}", compact) else None


def point(node):
    rect = node["rect"]
    return {"x": rect["x"] + rect["width"] / 2, "y": rect["y"] + rect["height"] / 2}


def recipient_number(value):
    number = phone_number(value)
    if number and re.fullmatch(r"\+861[3-9][0-9]{9}", number):
        return number[3:]
    return number.lstrip("+") if number else None


def matches_repeated_recipient(field, nodes, number):
    value = (field.get("value") or "").strip()
    parts = re.split(r"[,，]", value)
    if not number or len(parts) != 2 or any(recipient_number(part) != number for part in parts):
        return False
    bounds = field["rect"]
    buttons = []
    for node in nodes:
        if node["type"] != "XCUIElementTypeButton":
            continue
        rect = node["rect"]
        if (0 < rect["width"] and 0 < rect["height"]
                and bounds["x"] <= rect["x"] and rect["x"] + rect["width"] <= bounds["x"] + bounds["width"]
                and bounds["y"] <= rect["y"] and rect["y"] + rect["height"] <= bounds["y"] + bounds["height"]):
            buttons.append(node)
    return len(buttons) == 1 and (buttons[0].get("label") or "").strip() == value


def verify_recipient(nodes, number):
    to_fields = [node for node in nodes if node["type"] == "XCUIElementTypeTextField"
                 and RECIPIENT_LABELS.intersection((node.get("name"), node.get("label")))]
    recipient_nodes = to_fields or [node for node in nodes if node.get("name") == "ConversationTitle"]
    expected = recipient_number(number)
    recipients = {value for node in recipient_nodes for key in ("label", "value")
                  if (value := recipient_number(node.get(key) or ""))}
    if len(to_fields) == 1 and not recipients and matches_repeated_recipient(to_fields[0], nodes, expected):
        recipients = {expected}
    if len(to_fields) > 1 or recipients != {expected}:
        raise PhoneTaskError("界面上未核对到唯一且完整的收件人号码")


def conversation_number(nodes):
    title = next((node for node in reversed(nodes) if node.get("name") == "ConversationTitle"), {})
    return recipient_number(title.get("label") or "")


def active_conversation(nodes):
    start = max((index for index, node in enumerate(nodes) if node.get("name") == "ConversationTitle"), default=0)
    return nodes[start:]


def message_editors(nodes):
    return [node for node in nodes if node["type"] in EDITORS and node.get("name") == "messageBodyField"]


def fields(nodes, number):
    verify_recipient(nodes, number)
    editors = message_editors(nodes)
    if len(editors) != 1:
        raise PhoneTaskError("未核对到唯一短信输入栏")
    return editors[0]


def bubble_count(nodes, viewport, message, editor_y):
    return len({tuple(point(node).values()) for node in nodes if node.get("name") != "messageBodyField"
                and message in (node.get("label"), node.get("value"))
                and viewport["height"] * .15 < point(node)["y"] < editor_y
                and node["rect"]["x"] + node["rect"]["width"] > viewport["width"] * .7})


def send_buttons(nodes, viewport, editor):
    center = point(editor)
    return {tuple(point(node).values()) for node in nodes if node["type"] == "XCUIElementTypeButton"
            and SEND_LABELS.intersection((node.get("label"), node.get("name")))
            and node.get("enabled") != "false" and point(node)["x"] > viewport["width"] * .7
            and abs(point(node)["y"] - center["y"]) < editor["rect"]["height"]}


def failed_message_buttons(nodes, message):
    bodies = [node for node in nodes if node.get("name") != "messageBodyField"
              and message in (node.get("label"), node.get("value"))]
    return [node for node in nodes if node["type"] == "XCUIElementTypeButton"
            and FAILURE_BUTTONS.intersection((node.get("label"), node.get("name")))
            and any(body["rect"]["y"] <= point(node)["y"] <= body["rect"]["y"] + body["rect"]["height"]
                    for body in bodies)]


class MessagesApp:
    def __init__(self, phone, *, core=None):
        self.phone = phone
        self.core = core if core is not None else wda_core(phone)
        self.last_recipient = None

    def inspect(self, message, screen=None):
        nodes, viewport = screen.tree if screen is not None else self.phone.tree()
        editor = fields(nodes, message.phone)
        self.last_recipient = message.phone
        return nodes, viewport, editor

    def open_recipient(self, message):
        self.phone.client.session("POST", "/url", {"url": f"sms:{message.phone}"})
        deadline = time.monotonic() + 12
        while True:
            time.sleep(.5)
            try:
                return self.inspect(message)
            except PhoneTaskError:
                if time.monotonic() >= deadline:
                    raise

    def prepare_once(self, message, require_send, visual_focus=None, screen=None):
        if visual_focus:
            nodes, viewport = screen.tree if screen is not None else self.phone.tree()
            verify_recipient(nodes, message.phone)
            target = {"x": viewport["width"] * visual_focus["x"] / 100,
                      "y": viewport["height"] * visual_focus["y"] / 100}
            candidates = []
            for candidate in message_editors(nodes):
                rect = candidate["rect"]
                if (rect["x"] <= target["x"] < rect["x"] + rect["width"] / 2
                        and rect["y"] <= target["y"] < rect["y"] + rect["height"]):
                    candidates.append(candidate)
            if not candidates:
                raise PhoneTaskError("画面上的点击位置不在当前正文输入栏")
            editor = min(candidates, key=lambda node: abs(point(node)["y"] - target["y"]))
        elif self.last_recipient and self.last_recipient != message.phone:
            nodes, viewport, editor = self.open_recipient(message)
            target = point(editor)
        else:
            try:
                nodes, viewport, editor = self.inspect(message, screen)
            except PhoneTaskError:
                nodes, viewport, editor = self.open_recipient(message)
            target = point(editor)
        value = editor.get("value") or ""
        if value != message.text:
            if send_buttons(nodes, viewport, editor):
                raise PhoneTaskError("短信输入栏已有草稿")
            self.phone.post("/wda/tap", target)
            time.sleep(.5 if visual_focus else .2)
            if value in EMPTY_COMPOSER:
                self.phone.client.session("POST", "/wda/keys", {"value": [message.text], "frequency": 30})
            elif fill_focused(self.phone, message.text, reuse_exact=True, allow_placeholder=True) is None:
                raise PhoneTaskError("短信输入栏已有草稿")
            nodes, viewport = self.phone.tree()
        verify_recipient(nodes, message.phone)
        drafts = [editor for editor in message_editors(nodes) if editor.get("value") == message.text]
        if not drafts:
            raise PhoneTaskError("核对时发现短信正文已变化")
        self.last_recipient = message.phone
        if not require_send:
            return None
        buttons = set().union(*(send_buttons(nodes, viewport, editor) for editor in drafts))
        if len(buttons) != 1:
            raise PhoneTaskError("未核对到唯一短信发送按钮")
        x, y = buttons.pop()
        editor_y = min(point(editor)["y"] for editor in drafts)
        before = bubble_count(active_conversation(nodes), viewport, message.text, editor_y)
        before_frame = self.phone.client.request("GET", "/screenshot")["value"] if before else None
        return {"x": x, "y": y}, before, before_frame

    def execute(self, message, send=False, before_send=None):
        prepared = None
        phase = "prepare"
        preparation_error = None
        history = []
        retry_result = None

        def prepare(screen, choice=None):
            nonlocal prepared, phase, preparation_error
            action = choice["action"] if choice else None
            try:
                if action == "open_recipient":
                    self.open_recipient(message)
                elif action == "wait":
                    time.sleep(.5)
                prepared = self.prepare_once(message, send, choice if action == "tap_composer" else None, screen)
                phase = "send" if send else "draft"
            except (PhoneTaskError, WDAError) as error:
                if isinstance(error, WDAError) and error.code not in ("no_focused_field", "action_uncertain"):
                    raise
                preparation_error = error

        def retry():
            nonlocal retry_result
            retry_result = send_result(self.retry_delivery(message, prepared[2]))

        def next_step(screen):
            if phase == "prepare":
                if preparation_error is None:
                    return Step(lambda: prepare(screen))
                issue = getattr(preparation_error, "code", str(preparation_error))
                choice = decide(screen.frame, message, "prepare", issue, history)
                action = choice["action"]
                record = {"issue": issue, "action": action, "source": choice.get("source")}
                if action == "unresolved" or record in history:
                    return Result("失败", str(preparation_error))
                history.append(record)
                report(message, "看图修复", "本机 OCR 定位正文输入栏" if choice.get("source") == "local_ocr" else action)
                return Step(lambda: prepare(screen, choice))
            if phase == "draft":
                return Result("草稿已核对")
            if phase == "send":
                return Step(dispatch, commits=True)
            if retry_result is not None:
                return retry_result
            try:
                return send_result(self.confirm_send(message, prepared, screen))
            except DeliveryError:
                return Step(retry)

        def dispatch():
            nonlocal phase
            phase = "confirm"
            self.tap_send(prepared[0])

        try:
            return self.core.run(next_step, before_send)
        except DeliveryError as error:
            return Result("未送达", str(error))
        except (WDAError, VisionError, PhoneTaskError) as error:
            if phase == "confirm":
                return Result("待核对", str(error))
            if isinstance(error, PhoneTaskError):
                return Result("失败", str(error))
            raise

    def tap_send(self, button):
        try:
            self.phone.post("/wda/tap", button)
        except WDAError as error:
            if not error.uncertain:
                raise
            time.sleep(2)
        else:
            time.sleep(.5)

    def confirm_send(self, message, prepared, screen):
        _, before, before_frame = prepared
        for delay in (0, 1):
            if delay:
                time.sleep(delay)
            try:
                if self.sent_on_screen(message, before, None if delay else screen):
                    return True
            except DeliveryError:
                raise
            except PhoneTaskError:
                pass
        history = []
        while True:
            frame = self.core.observe().frame
            choice = decide(frame, message, "after_send", "当前界面未确认新发出的消息", history, before_frame)
            action = choice["action"]
            if action == "sent":
                report(message, "看图确认", "找到目标会话里的发出气泡")
                return True
            if action == "delivery_error":
                raise DeliveryError("手机显示尚未送达")
            try:
                if self.sent_on_screen(message, before):
                    return True
            except DeliveryError:
                raise
            except PhoneTaskError:
                pass
            if action != "wait" or history:
                return False
            history.append({"action": action})
            time.sleep(.5)

    def sent_on_screen(self, message, before, screen=None):
        nodes, viewport = screen.tree if screen is not None else self.phone.tree()
        if conversation_number(nodes) != recipient_number(message.phone):
            verify_recipient(nodes, message.phone)
        nodes = active_conversation(nodes)
        if failed_message_buttons(nodes, message.text):
            raise DeliveryError("手机显示尚未送达")
        editor_y = min((point(editor)["y"] for editor in message_editors(nodes)), default=viewport["height"])
        return bubble_count(nodes, viewport, message.text, editor_y) > before

    def open_delivery_options(self, message, before_frame=None):
        nodes, viewport = self.phone.tree()
        expected = recipient_number(message.phone)
        if conversation_number(nodes) != expected:
            return False
        buttons = failed_message_buttons(active_conversation(nodes), message.text)
        if not buttons:
            return False
        if len(buttons) == 1 and not before_frame:
            self.phone.post("/wda/tap", point(buttons[0]))
            time.sleep(.5)
            return True
        choice = decide(self.core.observe().frame, message, "delivery_error", "iPhone 显示尚未送达", [], before_frame)
        if choice["action"] != "tap_failure":
            return False
        target = {"x": viewport["width"] * choice["x"] / 100,
                  "y": viewport["height"] * choice["y"] / 100}
        for button in buttons:
            rect = button["rect"]
            if (rect["x"] <= target["x"] <= rect["x"] + rect["width"]
                    and rect["y"] <= target["y"] <= rect["y"] + rect["height"]):
                self.phone.post("/wda/tap", point(button))
                time.sleep(.5)
                return True
        return False

    def retry_delivery(self, message, before_frame=None, options_open=False):
        if not options_open and not self.open_delivery_options(message, before_frame):
            raise DeliveryError("手机显示尚未送达")
        nodes, _ = self.phone.tree()
        if conversation_number(nodes) != recipient_number(message.phone):
            raise DeliveryError("短信补发菜单下未核对到目标会话")
        options = [node for node in nodes if node["type"] == "XCUIElementTypeButton"
                   and SEND_AS_SMS.intersection((node.get("label"), node.get("name")))]
        if len(options) != 1:
            raise DeliveryError("手机未提供可用的短信补发选项")
        self.phone.post("/wda/tap", point(options[0]))
        report(message, "作为短信补发")
        time.sleep(2)
        return self.sent_on_screen(message, 0)


def send_result(confirmed):
    return Result("本机已发") if confirmed else Result("待核对", "发送后未核对到新气泡")


def report(task, status, reason=None):
    print(json.dumps({"task_id": task.task_id, "status": status, **({"reason": reason} if reason else {})},
                     ensure_ascii=False), flush=True)
