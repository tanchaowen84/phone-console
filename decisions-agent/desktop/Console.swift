import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct PhoneRun: Decodable {
    let id: String
    let title: String
    let started: String
    let stopping: Bool
    let exit_code: Int?
    let logs: [String]
    let result: String

    var active: Bool { exit_code == nil }
    var status: String {
        if active { return stopping ? "停止中" : "运行中" }
        if stopping { return "已停止" }
        return exit_code == 0 ? "已完成" : "未完成"
    }

    var summary: String {
        if !result.isEmpty { return result }
        if active || stopping { return "" }
        return exit_code == 0 ? "" : logs.last ?? ""
    }
}

struct Phone: Decodable, Identifiable {
    let id: String
    let name: String
    let ready: Bool
    let locked: Bool
    let usb_connected: Bool
    let control: String
    let control_note: String
    let external_io: Bool
    let run: PhoneRun?
    let bindings: [String: String]

    var visible: Bool { usb_connected || ready || external_io || run?.active == true }
    var canPreview: Bool { ready && !locked }
    var status: String {
        if run?.active == true { return "运行中" }
        switch control {
        case "ready": return locked ? "手机已锁屏" : "可控制"
        case "starting": return "正在开启控制"
        case "failed": return "开启失败"
        case "pending": return "USB 已连接 · 待开启"
        case "first_setup": return "USB 已连接 · 待首次设置"
        default: return external_io ? "外接设备 · 连接待检查" : "未连接"
        }
    }
}

struct Plugin: Decodable, Identifiable {
    let id: String
    let name: String
    let kind: String
    let detail: String
    let enabled: Bool
    let can_toggle: Bool
}

private let controlRoles = [("画面来源", "screen-source", "wda"), ("点击与输入", "input-device", "wda"),
                            ("下一步判断", "decision", "codex"), ("执行与确认", "loop", "loop")]

struct ConsoleState: Decodable {
    let auth: String
    let devices: [Phone]
    let plugins: [Plugin]
}

struct PlannedTask: Decodable, Identifiable {
    let device_id: String
    let device_name: String
    let title: String
    let plugin_name: String
    var id: String { device_id }
}

struct Plan: Decodable {
    let id: String
    let reply: String
    let tasks: [PlannedTask]
    let bulk_sms: Bool?
}

struct DispatchResult: Decodable {
    struct Entry: Decodable {
        let device_name: String
        let status: String
    }
    let results: [Entry]
}

struct BridgeError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

@MainActor
final class DesktopClient {
    private let token = UUID().uuidString + UUID().uuidString
    private var process: Process?
    private var base: URL?
    private let session = URLSession(configuration: .ephemeral)

    func connect() async throws {
        if base != nil { return }
        guard let backend = Bundle.main.resourceURL?.appendingPathComponent("backend/app/desktop_backend.py"),
              FileManager.default.fileExists(atPath: backend.path) else {
            throw BridgeError(message: "找不到手机控制台后端")
        }
        let task = Process()
        let pipe = Pipe()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        task.arguments = ["python3", "-u", backend.path]
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + (environment["PATH"] ?? "/usr/bin:/bin:/usr/sbin")
        environment["PHONE_DESKTOP_TOKEN"] = token
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        let developerDirectory = "/Applications/Xcode.app/Contents/Developer"
        if environment["DEVELOPER_DIR"] == nil && FileManager.default.fileExists(atPath: developerDirectory) {
            environment["DEVELOPER_DIR"] = developerDirectory
        }
        task.environment = environment
        task.standardOutput = pipe
        task.standardError = FileHandle.standardError
        try task.run()
        process = task
        let handle = pipe.fileHandleForReading
        let port = try await Task.detached {
            defer { try? handle.close() }
            var line = Data()
            while let byte = try handle.read(upToCount: 1), !byte.isEmpty, byte.first != 10 {
                line.append(byte)
            }
            guard let handshake = try JSONSerialization.jsonObject(with: line) as? [String: Int],
                  let port = handshake["port"] else {
                throw BridgeError(message: "本地控制台启动失败")
            }
            return port
        }.value
        base = URL(string: "http://127.0.0.1:\(port)")
    }

    func request<T: Decodable>(_ path: String, body: [String: Any]? = nil) async throws -> T {
        guard let base, let url = URL(string: path, relativeTo: base) else {
            throw BridgeError(message: "本地控制台正在启动")
        }
        var request = URLRequest(url: url, timeoutInterval: path == "/plan" ? 130 : 15)
        request.setValue(token, forHTTPHeaderField: "Authorization")
        if let body {
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, response) = try await session.data(for: request)
        if let status = (response as? HTTPURLResponse)?.statusCode, status != 200 {
            let result = try? JSONDecoder().decode([String: String].self, from: data)
            throw BridgeError(message: result?["error"] ?? "控制台请求失败")
        }
        return try JSONDecoder().decode(T.self, from: data)
    }

    func stop() {
        if let process, process.isRunning { process.terminate() }
    }
}

@MainActor
final class ConsoleModel: ObservableObject {
    let client = DesktopClient()
    @Published var state = ConsoleState(auth: "检查 Codex 登录…", devices: [], plugins: [])
    @Published var selected: String? { didSet { updateScreenFeeds() } }
    @Published var alert = ""
    @Published var plan: Plan?
    @Published var reply = ""
    @Published var planning = false
    @Published var connected = false
    @Published private(set) var previews: [String: NSImage] = [:]
    @Published var preparationErrors: [String: String] = [:]
    private var history: [String] = []
    private var consoleVisible = false
    private var openPreviews: Set<String> = []
    private var screenFeeds: [String: Task<Void, Never>] = [:]
    private var stateFeed: Task<Void, Never>?

    var phone: Phone? { state.devices.first { $0.id == selected } }
    var visiblePhones: [Phone] { state.devices.filter(\.visible) }

    func setConsoleVisible(_ visible: Bool) {
        consoleVisible = visible
        updateScreenFeeds()
    }

    func setPreviewVisible(_ id: String, _ visible: Bool) {
        if visible { openPreviews.insert(id) } else { openPreviews.remove(id) }
        updateScreenFeeds()
    }

    private func updateScreenFeeds() {
        var wanted = openPreviews
        if consoleVisible, let selected { wanted.insert(selected) }
        let ready = Set(state.devices.filter(\.canPreview).map(\.id))
        wanted.formIntersection(ready)

        for id in Set(screenFeeds.keys).subtracting(wanted) {
            screenFeeds.removeValue(forKey: id)?.cancel()
            previews[id] = nil
        }
        for id in wanted where screenFeeds[id] == nil {
            screenFeeds[id] = Task { await watchScreen(id) }
        }
    }

    private func watchScreen(_ id: String) async {
        var lastFrame = ""
        while !Task.isCancelled {
            do {
                let response: [String: String] = try await client.request("/screen?device=" + id)
                guard !Task.isCancelled else { return }
                if let frame = response["frame"], frame != lastFrame,
                   let data = Data(base64Encoded: frame), let image = NSImage(data: data) {
                    previews[id] = image
                    lastFrame = frame
                }
            } catch {
                guard !Task.isCancelled else { return }
                previews[id] = nil
                lastFrame = ""
            }
            try? await Task.sleep(for: .milliseconds(350))
        }
    }

    func startRefreshing() {
        guard stateFeed == nil else { return }
        stateFeed = Task {
            await connect()
            while !Task.isCancelled {
                try? await Task.sleep(for: .seconds(2))
                if !Task.isCancelled { await refresh() }
            }
        }
    }

    func shutdown() {
        stateFeed?.cancel()
        for feed in screenFeeds.values { feed.cancel() }
        client.stop()
    }

    func connect() async {
        guard !connected else { return }
        do {
            try await client.connect()
            connected = true
            await refresh()
        } catch {
            alert = error.localizedDescription
        }
    }

    func refresh() async {
        guard connected else { return }
        do {
            let snapshot: ConsoleState = try await client.request("/state")
            state = snapshot
            if selected == nil || !visiblePhones.contains(where: { $0.id == selected }) {
                selected = visiblePhones.first?.id
            } else {
                updateScreenFeeds()
            }
        } catch {
            alert = error.localizedDescription
        }
    }

    func prepareDevice(_ id: String) async {
        preparationErrors[id] = nil
        do {
            let _: [String: Bool] = try await client.request("/device/prepare", body: ["device_id": id])
            await refresh()
        } catch {
            preparationErrors[id] = error.localizedDescription
        }
    }

    @discardableResult
    func change(_ path: String, _ body: [String: Any]) async -> Bool {
        do {
            let _: [String: Bool] = try await client.request(path, body: body)
            await refresh()
            return true
        } catch {
            alert = error.localizedDescription
            return false
        }
    }

    func propose(_ goal: String) async {
        planning = true
        plan = nil
        reply = ""
        defer { planning = false }
        do {
            let plan: Plan = try await client.request("/plan", body: [
                "goal": goal, "history": history, "selected_device_id": selected ?? "",
            ])
            history = Array((history + ["用户：" + goal, "AI：" + plan.reply]).suffix(8))
            reply = plan.reply
            self.plan = plan.tasks.isEmpty ? nil : plan
        } catch {
            alert = error.localizedDescription
        }
    }

    func execute(_ plan: Plan) async {
        self.plan = nil
        planning = true
        defer { planning = false }
        do {
            let outcome: DispatchResult = try await client.request("/execute", body: ["id": plan.id])
            reply = outcome.results.map { "\($0.device_name)  \($0.status)" }.joined(separator: "\n")
            await refresh()
        } catch {
            alert = error.localizedDescription
        }
    }
}

struct PhoneOutline: View {
    let index: Int
    let selected: Bool
    let offline: Bool
    var add = false

    var body: some View {
        ZStack(alignment: .top) {
            RoundedRectangle(cornerRadius: 24)
                .fill(selected ? Color.accentColor.opacity(0.08) : Color.secondary.opacity(0.04))
            RoundedRectangle(cornerRadius: 24)
                .stroke(selected ? Color.accentColor : Color.secondary.opacity(0.48),
                        style: StrokeStyle(lineWidth: selected ? 2.5 : 1.5, dash: add ? [5, 4] : []))
            if !add {
                Capsule().fill(Color.secondary.opacity(0.55))
                    .frame(width: 25, height: 6).padding(.top, 8)
            }
            Group {
                if add {
                    Image(systemName: "plus").font(.title2)
                } else {
                    Text("\(index)").font(.system(size: 36, weight: .light))
                }
            }
            .foregroundStyle(offline || add ? .secondary : .primary)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(width: 108, height: 195)
    }
}

struct ConsoleView: View {
    @ObservedObject var model: ConsoleModel
    @Environment(\.openWindow) private var openWindow
    @State private var command = ""
    @State private var lastCommand = ""
    @State private var showPlugins = false
    @State private var showAdd = false
    @State private var showTask = false
    @State private var deviceSettingsID: String?
    @State private var showProgress = true
    @State private var preparing: String?
    @FocusState private var commandFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    devices
                    if model.planning || model.plan != nil || !model.reply.isEmpty { assistant }
                    if model.state.devices.contains(where: { $0.run != nil }) { progress }
                }
                .padding(.horizontal, 28).padding(.vertical, 18)
                .frame(maxWidth: 920)
                .frame(maxWidth: .infinity)
            }
            Divider()
            composer
        }
        .frame(minWidth: 760, minHeight: 560)
        .alert("手机控制台", isPresented: Binding(get: { !model.alert.isEmpty }, set: { if !$0 { model.alert = "" } })) {
            Button("好") { model.alert = "" }
        } message: { Text(model.alert) }
        .onAppear {
            model.setConsoleVisible(true)
            model.startRefreshing()
        }
        .onDisappear { model.setConsoleVisible(false) }
    }

    var toolbar: some View {
        HStack {
            Label(model.state.auth, systemImage: "sparkle")
                .font(.callout).foregroundStyle(.secondary)
            if model.state.auth != "ChatGPT 已登录" && model.connected {
                Button("刷新") { Task { await model.change("/auth", [:]) } }.buttonStyle(.borderless)
            }
            Spacer()
            Button { showPlugins.toggle() } label: { Label("插件", systemImage: "puzzlepiece") }
                .popover(isPresented: $showPlugins, arrowEdge: .bottom) { PluginList(model: model) }
        }
        .padding(.horizontal, 28).padding(.top, 15)
    }

    var devices: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 176, maximum: 230), spacing: 20, alignment: .top)], spacing: 20) {
            ForEach(Array(model.visiblePhones.enumerated()), id: \.element.id) { index, phone in
                let starting = preparing == phone.id || phone.control == "starting"
                let error = model.preparationErrors[phone.id]
                let failed = !phone.ready && (phone.control == "failed" || error != nil)
                VStack(spacing: 9) {
                    Button {
                        model.selected = phone.id
                        if phone.ready {
                            openWindow(id: "phone-screen", value: phone.id)
                        } else {
                            deviceSettingsID = phone.id
                        }
                    } label: {
                        if phone.ready {
                            LivePhoneFrame(image: model.previews[phone.id], thumbnail: true)
                                .aspectRatio(LivePhoneFrame.aspect(for: model.previews[phone.id]), contentMode: .fit)
                                .frame(width: 108, height: 195)
                                .overlay(alignment: .bottomTrailing) {
                                    Image(systemName: "arrow.up.left.and.arrow.down.right")
                                        .font(.system(size: 10, weight: .semibold))
                                        .padding(6)
                                        .background(.regularMaterial, in: Circle())
                                        .accessibilityHidden(true)
                                }
                        } else {
                            PhoneOutline(index: index + 1, selected: model.selected == phone.id, offline: true)
                        }
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel(phone.ready ? "放大查看 \(phone.name) 画面" : phone.name)
                    .help(phone.ready ? "放大查看手机画面" : "打开设备设置")
                    HStack(spacing: 6) {
                        Text(phone.name).lineLimit(1)
                        Circle().fill(phone.canPreview ? Color.green : phone.usb_connected ? Color.orange : Color.secondary.opacity(0.55))
                            .frame(width: 6, height: 6)
                        Text(starting ? "正在开启控制" : failed ? "开启失败" : phone.status)
                            .foregroundStyle(.secondary).font(.caption)
                        Button {
                            model.selected = phone.id
                            deviceSettingsID = phone.id
                        } label: {
                            Image(systemName: "gearshape")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                                .frame(width: 22, height: 22)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("\(phone.name) 设备设置")
                        .help("设备设置")
                        .popover(isPresented: Binding(get: { deviceSettingsID == phone.id },
                                                      set: { if !$0 && deviceSettingsID == phone.id { deviceSettingsID = nil } })) {
                            DeviceSettings(model: model, deviceID: phone.id)
                        }
                    }
                    if failed && !starting {
                        Text(error ?? phone.control_note)
                            .font(.caption).foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if model.selected == phone.id && (phone.ready || phone.external_io) {
                        Button("下发任务") { showTask.toggle() }
                            .buttonStyle(.borderedProminent)
                            .popover(isPresented: $showTask, arrowEdge: .bottom) {
                                TaskForm(model: model, phone: phone) { showTask = false }
                            }
                    } else if starting {
                        ProgressView().controlSize(.small).frame(height: 25)
                            .accessibilityLabel("正在开启控制")
                    } else if model.selected == phone.id && (phone.control == "pending" || phone.control == "failed") {
                        Button(failed ? "重试" : "开启控制") {
                            preparing = phone.id
                            Task {
                                await model.prepareDevice(phone.id)
                                preparing = nil
                            }
                        }.buttonStyle(.borderedProminent).disabled(preparing != nil)
                    } else if model.selected == phone.id && phone.control == "first_setup" {
                        Button("首次设置") { deviceSettingsID = phone.id }.buttonStyle(.bordered)
                    } else {
                        Color.clear.frame(height: 25)
                    }
                }
                .frame(maxWidth: .infinity)
            }
            Button { showAdd.toggle() } label: {
                VStack(spacing: 9) {
                    PhoneOutline(index: 0, selected: false, offline: false, add: true)
                    Text("连接手机").foregroundStyle(.secondary)
                    Color.clear.frame(height: 25)
                }
                .frame(maxWidth: .infinity)
            }
            .buttonStyle(.plain)
            .popover(isPresented: $showAdd, arrowEdge: .bottom) {
                VStack(alignment: .leading, spacing: 14) {
                    Text("连接手机").font(.headline)
                    Text("用 USB 数据线连接并解锁 iPhone。手机会自动出现在这里。").foregroundStyle(.secondary)
                    Text("如果手机询问是否信任此电脑，请在手机上点「信任」。").foregroundStyle(.secondary)
                    Button("重新检测") { Task { await model.refresh() } }.buttonStyle(.borderedProminent)
                }.padding(18).frame(width: 330)
            }
        }
        .padding(.top, 10)
    }

    var assistant: some View {
        VStack(alignment: .leading, spacing: 10) {
            if model.planning {
                HStack { ProgressView().controlSize(.small); Text("AI 正在处理…") }
            }
            if !model.reply.isEmpty {
                Text(model.reply).textSelection(.enabled)
            }
            if let plan = model.plan {
                ForEach(plan.tasks) { task in
                    Divider()
                    HStack {
                        Text(task.device_name).fontWeight(.medium)
                        Spacer()
                        if !task.plugin_name.isEmpty { Text(task.plugin_name).foregroundStyle(.secondary) }
                        Text(task.title).lineLimit(2)
                    }
                }
                HStack {
                    Spacer()
                    Button("编辑指令") {
                        command = lastCommand
                        model.plan = nil
                        commandFocused = true
                    }
                    Button(plan.bulk_sms == true ? "确认发送整份名单" : "执行任务") {
                        Task { await model.execute(plan) }
                    }.buttonStyle(.borderedProminent)
                }
            }
        }
        .font(.callout)
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.65), in: RoundedRectangle(cornerRadius: 11))
    }

    var progress: some View {
        DisclosureGroup("任务进度", isExpanded: $showProgress) {
            VStack(spacing: 0) {
                ForEach(model.state.devices) { phone in
                    if let run = phone.run {
                        Divider()
                        ProgressRow(model: model, phone: phone, run: run)
                    }
                }
            }.padding(.top, 7)
        }.font(.headline)
    }

    var composer: some View {
        HStack(spacing: 10) {
            Image(systemName: "sparkle").foregroundStyle(.secondary)
            TextField("让 AI 操作设备…", text: $command)
                .textFieldStyle(.plain).focused($commandFocused).onSubmit(sendCommand)
            Button(action: sendCommand) { Image(systemName: "arrow.up") }
                .buttonStyle(.borderedProminent)
                .accessibilityLabel("发送指令")
                .disabled(!model.connected || model.planning || command.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
        }
        .padding(10)
        .background(.background, in: RoundedRectangle(cornerRadius: 13))
        .overlay { RoundedRectangle(cornerRadius: 13).strokeBorder(.quaternary) }
        .padding(16)
    }

    func sendCommand() {
        let input = command.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !input.isEmpty, !model.planning, model.connected else { return }
        lastCommand = input
        command = ""
        Task { await model.propose(input) }
    }
}

struct ProgressRow: View {
    @ObservedObject var model: ConsoleModel
    let phone: Phone
    let run: PhoneRun
    @State private var showOutput = false

    var body: some View {
        HStack(spacing: 12) {
            Text(phone.name).frame(width: 90, alignment: .leading)
            Text(run.title).lineLimit(2)
            Spacer()
            if !run.summary.isEmpty {
                Text(run.summary).lineLimit(2).textSelection(.enabled)
            }
            Text(run.status).foregroundStyle(.secondary)
            if run.active {
                Button("停止") {
                    Task { await model.change("/stop", ["device_id": phone.id, "run_id": run.id]) }
                }.buttonStyle(.borderless).disabled(run.stopping)
            }
            Button { showOutput.toggle() } label: { Image(systemName: "ellipsis.circle") }
                .buttonStyle(.borderless).help("任务记录").accessibilityLabel("任务记录")
                .popover(isPresented: $showOutput) {
                    ScrollView {
                        Text(run.logs.isEmpty ? "等待任务输出…" : run.logs.joined(separator: "\n"))
                            .font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading).padding(14)
                    }.frame(width: 420, height: 220)
                }
        }
        .font(.callout)
        .padding(.vertical, 9)
    }
}

func chooseFile(types: [UTType] = []) -> String? {
    let panel = NSOpenPanel()
    panel.canChooseDirectories = false
    panel.canChooseFiles = true
    panel.allowedContentTypes = types
    return panel.runModal() == .OK ? panel.url?.path : nil
}

struct PluginList: View {
    @ObservedObject var model: ConsoleModel

    private var corePlugins: [Plugin] { model.state.plugins.filter { $0.can_toggle && $0.kind != "task" } }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                HStack {
                    Text("核心插件").font(.headline).accessibilityAddTraits(.isHeader)
                    if let phone = model.phone {
                        Text(phone.name).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    }
                    Spacer()
                    Button("添加插件…") {
                        if let path = chooseFile(types: [.json]) {
                            Task { await model.change("/plugin/add", ["path": path]) }
                        }
                    }
                }
                ForEach(controlRoles.prefix(2), id: \.1) { label, role, fallback in
                    CorePluginPicker(model: model, phone: model.phone, label: label, role: role, fallback: fallback)
                }
                if !corePlugins.isEmpty {
                    DisclosureGroup("已添加的核心插件") {
                        ForEach(corePlugins) { plugin in pluginRow(plugin) }
                    }
                }

                Divider()
                Text("任务插件").font(.headline).accessibilityAddTraits(.isHeader)
                ForEach(model.state.plugins.filter { $0.can_toggle && $0.kind == "task" }) { plugin in
                    pluginRow(plugin)
                }
            }.padding(17)
        }.frame(width: 320, height: corePlugins.isEmpty ? 214 : 275)
    }

    private func pluginRow(_ plugin: Plugin) -> some View {
        HStack(spacing: 12) {
            Text(plugin.name).help(plugin.detail)
            Spacer()
            Toggle(plugin.name, isOn: Binding(get: { plugin.enabled }, set: { enabled in
                Task { await model.change("/plugin/toggle", ["id": plugin.id, "enabled": enabled]) }
            }))
            .labelsHidden().toggleStyle(.switch).controlSize(.small)
        }
    }
}

struct CorePluginPicker: View {
    @ObservedObject var model: ConsoleModel
    let phone: Phone?
    let label: String
    let role: String
    let fallback: String

    var body: some View {
        HStack(spacing: 10) {
            Text(label)
            Spacer()
            Picker(label, selection: Binding(get: { phone?.bindings[role] ?? fallback }, set: { plugin in
                guard let phone, phone.control != "first_setup" else { return }
                Task { await model.change("/device/bind", ["device_id": phone.id, "role": role, "id": plugin]) }
            })) {
                ForEach(model.state.plugins.filter { $0.id == fallback || $0.kind == role }) { plugin in
                    let name = plugin.id == "wda" ? "iPhone USB" : plugin.name
                    Text(name + (plugin.enabled ? "" : "（停用）")).tag(plugin.id)
                        .disabled(!plugin.enabled)
                }
            }
            .labelsHidden()
            .disabled(phone == nil || phone?.control == "first_setup")
        }
    }
}

struct DeviceSettings: View {
    @ObservedObject var model: ConsoleModel
    let deviceID: String
    @State private var name = ""

    var body: some View {
        if let phone = model.state.devices.first(where: { $0.id == deviceID }) {
            VStack(alignment: .leading, spacing: 10) {
                Text(phone.control == "first_setup" ? "首次设置" : "设备设置").font(.headline)
                Text(phone.status).font(.caption).foregroundStyle(.secondary)
                if !phone.control_note.isEmpty {
                    Text(phone.control_note).fixedSize(horizontal: false, vertical: true).foregroundStyle(.secondary)
                }
                if phone.control == "first_setup" {
                    Text("完成信任后，请在 iPhone Use 完成首次手机控制设置。")
                        .fixedSize(horizontal: false, vertical: true).foregroundStyle(.secondary)
                } else {
                    HStack {
                        TextField("设备名称", text: $name).textFieldStyle(.roundedBorder)
                        Button("保存") {
                            Task { await model.change("/device/rename", ["device_id": deviceID, "name": name]) }
                        }.disabled(name == phone.name || name.isEmpty)
                    }
                    DisclosureGroup("通用任务控制方式") {
                        ForEach(controlRoles, id: \.1) { label, role, fallback in
                            CorePluginPicker(model: model, phone: phone, label: label, role: role, fallback: fallback)
                        }
                    }
                }
            }
            .frame(width: 254)
            .padding(16)
            .onAppear { name = phone.name }
        }
    }
}

struct TaskForm: View {
    @ObservedObject var model: ConsoleModel
    let phone: Phone
    let close: () -> Void
    @State private var selected = "once"
    @State private var goal = ""
    @State private var path = ""
    @State private var mode = "preview"
    @State private var watchMode = "terra"
    @State private var recipient = ""
    @State private var submitting = false

    var taskPlugins: [Plugin] { model.state.plugins.filter { $0.kind == "task" && $0.enabled } }
    var title: String {
        if selected == "watch" { return "开始值守" }
        if selected == "sms" && mode == "send" { return "发送整份名单" }
        if selected == "sms" && mode == "preview" { return "查看名单" }
        return "下发任务"
    }
    var valid: Bool {
        taskPlugins.contains { $0.id == selected } && !submitting
        && ((selected == "watch" || selected == "sms") ? !path.isEmpty : !goal.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(phone.name).font(.headline)
            Picker("要做什么", selection: $selected) {
                ForEach(taskPlugins) { Text($0.name).tag($0.id) }
            }
            .onChange(of: selected) { _, _ in path = "" }
            if selected == "watch" || selected == "sms" {
                HStack {
                    Text(path.isEmpty ? (selected == "watch" ? "知识库" : "短信名单") : URL(fileURLWithPath: path).lastPathComponent)
                        .lineLimit(1)
                    Spacer()
                    Button("选择文件…") {
                        if let file = chooseFile(types: selected == "watch" ? [.json] : [.commaSeparatedText]) { path = file }
                    }
                }
                if selected == "watch" {
                    DisclosureGroup("值守设置") {
                        Picker("模式", selection: $watchMode) {
                            Text("常规值守").tag("terra")
                            Text("兼容模式").tag("decisions")
                        }
                        TextField("指定实发会话（留空仅生成草稿）", text: $recipient).textFieldStyle(.roundedBorder)
                    }
                } else {
                    Picker("操作", selection: $mode) {
                        Text("查看名单").tag("preview")
                        Text("准备草稿").tag("draft")
                        Text("发送整份名单").tag("send")
                    }
                }
            } else {
                TextField("任务说明", text: $goal, axis: .vertical)
                    .textFieldStyle(.roundedBorder).lineLimit(3...5)
            }
            HStack {
                Spacer()
                Button("取消", action: close)
                Button(title) {
                    submitting = true
                    Task {
                        if await model.change("/start", [
                            "device_id": phone.id, "plugin": selected, "goal": goal, "path": path,
                            "mode": selected == "watch" ? watchMode : mode, "recipient": recipient,
                        ]) { close() }
                        submitting = false
                    }
                }.buttonStyle(.borderedProminent).disabled(!valid)
            }
        }
        .onAppear { selected = taskPlugins.first?.id ?? "" }
        .padding(17).frame(width: 360)
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    var shutdown: (() -> Void)?
    func applicationWillTerminate(_ notification: Notification) { shutdown?() }
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct PhoneConsoleApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var delegate
    @StateObject private var model = ConsoleModel()
    @Environment(\.openWindow) private var openWindow

    var body: some Scene {
        Window("手机控制台", id: "main") {
            ConsoleView(model: model)
                .onAppear { delegate.shutdown = { model.shutdown() } }
        }
        .defaultSize(width: 880, height: 690)
        .commands {
            CommandGroup(after: .windowArrangement) {
                Button("打开手机控制台") { openWindow(id: "main") }
            }
        }

        WindowGroup("手机画面", id: "phone-screen", for: String.self) { $deviceID in
            if let deviceID {
                PhonePreviewWindow(model: model, deviceID: deviceID)
            }
        }
        .defaultSize(width: 520, height: 740)
        .windowResizability(.contentMinSize)
        .commandsRemoved()
    }
}
