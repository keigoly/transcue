// subtitle_window — Phase 3-C: フローティング字幕ウィンドウ(受信=相手の英語→日本語)。
//
// 常時最前面・半透明の NSPanel。既定はドラッグで移動でき、⌃⌥L で「固定(クリック透過)」に切替。
// 移動後の位置は記憶する。stdin から JSON 行で字幕を受け、トークン逐次でストリーム更新する。
// stt_helper / tap_helper と同じ「stdin で駆動する Swift helper」パターン。
//
// stdin プロトコル(1 行 1 コマンド):
//   {"type":"subtitle","en":"…","ja":"…"}   現在の字幕を更新(en=原文, ja=訳文。ja は逐次伸びる)
//   {"type":"clear"}                          字幕を消す(フェードアウト)
//   {"type":"quit"}                           終了
// 停止は SIGINT/SIGTERM でも可。stdin EOF では終了しない(orchestrator 起動時に閉じるため)。
// 診断は stderr へ JSON。
//
// ビルド: swiftc -parse-as-library -O main.swift -o subtitle-window
// 単体確認(サンプル字幕を表示): ./subtitle-window --demo
// オプション: --clear-after <秒>  字幕更新が無いまま この秒数 経過したら自動で消す(既定 5, 0=無効)

import AppKit
import Carbon
import Foundation

func diag(_ fields: [String: Any]) {
    var f = fields
    f["t_ms"] = Int(Date().timeIntervalSince1970 * 1000)
    if let d = try? JSONSerialization.data(withJSONObject: f, options: [.sortedKeys]),
       let s = String(data: d, encoding: .utf8) {
        FileHandle.standardError.write(Data((s + "\n").utf8))
    }
}

// 単一インスタンス。Carbon ホットキーのハンドラ(C 関数)から参照するためグローバルに置く。
var gController: SubtitleController?

// 画面下部中央のオーバーレイ。半透明の角丸ボックスに en(小・淡)/ ja(大・白)を積む。
final class SubtitleController {
    let panel: NSPanel
    private let box: NSView
    private let enLabel: NSTextField
    private let jaLabel: NSTextField
    private(set) var locked = false   // false=移動可(ドラッグ可) / true=固定(クリック透過)
    private var clearTimer: Timer?
    private let autoClearAfter: TimeInterval   // 無応答がこの秒数続いたら字幕を消す(0=無効)

    init() {
        // --clear-after <秒>: 相手の英語が止まり字幕更新が無いまま この秒数 経過したら消す(既定 5, 0=無効)。
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--clear-after"), i + 1 < args.count, let v = Double(args[i + 1]) {
            autoClearAfter = max(0, v)
        } else {
            autoClearAfter = 5.0
        }
        let sf = NSScreen.main?.frame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let w = min(1000, sf.width * 0.72)
        let h: CGFloat = 150
        let rect = NSRect(x: sf.midX - w / 2, y: sf.minY + 90, width: w, height: h)

        panel = NSPanel(contentRect: rect,
                        styleMask: [.borderless, .nonactivatingPanel, .resizable],  // エッジ/角ドラッグでリサイズ
                        backing: .buffered, defer: false)
        panel.level = .screenSaver                               // 全画面 Zoom より前面に出す(通常ウィンドウ含め最前面)
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        panel.ignoresMouseEvents = false                         // 既定は移動可(ドラッグで動かせる)。⌃⌥L で固定=透過へ
        panel.isMovableByWindowBackground = true                 // 背景ドラッグでウィンドウ移動
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
        panel.isReleasedWhenClosed = false
        panel.becomesKeyOnlyIfNeeded = true

        box = NSView(frame: NSRect(x: 0, y: 0, width: w, height: h))
        box.wantsLayer = true
        box.layer?.backgroundColor = NSColor.black.withAlphaComponent(0.55).cgColor
        box.layer?.cornerRadius = 18
        box.layer?.masksToBounds = true
        box.layer?.borderWidth = 2                               // 移動可のとき枠を表示(掴める目印)
        box.layer?.borderColor = NSColor.controlAccentColor.withAlphaComponent(0.9).cgColor
        box.autoresizingMask = [.width, .height]

        func makeLabel(size: CGFloat, weight: NSFont.Weight, color: NSColor) -> NSTextField {
            let l = NSTextField(labelWithString: "")
            l.font = .systemFont(ofSize: size, weight: weight)
            l.textColor = color
            l.alignment = .center
            l.maximumNumberOfLines = 3
            l.lineBreakMode = .byWordWrapping
            l.drawsBackground = false
            l.isBezeled = false
            l.isEditable = false
            l.isSelectable = false
            l.cell?.wraps = true
            l.cell?.isScrollable = false
            return l
        }
        let pad: CGFloat = 18
        enLabel = makeLabel(size: 15, weight: .medium, color: NSColor.white.withAlphaComponent(0.65))
        jaLabel = makeLabel(size: 30, weight: .semibold, color: .white)
        enLabel.frame = NSRect(x: pad, y: h - 40, width: w - pad * 2, height: 24)
        enLabel.autoresizingMask = [.width, .minYMargin]
        jaLabel.frame = NSRect(x: pad, y: pad, width: w - pad * 2, height: h - 54)
        jaLabel.autoresizingMask = [.width, .height]

        box.addSubview(enLabel)
        box.addSubview(jaLabel)
        panel.contentView = box
        _ = panel.setFrameAutosaveName("zrt-subtitle-panel")     // 移動後の位置・サイズを記憶(次回も同じ)
        panel.minSize = NSSize(width: 320, height: 80)           // リサイズ下限
        panel.maxSize = NSSize(width: 2400, height: 600)         // リサイズ上限
        NotificationCenter.default.addObserver(forName: NSWindow.didResizeNotification,
                                               object: panel, queue: .main) { [weak self] _ in
            self?.rescale()                                      // リサイズに合わせて文字サイズを追従
        }
        rescale()
        panel.alphaValue = 0.0                                   // 字幕が来るまで非表示
        panel.orderFrontRegardless()                             // アクティブ化せず前面へ
    }

    // ウィンドウ高に合わせてフォントを拡縮(大きくすると字も大きく)。基準 h=150 で ja=30 / en=15pt。
    func rescale() {
        let scale = max(0.6, min(2.5, panel.frame.height / 150.0))
        jaLabel.font = .systemFont(ofSize: 30 * scale, weight: .semibold)
        enLabel.font = .systemFont(ofSize: 15 * scale, weight: .medium)
    }

    // ⌃⌥L: 移動可(ドラッグ)⇄ 固定(クリック透過)を切り替える。
    func toggleLock() {
        locked.toggle()
        panel.ignoresMouseEvents = locked                        // 固定 = クリック透過
        box.layer?.borderWidth = locked ? 0 : 2                  // 固定時は枠を消してすっきり
        panel.alphaValue = 1.0                                   // 切替が見えるように表示
        diag(["phase": "lock", "locked": locked,
              "hint": locked ? "固定(クリック透過)" : "移動可(ドラッグ)"])
    }

    func set(en: String, ja: String) {
        enLabel.stringValue = en
        jaLabel.stringValue = ja
        panel.alphaValue = 1.0
        scheduleAutoClear()
    }

    // 字幕更新のたびにタイマーをリセット。無応答が autoClearAfter 秒続いたら消す(相手が話し続けている間は消えない)。
    private func scheduleAutoClear() {
        clearTimer?.invalidate()
        guard autoClearAfter > 0 else { return }
        clearTimer = Timer.scheduledTimer(withTimeInterval: autoClearAfter, repeats: false) { [weak self] _ in
            diag(["phase": "auto_clear", "after_s": self?.autoClearAfter ?? 0])
            self?.clear()
        }
    }

    func clear() {
        panel.alphaValue = 0.0
    }
}

@main
struct SubtitleWindowApp {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)                      // Dock 非表示のエージェントとして UI を出す
        let controller = SubtitleController()
        gController = controller

        // ⌃⌥L: 「移動可 ⇄ 固定(クリック透過)」トグル(Carbon RegisterEventHotKey, Accessibility 不要)。
        let lockHandler: EventHandlerUPP = { (_, _, _) -> OSStatus in
            DispatchQueue.main.async { gController?.toggleLock() }
            return noErr
        }
        var lockSpec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetEventDispatcherTarget(), lockHandler, 1, &lockSpec, nil, nil)
        var lockRef: EventHotKeyRef?
        let lockID = EventHotKeyID(signature: OSType(0x5A52_5453), id: 1)  // 'ZRTS'
        let lockSt = RegisterEventHotKey(UInt32(kVK_ANSI_L), UInt32(controlKey | optionKey), lockID,
                                         GetEventDispatcherTarget(), 0, &lockRef)
        diag(["phase": "hotkey", "combo": "⌃⌥L", "status": lockSt, "note": "移動/固定トグル"])

        // SIGINT/SIGTERM でクリーン終了(orchestrator の kill 用)。
        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        let onSig: @convention(block) () -> Void = { NSApp.terminate(nil) }
        let sigint = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        sigint.setEventHandler(handler: onSig)
        sigterm.setEventHandler(handler: onSig)
        sigint.resume()
        sigterm.resume()

        if CommandLine.arguments.contains("--demo") {
            controller.set(en: "The budget is expected to increase by about 10%.",
                           ja: "予算は約10%増加する見込みです。")
        }

        // stdin: 1 行 1 コマンド(JSON)。UI 更新はメインスレッドへ。
        Thread.detachNewThread {
            while let line = readLine(strippingNewline: true) {
                let t = line.trimmingCharacters(in: .whitespaces)
                if t.isEmpty { continue }
                guard let data = t.data(using: .utf8),
                      let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { continue }
                let type = obj["type"] as? String ?? "subtitle"
                DispatchQueue.main.async {
                    switch type {
                    case "quit": NSApp.terminate(nil)
                    case "clear": controller.clear()
                    default: controller.set(en: obj["en"] as? String ?? "", ja: obj["ja"] as? String ?? "")
                    }
                }
            }
            // stdin EOF では終了しない。停止は quit / SIGTERM。
        }

        _ = sigint
        _ = sigterm
        diag(["phase": "ready", "note": "subtitle window up"])
        app.run()
    }
}
