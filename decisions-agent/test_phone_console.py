from contextlib import closing
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from phone_console import PhoneConsole
from phone_runtime import PhoneTaskError
from trigger_settings import RuleStore, settings_handler


class PhoneConsoleTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.csv = self.root / "test batch.csv"
        self.csv.write_text("task_id,phone,message\npreview-case,+12025550147,Hello\n")
        self.console = PhoneConsole(SimpleNamespace(state_dir=self.root, port=0))
        self.addCleanup(self.console.close)
        self.preview = {"task": "once", "template": "sms", "mode": "preview", "path": str(self.csv)}

    def wait_for(self, condition):
        deadline = time.monotonic() + 4
        while not condition(self.console.status()):
            if time.monotonic() >= deadline:
                self.fail(f"未达到预期进程状态：{self.console.status()}")
            time.sleep(.01)

    def test_preview_runs_existing_cli_and_collects_output(self):
        self.console.start(self.preview)
        self.wait_for(lambda run: run["exit_code"] is not None)
        run = self.console.status()
        self.assertEqual(run["exit_code"], 0)
        self.assertIn("preview-case", run["logs"][0])
        self.assertIn("待处理", run["logs"][0])

    def test_stop_interrupts_the_running_job(self):
        popen = subprocess.Popen

        def sleeping_child(command, **options):
            return popen([sys.executable, "-u", "-c", "import time; print('ready'); time.sleep(30)"], **options)

        with patch("phone_console.subprocess.Popen", side_effect=sleeping_child):
            self.console.start(self.preview)
        self.wait_for(lambda run: "ready" in run["logs"])
        with self.assertRaisesRegex(PhoneTaskError, "已有任务"):
            self.console.start(self.preview)
        self.console.stop()
        self.wait_for(lambda run: run["exit_code"] is not None)
        self.assertTrue(self.console.status()["stopping"])

    def test_process_api_rejects_foreign_origin_and_unconfirmed_send(self):
        store = RuleStore(self.root / "rules.sqlite3")
        handler = settings_handler(store, "http://127.0.0.1:8766/events", "test", self.console)
        with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            try:
                def status(origin, data):
                    with closing(http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2)) as client:
                        client.request("POST", "/api/run", json.dumps(data), {"Origin": origin})
                        response = client.getresponse()
                        response.read()
                        return response.status

                self.assertEqual(status("http://other.test", self.preview), 403)
                local = f"http://127.0.0.1:{server.server_port}"
                self.assertEqual(status(local, {**self.preview, "mode": "send"}), 400)
                self.assertIsNone(self.console.status())
            finally:
                server.shutdown()
                worker.join()
