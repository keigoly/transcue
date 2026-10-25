// hotkey_helper — Phase 3-E: 一時停止/再開のグローバルホットキー(既定 ⌃⌥P)。
//
// Carbon RegisterEventHotKey を使う。システム全体で有効・**Accessibility 許可不要**
// (NSEvent グローバルモニタや CGEventTap と違い TCC プロンプトが出ない)。
// キー押下ごとに stdout へ {"type":"toggle"} を 1 行出力。Python 側(hotkey.py)が
// paused を反転し、翻訳/TTS送出/字幕をスキップする。stt/tap/subtitle helper と同じ
// 「stdin/stdout 駆動の Swift helper」パターン(4 本目)。
//
// stdin: "quit" で終了。停止は SIGINT/SIGTERM でも可。
// テスト用: --emit-toggles "3,6" で 3 秒後・6 秒後に toggle を自動発火(実キー不要の検証)。
//
// ビルド: swiftc -parse-as-library -O main.swift -o hotkey-helper

import Carbon
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
    }
}

@main
struct HotkeyHelper {
    static func main() {
        // --emit-toggles "3,6"(テスト用: 指定秒後に toggle を自動発火)
        let args = CommandLine.arguments
        var testTimes: [Double] = []
        if let i = args.firstIndex(of: "--emit-toggles"), i + 1 < args.count {
            testTimes = args[i + 1].split(separator: ",").compactMap { Double($0.trimmingCharacters(in: .whitespaces)) }
        }

        // グローバルホットキー ⌃⌥P を登録(非キャプチャクロージャ=C 関数ポインタとして渡せる)。
        let handler: EventHandlerUPP = { (_, _, _) -> OSStatus in
            emit(["type": "toggle"])
            return noErr
        }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetEventDispatcherTarget(), handler, 1, &spec, nil, nil)

        let hotID = EventHotKeyID(signature: OSType(0x5A52_5448), id: 1)  // 'ZRTH'
        var ref: EventHotKeyRef?
        let mods = UInt32(controlKey | optionKey)
        let st = RegisterEventHotKey(UInt32(kVK_ANSI_P), mods, hotID, GetEventDispatcherTarget(), 0, &ref)
        emit(["type": "ready", "hotkey": "⌃⌥P", "status": st])
        if st != noErr { diag(["phase": "warn", "note": "hotkey_register_failed", "status": st]) }

        // 停止: SIGINT/SIGTERM → run loop を止める。
        signal(SIGINT, SIG_IGN)
        signal(SIGTERM, SIG_IGN)
        let onSig: @convention(block) () -> Void = { CFRunLoopStop(CFRunLoopGetMain()) }
        let si = DispatchSource.makeSignalSource(signal: SIGINT, queue: .main)
        let sg = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        si.setEventHandler(handler: onSig)
        sg.setEventHandler(handler: onSig)
        si.resume()
        sg.resume()

        // stdin "quit" で終了。EOF では終了しない(orchestrator 起動時に閉じるため)。
        Thread.detachNewThread {
            while let line = readLine(strippingNewline: true) {
                if line.trimmingCharacters(in: .whitespaces) == "quit" {
                    CFRunLoopStop(CFRunLoopGetMain())
                    return
                }
            }
        }

        // テスト自動発火(実キー不要で paused 配線を検証)。
        for t in testTimes {
            DispatchQueue.main.asyncAfter(deadline: .now() + t) { emit(["type": "toggle"]) }
        }

        _ = si
        _ = sg
        CFRunLoopRun()
        emit(["type": "bye"])
        exit(0)
    }
}
