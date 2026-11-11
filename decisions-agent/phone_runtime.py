"""Shared iPhone connection, device lock and verified text entry."""

from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent / "source" / "server"))
from wda_client import WDAClient, WDAError
from wda_controller import PhoneController
from wda_setup import state_directory


class PhoneTaskError(RuntimeError):
    pass


class PhoneBusy(PhoneTaskError):
    pass


class PhoneDevice:
    def __init__(self, state_dir):
        self.state_dir = state_directory(state_dir)
        config = json.loads((self.state_dir / "config.json").read_text())
        self.udid, self.port = config["udid"], int(config["local_port"])
        if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", self.udid):
            raise PhoneTaskError("iPhone Use 配置中的 UDID 无效")
        self.storage = Path.home() / ".local" / "share" / "iphone-use-desk"
        self.storage.mkdir(parents=True, exist_ok=True, mode=0o700)

    @contextmanager
    def exclusive(self, purpose=None):
        suffix = f".{purpose}" if purpose else ""
        with (self.storage / f"{self.port}{suffix}.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PhoneBusy(f"WDA 端口 {self.port} 已有手机任务") from None
            yield

    @contextmanager
    def acquire(self):
        with self.exclusive():
            client = WDAClient(f"http://127.0.0.1:{self.port}", timeout=60)
            try:
                yield PhoneController(client, self.state_dir)
            finally:
                client.close()


@contextmanager
def phone_task(state_dir):
    device = PhoneDevice(state_dir)
    with device.acquire() as phone:
        yield phone, device.storage, device.udid


def fill_focused(phone, text, reuse_exact=False, allow_placeholder=False):
    path, _ = phone.focused_field()
    client = phone.client
    value_path = path + "/attribute/value"
    existing = client.session("GET", value_path)
    placeholder = client.session("GET", path + "/attribute/placeholderValue") if allow_placeholder and existing else None
    if reuse_exact and existing == text and existing != placeholder:
        return value_path
    if existing not in ("", None, placeholder):
        return None
    client.session("POST", "/wda/keys", {"value": [text], "frequency": 30})
    if client.session("GET", value_path) != text:
        raise PhoneTaskError("输入栏文字未通过逐字核对")
    return value_path
