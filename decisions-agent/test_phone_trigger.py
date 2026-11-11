from contextlib import closing
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import Mock, patch

from phone_runtime import PhoneBusy, PhoneDevice, WDAError
from phone_trigger import InboxQueue, load_rules, process_event, request_handler
from wechat_desk import watch_once


class PhoneTriggerTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.queue = InboxQueue(self.path / "events.sqlite3")
        self.sender = "+12025550147"
        (self.path / "config.json").write_text(json.dumps({"udid": "test-device", "local_port": 18101}))
        with patch("phone_runtime.Path.home", return_value=self.path):
            self.device = PhoneDevice(self.path)
        self.phone = Mock()
        self.phone.client.request.return_value = {"value": False}
        self.phone.active_app.return_value = "com.tencent.xin"
        self.phone._source_app = "com.apple.MobileSMS"
        self.phone.tree.return_value = ([
            {"type": "XCUIElementTypeCollectionView", "name": "ConversationList"},
            {"type": "XCUIElementTypeCell", "label": "+1 (202) 555-0147, 10:40, Hello"},
        ], {})

    def test_http_rejects_bad_token_and_deduplicates_event_ids(self):
        rules = {
            "messages": {"action": "messages_preview", "sender": self.sender},
            "settings": {"action": "app_snapshot", "bundle_id": "com.apple.Preferences"},
        }
        handler = request_handler(self.queue, "test-token", rules)
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                def post(token, body, name="messages"):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                    try:
                        connection.request("POST", f"/events/{name}", json.dumps(body),
                                           {"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
                        response = connection.getresponse()
                        return response.status, response.read()
                    finally:
                        connection.close()

                self.assertEqual(post("wrong-token", {"event_id": "event-1"})[0], 401)
                self.assertIsNone(self.queue.next_event())
                self.assertEqual(post("test-token", {"event_id": "event-1"})[0], 202)
                status, body = post("test-token", {"event_id": "event-1"})
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)["duplicate"])
                self.assertEqual(self.queue.next_event()[:2], ("event-1", self.sender))
                self.assertEqual(post("test-token", {"event_id": "event-1"}, "settings")[0], 202)
                self.assertEqual(post("test-token", {"event_id": "event-1"}, "unknown")[0], 404)
                self.queue.complete("event-1")
                event = self.queue.next_event()
                self.assertEqual(event[0], "settings/event-1")
                self.assertEqual(json.loads(event[4]), rules["settings"])
            finally:
                server.shutdown()
                worker.join()

    @patch("phone_runtime.PhoneController")
    def test_managed_http_runs_the_task_saved_for_a_matching_event(self, controller):
        controller.return_value = self.phone
        self.phone._source_app = task_app = "com.apple.Preferences"
        self.phone.tree.return_value = ([], {})
        queue = InboxQueue(self.queue.path, managed=True)
        queue.store.save("read-app", {"condition": {"type": "message", "sender": self.sender},
                                      "task": {"action": "app_snapshot", "bundle_id": task_app}}, create=True)
        handler = request_handler(queue, "test-token", None)
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                def post(sender):
                    with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)) as client:
                        client.request("POST", "/events/read-app", json.dumps({"event_id": "same", "sender": sender}),
                                       {"Authorization": "Bearer test-token", "Content-Type": "application/json"})
                        response = client.getresponse()
                        response.read()
                        return response.status

                self.assertEqual(post(self.sender), 409)
                queue.store.enable("read-app", True)
                self.assertEqual(post("+12025550148"), 422)
                self.assertEqual(post(self.sender), 202)
                self.assertEqual(post(self.sender), 200)
                self.assertTrue(process_event(queue, self.device))
                self.assertEqual([call.args[0] for call in self.phone.launch_app.call_args_list],
                                 [task_app, "com.tencent.xin"])
                self.assertIsNone(queue.next_event())
            finally:
                server.shutdown()
                worker.join()

    @patch("phone_runtime.PhoneController")
    def test_event_waits_during_wechat_scan_and_runs_after_lock_release(self, controller):
        controller.return_value = self.phone
        self.queue.enqueue("event-1", self.sender)
        desk = Mock()

        def scan(tasks):
            self.assertIs(tasks.phone, self.phone)
            self.assertFalse(process_event(self.queue, self.device))
            self.phone.launch_app.assert_not_called()

        desk.scan.side_effect = scan
        with self.device.exclusive("watch"):
            with self.assertRaises(PhoneBusy), self.device.exclusive("watch"):
                self.fail("another watcher obtained the role lock")
            watch_once(desk, self.device, self.path / "screen.png")
            self.assertTrue(process_event(self.queue, self.device))
        self.assertEqual([call.args[0] for call in self.phone.launch_app.call_args_list],
                         ["com.apple.MobileSMS", "com.tencent.xin"])
        self.assertTrue(self.phone.launch_app.call_args.kwargs["verify"])
        self.assertIsNone(self.queue.next_event())

    @patch("phone_runtime.PhoneController")
    def test_failed_handoff_resumes_without_repeating_inbox_read(self, controller):
        controller.return_value = self.phone
        self.queue.enqueue("event-1", self.sender)
        self.phone.launch_app.side_effect = [None, WDAError("pua_timeout", "restore timed out"), None]
        self.assertFalse(process_event(self.queue, self.device))
        self.assertEqual(self.queue.next_event()[2], "com.tencent.xin")
        self.assertTrue(process_event(self.queue, self.device))
        self.assertEqual(self.phone.tree.call_count, 1)
        self.assertEqual(self.phone.launch_app.call_args.args[0], "com.tencent.xin")
        self.assertIsNone(self.queue.next_event())

    @patch("phone_runtime.PhoneController")
    def test_locked_phone_keeps_event_queued(self, controller):
        controller.return_value = self.phone
        self.phone.client.request.return_value = {"value": True}
        self.queue.enqueue("event-1", self.sender)
        self.assertFalse(process_event(self.queue, self.device))
        self.assertEqual(self.queue.next_event()[0], "event-1")
        self.phone.launch_app.assert_not_called()

    def test_config_resolves_csv_relative_to_rules_file(self):
        path = self.path / "rules.json"
        path.write_text(json.dumps({"draft": {"action": "sms_draft", "csv": "batch.csv"}}))
        self.assertEqual(load_rules(path)["draft"]["csv"], str((self.path / "batch.csv").resolve()))

    def test_existing_message_events_keep_their_sender_after_schema_change(self):
        path = self.path / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE events (id TEXT PRIMARY KEY, sender TEXT NOT NULL, return_app TEXT, result TEXT, done INTEGER DEFAULT 0)")
            db.execute("INSERT INTO events (id, sender) VALUES (?, ?)", ("old", self.sender))
        legacy = InboxQueue(path)
        self.assertEqual(legacy.next_event(), ("old", self.sender, None, None, None))

    @patch("phone_runtime.PhoneController")
    def test_custom_app_event_opens_configured_app_and_returns_to_wechat(self, controller):
        controller.return_value = self.phone
        rule = {"action": "app_snapshot", "bundle_id": "com.apple.Preferences"}
        self.queue.enqueue("button/event-1", "", rule)
        self.phone._source_app = rule["bundle_id"]
        self.phone.tree.return_value = ([{"type": "XCUIElementTypeStaticText", "label": "设置"}], {})
        self.assertTrue(process_event(self.queue, self.device))
        self.assertEqual([call.args[0] for call in self.phone.launch_app.call_args_list],
                         ["com.apple.Preferences", "com.tencent.xin"])
        with closing(sqlite3.connect(self.queue.path)) as db:
            result = json.loads(db.execute("SELECT result FROM events").fetchone()[0])
        self.assertEqual(result["text"], ["设置"])

    @patch("phone_runtime.PhoneController")
    def test_app_activation_failure_is_recorded_and_next_event_can_run(self, controller):
        controller.return_value = self.phone
        app = "com.apple.Preferences"
        self.phone.launch_app.side_effect = [WDAError("postcondition_failed", "App did not come to foreground"),
                                             None, None, None]
        self.queue.enqueue("button/event-1", "", {"action": "app_snapshot", "bundle_id": app})
        self.queue.enqueue("messages/event-2", self.sender)
        self.assertTrue(process_event(self.queue, self.device))
        with closing(sqlite3.connect(self.queue.path)) as db:
            saved = db.execute("SELECT result FROM events WHERE id = ?", ("button/event-1",)).fetchone()[0]
        self.assertIn("未切到前台", json.loads(saved)["reason"])
        self.assertTrue(process_event(self.queue, self.device))
        self.assertIsNone(self.queue.next_event())

    @patch("messages_task.MessagesApp.prepare_once")
    @patch("phone_runtime.PhoneController")
    def test_custom_sms_draft_does_not_send(self, controller, prepare):
        controller.return_value = self.phone
        csv = self.path / "batch.csv"
        csv.write_text("task_id,phone,message\nrow-1,+12025550147,hello\n")
        self.queue.enqueue("draft/event-1", "", {"action": "sms_draft", "csv": str(csv)})
        self.assertTrue(process_event(self.queue, self.device))
        self.assertEqual(prepare.call_args.args[0].text, "hello")
        self.assertFalse(prepare.call_args.args[1])
        self.phone.post.assert_not_called()

    @patch("phone_runtime.PhoneController")
    def test_non_utf8_csv_failure_does_not_block_the_next_event(self, controller):
        controller.return_value = self.phone
        csv = self.path / "batch.csv"
        csv.write_bytes("task_id,phone,message\nrow-1,+12025550147,中文\n".encode("gbk"))
        self.queue.enqueue("draft/event-1", "", {"action": "sms_draft", "csv": str(csv)})
        self.queue.enqueue("messages/event-2", self.sender)
        self.assertTrue(process_event(self.queue, self.device))
        with closing(sqlite3.connect(self.queue.path)) as db:
            saved = db.execute("SELECT result FROM events WHERE id = ?", ("draft/event-1",)).fetchone()[0]
        self.assertIn("UTF-8", json.loads(saved)["reason"])
        self.assertTrue(process_event(self.queue, self.device))
        self.assertIsNone(self.queue.next_event())
