// control_panel — Phase 3-H: 会議中に英語通訳の ON/OFF を1クリックで切り替える GUI。
//
// 常時最前面の小さなフローティングボタン。緑「🔊 英語 ON」⇄ 赤「🔴 英語 OFF」。
// クリック または ⌃⌥P で {"type":"toggle"} を stdout に出す(hotkey_helper の GUI 版・protocol 互換)。
// pipeline.py が --panel でこれを起動し、toggle で送信(TTS→相手)を一時停止/再開する。
// 非アクティブパネルなので、クリックしても Zoom のフォーカスを奪わない。
//
// stdin: "quit" で終了。停止は SIGINT/SIGTERM でも可。
// --start-off: 英語OFF(赤)状態で起動する(会議参加時の自動起動フローの既定。地声パススルー中)。
// --only-app <bundleID,...>: そのどれかが最前面のときだけ表示(例: us.zoom.xos,com.google.Chrome = Zoom か Chrome の Meet)。
//                        省略時は常時表示(従来動作)。⌃⌥P は非表示中でも有効(グローバル)。
// --meeting-title "ミーティング,Meeting": --only-app と併用。最前面の対象アプリ・ウィンドウの
//                        タイトルがこのいずれかを含むときだけ表示(Zoom「ミーティング」ウィンドウのみ、
//                        「Zoom Workplace」ホーム画面では非表示)。ウィンドウ・タイトル取得には
//                        画面収録許可が要る(本アプリは音声タップで既に付与済みの想定)。
// テスト用: --emit-toggles "1,2" で 1秒後・2秒後にトグルを自動発火(実クリック不要の検証)。
//
// ビルド: swiftc -parse-as-library -O main.swift -o control-panel

import AppKit
import Carbon
import CoreGraphics
import Foundation

func emit(_ obj: [String: Any]) {
    var f = obj
    f["t_ms"] = Int(Date().timeIntervalSince1970 * 1000)
    if let d = try? JSONSerialization.data(withJSONObject: f, options: [.sortedKeys]),
       let s = String(data: d, encoding: .utf8) {
        print(s)
        fflush(stdout)
    }
}

func diag(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]),
       let s = String(data: d, encoding: .utf8) {
        FileHandle.standardError.write(Data((s + "\n").utf8))
        // 親(pipeline)は helper の stderr を捨てるため、診断はファイルにも残す。
        let line = Data((s + "\n").utf8)
        let path = "logs/panel_diag.log"
        if let fh = FileHandle(forWritingAtPath: path) {
            fh.seekToEndOfFile(); fh.write(line); try? fh.close()
        } else {
            try? line.write(to: URL(fileURLWithPath: path))
        }
    }
}

// Carbon ホットキーの C ハンドラから参照するためグローバルに置く(単一インスタンス)。
var gPanel: PanelController?

// ドラッグ⇄クリックを1枚で兼ねるビュー。
// ・押して 3px 超動かしたら「移動」(ウィンドウを追従)→ 離しても toggle しない。
// ・ほぼ動かず離したら「クリック」= toggle。
// これで縁の細い余白に頼らず、ボタンのどこを掴んでも動かせて、かつクリックで ON/OFF できる。
final class DragToggleView: NSView {
    var onToggle: (() -> Void)?
    var onDragEnded: (() -> Void)?
    private var title = "🔴 英語 OFF"
    private var bg = NSColor.systemRed
    private var downMouse = NSPoint.zero      // 押した瞬間の画面座標
    private var winOrigin = NSPoint.zero       // 押した瞬間のウィンドウ原点
    private var dragged = false

    func update(title: String, color: NSColor) {
        self.title = title
        bg = color
        needsDisplay = true
    }

    // 非アクティブ(Zoom が前面)状態でも最初の1クリックを取りこぼさない。
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        let path = NSBezierPath(roundedRect: bounds, xRadius: 14, yRadius: 14)
        bg.withAlphaComponent(0.92).setFill()
        path.fill()
        let style = NSMutableParagraphStyle()
        style.alignment = .center
        let attrs: [NSAttributedString.Key: Any] = [
            .foregroundColor: NSColor.white,
            .font: NSFont.systemFont(ofSize: 15, weight: .bold),
            .paragraphStyle: style,
        ]
        let s = title as NSString
        let sz = s.size(withAttributes: attrs)
        s.draw(in: NSRect(x: 0, y: (bounds.height - sz.height) / 2, width: bounds.width, height: sz.height),
               withAttributes: attrs)
    }

    override func mouseDown(with event: NSEvent) {
        downMouse = NSEvent.mouseLocation
        winOrigin = window?.frame.origin ?? .zero
        dragged = false
    }

    override func mouseDragged(with event: NSEvent) {
        let now = NSEvent.mouseLocation
        let dx = now.x - downMouse.x, dy = now.y - downMouse.y
        if abs(dx) > 3 || abs(dy) > 3 { dragged = true }         // しきい値超で「移動」と判定
        if dragged { window?.setFrameOrigin(NSPoint(x: winOrigin.x + dx, y: winOrigin.y + dy)) }
    }

    override func mouseUp(with event: NSEvent) {
        if dragged { onDragEnded?() } else { onToggle?() }        // 動かした=移動 / 動かさず=クリック
    }
}

final class PanelController: NSObject {
    let panel: NSPanel
    private let view: DragToggleView
    private(set) var isOn: Bool    // true=英語 通訳中(pipeline の paused=false と一致) / false=停止(地声)
    private let onlyApp: String?   // 指定 bundleID(カンマ区切り)のどれかが最前面のときだけ表示。nil=常時表示(従来動作)
    private let meetingTitles: [String]  // 非空: 最前面ウィンドウのタイトルがこれを含むときだけ表示
    private var pollTimer: Timer?
    private var lastShow: Bool?    // 直近の表示状態(ログのスパム防止)
    private let autosave = "zrt-control-panel3"

    init(startOn: Bool, onlyApp: String?, meetingTitles: [String]) {
        isOn = startOn
        self.onlyApp = onlyApp
        self.meetingTitles = meetingTitles
        let sf = NSScreen.main?.frame ?? NSRect(x: 0, y: 0, width: 1440, height: 900)
        let w: CGFloat = 150, h: CGFloat = 44
        let rect = NSRect(x: sf.maxX - w - 30, y: sf.maxY - h - 100, width: w, height: h)  // 既定は右上寄り

        panel = NSPanel(contentRect: rect,
                        styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        panel.level = .screenSaver                               // 表示時は全画面 Zoom より前面
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]
        panel.isReleasedWhenClosed = false
        panel.becomesKeyOnlyIfNeeded = true

        view = DragToggleView(frame: NSRect(x: 0, y: 0, width: w, height: h))
        view.wantsLayer = true
        view.layer?.cornerRadius = 14
        view.layer?.masksToBounds = true
        view.autoresizingMask = [.width, .height]
        panel.contentView = view

        super.init()
        view.onToggle = { [weak self] in self?.toggle() }
        view.onDragEnded = { [weak self] in self?.saveFrame() }
        _ = panel.setFrameAutosaveName(autosave)                 // 移動後の位置を記憶
        updateAppearance()

        if onlyApp == nil {
            panel.orderFrontRegardless()                         // 常時表示(従来動作)
        } else {
            // アプリ切替は通知で即反応。アプリ内のウィンドウ切替(ミーティング⇄Workplace)は
            // 通知が出ないので 0.5s ポーリングでも追従する(両方でハイブリッド)。
            NSWorkspace.shared.notificationCenter.addObserver(
                self, selector: #selector(appActivated), name: NSWorkspace.didActivateApplicationNotification,
                object: nil)
            let t = Timer(timeInterval: 0.5, target: self, selector: #selector(poll), userInfo: nil, repeats: true)
            RunLoop.main.add(t, forMode: .common)
            pollTimer = t
            applyVisibility()
        }
    }

    private func saveFrame() { panel.saveFrame(usingName: autosave) }

    @objc private func appActivated() { applyVisibility() }
    @objc private func poll() { applyVisibility() }

    // 最前面アプリが onlyApp で、かつ(meetingTitles 指定時は)最前面ウィンドウのタイトルが
    // いずれかを含むときだけ表示。ウィンドウ・タイトルで「ミーティング」と「Workplace ホーム」を区別する。
    private func applyVisibility() {
        let show = shouldShow()
        if show {
            if !panel.isVisible { panel.orderFrontRegardless() }
        } else {
            if panel.isVisible { panel.orderOut(nil) }
        }
        if show != lastShow {
            diag(["phase": "visibility", "show": show, "app": frontApp ?? "?", "match": frontMatch ?? "-"])
            lastShow = show
        }
    }

    // ログ用(直近の判定)。タイトルそのものは残さない: Chrome ではほかのタブの題名(メールの件名など)になるため。
    private var frontApp: String?     // 最前面アプリの bundleID
    private var frontMatch: String?   // 一致した meetingTitles の語。"(app)" = アプリだけで判定・"(no title)" = タイトルが取れず表示

    private func shouldShow() -> Bool {
        guard let want = onlyApp else { return true }
        frontMatch = nil
        guard let app = NSWorkspace.shared.frontmostApplication else { frontApp = nil; return false }
        let bid = app.bundleIdentifier ?? ""
        frontApp = bid
        // Zoom は会議UIが別プロセス(us.zoom.ZoomHybridConf)になることがあるため同族 bundle も許容。
        let sameFamily = want.split(separator: ",").contains { raw in
            let w = raw.trimmingCharacters(in: .whitespaces)
            return bid == w || (w.hasPrefix("us.zoom.") && bid.hasPrefix("us.zoom."))
        }
        guard sameFamily else { return false }
        if meetingTitles.isEmpty { frontMatch = "(app)"; return true }  // アプリレベル判定
        let title = frontmostWindowTitle(pid: app.processIdentifier)
        // ⚠️ ウィンドウ名は「画面収録」許可が無いと取得できない(launchd 起動で顕著)。
        // 取れないときは隠す(=永久に不可視)のではなく **表示にフォールバック**する。
        // 許可があればタイトルで会議窓のみに絞られる。
        guard let t = title, !t.isEmpty else { frontMatch = "(no title)"; return true }
        frontMatch = meetingTitles.first { t.lowercased().contains($0.lowercased()) }
        return frontMatch != nil
    }

    // 指定 pid の「最前面の通常ウィンドウ(layer 0・十分な高さ)」のタイトル。
    private func frontmostWindowTitle(pid: pid_t) -> String? {
        let opts: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
        guard let list = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] else { return nil }
        for w in list {   // 前面→背面の順
            guard (w[kCGWindowOwnerPID as String] as? Int) == Int(pid) else { continue }
            guard (w[kCGWindowLayer as String] as? Int) == 0 else { continue }   // メニュー帯/ダイアログを除外
            let b = w[kCGWindowBounds as String] as? [String: Any]
            guard (b?["Height"] as? Double ?? 0) > 100 else { continue }         // 1470x32 等の帯を除外
            return w[kCGWindowName as String] as? String ?? ""                   // 最前面の実ウィンドウ
        }
        return nil
    }

    // クリック / ⌃⌥P / テストから呼ばれる。状態を反転し pipeline へ toggle を送る。
    func toggle() {
        isOn.toggle()
        updateAppearance()
        emit(["type": "toggle", "on": isOn])
    }

    private func updateAppearance() {
        view.update(title: isOn ? "🔊 英語 ON" : "🔴 英語 OFF",
                    color: isOn ? .systemGreen : .systemRed)
    }
}

@main
struct ControlPanelApp {
    static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.accessory)                      // Dock 非表示のエージェントとして UI を出す
        let args = CommandLine.arguments
        let startOn = !args.contains("--start-off")
        var onlyApp: String? = nil
        if let i = args.firstIndex(of: "--only-app"), i + 1 < args.count { onlyApp = args[i + 1] }
        var meetingTitles: [String] = []
        if let i = args.firstIndex(of: "--meeting-title"), i + 1 < args.count {
            meetingTitles = args[i + 1].split(separator: ",").map {
                $0.trimmingCharacters(in: .whitespaces)
            }.filter { !$0.isEmpty }
        }
        let controller = PanelController(startOn: startOn, onlyApp: onlyApp, meetingTitles: meetingTitles)
        gPanel = controller

        // ⌃⌥P でも切替(hotkey_helper と同じキー。クリックと二役)。
        let handler: EventHandlerUPP = { (_, _, _) -> OSStatus in
            DispatchQueue.main.async { gPanel?.toggle() }
            return noErr
        }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetEventDispatcherTarget(), handler, 1, &spec, nil, nil)
        var ref: EventHotKeyRef?
        let hid = EventHotKeyID(signature: OSType(0x5A52_5450), id: 1)  // 'ZRTP'
        let st = RegisterEventHotKey(UInt32(kVK_ANSI_P), UInt32(controlKey | optionKey), hid,
                                     GetEventDispatcherTarget(), 0, &ref)
        diag(["phase": "hotkey", "combo": "⌃⌥P", "status": st])

        // 停止: SIGINT/SIGTERM + stdin "quit"。
        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        let onSig: @convention(block) () -> Void = { NSApp.terminate(nil) }
        let si = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        let sg = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        si.setEventHandler(handler: onSig)
        sg.setEventHandler(handler: onSig)
        si.resume()
        sg.resume()
        Thread.detachNewThread {
            while let line = readLine(strippingNewline: true) {
                if line.trimmingCharacters(in: .whitespaces) == "quit" {
                    DispatchQueue.main.async { NSApp.terminate(nil) }
                    return
                }
            }
            // stdin EOF(親 pipeline が終了/切断)→ パネルも自動で閉じる(取り残さない)。
            DispatchQueue.main.async { NSApp.terminate(nil) }
        }

        // テスト自動発火(実クリック不要で toggle 配線を検証)。
        if let i = args.firstIndex(of: "--emit-toggles"), i + 1 < args.count {
            for t in args[i + 1].split(separator: ",").compactMap({ Double($0.trimmingCharacters(in: .whitespaces)) }) {
                DispatchQueue.main.asyncAfter(deadline: .now() + t) { gPanel?.toggle() }
            }
        }

        _ = si
        _ = sg
        // 画面収録許可の有無を記録(ウィンドウ名判定の可否を決める。launchd 起動だと未付与になりやすい)。
        // 未付与なら shouldShow が fail open(表示)にフォールバックし、ボタンが消えたままにはならない。
        let sc = CGPreflightScreenCaptureAccess()
        diag(["phase": "screen_capture", "granted": sc,
              "note": sc ? "ウィンドウ名で会議窓のみに絞れる" : "未付与のため常時表示にフォールバック"])
        if !sc { CGRequestScreenCaptureAccess() }   // 一度だけ許可プロンプトを出す(以後は設定で管理)
        emit(["type": "ready", "on": controller.isOn])
        diag(["phase": "ready", "note": "control panel up", "start_on": controller.isOn])
        app.run()
    }
}
