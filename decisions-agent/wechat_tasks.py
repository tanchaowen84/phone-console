"""Finite WeChat screen tasks run by the shared phone execution loop."""

import base64
from difflib import SequenceMatcher
import json
import re
import subprocess
import time
import unicodedata

from phone_core import Result, Step
from phone_plugins import WDAInput, wda_core
from phone_runtime import ROOT, WDAError, fill_focused
from wda_image import model_image, png_size


def center(box):
    x, y, width, height = box["rect"]
    return x + width / 2, y + height / 2


def normalized_name(text):
    return unicodedata.normalize("NFKC", text).strip()


def is_chat_list(rows):
    tabs = {"微信", "通讯录", "发现", "我", "Chats", "Contacts", "Discover", "Me"}
    return len({row["text"] for row in rows if center(row)[1] > .86} & tabs) >= 3


def chat_header(rows, contact):
    titles = [row for row in rows if normalized_name(row["text"]) == normalized_name(contact) and
              .035 < center(row)[1] < .14 and .25 < center(row)[0] < .75]
    return titles[0] if len(titles) == 1 and not is_chat_list(rows) else None


def outgoing_count(rows, reply):
    def plain(text):
        return re.sub(r"[\W_]", "", normalized_name(text)).casefold()

    expected = plain(reply)
    lines = sorted((row for row in rows if .14 < center(row)[1] < .88), key=lambda row: center(row)[1])
    count = 0
    for i, row in enumerate(lines):
        x, _, width, _ = row["rect"]
        if x + width < .75:
            continue
        text = plain(row["text"])
        previous_y = center(row)[1]
        for following in lines[i+1:]:
            next_y = center(following)[1]
            if next_y - previous_y > .035 or abs(following["rect"][0] - x) > .025:
                break
            text += plain(following["text"])
            previous_y = next_y
        # WeChat wraps long replies, and Vision can miss a character in a wrapped line.
        if text == expected or (len(expected) >= 12 and text[:6] == expected[:6]
                                and SequenceMatcher(None, text, expected).ratio() >= .9):
            count += 1
    return count


def visible_chats(rows, contacts):
    chats = {}
    title_rows = [row for row in rows if .13 < center(row)[1] < .85 and .13 < row["rect"][0] < .46]
    if not contacts:
        contacts = dict.fromkeys(row["text"] for row in title_rows)
    for contact in contacts:
        titles = [row for row in title_rows if normalized_name(row["text"]) == normalized_name(contact)]
        if len(titles) != 1:
            continue
        title = titles[0]
        x, y, width, height = title["rect"]
        previews = [row for row in rows if abs(row["rect"][0] - x) <= .03 and
                    y + height * .6 < row["rect"][1] < y + height + .024 and row["rect"][0] < .65]
        if len(previews) == 1:
            badges = [row["text"] for row in rows if .09 < center(row)[0] < x - .008
                      and y - height * .85 < center(row)[1] < y + height * .35
                      and re.fullmatch(r"[1-9][0-9]{0,2}\+?", row["text"])]
            chats[contact] = (title, previews[0]["text"], badges[0] if len(badges) == 1 else None)
    return chats


class WeChatTasks:
    def __init__(self, phone, scratch, *, core=None):
        self.phone = phone
        self.scratch = scratch
        self.core = core if core is not None else wda_core(
            phone, controls=WDAInput(phone, viewport=lambda: self.viewport())
        )

    def observe(self, screen):
        self.scratch.write_bytes(base64.b64decode(screen.frame))
        return json.loads(subprocess.check_output([str(ROOT / "ocr-local"), str(self.scratch)]))

    def row_image(self, title):
        with self.scratch.open("rb") as stream:
            width, height = png_size(stream.read(24))
        _, y, _, title_height = title["rect"]
        top = max(0, round((y - title_height) * height))
        bottom = min(height, round((y + title_height + .037) * height))
        row_image = self.scratch.with_name("row.png")
        row_width = round(width * .84)
        subprocess.run(["/usr/bin/sips", "-c", str(bottom - top), str(row_width),
                        "--cropOffset", str(top), "0", str(self.scratch), "--out", str(row_image)],
                       check=True, stdout=subprocess.DEVNULL)
        with row_image.open("rb") as stream:
            if png_size(stream.read(24)) != (row_width, bottom - top):
                raise RuntimeError("会话截图裁剪失败")
        image, mime, _, _ = model_image(row_image)
        return f"data:{mime};base64,{base64.b64encode(image.read_bytes()).decode()}"

    def viewport(self):
        with self.scratch.open("rb") as stream:
            width, height = png_size(stream.read(24))
        screens = self.phone.client.request("GET", "/wda/screens")["value"]
        scale = next(screen["scale"] for screen in screens if screen["isMain"])
        if width >= height:
            raise RuntimeError("请让微信保持竖屏")
        return {"width": width / scale, "height": height / scale}

    def tap(self, point):
        try:
            self.core.controls.tap(*point)
        except WDAError as exc:
            if not exc.uncertain:
                raise
            print("点击响应不确定，按当前页面核对结果", flush=True)
            time.sleep(2)
        else:
            time.sleep(.5)

    def scan(self, contacts):
        def decide(screen):
            rows = self.observe(screen)
            return visible_chats(rows, contacts) if is_chat_list(rows) else None

        return self.core.run(decide)

    def read_message(self, contact, preview):
        opened = False

        def open_row(point):
            nonlocal opened
            self.tap(point)
            opened = True

        def decide(screen):
            rows = self.observe(screen)
            if not opened:
                fresh = visible_chats(rows, [contact]) if is_chat_list(rows) else {}
                if contact not in fresh or fresh[contact][1] != preview:
                    return Result("changed")
                return Step(lambda: open_row(center(fresh[contact][0])))
            if not chat_header(rows, contact):
                return Result("wrong_chat", "点击后未核对到目标聊天标题")
            for row in rows:
                if row["text"] == preview and .14 < center(row)[1] < .88 and .09 < row["rect"][0] < .38:
                    return row["text"]
            return Result("unreadable", "无法核对入站文字")

        return self.core.run(decide)

    def reply(self, contact, incoming, text, before_send):
        phase = "focus"
        before = 0
        value_path = None

        def tap_then(point, next_phase):
            nonlocal phase
            self.tap(point)
            phase = next_phase

        def fill():
            nonlocal value_path, phase
            value_path = fill_focused(self.phone, text)
            phase = "send"

        def decide(screen):
            nonlocal before
            if phase == "send" and value_path is None:
                return Result("draft", "输入栏已有文字")
            rows = self.observe(screen)
            header = chat_header(rows, contact)
            if phase == "focus":
                visible = [row for row in rows if .14 < center(row)[1] < .88 and .17 < row["rect"][0] < .27]
                if not header or not visible or max(visible, key=lambda row: center(row)[1])["text"] != incoming:
                    return Result("draft", "拟稿期间会话或最新来信变化")
                before = outgoing_count(rows, text)
                return Step(lambda: tap_then((.43, .925), "fill"))
            if phase == "fill":
                return Step(fill) if header else Result("draft", "输入前目标会话核对失败")
            if phase == "send":
                buttons = [row for row in rows if row["text"] in ("发送", "Send")
                           and center(row)[0] > .72 and center(row)[1] > .63]
                if not header or len(buttons) != 1:
                    return Result("draft", "未唯一核对到目标会话和发送按钮")
                return Step(lambda: tap_then(center(buttons[0]), "confirm"), commits=True)
            empty = self.phone.client.session("GET", value_path) in ("", None)
            if empty and header and outgoing_count(rows, text) > before:
                return Result("sent")
            return Result("send_uncertain", "发送结果未确认")

        return self.core.run(decide, before_send)

    def return_to_list(self, contact):
        tapped = False

        def go_back(header):
            nonlocal tapped
            self.tap((.055, center(header)[1]))
            tapped = True

        def decide(screen):
            rows = self.observe(screen)
            if is_chat_list(rows):
                return True
            header = chat_header(rows, contact)
            if tapped or not header:
                return False
            return Step(lambda: go_back(header))

        return self.core.run(decide)
