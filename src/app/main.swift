// transcue — v2 G: TransCue の有効/無効を切り替える小さな GUI。
//
// 有効/無効を状態ファイル(~/.transcue/enabled.json)に書き、meeting_watcher が
// 2 s ごとのポーリングで読む。watcher は自分の状態と心拍を watcher.json に書き、本アプリが 1 s ごとに
// 読んで表示する。Zoom 上の英語 ON/OFF ボタン(control_panel)と字幕は watcher が起動するスタック側にあり、
// 本アプリはそれらを直接は操作しない。
//
// 表示(縦長の 1 枚のカード・2026-10-25 利用者のスケッチ): 上に大きな電源ボタンと状態(有効(緑)/ 無効(灰)/
// 異常(橙))、区切り線の下に「クラウド翻訳(Claude)」の行と「設定」の行。会議中は窓の上端に線(緑 = 翻訳中・灰 = 地声だけ)。
// クラウド翻訳の ON/OFF は cloud.json。翻訳スタック(translate_claude.py)が発言ごとに読み、OFF なら Claude を呼ばずに
// Mac の中(PLaMo)で訳す。watcher を通さないので、会議中でも次の発言から効く。
// 「設定」は小さな設定画面(シート)。字幕・英語のモデルと API キーの有無を出す(モデル名は config.toml から読む)。
// 閉じると終了する(watcher は動き続け、最後の設定に従う)。
//
// オプション:
//   --state-dir <dir>                    状態ファイルの置き場所(既定 ~/.transcue。テスト用)
//   --config <path>                      config.toml の場所(既定はアプリの 2 つ上の src/config.toml。ZRT_CONFIG でも可)
//   --set-enabled on|off                 画面を出さずに有効/無効を書いて終了(スクリプト・試験用)
//   --set-cloud on|off                   画面を出さずにクラウド翻訳の ON/OFF を書いて終了(同上)
//   --ui-snapshot <png> [--mock <名前>]  画面を PNG に書き出して終了。--mock なしは状態ファイルの今の中身
//                                        (見た目の確認用。名前は mockNames)
//   --write-iconset <dir>                アイコンの PNG 一式を書き出して終了(build.sh が .icns にする)
//
// ビルド: ./build.sh(swiftc → "TransCue.app" を組み立てる)

import AppKit

// MARK: - 色(暗い配色。有効の緑を足した)

func rgb(_ v: UInt32, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat(v >> 16 & 0xFF) / 255, green: CGFloat(v >> 8 & 0xFF) / 255,
            blue: CGFloat(v & 0xFF) / 255, alpha: a)
}

enum Pal {
    static let canvas: UInt32 = 0x13161C
    static let hero: [UInt32] = [0x191E28, 0x151921, 0x262C3A, 0x303747]      // 上・下・枠・上辺の線
    static let heroOn: [UInt32] = [0x16211C, 0x131A18, 0x1F4A35, 0x2A6347]
    static let heroWarn: [UInt32] = [0x231C16, 0x181619, 0x5C431C, 0x735422]
    static let text: UInt32 = 0xF2F4F8, text2: UInt32 = 0xB4BCCB, text3: UInt32 = 0x6E7787, hint: UInt32 = 0x687182
    static let idleOuter: UInt32 = 0x11141B, idleRing: UInt32 = 0x2E3646, ringHot: UInt32 = 0x3B4559
    static let busy: UInt32 = 0x3A3F48
    static let offFill: UInt32 = 0x161922, offBorder: UInt32 = 0x222733, offText: UInt32 = 0x5A6270
    static let green: UInt32 = 0x22C55E, greenHi: UInt32 = 0x4ADE80, greenPale: UInt32 = 0xBBF7D0
    static let greenOuter: UInt32 = 0x0E1F16, greenChip: UInt32 = 0x0F2A1B
    static let amber: UInt32 = 0xFBBF24, amberText: UInt32 = 0xFCD34D, warnBg: UInt32 = 0x2B1F11
    static let warnBorder: UInt32 = 0xD97706
}

// MARK: - 状態ファイル(GUI ⇄ watcher)

struct WatcherStatus {
    var state = ""            // IDLE / ACTIVE / BYPASS / EXTERNAL / FAILED
    var enabled = true        // watcher が最後に読んだ有効/無効
    var inMeeting = false
    var busy = false          // スタックの停止など、ポーリングが止まる処理の最中
    var sinceMs: Double = 0   // 今の状態に入った時刻
    var heartbeatMs: Double = 0
    var watcherPid: Int32 = 0
    var detail = ""
}

final class StateFiles {
    let dir: URL
    init(dir: URL) { self.dir = dir }
    var enabledURL: URL { dir.appendingPathComponent("enabled.json") }
    var cloudURL: URL { dir.appendingPathComponent("cloud.json") }
    var statusURL: URL { dir.appendingPathComponent("watcher.json") }

    // 無いときは「有効」(GUI を一度も使っていない = 今までどおりの挙動)。
    func readEnabled() -> Bool { readFlag(enabledURL, "enabled") }
    func writeEnabled(_ on: Bool) -> Bool { writeFlag(enabledURL, "enabled", on) }

    // クラウド翻訳(Claude)。無いときは ON(切り替えを足す前と同じ挙動)。
    func readCloud() -> Bool { readFlag(cloudURL, "cloud") }
    func writeCloud(_ on: Bool) -> Bool { writeFlag(cloudURL, "cloud", on) }

    private func readFlag(_ url: URL, _ key: String) -> Bool {
        guard let d = try? Data(contentsOf: url),
              let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return true }
        return o[key] as? Bool ?? true
    }

    private func writeFlag(_ url: URL, _ key: String, _ on: Bool) -> Bool {
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let o: [String: Any] = [key: on, "updated_ms": Int(Date().timeIntervalSince1970 * 1000), "by": "gui"]
            let d = try JSONSerialization.data(withJSONObject: o, options: [.sortedKeys])
            try d.write(to: url, options: .atomic)    // 一時ファイル → rename(読み手が書きかけを読まない)
            return true
        } catch {
            return false
        }
    }

    func readStatus() -> WatcherStatus? {
        guard let d = try? Data(contentsOf: statusURL),
              let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return nil }
        var s = WatcherStatus()
        s.state = o["state"] as? String ?? ""
        s.enabled = o["enabled"] as? Bool ?? true
        s.inMeeting = o["in_meeting"] as? Bool ?? false
        s.busy = o["busy"] as? Bool ?? false
        s.sinceMs = (o["since_ms"] as? NSNumber)?.doubleValue ?? 0
        s.heartbeatMs = (o["heartbeat_ms"] as? NSNumber)?.doubleValue ?? 0
        s.watcherPid = (o["watcher_pid"] as? NSNumber)?.int32Value ?? 0
        s.detail = o["detail"] as? String ?? ""
        return s
    }
}

func pidAlive(_ pid: Int32) -> Bool {
    pid > 0 && (kill(pid, 0) == 0 || errno == EPERM)
}

// MARK: - config.toml(クラウド翻訳の行と設定画面に出すモデル)

struct AppConfig {
    var recvEngine = "plamo", sendEngine = "plamo"
    var recvModel = "claude-haiku-5-5", sendModel = "claude-sonnet-5-5", sendBackup = ""
    var apiKeyFile = "~/.transcue/anthropic_api_key"

    // 要る値だけを読む(TOML の全部は解さない: [section] の行と key = "value" の行だけ)。読めなければ既定値。
    static func load(_ url: URL) -> AppConfig {
        var c = AppConfig()
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return c }
        var section = ""
        for raw in text.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("["), let end = line.firstIndex(of: "]") {
                section = String(line[line.index(after: line.startIndex)..<end])
                continue
            }
            guard let eq = line.firstIndex(of: "="), let q1 = line[eq...].firstIndex(of: "\"") else { continue }
            let rest = line[line.index(after: q1)...]
            guard let q2 = rest.firstIndex(of: "\"") else { continue }
            let value = String(rest[..<q2])
            switch (section, line[..<eq].trimmingCharacters(in: .whitespaces)) {
            case ("translate", "recv_engine"): c.recvEngine = value
            case ("translate", "send_engine"): c.sendEngine = value
            case ("claude", "recv_model"): c.recvModel = value
            case ("claude", "send_model"): c.sendModel = value
            case ("claude", "send_backup_model"): c.sendBackup = value
            case ("claude", "api_key_file"): c.apiKeyFile = value
            default: break
            }
        }
        return c
    }

    // クラウド翻訳の行の 2 行目(ON のとき)。config で "plamo" の向きは PLaMo と出す。
    var cloudSummary: String {
        if recvEngine != "claude" && sendEngine != "claude" { return "config.toml で使わないことになっています" }
        return "字幕 \(recvEngine == "claude" ? modelLabel(recvModel) : "PLaMo")・英語 \(sendEngine == "claude" ? modelLabel(sendModel) : "PLaMo")"
    }
}

// claude-haiku-5-5 → Haiku 5.5
func modelLabel(_ id: String) -> String {
    var parts = id.split(separator: "-").map(String.init)
    if parts.first == "claude" { parts.removeFirst() }
    guard let name = parts.first else { return id }
    let version = parts.dropFirst().joined(separator: ".")
    return name.prefix(1).uppercased() + name.dropFirst() + (version.isEmpty ? "" : " " + version)
}

// .app なら src/app/TransCue.app の 2 つ上、素のバイナリ(src/app/transcue)なら 1 つ上の config.toml。
func defaultConfigURL() -> URL {
    let b = Bundle.main.bundleURL
    let appDir = b.pathExtension == "app" ? b.deletingLastPathComponent() : b
    return appDir.deletingLastPathComponent().appendingPathComponent("config.toml")
}

// API キーのファイルの有無と置いた日(中身は読まない)。翻訳スタックは環境変数 ANTHROPIC_API_KEY でも動くが、画面からは見えない。
struct KeyInfo {
    var exists = false
    var date: Date?
}

func apiKeyInfo(_ c: AppConfig) -> KeyInfo {
    var k = KeyInfo()
    if let a = try? FileManager.default.attributesOfItem(atPath: (c.apiKeyFile as NSString).expandingTildeInPath) {
        k.exists = ((a[.size] as? NSNumber)?.intValue ?? 0) > 0
        k.date = a[.modificationDate] as? Date
    }
    return k
}

// MARK: - 表示の中身(状態 → 文言と色)

enum Tone { case on, off, warn }

struct Look {
    var tone = Tone.off
    var enabled = true            // 有効 / 無効(電源ボタンの読み上げ用の値)
    var title = ""
    var status = ""
    var hint = ""                 // 状態の下の案内(異常のときの直し方など。最大 2 行)
    var tint = Pal.hero           // カードの色
    var band: UInt32? = nil       // 窓の上端の線(会議中・異常)
    var cloud = true              // クラウド翻訳の行の札(ON / OFF)
    var cloudLine = ""
}

// watcher の心拍がこれより古ければ止まっているとみなす(スタックの停止は最長 ~20 s ポーリングを止める)。
let staleAfterSec = 30.0
// 切り替えを書いてから watcher が受け取るまでの待ち(ポーリングは 2 s)。
let pendingTimeoutSec = 8.0

func clock(_ sec: Double) -> String {
    let s = max(0, Int(sec))
    return s >= 3600 ? String(format: "%d:%02d:%02d", s / 3600, s / 60 % 60, s % 60)
                     : String(format: "%d:%02d", s / 60, s % 60)
}

func makeLook(desired: Bool, cloud: Bool = true, cloudSummary: String = "", status: WatcherStatus?, pending: Bool,
              pendingTimedOut: Bool, now: Date, alive: (Int32) -> Bool = pidAlive) -> Look {
    var l = Look()
    l.cloud = cloud
    l.cloudLine = cloud ? cloudSummary : "Mac の中だけで訳します(外へ送りません)"
    l.enabled = desired
    l.tone = desired ? .on : .off
    l.title = desired ? "有効" : "無効"
    // 英語の ON/OFF(⌃⌥P)の案内は設定画面へ移した(10/25)
    l.hint = desired ? "" : "無効の間も、地声はそのまま会議へ届きます"
    let nowMs = now.timeIntervalSince1970 * 1000

    func warn(_ title: String, _ status: String, _ hint: String? = nil) {
        l.tone = .warn
        l.title = title
        l.status = status
        if let h = hint { l.hint = h }
        l.tint = Pal.heroWarn
        l.band = Pal.amber
    }

    guard let s = status else {
        warn("異常", "監視プロセスの状態を読めません", "監視を登録する: ./install_watcher.command")
        return l
    }
    if !alive(s.watcherPid) || nowMs - s.heartbeatMs > staleAfterSec * 1000 {
        warn("異常", "監視プロセスが止まっています", "監視を登録し直す: ./install_watcher.command")
        return l
    }
    if pendingTimedOut {
        warn(l.title, "監視プロセスが切り替えを受け取っていません")
        return l
    }
    if pending {
        l.status = "切り替えています…"
        return l
    }
    if s.busy && !s.detail.isEmpty {
        l.status = s.detail
        return l
    }
    let elapsed = (nowMs - s.sinceMs) / 1000
    switch s.state {
    case "ACTIVE":
        l.status = "会議中 — 翻訳を動かしています(\(clock(elapsed)))"
        l.tint = Pal.heroOn
        l.band = Pal.green
    case "BYPASS":
        l.status = "会議中 — 地声だけを会議へ送っています"
        l.band = Pal.text3
    case "EXTERNAL":
        l.status = "手動で起動した翻訳が動いています"
        l.hint = "手動で起動したものは、自動で止めたり起動したりしません"
        l.band = Pal.text3
    case "FAILED":
        // 有効なら翻訳スタック、無効なら地声パススルーの起動に失敗している。切り替えると watcher がやり直す。
        if desired {
            warn("異常", "翻訳を起動できませんでした", "無効にすると地声だけを流せます。ログ: logs/watcher_run_*.log")
        } else {
            warn("異常", "地声を会議へ流せませんでした", "会議アプリのマイクを MacBook のマイクに切り替えてください")
        }
    default:   // IDLE
        if s.inMeeting {
            l.status = desired ? "会議を検知しました。翻訳を起動します…" : "会議中 — 翻訳は起動していません"
        } else {
            l.status = desired ? "待機中 — 会議を待っています(Zoom・Meet)" : "会議に入っても翻訳は起動しません"
        }
    }
    return l
}

// MARK: - 画面

// 丸ボタンを VoiceOver などから「スイッチ」として押せるようにする(画面は自前描画で、部品を持たないため)。
final class ToggleElement: NSAccessibilityElement {
    var press: (() -> Void)?
    override func accessibilityPerformPress() -> Bool {
        press?()
        return press != nil
    }
}

final class MainView: NSView {
    static let size = NSSize(width: 300, height: 434)
    var look = Look() {
        didSet {
            needsDisplay = true
            toggleElement.setAccessibilityValue(NSNumber(value: look.enabled ? 1 : 0))
            toggleElement.setAccessibilityHelp(look.status)
            cloudElement.setAccessibilityValue(NSNumber(value: look.cloud ? 1 : 0))
            cloudElement.setAccessibilityHelp(look.cloudLine)
        }
    }
    var onToggle: (() -> Void)?
    var onToggleCloud: (() -> Void)?
    var onSettings: (() -> Void)?
    private enum Control { case main, cloud, settings }
    private var hot: Control?
    private var pressed: Control?

    // 縦長の 1 枚のカード: 上に大きな電源ボタンと状態、区切り線の下にクラウド翻訳と設定の行(10/25 利用者のスケッチ)
    private let card = NSRect(x: 14, y: 38, width: 272, height: 382)
    private let buttonR: CGFloat = 64
    private var buttonCenter: NSPoint { NSPoint(x: card.midX, y: card.minY + 98) }
    private var cloudRow: NSRect { NSRect(x: card.minX, y: card.minY + 262, width: card.width, height: 64) }
    private var settingsRow: NSRect { NSRect(x: card.minX, y: cloudRow.maxY, width: card.width, height: card.maxY - cloudRow.maxY) }
    private var cloudChipRect: NSRect { NSRect(x: card.maxX - 14 - 54, y: cloudRow.minY + 12, width: 54, height: 20) }

    private lazy var toggleElement: ToggleElement = {
        let e = ToggleElement()
        e.setAccessibilityRole(.checkBox)
        e.setAccessibilitySubrole(.switch)
        e.setAccessibilityLabel("翻訳を有効にする")
        e.setAccessibilityParent(self)
        let c = buttonCenter
        e.setAccessibilityFrameInParentSpace(NSRect(x: c.x - buttonR, y: c.y - buttonR, width: 2 * buttonR, height: 2 * buttonR))
        e.press = { [weak self] in self?.onToggle?() }
        return e
    }()

    private lazy var cloudElement: ToggleElement = {
        let e = ToggleElement()
        e.setAccessibilityRole(.checkBox)
        e.setAccessibilitySubrole(.switch)
        e.setAccessibilityLabel("クラウド翻訳(Claude)を使う")
        e.setAccessibilityParent(self)
        e.setAccessibilityFrameInParentSpace(cloudRow)
        e.press = { [weak self] in self?.onToggleCloud?() }
        return e
    }()

    private lazy var settingsElement: ToggleElement = {
        let e = ToggleElement()
        e.setAccessibilityRole(.button)
        e.setAccessibilityLabel("設定を開く")
        e.setAccessibilityParent(self)
        e.setAccessibilityFrameInParentSpace(settingsRow)
        e.press = { [weak self] in self?.onSettings?() }
        return e
    }()

    override func isAccessibilityElement() -> Bool { false }
    override func accessibilityChildren() -> [Any]? { [toggleElement, cloudElement, settingsElement] }

    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        trackingAreas.forEach(removeTrackingArea)
        addTrackingArea(NSTrackingArea(rect: bounds, options: [.mouseMoved, .mouseEnteredAndExited, .activeAlways],
                                       owner: self, userInfo: nil))
    }

    // 有効/無効は電源ボタン、クラウド翻訳と設定はそれぞれの行のどこを押しても反応する。
    private func control(at p: NSPoint) -> Control? {
        if hypot(p.x - buttonCenter.x, p.y - buttonCenter.y) <= buttonR + 2 { return .main }
        if cloudRow.contains(p) { return .cloud }
        return settingsRow.contains(p) ? .settings : nil
    }

    private func setHot(_ c: Control?) {
        if c != hot { hot = c; needsDisplay = true }
        (c != nil ? NSCursor.pointingHand : NSCursor.arrow).set()
    }

    override func mouseMoved(with event: NSEvent) { setHot(control(at: convert(event.locationInWindow, from: nil))) }
    override func mouseExited(with event: NSEvent) { setHot(nil) }

    override func mouseDown(with event: NSEvent) {
        pressed = control(at: convert(event.locationInWindow, from: nil))
        if pressed != nil { needsDisplay = true }
    }

    override func mouseUp(with event: NSEvent) {
        if let p = pressed, p == control(at: convert(event.locationInWindow, from: nil)) {
            switch p {
            case .main: onToggle?()
            case .cloud: onToggleCloud?()
            case .settings: onSettings?()
            }
        }
        pressed = nil
        needsDisplay = true
    }

    override func keyDown(with event: NSEvent) {
        if event.charactersIgnoringModifiers == " " { onToggle?() } else { super.keyDown(with: event) }
    }

    override func draw(_ dirtyRect: NSRect) {
        rgb(Pal.canvas).setFill()
        bounds.fill()
        if let band = look.band {
            rgb(band).setFill()
            NSRect(x: 0, y: 0, width: bounds.width, height: 2).fill()
        }
        // 名前は信号機ボタン(閉じる・しまう)の右。
        text("TransCue", .systemFont(ofSize: 13, weight: .bold), Pal.text, x: 78, centerY: 16, width: bounds.width - 92)
        drawCard(card, tint: look.tint)
        drawButton()
        let warn = look.tone == .warn
        let below = buttonCenter.y + buttonR
        text(look.title, .systemFont(ofSize: 17, weight: .semibold), warn ? Pal.amberText : Pal.text,
             x: card.minX + 14, centerY: below + 24, width: card.width - 28, align: .center)
        text(look.status, .systemFont(ofSize: 11.5), warn ? Pal.amberText : Pal.text2,
             x: card.minX + 14, centerY: below + 46, width: card.width - 28, align: .center)
        para(look.hint, .systemFont(ofSize: 10.5), Pal.hint, x: card.minX + 14, top: below + 58, width: card.width - 28,
             lines: 2)
        drawRows()
    }

    private func drawRows() {
        for (row, c) in [(cloudRow, Control.cloud), (settingsRow, Control.settings)] {
            if hot == c {                                  // 押せることが分かるよう、指している行だけ少し明るく
                NSGraphicsContext.saveGraphicsState()
                NSBezierPath(roundedRect: card.insetBy(dx: 1, dy: 1), xRadius: 7, yRadius: 7).addClip()
                rgb(0xFFFFFF, 0.035).setFill()
                row.fill()
                NSGraphicsContext.restoreGraphicsState()
            }
            rgb(look.tint[2]).setFill()                    // 区切り線
            NSRect(x: row.minX + 1, y: row.minY, width: row.width - 2, height: 1).fill()
        }
        let x = card.minX + 16
        drawChip(cloudChipRect, on: look.cloud, hot: hot == .cloud)
        text("クラウド翻訳(Claude)", .systemFont(ofSize: 13, weight: .semibold), Pal.text,
             x: x, centerY: cloudRow.minY + 22, width: cloudChipRect.minX - x - 8)
        text(look.cloudLine, .systemFont(ofSize: 11), Pal.text2, x: x, centerY: cloudRow.minY + 44,
             width: card.maxX - 14 - x)
        text("設定", .systemFont(ofSize: 13, weight: .semibold), Pal.text, x: x, centerY: settingsRow.midY, width: 120)
        chevron(at: NSPoint(x: card.maxX - 22, y: settingsRow.midY), color: hot == .settings ? Pal.text2 : Pal.text3)
    }

    private func drawCard(_ r: NSRect, tint t: [UInt32]) {
        let path = NSBezierPath(roundedRect: r.insetBy(dx: 0.5, dy: 0.5), xRadius: 8, yRadius: 8)
        NSGradient(starting: rgb(t[0]), ending: rgb(t[1]))?.draw(in: path, angle: 90)
        rgb(t[2]).setStroke()
        path.lineWidth = 1
        path.stroke()
        rgb(t[3]).setStroke()
        let edge = NSBezierPath()
        edge.move(to: NSPoint(x: r.minX + 8, y: r.minY + 0.5))
        edge.line(to: NSPoint(x: r.maxX - 8, y: r.minY + 0.5))
        edge.lineWidth = 1
        edge.stroke()
    }

    private func drawButton() {
        let c = buttonCenter
        let hot = self.hot == .main
        func oval(_ r: CGFloat) -> NSBezierPath { NSBezierPath(ovalIn: NSRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r)) }
        let outer: UInt32, ring: UInt32
        switch look.tone {
        case .on: outer = Pal.greenOuter; ring = Pal.green
        case .off: outer = Pal.idleOuter; ring = hot ? Pal.ringHot : Pal.idleRing
        case .warn: outer = Pal.warnBg; ring = Pal.warnBorder
        }
        rgb(outer).setFill()
        oval(buttonR).fill()
        let rp = oval(buttonR - 1.5)
        rp.lineWidth = 3
        rgb(ring).setStroke()
        rp.stroke()

        let r: CGFloat = pressed == .main ? 41 : 42
        let disc = oval(r)
        switch look.tone {
        case .on:
            NSGradient(starting: rgb(hot ? Pal.greenPale : Pal.greenHi), ending: rgb(Pal.green))?.draw(in: disc, angle: 90)
            rgb(Pal.greenHi).setStroke()
            disc.lineWidth = 1
            disc.stroke()
        case .off:
            rgb(Pal.busy).setFill()
            disc.fill()
        case .warn:
            NSGradient(starting: rgb(Pal.amber), ending: rgb(Pal.warnBorder))?.draw(in: disc, angle: 90)
        }
        powerGlyph(center: c, radius: 17, width: 3.6, color: look.tone == .off ? (hot ? Pal.text2 : Pal.text3) : 0xFFFFFF)
    }

    private func drawChip(_ r: NSRect, on: Bool, hot: Bool) {
        let path = NSBezierPath(roundedRect: r.insetBy(dx: 0.5, dy: 0.5), xRadius: 4, yRadius: 4)
        rgb(on ? Pal.greenChip : Pal.offFill).setFill()
        path.fill()
        rgb(on ? Pal.green : (hot ? Pal.ringHot : Pal.offBorder)).setStroke()
        path.lineWidth = 1
        path.stroke()
        rgb(on ? Pal.greenHi : Pal.offText).setFill()
        NSBezierPath(ovalIn: NSRect(x: r.minX + 8, y: r.midY - 3, width: 6, height: 6)).fill()
        text(on ? "ON" : "OFF", .monospacedSystemFont(ofSize: 11, weight: .bold), on ? Pal.greenPale : Pal.offText,
             x: r.minX + 19, centerY: r.midY, width: r.width - 21)
    }

    private func chevron(at c: NSPoint, color: UInt32) {
        let p = NSBezierPath()
        p.move(to: NSPoint(x: c.x - 3, y: c.y - 6))
        p.line(to: NSPoint(x: c.x + 3, y: c.y))
        p.line(to: NSPoint(x: c.x - 3, y: c.y + 6))
        p.lineWidth = 1.8
        p.lineCapStyle = .round
        p.lineJoinStyle = .round
        rgb(color).setStroke()
        p.stroke()
    }

    private func text(_ s: String, _ font: NSFont, _ color: UInt32, x: CGFloat, centerY: CGFloat, width: CGFloat,
                      align: NSTextAlignment = .left) {
        guard !s.isEmpty, width > 0 else { return }
        let ps = NSMutableParagraphStyle()
        ps.lineBreakMode = .byTruncatingTail
        ps.alignment = align
        let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: rgb(color), .paragraphStyle: ps]
        let h = ceil((s as NSString).size(withAttributes: attrs).height)
        (s as NSString).draw(with: NSRect(x: x, y: centerY - h / 2, width: width, height: h),
                             options: [.usesLineFragmentOrigin, .truncatesLastVisibleLine], attributes: attrs)
    }

    // 折り返して最大 lines 行、中央揃え(異常のときの直し方など、長い案内用)。
    private func para(_ s: String, _ font: NSFont, _ color: UInt32, x: CGFloat, top: CGFloat, width: CGFloat, lines: Int) {
        guard !s.isEmpty, width > 0 else { return }
        let ps = NSMutableParagraphStyle()
        ps.lineBreakMode = .byWordWrapping
        ps.alignment = .center
        let attrs: [NSAttributedString.Key: Any] = [.font: font, .foregroundColor: rgb(color), .paragraphStyle: ps]
        let one = ceil(("あ" as NSString).size(withAttributes: attrs).height)
        let all = (s as NSString).boundingRect(with: NSSize(width: width, height: .greatestFiniteMagnitude),
                                               options: [.usesLineFragmentOrigin], attributes: attrs)
        let h = min(ceil(all.height), one * CGFloat(lines))
        (s as NSString).draw(with: NSRect(x: x, y: top, width: width, height: h),
                             options: [.usesLineFragmentOrigin, .truncatesLastVisibleLine], attributes: attrs)
    }
}

// 電源の記号(上が開いた円と縦線)。flipped でも非 flipped でも上向きになるよう、上の向きを座標系から決める。
func powerGlyph(center c: NSPoint, radius r: CGFloat, width: CGFloat, color: UInt32) {
    let flipped = NSGraphicsContext.current?.isFlipped ?? false
    let up: CGFloat = flipped ? -1 : 1
    let top: CGFloat = flipped ? 270 : 90
    let arc = NSBezierPath()
    arc.appendArc(withCenter: c, radius: r, startAngle: top + 38, endAngle: top - 38 + 360, clockwise: false)
    arc.lineWidth = width
    arc.lineCapStyle = .round
    rgb(color).setStroke()
    arc.stroke()
    let bar = NSBezierPath()
    bar.move(to: NSPoint(x: c.x, y: c.y + up * 0.5))
    bar.line(to: NSPoint(x: c.x, y: c.y + up * (r + 1.5)))
    bar.lineWidth = width
    bar.lineCapStyle = .round
    bar.stroke()
}

// MARK: - アイコン(Dock と .app)

func drawIcon(side s: CGFloat) {
    let body = NSRect(x: s * 0.1, y: s * 0.1, width: s * 0.8, height: s * 0.8)
    let bg = NSBezierPath(roundedRect: body, xRadius: s * 0.18, yRadius: s * 0.18)
    NSGradient(starting: rgb(0x1B202A), ending: rgb(Pal.canvas))?.draw(in: bg, angle: -90)
    rgb(0x2B3240).setStroke()
    bg.lineWidth = max(1, s * 0.006)
    bg.stroke()
    let c = NSPoint(x: s / 2, y: s / 2)
    func oval(_ r: CGFloat) -> NSBezierPath { NSBezierPath(ovalIn: NSRect(x: c.x - r, y: c.y - r, width: 2 * r, height: 2 * r)) }
    rgb(Pal.greenOuter).setFill()
    oval(s * 0.27).fill()
    let ring = oval(s * 0.26)
    ring.lineWidth = s * 0.022
    rgb(Pal.green).setStroke()
    ring.stroke()
    NSGradient(starting: rgb(Pal.greenHi), ending: rgb(Pal.green))?.draw(in: oval(s * 0.18), angle: -90)
    powerGlyph(center: c, radius: s * 0.08, width: s * 0.03, color: 0xFFFFFF)
}

func iconImage(side: CGFloat) -> NSImage {
    NSImage(size: NSSize(width: side, height: side), flipped: false) { _ in drawIcon(side: side); return true }
}

func writeIconset(to dir: String) {
    let url = URL(fileURLWithPath: dir)
    try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    for base in [16, 32, 128, 256, 512] {
        for scale in [1, 2] {
            let px = base * scale
            guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: px, pixelsHigh: px, bitsPerSample: 8,
                                             samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                             colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0) else { continue }
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            drawIcon(side: CGFloat(px))
            NSGraphicsContext.restoreGraphicsState()
            let name = "icon_\(base)x\(base)\(scale == 2 ? "@2x" : "").png"
            try? rep.representation(using: .png, properties: [:])?.write(to: url.appendingPathComponent(name))
        }
    }
}

// MARK: - アプリ

func makeWindow(_ view: MainView) -> NSWindow {
    let win = NSWindow(contentRect: NSRect(origin: .zero, size: MainView.size),
                       styleMask: [.titled, .closable, .miniaturizable, .fullSizeContentView],
                       backing: .buffered, defer: false)
    win.title = "TransCue"
    win.titleVisibility = .hidden
    win.titlebarAppearsTransparent = true
    win.appearance = NSAppearance(named: .darkAqua)
    win.backgroundColor = rgb(Pal.canvas)
    win.isReleasedWhenClosed = false
    win.contentView = view
    return win
}

// MARK: - 設定画面(シート)

// ボタンの押下を閉じ込めた処理へ渡す(NSButton の target は弱参照なので、持ち主が保持する)。
final class ClosureTarget: NSObject {
    let fn: () -> Void
    init(_ fn: @escaping () -> Void) { self.fn = fn }
    @objc func fire() { fn() }
}

final class FilledView: NSView {
    let color: NSColor
    init(color: NSColor) {
        self.color = color
        super.init(frame: .zero)
    }
    required init?(coder: NSCoder) { nil }
    override func draw(_ dirtyRect: NSRect) {
        color.setFill()
        bounds.fill()
    }
}

// 今は表示だけ(切り替えは後で足す)。モデルは config.toml、API キーはファイルの有無と置いた日。
func makeSettingsView(config c: AppConfig, key: KeyInfo, close: ClosureTarget?) -> NSView {
    func label(_ s: String, size: CGFloat = 12, weight: NSFont.Weight = .regular, color: UInt32 = Pal.text,
               width: CGFloat = 256) -> NSTextField {
        let t = NSTextField(wrappingLabelWithString: s)
        t.font = .systemFont(ofSize: size, weight: weight)
        t.textColor = rgb(color)
        t.preferredMaxLayoutWidth = width
        return t
    }
    func model(_ engine: String, _ id: String, backup: String = "") -> String {
        guard engine == "claude" else { return "PLaMo(Mac の中)" }
        return modelLabel(id) + (backup.isEmpty ? "" : "\n遅いときは \(modelLabel(backup))")
    }
    let day = DateFormatter()
    day.dateFormat = "M/d"
    let keyText = key.exists ? "あり(\(key.date.map { day.string(from: $0) } ?? "—") に置いたもの)"
                             : "ありません(すべて Mac の中で訳します)"
    let grid = NSGridView(views: [
        [label("字幕(英→日)", color: Pal.text2, width: 84), label(model(c.recvEngine, c.recvModel), width: 160)],
        [label("英語(日→英)", color: Pal.text2, width: 84),
         label(model(c.sendEngine, c.sendModel, backup: c.sendBackup), width: 160)],
        [label("API キー", color: Pal.text2, width: 84),
         label(keyText, color: key.exists ? Pal.text : Pal.amberText, width: 160)],
    ])
    grid.rowSpacing = 8
    grid.columnSpacing = 12
    let button = NSButton(title: "閉じる", target: close, action: #selector(ClosureTarget.fire))
    button.bezelStyle = .rounded
    button.keyEquivalent = "\r"
    // 見出しの「設定」はウィンドウのタイトルに出す
    let views: [NSView] = [
        label("クラウド翻訳(Claude)", size: 12, weight: .semibold, color: Pal.text2),
        grid,
        label(c.apiKeyFile, size: 10, color: Pal.text3),
        label("会議アプリ", size: 12, weight: .semibold, color: Pal.text2),
        label("Zoom は会議に入ると、Meet などはマイクに BlackHole 2ch を選ぶと、自動で動きます"),
        label("英語の ON/OFF", size: 12, weight: .semibold, color: Pal.text2),
        label("会議画面のボタン(⌃⌥P)で切り替えます"),
        label("モデルは config.toml の [claude] で変えます", size: 10.5, color: Pal.text3),
        button,
    ]
    let stack = NSStackView(views: views)
    stack.orientation = .vertical
    stack.alignment = .leading
    stack.spacing = 8
    stack.edgeInsets = NSEdgeInsets(top: 18, left: 22, bottom: 18, right: 22)
    stack.setCustomSpacing(16, after: views[2])
    stack.setCustomSpacing(16, after: views[4])
    stack.setCustomSpacing(16, after: views[7])
    stack.translatesAutoresizingMaskIntoConstraints = false
    let root = FilledView(color: rgb(Pal.canvas))
    root.addSubview(stack)
    NSLayoutConstraint.activate([
        stack.leadingAnchor.constraint(equalTo: root.leadingAnchor), stack.trailingAnchor.constraint(equalTo: root.trailingAnchor),
        stack.topAnchor.constraint(equalTo: root.topAnchor), stack.bottomAnchor.constraint(equalTo: root.bottomAnchor),
        root.widthAnchor.constraint(equalToConstant: MainView.size.width),
    ])
    root.layoutSubtreeIfNeeded()
    root.setFrameSize(root.fittingSize)
    return root
}

final class AppController: NSObject, NSApplicationDelegate, NSWindowDelegate {
    private let files: StateFiles
    private let configURL: URL
    private let view = MainView(frame: NSRect(origin: .zero, size: MainView.size))
    private var window: NSWindow?
    private var desired = true
    private var pendingSince: Date?
    private var writeFailed = false
    private var cloud = true
    private var cloudWriteFailed = false
    private var settingsWindow: NSWindow?
    private var closeTarget: ClosureTarget?

    init(files: StateFiles, configURL: URL) {
        self.files = files
        self.configURL = configURL
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.applicationIconImage = iconImage(side: 512)
        installMenu()
        desired = files.readEnabled()
        cloud = files.readCloud()
        view.onToggle = { [weak self] in self?.toggle() }
        view.onToggleCloud = { [weak self] in self?.toggleCloud() }
        view.onSettings = { [weak self] in self?.openSettings() }
        let win = makeWindow(view)
        if win.setFrameUsingName("zrt-app-main") {
            // 保存された大きさは前の版のことがある(10/25 に縦長へ変えた)。左上の位置はそのままに中身の大きさへ合わせる
            let topLeft = NSPoint(x: win.frame.minX, y: win.frame.maxY)
            win.setContentSize(MainView.size)
            win.setFrameTopLeftPoint(topLeft)
        } else {
            win.center()
        }
        win.setFrameAutosaveName("zrt-app-main")
        // 前面に出すかは開き方に任せる(Finder から開けば前面、watcher の open -g なら後ろ)。
        win.makeKeyAndOrderFront(nil)
        win.makeFirstResponder(view)
        window = win
        refresh()
        let t = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in self?.refresh() }
        RunLoop.main.add(t, forMode: .common)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    private func installMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "設定…", action: #selector(openSettings), keyEquivalent: ",")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "TransCue を終了", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu
        let winItem = NSMenuItem()
        main.addItem(winItem)
        let winMenu = NSMenu(title: "ウインドウ")
        winMenu.addItem(withTitle: "閉じる", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        winItem.submenu = winMenu
        NSApp.mainMenu = main
    }

    private func toggle() {
        desired.toggle()
        writeFailed = !files.writeEnabled(desired)
        pendingSince = writeFailed ? nil : Date()
        refresh()
    }

    // クラウド翻訳は翻訳スタックが発言ごとにファイルを読むので、watcher の受け取りを待つ「切り替え中」は無い。
    private func toggleCloud() {
        cloud.toggle()
        cloudWriteFailed = !files.writeCloud(cloud)
        refresh()
    }

    // 設定は独立した小さなウィンドウ(シートにすると、開いている間はアプリが終了を断り、ログアウトを止めてしまう。
    // 10/25 に試して分かった)。開くたびに config.toml と API キーの有無を読み直す。開いていれば前に出すだけ。
    @objc func openSettings() {
        if let s = settingsWindow {
            s.makeKeyAndOrderFront(nil)
            return
        }
        let c = AppConfig.load(configURL)
        let target = ClosureTarget { [weak self] in self?.settingsWindow?.close() }
        let v = makeSettingsView(config: c, key: apiKeyInfo(c), close: target)
        let s = NSWindow(contentRect: NSRect(origin: .zero, size: v.frame.size), styleMask: [.titled, .closable],
                         backing: .buffered, defer: false)
        s.title = "設定"
        s.appearance = NSAppearance(named: .darkAqua)
        s.backgroundColor = rgb(Pal.canvas)
        s.isReleasedWhenClosed = false
        s.contentView = v
        s.delegate = self
        if let w = window {                                // 本体の窓に重ねて開く(左上を揃える)
            s.setFrameTopLeftPoint(NSPoint(x: w.frame.minX, y: w.frame.maxY))
        } else {
            s.center()
        }
        closeTarget = target
        settingsWindow = s
        s.makeKeyAndOrderFront(nil)
    }

    func windowWillClose(_ notification: Notification) {
        if (notification.object as? NSWindow) === settingsWindow {
            settingsWindow = nil
            closeTarget = nil
        }
    }

    private func refresh() {
        let st = files.readStatus()
        // 切り替えの最中でなければ、ファイルの値に合わせる(手で書き換えた場合などにも追従する)。
        if pendingSince == nil && !writeFailed { desired = files.readEnabled() }
        if !cloudWriteFailed { cloud = files.readCloud() }
        if let s = st, s.enabled == desired { pendingSince = nil }
        let timedOut = pendingSince.map { Date().timeIntervalSince($0) > pendingTimeoutSec } ?? false
        // config.toml も毎秒読む(小さいファイル。モデルを書き換えたらすぐ行の表示に出る)
        var l = makeLook(desired: desired, cloud: cloud, cloudSummary: AppConfig.load(configURL).cloudSummary, status: st,
                         pending: pendingSince != nil, pendingTimedOut: timedOut, now: Date())
        if writeFailed {
            l.tone = .warn
            l.status = "設定を保存できませんでした(\(files.dir.path))"
            l.tint = Pal.heroWarn
            l.band = Pal.amber
        }
        if cloudWriteFailed { l.cloudLine = "設定を保存できませんでした(\(files.dir.path))" }
        view.look = l
    }
}

// MARK: - 見た目の確認用(--ui-snapshot)

let mockNames = ["idle", "off", "detected", "active", "bypass", "external", "failed", "dead", "pending", "stopping",
                 "cloud-off", "cloud-off-active", "settings"]

func mockLook(_ name: String, cloudSummary: String) -> Look {
    let now = Date()
    var s = WatcherStatus()
    s.state = "IDLE"
    s.heartbeatMs = now.timeIntervalSince1970 * 1000
    s.sinceMs = s.heartbeatMs - 83_000
    s.watcherPid = getpid()
    var desired = true
    var cloud = true
    var pending = false
    switch name {
    case "off": desired = false; s.enabled = false
    case "detected": s.inMeeting = true
    case "active": s.state = "ACTIVE"; s.inMeeting = true
    case "bypass": desired = false; s.enabled = false; s.state = "BYPASS"; s.inMeeting = true
    case "external": s.state = "EXTERNAL"; s.inMeeting = true
    case "failed": s.state = "FAILED"; s.inMeeting = true
    case "dead":
        return makeLook(desired: true, cloudSummary: cloudSummary, status: nil, pending: false, pendingTimedOut: false, now: now)
    case "pending": desired = false; pending = true
    case "stopping": desired = false; s.state = "ACTIVE"; s.enabled = false; s.busy = true; s.detail = "翻訳を停止しています…"
    case "cloud-off": cloud = false
    case "cloud-off-active": cloud = false; s.state = "ACTIVE"; s.inMeeting = true
    default: break
    }
    return makeLook(desired: desired, cloud: cloud, cloudSummary: cloudSummary, status: s, pending: pending,
                    pendingTimedOut: false, now: now)
}

func writePNG(of target: NSView, to path: String) -> Bool {
    guard let rep = target.bitmapImageRepForCachingDisplay(in: target.bounds) else { return false }
    target.cacheDisplay(in: target.bounds, to: rep)
    guard let png = rep.representation(using: .png, properties: [:]) else { return false }
    return (try? png.write(to: URL(fileURLWithPath: path))) != nil
}

func snapshot(to path: String, look: Look) -> Bool {
    let view = MainView(frame: NSRect(origin: .zero, size: MainView.size))
    view.look = look
    let win = makeWindow(view)
    // 信号機ボタンも写るよう、タイトルバーを含むフレーム全体を描く。
    return writePNG(of: win.contentView?.superview ?? view, to: path)
}

func snapshotSettings(to path: String, config: AppConfig) -> Bool {
    let v = makeSettingsView(config: config, key: apiKeyInfo(config), close: nil)
    let win = NSWindow(contentRect: NSRect(origin: .zero, size: v.frame.size), styleMask: [.borderless],
                       backing: .buffered, defer: false)
    win.appearance = NSAppearance(named: .darkAqua)
    win.contentView = v
    return writePNG(of: v, to: path)
}

@main
struct ZoomTranslatorApp {
    static func main() {
        let args = CommandLine.arguments
        func opt(_ name: String) -> String? {
            guard let i = args.firstIndex(of: name), i + 1 < args.count else { return nil }
            return args[i + 1]
        }
        if let dir = opt("--write-iconset") {
            writeIconset(to: dir)
            return
        }
        let stateDir = opt("--state-dir") ?? ProcessInfo.processInfo.environment["ZRT_STATE_DIR"]
            ?? (NSHomeDirectory() as NSString).appendingPathComponent(".transcue")
        let files = StateFiles(dir: URL(fileURLWithPath: (stateDir as NSString).expandingTildeInPath))
        let configURL = (opt("--config") ?? ProcessInfo.processInfo.environment["ZRT_CONFIG"])
            .map { URL(fileURLWithPath: ($0 as NSString).expandingTildeInPath) } ?? defaultConfigURL()
        if let v = opt("--set-enabled") {
            guard v == "on" || v == "off" else {
                FileHandle.standardError.write(Data("--set-enabled は on か off\n".utf8))
                exit(2)
            }
            exit(files.writeEnabled(v == "on") ? 0 : 1)
        }
        if let v = opt("--set-cloud") {
            guard v == "on" || v == "off" else {
                FileHandle.standardError.write(Data("--set-cloud は on か off\n".utf8))
                exit(2)
            }
            exit(files.writeCloud(v == "on") ? 0 : 1)
        }
        let app = NSApplication.shared
        if let png = opt("--ui-snapshot") {
            let config = AppConfig.load(configURL)
            if let mock = opt("--mock") {
                guard mockNames.contains(mock) else {
                    FileHandle.standardError.write(Data("unknown --mock \(mock) (\(mockNames.joined(separator: ", ")))\n".utf8))
                    exit(2)
                }
                if mock == "settings" { exit(snapshotSettings(to: png, config: config) ? 0 : 1) }
                exit(snapshot(to: png, look: mockLook(mock, cloudSummary: config.cloudSummary)) ? 0 : 1)
            }
            let look = makeLook(desired: files.readEnabled(), cloud: files.readCloud(), cloudSummary: config.cloudSummary,
                                status: files.readStatus(), pending: false, pendingTimedOut: false, now: Date())
            exit(snapshot(to: png, look: look) ? 0 : 1)
        }
        app.setActivationPolicy(.regular)
        let controller = AppController(files: files, configURL: configURL)
        app.delegate = controller
        withExtendedLifetime(controller) { app.run() }
    }
}
