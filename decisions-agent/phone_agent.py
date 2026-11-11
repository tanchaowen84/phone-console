"""Start an iPhone task from the Mac."""

import argparse
import sys

import phone_once
from phone_plugins import run_plugin
import phone_trigger
import sms_batch
import wechat_desk


def main(argv=None):
    command = argparse.ArgumentParser(description=__doc__)
    tasks = {"once": phone_once.main, "watch": wechat_desk.main, "sms": sms_batch.main, "console": phone_trigger.main,
             "trigger": phone_trigger.main, "plugin": run_plugin}
    command.add_argument("task", choices=tasks,
                         help="once 单次任务，sms 短信名单，watch 微信值守，console 控制台，trigger 自定义事件，plugin 本地插件")
    arguments = sys.argv[1:] if argv is None else argv
    task, *rest = arguments or ["--help"]
    args = command.parse_args([task])
    return tasks[args.task](rest)


if __name__ == "__main__":
    raise SystemExit(main())
