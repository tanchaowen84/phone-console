"""Exercise the plugin entry points through the actual CLI."""

import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from textwrap import dedent
import unittest


class PhonePluginsTests(unittest.TestCase):
    def test_one_off_task_loads_independent_plugins_without_wda(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "demo_phone.py").write_text(dedent('''
                from contextlib import nullcontext
                from phone_core import execute_once

                tapped = False

                def capture():
                    return "after" if tapped else "before"

                class Controls:
                    def tap(self, x, y):
                        global tapped
                        assert (x, y) == (.42, .67) and not tapped
                        tapped = True

                def screen():
                    return nullcontext(capture)

                def input():
                    return nullcontext(Controls())

                def decide(frame, prompt, schema):
                    if frame == "after":
                        return {"action": "done", "value": "read after tap", "x": 0, "y": 0}
                    return {"action": "tap", "value": "", "x": 42, "y": 67}

                def loop(observe, decide, before_effect):
                    print("plugin loop")
                    return execute_once(observe, decide, before_effect)

                def main(argv):
                    assert argv == ["--example", "value"]
                    return 0
            '''))
            cli = Path(__file__).with_name("phone_agent.py").resolve()
            env = {**os.environ, "PYTHONPATH": str(root)}
            run = subprocess.run([sys.executable, str(cli), "once", "--goal", "Read after tapping",
                                  "--screen-source", "demo_phone:screen", "--input-device", "demo_phone:input",
                                  "--decision", "demo_phone:decide", "--loop", "demo_phone:loop"],
                                 env=env, text=True, capture_output=True, check=True)
            lines = run.stdout.splitlines()
            self.assertIn("plugin loop", lines)
            result = json.loads(lines[-1])
            self.assertEqual(result["status"], "已完成")
            self.assertEqual(result["reason"], "read after tap")
            subprocess.run([sys.executable, str(cli), "plugin", "demo_phone:main", "--example", "value"],
                           env=env, check=True)
