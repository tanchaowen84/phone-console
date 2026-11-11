"""Load local Python plugins and assemble the phone runtime."""

import argparse
from contextlib import ExitStack, contextmanager
from importlib import import_module

from phone_core import Core, execute_once
from phone_runtime import PhoneDevice, PhoneTaskError, phone_task


class WDAInput:
    def __init__(self, phone, viewport=None):
        self.phone = phone
        self.viewport = viewport if viewport is not None else phone.viewport

    def tap(self, x, y):
        viewport = self.viewport()
        self.phone.post("/wda/tap", {"x": viewport["width"] * x, "y": viewport["height"] * y})

    def type_text(self, text):
        self.phone.type_text(text=text, replace=False, allow_newlines=True)

    def swipe(self, direction):
        self.phone.swipe(direction)

    def home(self):
        self.phone.press_button("home")


def wda_core(phone, *, capture=None, controls=None, loop=execute_once):
    def screenshot():
        return phone.client.request("GET", "/screenshot")["value"]

    return Core(capture if capture is not None else screenshot,
                controls if controls is not None else WDAInput(phone), loop, phone.tree)


def load_entry(reference):
    module, separator, name = reference.partition(":")
    if not separator or not module or not name:
        raise PhoneTaskError("插件入口格式为 Python模块:函数")
    try:
        entry = getattr(import_module(module), name)
    except (ImportError, AttributeError) as error:
        raise PhoneTaskError(f"无法加载插件 {reference}：{error}") from error
    if not callable(entry):
        raise PhoneTaskError(f"插件入口不可调用：{reference}")
    return entry


def add_plugin_arguments(parser):
    parser.add_argument("--screen-source", metavar="MODULE:FACTORY", help="截图插件，提供 Base64 PNG")
    parser.add_argument("--input-device", metavar="MODULE:FACTORY", help="输入插件，处理归一化坐标和键盘操作")
    parser.add_argument("--decision", metavar="MODULE:FUNCTION", help="替换自定义任务的决策函数")
    parser.add_argument("--loop", metavar="MODULE:FUNCTION", help="替换任务执行循环")


@contextmanager
def open_core(args):
    with ExitStack() as stack:
        phone = None
        if not (args.screen_source and args.input_device):
            phone, _, _ = stack.enter_context(phone_task(args.wda_state_dir))
        elif args.wda_state_dir:
            stack.enter_context(PhoneDevice(args.wda_state_dir).exclusive())
        capture = stack.enter_context(load_entry(args.screen_source)()) if args.screen_source else None
        controls = stack.enter_context(load_entry(args.input_device)()) if args.input_device else None
        loop = load_entry(args.loop) if args.loop else execute_once
        if phone is not None:
            yield wda_core(phone, capture=capture, controls=controls, loop=loop)
        else:
            yield Core(capture, controls, loop)


def run_plugin(argv=None):
    parser = argparse.ArgumentParser(prog="phone_agent.py plugin")
    parser.add_argument("entry", metavar="MODULE:FUNCTION")
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        return load_entry(args.entry)(args.arguments)
    except PhoneTaskError as error:
        parser.exit(1, f"{error}\n")
