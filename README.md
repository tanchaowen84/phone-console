# 手机控制台

在 Mac 上管理 USB 连接的 iPhone、查看和放大实时画面，并用 Codex 或任务表单下发操作。通用任务支持独立的画面来源和输入插件，微信客服、短信名单和本地触发器也包含在仓库里。

## 安装

从 [GitHub Releases](https://github.com/tanchaowen84/phone-console/releases) 下载 `phone-console-*-universal.dmg`，打开后把「手机控制台」拖进「应用程序」。安装包适用于 macOS 14 或更新版本的 Apple 芯片和 Intel Mac，并内置 Python。

当前版本采用临时签名。若 macOS 在首次打开时拦截，请确认文件来自本仓库，再按 [Apple 的打开说明](https://support.apple.com/zh-cn/102445) 操作。

换机后，用 ChatGPT 登录官方 Codex CLI，以启用任务下发。通过 USB 操控 iPhone 需要安装 Xcode、Node.js 和 npm，并在新 Mac 上完成手机信任和 iPhone Use 配置。

## 构建

需要 macOS 14 或更新版本、Xcode、Python 3.10 或更新版本，以及已通过 ChatGPT 登录的 Codex CLI。USB 控制还需要 Node.js 和 npm。

```sh
./decisions-agent/desktop/build.sh
open decisions-agent/dist/手机控制台.app
```

构建通用 DMG 和 SHA-256 校验文件。

```sh
./decisions-agent/desktop/build.sh --release 0.1.0
```

App 会识别已连接的 iPhone，并读取 Mac 本地已有的 iPhone Use 配置。新手机可用 iPhone Use 完成信任、开发签名和首次配置。设备和签名配置存放在本机。

## 恢复直接点击补丁

`source/patches/wda-direct-touch.patch` 保存了控制组件的直接点击补丁。在固定版本的 WebDriverAgent 干净检出中应用一次，再用仓库内的设置脚本重新构建和签名。以下命令使用默认设备目录。

```sh
git -C "$HOME/.local/share/iphone-use/runtime/WebDriverAgent" apply "$PWD/source/patches/wda-direct-touch.patch"
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer python3 source/server/wda_setup.py build
```

构建返回任务 ID 后，用 `python3 source/server/wda_setup.py status --job-id <任务 ID>` 查询进度。构建成功后在 App 中点「开启控制」。

使用说明见 [手机控制台使用说明](decisions-agent/README.md)。`source/` 保留了控制台所需的 iPhone Use 运行组件及其上游许可。
