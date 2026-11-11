"""Structured local Codex calls using the user's official ChatGPT login."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock


class CodexError(RuntimeError):
    pass


_planning = set()
_lock = Lock()
_closing = False


def kill_group(process):
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def cancel_planning():
    global _closing
    with _lock:
        _closing = True
        for process in _planning:
            kill_group(process)


def codex_env():
    excluded = {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "CODEX_ACCESS_TOKEN"}
    return {key: value for key, value in os.environ.items() if key not in excluded}


def login_status():
    executable = shutil.which("codex")
    if not executable:
        return "未安装 Codex CLI"
    try:
        result = subprocess.run([executable, "login", "status"], env=codex_env(),
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return "无法读取 Codex 登录状态"
    if result.returncode == 0 and "logged in using chatgpt" in (result.stdout + result.stderr).lower():
        return "ChatGPT 已登录"
    return "请在终端运行 codex login，选择使用 ChatGPT 登录"


def require_chatgpt():
    status = login_status()
    if status != "ChatGPT 已登录":
        raise CodexError(status)


def ask(prompt, schema, timeout=120):
    require_chatgpt()
    with TemporaryDirectory(prefix="phone-codex-") as temporary:
        directory = Path(temporary)
        shape = directory / "schema.json"
        answer = directory / "answer.json"
        shape.write_text(json.dumps(schema), encoding="utf-8")
        command = [
            shutil.which("codex"), "-a", "never", "exec", "--ignore-user-config", "--ephemeral",
            "--skip-git-repo-check", "--sandbox", "read-only", "--disable", "shell_tool",
            "--disable", "multi_agent", "--disable", "plugins", "--disable", "remote_plugin",
            "-c", 'web_search="disabled"', "-c", 'model_reasoning_effort="low"',
            "-C", temporary, "--output-schema", str(shape), "--output-last-message", str(answer),
        ]
        with _lock:
            if _closing:
                raise CodexError("控制台正在关闭")
            process = subprocess.Popen(command + ["-"], env=codex_env(), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
            _planning.add(process)
        try:
            _, stderr = process.communicate(input=prompt, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            kill_group(process)
            process.communicate()
            raise CodexError("Codex 响应超时") from error
        finally:
            with _lock:
                _planning.discard(process)
        if process.returncode or not answer.is_file():
            diagnostic = stderr.lower()
            if "usage limit" in diagnostic or "rate limit" in diagnostic:
                raise CodexError("Codex 达到使用限额，请稍后再试")
            raise CodexError(f"Codex 未返回结果（退出码 {process.returncode}）")
        try:
            value = json.loads(answer.read_text(encoding="utf-8"))
        except ValueError as error:
            raise CodexError("Codex 返回了无效的结构化结果") from error
        if not isinstance(value, dict):
            raise CodexError("Codex 返回了无效的结构化结果")
        return value


def object_schema(fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}
