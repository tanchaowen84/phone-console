import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from desktop_backend import Desktop
from desktop_discovery import parse_usb_devices
from phone_runtime import PhoneTaskError


class DesktopTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        profile = self.root / "device"
        profile.mkdir()
        (profile / "config.json").write_text(json.dumps({"udid": "test-desktop-device", "local_port": 19983}))
        with patch("desktop_backend.login_status", return_value="ChatGPT 已登录"), patch("phone_runtime.Path.home", return_value=self.root):
            self.desktop = Desktop(self.root / "devices.json", [profile], usb_probe=lambda: {})
        self.addCleanup(self.desktop.close)
        self.ident = next(iter(self.desktop.devices))

    def test_sms_plan_shows_full_file_and_rejects_changed_or_subset_input(self):
        path = self.root / "名单.csv"
        path.write_text("test data")
        console, settings = self.desktop.device(self.ident)
        settings["files"] = {"sms": str(path)}
        model_task = {"device_id": self.ident, "plugin": "sms", "goal": "", "mode": "send"}
        model_task["scope_quote"] = "发给名单中的一个号码"
        with patch("desktop_backend.ask", return_value={"reply": "", "tasks": [model_task]}), patch.object(
                console, "device_status", return_value={"ready": True}):
            with self.assertRaisesRegex(PhoneTaskError, "整份名单"):
                self.desktop.plan({"goal": model_task["scope_quote"]})
            model_task["scope_quote"] = "发送给整份名单"
            with self.assertRaisesRegex(PhoneTaskError, "整份名单"):
                self.desktop.plan({"goal": "不要发送给整份名单，只给一个号码发"})
            plan = self.desktop.plan({"goal": model_task["scope_quote"]})
        self.assertTrue(plan["bulk_sms"])
        self.assertIn("整份 名单.csv", plan["tasks"][0]["title"])
        path.write_text("replacement")
        with patch.object(self.desktop, "start") as launch:
            outcome = self.desktop.execute(plan["id"])
        self.assertIn("文件在预览后已更改", outcome["results"][0]["status"])
        launch.assert_not_called()
        with self.assertRaisesRegex(PhoneTaskError, "已执行或已失效"):
            self.desktop.execute(plan["id"])

    def test_configured_offline_device_is_not_reported_as_connected(self):
        console, _ = self.desktop.device(self.ident)
        with patch.object(console, "device_status", return_value={"ready": False}), patch("desktop_backend.ask") as model:
            status = self.desktop.plan({"goal": "现在连接了几台手机？", "selected_device_id": self.ident})
            self.assertIn("0 台手机", status["reply"])
            self.assertIn("iPhone 1 未连接", status["reply"])
            model.assert_not_called()
            model.return_value = {"kind": "status", "reply": "iPhone 1 已连接", "tasks": []}
            status = self.desktop.plan({"goal": "帮我检查下两台手机是不是可操作"})
            self.assertIn("0 台手机", status["reply"])
            self.assertNotIn("iPhone 1 已连接", status["reply"])
            model.return_value = {"kind": "plan", "reply": "", "tasks": [
                {"device_id": self.ident, "plugin": "once", "goal": "打开设置", "mode": "default"},
            ]}
            with self.assertRaisesRegex(PhoneTaskError, "还不能控制"):
                self.desktop.plan({"goal": "让 iPhone 1 打开设置"})

    def test_usb_recognition_distinguishes_first_use_from_control_ready(self):
        console, _ = self.desktop.device(self.ident)
        attached = {"testdesktopdevice": "iPhone", "0000810100abcdef": "iPhone"}
        self.desktop.usb_probe = lambda: attached
        with patch.object(console, "device_status", return_value={"ready": False}):
            saved, first = self.desktop.state()["devices"]
            self.assertEqual(saved["id"], self.ident)
            self.assertEqual(saved["control"], "pending")
            self.assertTrue(saved["usb_connected"])
            self.assertEqual(first["control"], "first_setup")
            self.assertFalse(first["ready"])
            reply = self.desktop.plan({"goal": "现在连接了几台手机？"})["reply"]
            self.assertIn("连接了 2 台手机，0 台可控制", reply)
            attached.clear()
            rows = self.desktop.state()["devices"]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["control"], "offline")

    def test_existing_phone_reuses_an_active_preparation_job(self):
        self.desktop.usb_probe = lambda: {"testdesktopdevice": "iPhone"}
        with patch("desktop_backend.SetupManager") as setup:
            manager = setup.return_value
            manager.setup.return_value = {"jobs": [{"id": "live-start", "state": "running", "action": "start"}]}
            self.desktop.prepare_device(self.ident)
            self.assertEqual(self.desktop.setup_jobs[self.ident][1], "live-start")
            manager.setup.assert_called_once_with("status")

    def test_usb_tree_uses_apple_phone_serial_and_ignores_other_devices(self):
        nodes = [{"IORegistryEntryChildren": [
            {"idVendor": 1452, "USB Product Name": "iPhone", "USB Serial Number": "00008101-AB12CD34"},
            {"idVendor": 1, "USB Product Name": "iPhone", "USB Serial Number": "ABCDEF12"},
            {"idVendor": 1452, "USB Product Name": "USB Hub", "USB Serial Number": "ABCDEF00"},
        ]}]
        self.assertEqual(parse_usb_devices(nodes), {"00008101ab12cd34": "iPhone"})

    def test_imported_plugin_loads_python_module_next_to_manifest(self):
        manifest = self.root / "plugin.json"
        manifest.write_text(json.dumps({"name": "本地示例", "kind": "task", "entry": "desktop_test_local:main"}))
        (self.root / "desktop_test_local.py").write_text(
            "import json\n"
            "def main(args):\n"
            "    assert args[:2] == ['--goal', '验证插件入口']\n"
            "    print(json.dumps({'status': 'done', 'reason': 'plugin loaded'}))\n"
            "    return 0\n"
        )
        self.desktop.add_plugin(manifest)
        plugin_id = next(item["id"] for item in self.desktop.plugin_list() if item["name"] == "本地示例")
        self.desktop.start(self.ident, {"plugin": plugin_id, "goal": "验证插件入口"})
        console, _ = self.desktop.device(self.ident)
        deadline = time.monotonic() + 5
        while console.status()["exit_code"] is None and time.monotonic() < deadline:
            time.sleep(0.05)
        run = self.desktop.state()["devices"][0]["run"]
        self.assertEqual(run["exit_code"], 0)
        self.assertEqual(run["result"], "plugin loaded")


if __name__ == "__main__":
    unittest.main()
