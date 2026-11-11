"""Device-independent execution loop and interfaces for screen and input plugins."""

from dataclasses import dataclass
from functools import cached_property
from typing import Callable, NamedTuple, Protocol


class InputDevice(Protocol):
    def tap(self, x: float, y: float) -> None: ...
    def type_text(self, text: str) -> None: ...
    def swipe(self, direction: str) -> None: ...
    def home(self) -> None: ...


class Screen:
    def __init__(self, capture, read_tree=None):
        self.capture = capture
        self.read_tree = read_tree

    @cached_property
    def frame(self):
        return self.capture()

    @cached_property
    def tree(self):
        if self.read_tree is None:
            raise ValueError("当前图源未配置组件树读取")
        return self.read_tree()


class Step(NamedTuple):
    run: Callable[[], None]
    commits: bool = False


class Result(NamedTuple):
    status: str
    reason: str | None = None


def execute_once(observe, decide, before_effect=None):
    """Read fresh state for each decision and return the completed task value."""
    while True:
        step = decide(observe())
        if not isinstance(step, Step):
            return step
        if step.commits and before_effect is not None:
            before_effect()
            before_effect = None
        step.run()


@dataclass(frozen=True)
class Core:
    capture: Callable[[], str]
    controls: InputDevice
    loop: Callable = execute_once
    read_tree: Callable | None = None

    def observe(self):
        return Screen(self.capture, self.read_tree)

    def run(self, decide, before_effect=None):
        return self.loop(self.observe, decide, before_effect)
