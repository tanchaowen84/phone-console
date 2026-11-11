"""Offline coverage of the two routes and the no-send draft boundary."""

from contextlib import nullcontext
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from desk_models import DeskModels
from phone_runtime import WDAError
from wechat_desk import Desk, is_sent_preview, watch_once
from wechat_tasks import WeChatTasks, outgoing_count, visible_chats


def row(text, x, y, width=.10, height=.02):
    return {"text": text, "rect": [x, y, width, height]}


def chats(preview, name="测试客服"):
    return [row(text, x, .92) for text, x in zip(["微信", "通讯录", "发现", "我"], [.04, .28, .52, .76])] + [
        row(name, .19, .25), row(preview, .19, .277),
    ]


class WeChatDeskTests(unittest.TestCase):
    def test_restart_compares_new_preview_to_previous_scan(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            faq = base / "faq.json"
            faq.write_text("{}")
            args = SimpleNamespace(faq=faq, contact=["样例联系人～"], mode="terra")
            path = base / "dialogue.jsonl"
            tasks = WeChatTasks(Mock(), base / "screen.png")
            first = Desk(args, Mock(), path)
            with patch.object(tasks, "observe", return_value=chats("旧预览", "样例联系人~")):
                first.scan(tasks)
            second = Desk(args, Mock(), path)
            with patch.object(tasks, "observe", return_value=chats("中断期间的新消息", "样例联系人~")), patch.object(
                    second, "choose", return_value="uncertain") as choose:
                second.scan(tasks)
            self.assertEqual(choose.call_args.args[0], "new_message")
            self.assertEqual(choose.call_args.args[2]["previous"][0], "旧预览")

    def test_contact_filter_is_optional_and_normalizes_ocr_width(self):
        rows = chats("周末几点营业", "样例联系人~")
        self.assertIn("样例联系人～", visible_chats(rows, ["样例联系人～"]))
        self.assertIn("样例联系人~", visible_chats(rows, []))
        self.assertEqual(visible_chats(rows, ["其他联系人"]), {})

    def run_desk(self, mode, contact, revised=None, uncertain_tap=False):
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            faq = base / "faq.json"
            faq.write_text(json.dumps({"周末营业吗": "周末十点营业"}))
            phone = Mock()
            if uncertain_tap:
                phone.post.side_effect = [WDAError("action_uncertain", "Timed out", uncertain=True), None]
            changed = chats("周末营业吗", contact)
            models = Mock()
            models.draft_reply.return_value = ("reply", "周末十点营业")
            desk = Desk(SimpleNamespace(faq=faq, contact=[contact], mode=mode, send_to="另一个人"), models,
                        base / "drafts.jsonl")
            tasks = WeChatTasks(phone, base / "screen.png")
            if revised is None:
                desk.output.write_text(json.dumps({"contact":contact,"status":"sent","text":"请问要咨询什么？"}) + "\n")
            initial = chats("昨天的问题", contact)
            fresh = revised if revised is not None else changed
            conversation = [row(contact, .4, .075), row("周末营业吗", .15, .52)]
            frames = [initial, initial, changed, fresh, conversation, conversation, changed]
            with patch("wechat_tasks.time.sleep"), patch.object(tasks, "observe", side_effect=frames), patch.object(
                    tasks, "viewport", return_value={"width": 400, "height": 850}), patch.object(
                    desk, "choose", side_effect=["new", "open"]) as choose:
                desk.scan(tasks)
                desk.scan(tasks)
                choose.assert_not_called()
                desk.scan(tasks)
                self.assertEqual([(call.args[0], call.args[1]) for call in choose.call_args_list],
                                 [("new_message", "decisions"), ("open_chat", mode)])
            if revised is not None:
                phone.post.assert_not_called()
                self.assertFalse(desk.output.exists())
                self.assertEqual(desk.previews[contact][0], "昨天的问题")
                return
            output = json.loads(desk.output.read_text().splitlines()[-1])
            self.assertEqual(output["contact"], contact)
            self.assertEqual(output["status"], "draft")
            self.assertEqual(output["reply"], "周末十点营业")
            self.assertEqual(output["locator"], "ocr")
            self.assertEqual(models.draft_reply.call_args.args[0], [
                {"role":"service","text":"请问要咨询什么？"}, {"role":"customer","text":"周末营业吗"},
            ])
            self.assertEqual([call.args[0] for call in phone.post.call_args_list], ["/wda/tap", "/wda/tap"])
            phone.bounded_tree.assert_not_called()

    def test_shared_watcher_routes_both_execution_versions_to_a_local_draft(self):
        for mode, contact in (("terra", "测试甲"), ("decisions", "测试乙")):
            with self.subTest(mode=mode):
                self.run_desk(mode, contact)

    def test_changed_row_between_model_and_tap_is_not_opened_or_acknowledged(self):
        self.run_desk("decisions", "测试甲", revised=chats("又有新消息", "测试甲"))

    def test_uncertain_click_is_verified_from_chat_without_replaying_it(self):
        self.run_desk("terra", "测试甲", uncertain_tap=True)

    def test_send_requires_empty_composer_and_a_new_outgoing_bubble(self):
        for confirmed in (True, False):
            with self.subTest(confirmed=confirmed), TemporaryDirectory() as tmp:
                self.run_send_and_resume(tmp, confirmed)

    def run_send_and_resume(self, tmp, confirmed):
        p = Path(tmp)
        faq = p / "faq.json"
        faq.write_text("{}")
        contact = "样例联系人～"
        phone = Mock()
        phone.focused_field.return_value = ("/element/editor", "XCUIElementTypeTextView")
        reply = "二店周末十点开门。"
        phone.client.session.side_effect = ["", None, reply, ""]
        models = Mock()
        models.draft_reply.return_value = ("reply", reply)
        desk = Desk(SimpleNamespace(faq=faq, mode="terra", contact=[contact], send_to=contact), models, p / "ledger")
        scratch = p / "screen.png"
        device = Mock()
        device.acquire.side_effect = lambda: nullcontext(phone)
        header, inbound = row("样例联系人~", .4, .075), row("二店", .20, .48)
        changed = chats("二店", "样例联系人~")
        receipt = [header, row(reply, .57, .48, .33)] if confirmed else [header, inbound]
        restored = chats(reply if confirmed else "二店", "样例联系人~")
        frames = [chats("上次的消息", "样例联系人~"), changed, changed, [header, inbound], [header, inbound], [header],
                  [header, row("发送", .82, .90)], receipt, receipt, restored, restored]

        def statuses():
            return [json.loads(line)["status"] for line in desk.output.read_text().splitlines()]

        def tap(path, point):
            if phone.post.call_count == 3:
                self.assertEqual(statuses()[-1], "send_pending")

        phone.post.side_effect = tap
        with patch("wechat_tasks.time.sleep"), patch.object(WeChatTasks, "observe", side_effect=frames), patch.object(
                WeChatTasks, "viewport", return_value={"width": 390, "height": 844}), patch.object(
                desk, "choose", side_effect=["new", "open"]):
            watch_once(desk, device, scratch)
            watch_once(desk, device, scratch)
            watch_once(desk, device, scratch)
        self.assertEqual(statuses(), ["incoming", "send_pending", "sent" if confirmed else "send_uncertain"])
        self.assertEqual(phone.post.call_count, 4)
        self.assertEqual(device.acquire.call_count, 3)
        self.assertEqual(desk.status, "watching")
        self.assertEqual([call.args[:2] for call in phone.client.session.call_args_list],
                         [("GET","/element/editor/attribute/value"),("POST","/wda/keys"),
                          ("GET","/element/editor/attribute/value"),
                          ("GET","/element/editor/attribute/value")])

    def test_wrap_and_observed_ocr_variations_keep_one_receipt(self):
        rows = [row("请问您想咨询一店还是", .195, .462, .604),
                row("二店周未营业时间？", .195, .484, .462)]
        self.assertEqual(outgoing_count(rows, "请问您想咨询一店还是二店的周末营业时间？"), 1)

    def test_truncated_outgoing_preview_is_not_a_new_incoming_message(self):
        reply = "请问您想咨询一店还是二店的周末营业时间？"
        self.assertTrue(is_sent_preview("请问您想咨询一店还是二店…", reply))
        self.assertFalse(is_sent_preview("二店", reply))

    def test_provider_paths_keep_the_selected_models_and_output_contracts(self):
        models = DeskModels("fake-key", Mock())
        responses = [
            {"answers": [{"name": "decision", "choice": "open"}]},
            {"output": [{"type": "message", "content": [{"type": "output_text", "text": '{"choice":"open"}'}]}]},
        ]
        with patch.object(models, "post", side_effect=responses) as post:
            for provider in ("decisions", "terra"):
                self.assertEqual(models.choose("open_chat", provider, "data:image/png;base64,AA==", {},
                                               {"open": "Open", "handoff": "Handoff"}), "open")
        self.assertEqual([(call.args[0], call.args[1]["model"]) for call in post.call_args_list], [
            ("/v1/decisions", "gpt-6-luna"), ("/v1/responses", "gpt-6.1-terra"),
        ])
