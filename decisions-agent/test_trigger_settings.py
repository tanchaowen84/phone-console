from datetime import datetime
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

from phone_trigger import InboxQueue
from trigger_settings import settings_handler


class TriggerSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.queue = InboxQueue(Path(temporary.name) / "events.sqlite3", managed=True)
        self.task = {"action": "app_snapshot", "bundle_id": "com.apple.Preferences"}

    def test_ui_api_create_edit_toggle_and_origin(self):
        handler = settings_handler(self.queue.store, "http://127.0.0.1:8766/events", "test-token")
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                def call(method, path, body=None, origin=None):
                    client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)
                    headers = {"Origin": origin or f"http://127.0.0.1:{server.server_port}"}
                    try:
                        client.request(method, path, json.dumps(body) if body is not None else None, headers)
                        response = client.getresponse()
                        return response.status, response.read()
                    finally:
                        client.close()

                rule = {"name": "read-settings", "condition": {"type": "webhook"}, "task": self.task}
                self.assertEqual(call("POST", "/api/rules", rule, origin="http://wrong.test")[0], 403)
                self.assertEqual(call("POST", "/api/rules", rule)[0], 200)
                self.assertEqual(call("POST", "/api/rules", rule)[0], 400)
                self.assertFalse(self.queue.store.all()[0]["enabled"])
                self.assertEqual(call("PATCH", "/api/rules/read-settings", {"enabled": True})[0], 200)
                rule["condition"] = {"type": "daily", "time": "09:15"}
                self.assertEqual(call("PUT", "/api/rules/read-settings", rule)[0], 200)
                status, body = call("GET", "/api/rules")
                self.assertEqual(status, 200)
                saved = json.loads(body)["rules"][0]
                self.assertTrue(saved["enabled"])
                self.assertEqual(saved["condition"], rule["condition"])
            finally:
                server.shutdown()
                worker.join()

    def test_disabled_rule_keeps_unstarted_job_waiting(self):
        name = "external"
        self.queue.store.save(name, {"condition": {"type": "webhook"}, "task": self.task}, create=True)
        self.assertEqual(self.queue.enqueue_managed(name, "first"), "disabled")
        self.queue.store.enable(name, True)
        self.assertEqual(self.queue.enqueue_managed(name, "first"), "queued")
        event = self.queue.next_event()[0]
        self.queue.store.enable(name, False)
        self.assertIsNone(self.queue.next_event())
        self.assertFalse(self.queue.save_return_app(event, "com.tencent.xin"))
        self.queue.store.enable(name, True)
        self.assertEqual(self.queue.next_event()[0], event)
        self.assertTrue(self.queue.save_return_app(event, "com.tencent.xin"))
        self.queue.store.enable(name, False)
        self.assertEqual(self.queue.next_event()[0], event)

    def test_daily_rule_queues_once_for_each_matching_day(self):
        name = "morning"
        self.queue.store.save(name, {"condition": {"type": "webhook"}, "task": self.task}, create=True)
        self.queue.store.enable(name, True)
        self.assertEqual(self.queue.enqueue_managed(name, "2026-11-10"), "queued")
        self.queue.complete(self.queue.next_event()[0])
        self.queue.store.save(name, {"condition": {"type": "daily", "time": "09:15"}, "task": self.task}, create=False)
        self.assertEqual(self.queue.enqueue_managed(name, "manual"), "condition")
        self.queue.enqueue_due(datetime(2026, 11, 10, 9, 14))
        self.assertIsNone(self.queue.next_event())
        self.queue.enqueue_due(datetime(2026, 11, 10, 9, 15))
        self.queue.complete(self.queue.next_event()[0])
        self.queue.enqueue_due(datetime(2026, 11, 10, 9, 15, 30))
        self.assertIsNone(self.queue.next_event())
        self.queue.enqueue_due(datetime(2026, 11, 11, 9, 15))
        self.assertIn("2026-11-11", self.queue.next_event()[0])

    def test_message_rule_checks_sender_from_event(self):
        self.queue.store.save("message", {"condition": {"type": "message", "sender": "+12025550147"},
                                         "task": self.task}, create=True)
        self.queue.store.enable("message", True)
        self.assertEqual(self.queue.enqueue_managed("message", "test", "+12025550148"), "condition")
        self.assertEqual(self.queue.enqueue_managed("message", "test", "+1 (202) 555-0147"), "queued")
