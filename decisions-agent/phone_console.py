"""Run the existing phone CLI tasks from the local console."""

from collections import Counter, deque
from contextlib import closing
from datetime import datetime
import os
import json
from pathlib import Path
import signal
import subprocess
import sys
from threading import Lock, Thread
from uuid import uuid4

from phone_runtime import PhoneTaskError, WDAClient, WDAError

ROOT = Path(__file__).resolve().parent


def kill_task_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


class PhoneConsole:
    def __init__(self, device):
        self.device = device
        self.lock = Lock()
        self.run = None
        self.closed = False

    def device_status(self, *, check_lock=False):
        locked = False
        with closing(WDAClient(f"http://127.0.0.1:{self.device.port}", timeout=1)) as client:
            try:
                status = client.request("GET", "/status")
                ready = status.get("value", {}).get("ready") is True
            except WDAError:
                ready = False
            if ready and check_lock:
                try:
                    locked = client.request("GET", "/wda/locked").get("value") is True
                except WDAError:
                    pass
        return {
            "name": "iPhone 12" if self.device.state_dir.name == "iphone-use-iphone12" else "iPhone",
            "ready": ready,
            "locked": locked,
            "port": self.device.port,
            "model_key": bool(os.environ.get("OPENAI_API_KEY")),
        }

    def arguments(self, data):
        def field(name):
            value = data.get(name, "")
            if not isinstance(value, str) or "\0" in value:
                raise PhoneTaskError("任务参数格式无效")
            return value.strip()

        def file_path(name, label):
            value = field(name)
            path = Path(value).expanduser().resolve()
            if not value or not path.is_file():
                raise PhoneTaskError(f"找不到{label}，请填写 Mac 上的文件路径")
            return str(path)

        task = field("task")
        mode = field("mode")
        if task == "watch":
            if mode not in ("terra", "decisions"):
                raise PhoneTaskError("请选择值守版本")
            if not os.environ.get("OPENAI_API_KEY"):
                raise PhoneTaskError("请在启动控制台的终端配置 OPENAI_API_KEY 后重启控制台")
            args = ["watch", mode, "--knowledge", file_path("path", "知识库")]
            for contact in field("contacts").splitlines():
                if contact.strip():
                    args.append("--contact=" + contact.strip())
            if (recipient := field("recipient")):
                if data.get("confirmed") is not True:
                    raise PhoneTaskError("请确认只向指定会话实发")
                args.append("--send-to=" + recipient)
            title = "微信值守"
        elif task == "once" and field("template") == "custom":
            goal = field("goal")
            if not goal:
                raise PhoneTaskError("请填写任务说明")
            args = ["once", "--goal=" + goal]
            title = "单次任务"
        elif task == "sms" or (task == "once" and field("template") == "sms"):
            if mode not in ("preview", "draft", "send"):
                raise PhoneTaskError("请选择短信执行方式")
            if mode == "send" and data.get("confirmed") is not True:
                raise PhoneTaskError("请确认向名单中的号码发送短信")
            args = ["sms", file_path("path", "短信名单")]
            if mode != "preview":
                args.append("--" + mode)
            title = {"preview": "查看短信名单", "draft": "准备短信草稿", "send": "发送短信"}[mode]
        else:
            raise PhoneTaskError("请选择要运行的任务")
        return title, args + ["--wda-state-dir", str(self.device.state_dir)]

    def start(self, data):
        title, arguments = self.arguments(data)
        self.launch(title, arguments)

    def launch(self, title, arguments, *, env=None):
        with self.lock:
            if self.closed:
                raise PhoneTaskError("控制台正在关闭")
            if self.run and self.run["exit_code"] is None:
                raise PhoneTaskError("已有任务在运行，请等任务结束或停止后再启动")
            command = [sys.executable, "-u", str(ROOT / "phone_agent.py"), *arguments]
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, start_new_session=True, env=env)
            run = {"process": process, "id": uuid4().hex, "title": title, "started": datetime.now().isoformat(timespec="seconds"),
                   "stopping": False, "exit_code": None, "logs": deque(maxlen=120), "counts": Counter()}
            self.run = run
            Thread(target=self.read_output, args=(run,), daemon=True).start()

    def read_output(self, run):
        process = run["process"]
        with process.stdout as output:
            for line in output:
                try:
                    receipt = json.loads(line)
                except ValueError:
                    receipt = None
                with self.lock:
                    run["logs"].append(line.rstrip("\n"))
                    if isinstance(receipt, dict) and "task_id" in receipt and isinstance(receipt.get("status"), str):
                        run["counts"][receipt["status"]] += 1
        code = process.wait()
        with self.lock:
            if run["stopping"]:
                kill_task_group(process)
            run["exit_code"] = code

    def status(self):
        with self.lock:
            if self.run is None:
                return None
            return {key: self.run[key] for key in ("id", "title", "started", "stopping", "exit_code")} | {
                "logs": list(self.run["logs"]),
                "counts": dict(self.run["counts"]),
            }

    def stop(self, expected_id=None):
        with self.lock:
            if self.run and self.run["exit_code"] is None and not self.run["stopping"]:
                if expected_id is not None and self.run["id"] != expected_id:
                    raise PhoneTaskError("原任务已结束")
                process = self.run["process"]
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    return
                self.run["stopping"] = True

    def close(self):
        with self.lock:
            self.closed = True
            process = self.run["process"] if self.run and self.run["exit_code"] is None else None
        self.stop()
        if process:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            kill_task_group(process)
            process.wait()
