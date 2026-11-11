import AppKit
import SwiftUI

struct LivePhoneFrame: View {
    let image: NSImage?
    var thumbnail = false
    var placeholder = "正在读取画面…"

    static func aspect(for image: NSImage?) -> CGFloat {
        guard let image, image.size.width > 0, image.size.height > 0 else { return 9 / 19.5 }
        return image.size.width / image.size.height
    }

    var body: some View {
        GeometryReader { geometry in
            let size = geometry.size
            let edge = min(size.width, size.height)
            let inset = edge * 0.024
            let radius = edge * 0.13
            let buttonWidth = edge * 0.022

            ZStack {
                if size.height > size.width {
                    sideButton(width: buttonWidth, height: size.height * 0.036)
                        .position(x: buttonWidth / 2, y: size.height * 0.17)
                    sideButton(width: buttonWidth, height: size.height * 0.065)
                        .position(x: buttonWidth / 2, y: size.height * 0.245)
                    sideButton(width: buttonWidth, height: size.height * 0.065)
                        .position(x: buttonWidth / 2, y: size.height * 0.335)
                    sideButton(width: buttonWidth, height: size.height * 0.10)
                        .position(x: size.width - buttonWidth / 2, y: size.height * 0.33)
                }

                RoundedRectangle(cornerRadius: radius)
                    .fill(LinearGradient(colors: [Color(white: 0.34), .black, Color(white: 0.24)],
                                         startPoint: .topLeading, endPoint: .bottomTrailing))
                    .overlay {
                        RoundedRectangle(cornerRadius: radius).strokeBorder(.white.opacity(0.26), lineWidth: 1)
                    }
                    .overlay {
                        ZStack {
                            Color.black
                            if let image {
                                Image(nsImage: image).resizable().scaledToFit()
                            } else if thumbnail {
                                Image(systemName: "iphone").foregroundStyle(.white.opacity(0.52))
                            } else {
                                Text(placeholder).font(.callout).foregroundStyle(.white.opacity(0.7))
                            }
                        }
                        .clipShape(RoundedRectangle(cornerRadius: radius - inset))
                        .padding(inset)
                    }
                    .padding(.horizontal, buttonWidth / 2)
                    .shadow(color: .black.opacity(thumbnail ? 0.12 : 0.23), radius: thumbnail ? 4 : 14,
                            y: thumbnail ? 2 : 8)
            }
        }
        .accessibilityHidden(true)
    }

    private func sideButton(width: CGFloat, height: CGFloat) -> some View {
        RoundedRectangle(cornerRadius: width / 2)
            .fill(LinearGradient(colors: [Color(white: 0.55), Color(white: 0.21)],
                                 startPoint: .leading, endPoint: .trailing))
            .frame(width: width, height: height)
    }
}

struct PhonePreviewWindow: View {
    @ObservedObject var model: ConsoleModel
    let deviceID: String
    @State private var zoom: CGFloat = 1
    @GestureState private var pinch: CGFloat = 1

    private let zoomRange: ClosedRange<CGFloat> = 0.75...2.5

    private var phone: Phone? { model.state.devices.first { $0.id == deviceID } }
    private var frame: NSImage? { phone?.canPreview == true ? model.previews[deviceID] : nil }
    private var status: String {
        if frame != nil { return "实时" }
        if phone?.locked == true { return "请解锁 iPhone" }
        if phone?.ready == true { return "正在读取画面" }
        return "画面未连接"
    }

    var body: some View {
        VStack(spacing: 10) {
            HStack(spacing: 8) {
                Text(phone?.name ?? "手机画面").fontWeight(.semibold).lineLimit(1)
                Circle().fill(frame == nil ? Color.secondary.opacity(0.5) : Color.mint)
                    .frame(width: 6, height: 6)
                    .accessibilityHidden(true)
                Text(status).foregroundStyle(frame == nil ? Color.secondary : Color(nsColor: .systemGreen))
            }
            .font(.callout)
            .padding(.horizontal, 14).padding(.vertical, 8)
            .background(.regularMaterial, in: Capsule())
            .overlay { Capsule().strokeBorder(.primary.opacity(0.08)) }
            .accessibilityElement(children: .combine)

            GeometryReader { geometry in
                let viewport = geometry.size
                let aspect = LivePhoneFrame.aspect(for: frame)
                let fitHeight = min(max(1, viewport.height - 26), max(1, viewport.width - 36) / aspect)
                let height = fitHeight * clamped(zoom * pinch)

                ScrollView([.horizontal, .vertical]) {
                    LivePhoneFrame(image: frame, placeholder: status)
                        .frame(width: height * aspect, height: height)
                        .padding(.horizontal, 18).padding(.vertical, 13)
                        .frame(minWidth: viewport.width, minHeight: viewport.height)
                }
                .defaultScrollAnchor(.center)
                .gesture(MagnifyGesture()
                    .updating($pinch) { value, magnification, _ in magnification = value.magnification }
                    .onEnded { value in zoom = clamped(zoom * value.magnification) })
            }

            HStack(spacing: 4) {
                Button { zoom = clamped(zoom - 0.25) } label: { Image(systemName: "minus") }
                    .keyboardShortcut("-", modifiers: .command)
                    .disabled(zoom <= zoomRange.lowerBound)
                    .help("缩小").accessibilityLabel("缩小")
                Button {
                    zoom = 1
                } label: {
                    Text(zoom == 1 ? "适应窗口" : "\(Int((zoom * 100).rounded()))%")
                        .font(.callout.monospacedDigit())
                        .frame(minWidth: 62)
                }
                .keyboardShortcut("0", modifiers: .command)
                .help("适应窗口").accessibilityLabel("适应窗口")
                Button { zoom = clamped(zoom + 0.25) } label: { Image(systemName: "plus") }
                    .keyboardShortcut("+", modifiers: .command)
                    .disabled(zoom >= zoomRange.upperBound)
                    .help("放大").accessibilityLabel("放大")
            }
            .buttonStyle(.borderless)
            .padding(.horizontal, 13).padding(.vertical, 9)
            .background(.regularMaterial, in: Capsule())
            .overlay { Capsule().strokeBorder(.primary.opacity(0.08)) }
        }
        .padding(.vertical, 14)
        .frame(minWidth: 380, minHeight: 420)
        .background {
            Canvas { context, size in
                let spacing: CGFloat = 19
                for x in stride(from: spacing / 2, to: size.width, by: spacing) {
                    for y in stride(from: spacing / 2, to: size.height, by: spacing) {
                        context.fill(Path(ellipseIn: CGRect(x: x, y: y, width: 1.5, height: 1.5)),
                                     with: .color(.primary.opacity(0.14)))
                    }
                }
            }
            .background(Color(nsColor: .windowBackgroundColor))
        }
        .onAppear {
            model.setPreviewVisible(deviceID, true)
            model.startRefreshing()
        }
        .onDisappear { model.setPreviewVisible(deviceID, false) }
    }

    private func clamped(_ scale: CGFloat) -> CGFloat {
        min(zoomRange.upperBound, max(zoomRange.lowerBound, scale))
    }
}
