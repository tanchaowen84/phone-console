# iPhone 短信批次流程

```mermaid
flowchart TD
    A["Mac 控制台 → 单次任务 → 短信名单"] --> B{"CSV 和 SQLite 中还有未记录的号码？"}
    B -->|有| C["WDA 打开 iPhone「信息」<br/>填写并核对号码、正文和发送按钮"]
    B -->|无| Z["批次结束"]
    C -->|界面可恢复| D["同一执行循环回读截图<br/>本机 OCR 或 Codex 判断后由 WDA 操作"]
    D -->|重新检查| C
    C -->|准备无法完成| E["记录失败 · 未点发送"]
    C -->|核对通过| F["记录发送中 → WDA 点击发送<br/>「信息」App 选择发送通道"]
    F --> G{"回读目标会话"}
    G -->|新发出气泡| S["记录本机已发"]
    G -->|界面无法判断| H["截图让 Codex 判断发送结果"]
    H -->|确认新气泡| S
    H -->|结果仍不明| P["记录待核对"]
    G -->|显示未送达| R{"手机提供「作为短信发送」？"}
    H -->|识别到未送达| R
    R -->|可用| T{"补发一次并回读"}
    R -->|不可用| N["记录未送达"]
    T -->|确认气泡| S
    T -->|仍显示未送达| N
    T -->|结果不明| P
    E --> B
    S --> B
    P --> B
    N --> B
    D -.->|看图调用报错| X["本轮退出"]
    H -.->|看图调用报错| P
    classDef success fill:#ccffcc,stroke:#00aa00,color:#172b1a
    classDef error fill:#ffcccc,stroke:#cc0000,color:#391b1b
    class S success
    class E,N,X error
```

- 每条记录写入与 CSV 同名的 `.sms.sqlite3`。同一名单重启时，已入账的任务会显示已有状态并跳过。上次停在「发送中」的条目在输出中显示为「待核对」。
- 「本机已发」依据手机屏幕上的发出气泡。收件端的送达状态未在这条链路中验证。
- 单条准备失败、未送达或结果不明后继续下一条。发送前的 Codex 看图调用报错时退出，发送后的看图报错会记为「待核对」。

## 代码依据

- 控制台入口和子进程管理见 `phone_console.py:PhoneConsole.arguments`、`PhoneConsole.start` 与 `PhoneConsole.stop`。
- 批次入口、SQLite 状态和续跑见 `sms_batch.py:main` 与 `run_batch`，使用 `outcomes` 表的 `id`、`fingerprint`、`status`、`reason` 字段。
- 自定义任务、每条短信和微信手机任务均由 `phone_core.py:execute_once` 调用各自的画面判断和操作。
- 界面准备和纠错见 `messages_task.py:MessagesApp.execute` 与 `prepare_once`。
- 屏幕判定和短信补发见 `messages_task.py:MessagesApp.confirm_send`、`sent_on_screen`、`open_delivery_options` 与 `retry_delivery`。
- 本机 OCR 和 Codex CLI 看图见 `sms_vision.py:local_composer` 与 `decide`。CLI 调用未指定模型名称。
