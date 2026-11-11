import sqlite3
import unittest
from unittest.mock import Mock, patch

from messages_task import MESSAGES, MessagesApp, bubble_count, fields
from phone_runtime import PhoneTaskError
from sms_batch import Message, run_batch


class SmsBatchTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.tasks = [Message(str(i), f"+1202555010{i}", "hello") for i in range(3)]

    def messages_app(self):
        app = MessagesApp(Mock())
        app.prepare_once = Mock(return_value=({"x": 300, "y": 440}, 0, None))
        app.tap_send = Mock()
        app.confirm_send = Mock(return_value=True)
        return app

    @patch("sms_batch.report")
    def test_resume_skips_confirmed_and_uncertain_sends(self, report):
        app = self.messages_app()
        app.confirm_send.side_effect = [True, False, True]
        self.assertEqual(run_batch(self.tasks, self.db, app, send=True), 1)
        self.assertEqual(run_batch(self.tasks, self.db, app, send=True), 1)
        self.assertEqual([call.args[0].task_id for call in app.confirm_send.call_args_list], ["0", "1", "2"])
        statuses = dict(self.db.execute("SELECT id, status FROM outcomes"))
        self.assertEqual(statuses["0"], "本机已发")
        self.assertEqual(statuses["1"], "待核对")
        self.assertEqual(statuses["2"], "本机已发")

    @patch("sms_batch.report")
    def test_crash_during_send_keeps_uncertain_row_out_of_next_run(self, report):
        app = self.messages_app()
        app.tap_send.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            run_batch(self.tasks, self.db, app, send=True)
        self.assertEqual(self.db.execute("SELECT status FROM outcomes WHERE id='0'").fetchone()[0], "发送中")
        app = self.messages_app()
        self.assertEqual(run_batch(self.tasks, self.db, app, send=True), 1)
        self.assertEqual([call.args[0].task_id for call in app.confirm_send.call_args_list], ["1", "2"])

    @patch("sms_batch.report")
    def test_draft_prepares_first_row_without_claim_or_send(self, report):
        app = self.messages_app()
        self.assertEqual(run_batch(self.tasks, self.db, app), 0)
        self.assertEqual(app.prepare_once.call_args.args[0], self.tasks[0])
        app.tap_send.assert_not_called()
        self.assertEqual(self.db.execute("SELECT count(*) FROM outcomes").fetchone()[0], 0)

    @patch("messages_task.decide", return_value={"action": "tap_composer", "x": 40, "y": 80})
    def test_draft_recovers_focus_using_the_current_screenshot(self, decide):
        app = self.messages_app()
        app.prepare_once.side_effect = [PhoneTaskError("输入栏失焦"), None]
        app.phone.client.request.return_value = {"value": "current-screenshot"}
        self.assertEqual(run_batch(self.tasks, self.db, app), 0)
        self.assertEqual(decide.call_args.args[0], "current-screenshot")
        self.assertEqual(app.prepare_once.call_count, 2)
        app.tap_send.assert_not_called()

    @patch("sms_batch.report")
    def test_changed_payload_for_existing_task_id_stops_before_phone(self, report):
        self.assertEqual(run_batch(self.tasks[:1], self.db, self.messages_app(), send=True), 0)
        app = self.messages_app()
        with self.assertRaises(PhoneTaskError):
            run_batch([self.tasks[0]._replace(text="different")], self.db, app, send=True)
        app.prepare_once.assert_not_called()

    def test_background_phone_number_cannot_validate_searching_recipient(self):
        editor = {"type": "XCUIElementTypeTextField", "name": "messageBodyField"}
        nodes = [
            {"type": "XCUIElementTypeStaticText", "label": "+1 (202) 555-0147"},
            {"type": "XCUIElementTypeTextField", "name": "To:", "value": "No Name, Searching"},
            editor,
        ]
        with self.assertRaises(PhoneTaskError):
            fields(nodes, "+12025550147")
        nodes[1]["value"] = "+1 (202) 555-0147"
        self.assertIs(fields(nodes, "+12025550147"), editor)

    def test_repeated_country_code_display_requires_one_recipient_button(self):
        label = "+86 138 0013 8000, +86 138 0013 8000"
        field = {"type": "XCUIElementTypeTextField", "name": "To:", "value": label + " ",
                 "rect": {"x": 30, "y": 127, "width": 335, "height": 48}}
        chip = {"type": "XCUIElementTypeButton", "label": label,
                "rect": {"x": 82, "y": 139, "width": 154, "height": 24}}
        editor = {"type": "XCUIElementTypeTextField", "name": "messageBodyField"}
        self.assertIs(fields([field, chip, editor], "13800138000"), editor)
        with self.assertRaises(PhoneTaskError):
            fields([field, chip, editor], "13800138001")
        with self.assertRaises(PhoneTaskError):
            fields([field, chip, {**chip, "rect": {"x": 237, "y": 139, "width": 60, "height": 24}}, editor],
                   "13800138000")

    @patch("messages_task.time.sleep")
    def test_send_accepts_sms_text_view_after_compose_transition(self, sleep):
        task = self.tasks[0]
        editor = {"type": "XCUIElementTypeTextField", "name": "messageBodyField", "value": task.text,
                  "rect": {"x": 80, "y": 425, "width": 240, "height": 50}}
        bubble = {"type": "XCUIElementTypeTextView", "value": task.text,
                  "rect": {"x": 127, "y": 190, "width": 238, "height": 121}}
        title = {"type": "XCUIElementTypeButton", "name": "ConversationTitle", "label": task.phone}
        viewport = {"width": 390, "height": 844}
        phone = Mock()
        phone._source_app = MESSAGES
        previous_title = {**title, "label": self.tasks[1].phone}
        previous_failure = {"type": "XCUIElementTypeButton", "label": "发送失败",
                            "rect": {"x": 348, "y": 235, "width": 26, "height": 26}}
        phone.tree.return_value = [
            previous_title, bubble, previous_failure, title, editor, {**editor, "value": ""}, bubble,
        ], viewport
        self.assertEqual(bubble_count([editor], viewport, task.text, 460), 0)
        app = MessagesApp(phone)
        with patch.object(app, "prepare_once", return_value=({"x": 300, "y": 440}, 0, None)):
            self.assertEqual(app.execute(task, send=True).status, "本机已发")
        phone.tree.assert_called_once()
        phone.client.session.assert_not_called()

    @patch("messages_task.time.sleep")
    def test_open_recipient_waits_for_the_compose_screen_after_opening_once(self, sleep):
        task = self.tasks[0]
        phone = Mock()
        app = MessagesApp(phone)
        editor = {"name": "messageBodyField"}
        result = [], {}, editor
        with patch.object(app, "inspect", side_effect=[PhoneTaskError("当前前台不是 iPhone 信息 App"), result]):
            self.assertEqual(app.open_recipient(task), result)
        phone.client.session.assert_called_once()
        self.assertEqual(phone.client.session.call_args.args[:2], ("POST", "/url"))
