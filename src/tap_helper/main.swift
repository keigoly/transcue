// tap_helper — Phase 2-A: 受信方向 STT。Zoom(等)プロセスの出力音声を Core Audio process tap で
// 取得し、SpeechAnalyzer(既定 en-US)でストリーミング認識して stdout に JSONL で流す。
//
// 送信方向の stt_helper(マイク入力)の「鏡」。出力プロトコルと stdin コマンドは stt_helper と
// 完全に同一に保つため、Python 側(stt_reader.py / segmenter.py)は無改修で再利用できる:
//   {"type":"ready","locale":"en-US","dst_hz":16000,"tap_hz":48000,"source":"…"}
//   {"type":"partial","text":"…","start_s":0.0,"end_s":1.2,"t_ms":…}
//   {"type":"final","text":"…","start_s":0.0,"end_s":1.2,"t_ms":…}
//   {"type":"bye"}
// 診断・ライフサイクルは stderr へ(stdout は純粋なイベント列に保つ = reader が読みやすい)。
// stdin(1 行 1 コマンド): "finalize"(強制確定) / "quit"(終了)。停止は SIGINT/SIGTERM でも可。
//
// 音源の指定(CLI, locale の位置は不問):
//   tap-helper en-US --file <path>   … afplay で再生したファイルをタップ(テスト用, Spike2 方式)
//   tap-helper en-US --pid <pid>     … 指定 PID の出力をタップ
//   tap-helper en-US --name <substr> … 名前部分一致で GUI アプリを解決してタップ(既定: "zoom")
//
// ビルド: swiftc -parse-as-library -O main.swift -o tap-helper
// 権限:   初回の AudioHardwareCreateProcessTap で「システム音声録音」許可が要求される
//         (System Settings > Privacy & Security)。未許可だと tap_create が失敗する。

import AVFoundation
import AppKit
import AudioToolbox
import CoreAudio
import CoreMedia
import Foundation
import Speech

// stdout: 認識イベント(stt_helper と同一)。
func emit(_ fields: [String: Any]) {
    var f = fields
    f["t_ms"] = Int(Date().timeIntervalSince1970 * 1000)
    if let d = try? JSONSerialization.data(withJSONObject: f, options: [.sortedKeys]),
       let s = String(data: d, encoding: .utf8) {
        print(s)
        fflush(stdout)
    }
}

// stderr: 診断。
func diag(_ fields: [String: Any]) {
    var f = fields
    f["t_ms"] = Int(Date().timeIntervalSince1970 * 1000)
    if let d = try? JSONSerialization.data(withJSONObject: f, options: [.sortedKeys]),
       let s = String(data: d, encoding: .utf8) {
        FileHandle.standardError.write(Data((s + "\n").utf8))
    }
}

// フィード済み音声の最新位置(finalize(through:) の対象時刻)をスレッド安全に保持。
final class TimeBox: @unchecked Sendable {
    private let lock = NSLock()
    private var seconds: Double = 0
    func add(_ s: Double) { lock.lock(); seconds += s; lock.unlock() }
    func cmtime() -> CMTime { lock.lock(); let s = seconds; lock.unlock(); return CMTime(seconds: s, preferredTimescale: 1000) }
}

// タップ対象プロセスの解決結果。
struct TapTarget {
    var pid: pid_t
    var spawned: Process?   // --file モードで afplay を起動した場合のみ(auto-stop 用)
    var label: String
}

// システム全体タップ時に除外する PID(--exclude-pid)。0 = 通常のプロセスタップ。
var gGlobalExcludePID: pid_t = 0

// CLI から音源(タップ対象)を解決する。locale トークンは無視して --file/--pid/--name を探す。
func resolveTarget() -> TapTarget? {
    let args = CommandLine.arguments
    var mode = "name"
    var value = "zoom"
    var i = 1
    while i < args.count {
        switch args[i] {
        case "--file": mode = "file"; if i + 1 < args.count { value = args[i + 1]; i += 1 }
        case "--pid":  mode = "pid";  if i + 1 < args.count { value = args[i + 1]; i += 1 }
        case "--name": mode = "name"; if i + 1 < args.count { value = args[i + 1]; i += 1 }
        case "--exclude-pid": mode = "exclude"; if i + 1 < args.count { value = args[i + 1]; i += 1 }
        default: break
        }
        i += 1
    }
    switch mode {
    case "file":
        let player = Process()
        player.executableURL = URL(fileURLWithPath: "/usr/bin/afplay")
        player.arguments = [value]
        do { try player.run() } catch {
            diag(["phase": "abort", "reason": "afplay_spawn_failed", "error": "\(error)"])
            return nil
        }
        Thread.sleep(forTimeInterval: 0.5)  // afplay が HAL に登録されるのを待つ
        return TapTarget(pid: player.processIdentifier, spawned: player, label: "file:\(value)")
    case "pid":
        guard let p = pid_t(value) else {
            diag(["phase": "abort", "reason": "bad_pid", "value": value]); return nil
        }
        return TapTarget(pid: p, spawned: nil, label: "pid:\(p)")
    case "exclude":
        // システム全体タップ(指定 PID のみ除外)。Zoom の内部プロセス構成変更に影響されない。
        // 除外するのは自分の TTS 再生プロセス(自己モニタの英語を再認識しないため)。
        guard let p = pid_t(value) else {
            diag(["phase": "abort", "reason": "bad_pid", "value": value]); return nil
        }
        gGlobalExcludePID = p
        return TapTarget(pid: p, spawned: nil, label: "global-exclude:\(p)")
    default: // name
        let needle = value.lowercased()
        let match = NSWorkspace.shared.runningApplications.first { app in
            let n = (app.localizedName ?? "").lowercased()
            let b = (app.bundleIdentifier ?? "").lowercased()
            return n.contains(needle) || b.contains(needle)
        }
        guard let app = match else {
            diag(["phase": "abort", "reason": "process_not_found", "name": value]); return nil
        }
        return TapTarget(pid: app.processIdentifier, spawned: nil, label: "name:\(value)#\(app.processIdentifier)")
    }
}

// タップの音声フォーマット(ASBD)を AVAudioFormat として取得。IO proc が届ける形式と一致する。
func tapFormat(_ tapID: AudioObjectID) -> AVAudioFormat? {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioTapPropertyFormat,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var asbd = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    let st = AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &asbd)
    guard st == noErr else { diag(["phase": "tap_format_failed", "status": st]); return nil }
    return AVAudioFormat(streamDescription: &asbd)
}

// --list: 音声プロセス一覧(PID/アプリ名/「出力中」)。Zoom の会議音声を出しているプロセスの
// PID を特定して `receive.py --pid <PID>` に使うための診断。Core Audio 不要の tap は張らない。
func processPID(_ obj: AudioObjectID) -> pid_t {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioProcessPropertyPID, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var val: pid_t = -1
    var size = UInt32(MemoryLayout<pid_t>.size)
    let st = AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &val)
    return st == noErr ? val : -1
}

func processIsOutputting(_ obj: AudioObjectID) -> Bool {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioProcessPropertyIsRunningOutput, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var val: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    let st = AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &val)
    return st == noErr && val != 0
}

func processBundleID(_ obj: AudioObjectID) -> String {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioProcessPropertyBundleID, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var unmanaged: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    let st = AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &unmanaged)
    guard st == noErr, let u = unmanaged else { return "" }
    return u.takeRetainedValue() as String
}

func listAudioProcesses() {
    let sys = AudioObjectID(kAudioObjectSystemObject)
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyProcessObjectList, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var dataSize: UInt32 = 0
    var st = AudioObjectGetPropertyDataSize(sys, &addr, 0, nil, &dataSize)
    guard st == noErr else { print("音声プロセス一覧の取得に失敗 status=\(st)"); return }
    let count = Int(dataSize) / MemoryLayout<AudioObjectID>.size
    var procs = [AudioObjectID](repeating: 0, count: count)
    st = AudioObjectGetPropertyData(sys, &addr, 0, nil, &dataSize, &procs)
    guard st == noErr else { print("音声プロセス一覧の取得に失敗2 status=\(st)"); return }

    var rows: [(pid: pid_t, out: Bool, name: String, bundle: String)] = []
    for p in procs {
        let pid = processPID(p)
        let out = processIsOutputting(p)
        let bundle = processBundleID(p)
        let name = pid > 0 ? (NSRunningApplication(processIdentifier: pid)?.localizedName ?? "") : ""
        rows.append((pid, out, name, bundle))
    }
    rows.sort { (($0.out ? 0 : 1), $0.name.lowercased()) < (($1.out ? 0 : 1), $1.name.lowercased()) }

    print("音声プロセス一覧(♪ = 現在音声を出力中)")
    print("  PID    ♪  アプリ名  (bundle id)")
    print("  -----  -  ------------------------------------------")
    for r in rows {
        let mark = r.out ? "♪" : " "
        let nm = r.name.isEmpty ? "(名称不明)" : r.name
        print(String(format: "  %-5d  %@  %@  (%@)", r.pid, mark, nm, r.bundle.isEmpty ? "-" : r.bundle))
    }
    print("")
    print("使い方: Zoom会議で相手が話している間にこれを実行し、通話音声を出している行")
    print("        (♪ が付き、名前が zoom / 系ヘルパー)の PID を控える →")
    print("        python src/receive.py 120 --pid <PID>")
}

@main
struct TapHelper {
    static func main() async {
        if CommandLine.arguments.contains("--list") {
            listAudioProcesses()
            exit(0)
        }
        let args = CommandLine.arguments
        let localeId = (args.count > 1 && !args[1].hasPrefix("--")) ? args[1] : "en-US"
        let t0 = Date()
        func msSince(_ d: Date) -> Int { Int(Date().timeIntervalSince(d) * 1000) }

        // 1) タップ対象プロセスの解決(--file は afplay を起動)。
        guard let target = resolveTarget() else { exit(2) }
        diag(["phase": "target_resolved", "target": target.label, "pid": target.pid])

        do {
            // 2) SpeechAnalyzer(locale)の準備。
            let supported = await SpeechTranscriber.supportedLocales
            func bcp(_ l: Locale) -> String { l.identifier(.bcp47) }
            guard supported.contains(where: { bcp($0) == localeId }) else {
                diag(["phase": "abort", "reason": "locale_not_supported", "target": localeId])
                target.spawned?.terminate(); exit(3)
            }
            let transcriber = SpeechTranscriber(
                locale: Locale(identifier: localeId),
                transcriptionOptions: [],
                reportingOptions: [.volatileResults, .fastResults],
                attributeOptions: [.audioTimeRange])
            if let req = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                diag(["phase": "asset_download_start"])
                try await req.downloadAndInstall()
            }
            diag(["phase": "asset_ready"])
            guard let dstFmt = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
                diag(["phase": "abort", "reason": "no_format"])
                target.spawned?.terminate(); exit(4)
            }

            let (inputStream, cont) = AsyncStream.makeStream(of: AnalyzerInput.self)
            let analyzer = SpeechAnalyzer(modules: [transcriber])

            // 認識結果 → stdout イベント(stt_helper と同一プロトコル)。
            let consumer = Task {
                var partials = 0
                var finals = 0
                do {
                    for try await res in transcriber.results {
                        let startS = CMTimeGetSeconds(res.range.start)
                        let endS = CMTimeGetSeconds(res.range.end)
                        if res.isFinal { finals += 1 } else { partials += 1 }
                        emit(["type": res.isFinal ? "final" : "partial",
                              "text": String(res.text.characters),
                              "start_s": (startS * 100).rounded() / 100,
                              "end_s": (endS * 100).rounded() / 100])
                    }
                } catch {
                    diag(["phase": "results_error", "error": "\(error)"])
                }
                diag(["phase": "results_done", "partials": partials, "finals": finals])
            }

            try await analyzer.start(inputSequence: inputStream)

            // 3) PID → audio process object(Spike2 と同一)。
            var pid = target.pid
            var addr = AudioObjectPropertyAddress(
                mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain)
            var procObj = AudioObjectID(0)
            var size = UInt32(MemoryLayout<AudioObjectID>.size)
            var st = AudioObjectGetPropertyData(
                AudioObjectID(kAudioObjectSystemObject), &addr,
                UInt32(MemoryLayout<pid_t>.size), &pid, &size, &procObj)
            if st != noErr || procObj == 0 {
                // 全体タップ時は「除外対象が音声オブジェクトを持たない」ことが普通にある
                // (まだ音を鳴らしていないプロセス等)。中止せず "除外なしの全体タップ" に落とす。
                if gGlobalExcludePID != 0 {
                    diag(["phase": "exclude_unresolved", "pid": gGlobalExcludePID,
                          "note": "除外対象が音声オブジェクト未保有 → 除外なしの全体タップにフォールバック"])
                    procObj = 0
                } else {
                    diag(["phase": "abort", "reason": "pid_translate_failed", "status": st])
                    target.spawned?.terminate(); exit(5)
                }
            }

            // 4) タップ生成。既定はプロセス単体のステレオミックスダウン。
            // --exclude-pid 指定時は「システム全体タップ(指定PIDのみ除外)」にする:
            // Zoom は版によって会議音声の描画プロセスが変わる(ZoomHybridConf 等)ため、
            // プロセス単体タップだと取りこぼす。全体タップなら聞こえている音は必ず拾える。
            let desc: CATapDescription
            if gGlobalExcludePID != 0 {
                // procObj == 0 → 除外リスト空 = 全システム音声を拾う
                let excl: [AudioObjectID] = procObj != 0 ? [procObj] : []
                desc = CATapDescription(stereoGlobalTapButExcludeProcesses: excl)
                diag(["phase": "tap_mode", "mode": "global_exclude",
                      "exclude_pid": gGlobalExcludePID, "excluded": excl.count])
            } else {
                desc = CATapDescription(stereoMixdownOfProcesses: [procObj])
                diag(["phase": "tap_mode", "mode": "process", "pid": target.pid])
            }
            desc.name = "zrt-recv-tap"
            desc.isPrivate = true
            let tTap = Date()
            var tapID = AudioObjectID(0)
            st = AudioHardwareCreateProcessTap(desc, &tapID)
            guard st == noErr, tapID != 0 else {
                diag(["phase": "abort", "reason": "tap_create_failed", "status": st,
                      "hint": "System Settings > Privacy & Security > システム音声録音 を許可してください"])
                target.spawned?.terminate(); exit(6)
            }
            diag(["phase": "tap_create", "status": st, "tap_id": tapID, "ms": msSince(tTap)])

            guard let tapFmt = tapFormat(tapID) else {
                AudioHardwareDestroyProcessTap(tapID); target.spawned?.terminate(); exit(7)
            }
            diag(["phase": "tap_format", "hz": tapFmt.sampleRate, "ch": tapFmt.channelCount])

            // 5) タップを含む集約デバイス(Spike2 で本機実証済みのリテラルキー形式)。
            let aggDict: [String: Any] = [
                "uid": UUID().uuidString,                    // kAudioAggregateDeviceUIDKey
                "name": "zrt-recv-agg",                       // kAudioAggregateDeviceNameKey
                "private": 1,                                 // kAudioAggregateDeviceIsPrivateKey
                "taps": [["uid": desc.uuid.uuidString]]        // kAudioAggregateDeviceTapListKey / kAudioSubTapUIDKey
            ]
            var aggID = AudioObjectID(0)
            st = AudioHardwareCreateAggregateDevice(aggDict as CFDictionary, &aggID)
            guard st == noErr, aggID != 0 else {
                diag(["phase": "abort", "reason": "aggregate_create_failed", "status": st])
                AudioHardwareDestroyProcessTap(tapID); target.spawned?.terminate(); exit(8)
            }
            diag(["phase": "aggregate_create", "status": st, "agg_id": aggID])

            // 6) 変換器(タップ形式 → SpeechAnalyzer 形式)。マイク helper と同じ供給パターン。
            guard let conv = AVAudioConverter(from: tapFmt, to: dstFmt) else {
                diag(["phase": "abort", "reason": "no_converter"])
                AudioHardwareDestroyAggregateDevice(aggID); AudioHardwareDestroyProcessTap(tapID)
                target.spawned?.terminate(); exit(9)
            }
            let timeBox = TimeBox()

            // 7) IO proc: タップ音声 → 変換 → analyzer へ yield。
            var procID: AudioDeviceIOProcID?
            st = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, nil) { _, inInputData, _, _, _ in
                guard let srcBuf = AVAudioPCMBuffer(pcmFormat: tapFmt, bufferListNoCopy: inInputData, deallocator: nil),
                      srcBuf.frameLength > 0 else { return }
                let ratio = dstFmt.sampleRate / tapFmt.sampleRate
                let cap = AVAudioFrameCount(Double(srcBuf.frameLength) * ratio) + 1024
                guard let outBuf = AVAudioPCMBuffer(pcmFormat: dstFmt, frameCapacity: cap) else { return }
                var used = false
                var convErr: NSError?
                _ = conv.convert(to: outBuf, error: &convErr) { _, status in
                    if used { status.pointee = .noDataNow; return nil }
                    used = true
                    status.pointee = .haveData
                    return srcBuf
                }
                if convErr == nil && outBuf.frameLength > 0 {
                    cont.yield(AnalyzerInput(buffer: outBuf))
                    timeBox.add(Double(outBuf.frameLength) / dstFmt.sampleRate)
                }
            }
            guard st == noErr, let procID else {
                diag(["phase": "abort", "reason": "ioproc_create_failed", "status": st])
                AudioHardwareDestroyAggregateDevice(aggID); AudioHardwareDestroyProcessTap(tapID)
                target.spawned?.terminate(); exit(10)
            }
            st = AudioDeviceStart(aggID, procID)
            diag(["phase": "device_start", "status": st, "total_setup_ms": msSince(t0)])

            // 8) 停止トリガ: SIGINT/SIGTERM、stdin "quit"、--file モードは afplay 終了で自動停止。
            let stop = DispatchSemaphore(value: 0)
            signal(SIGINT, SIG_IGN)
            signal(SIGTERM, SIG_IGN)
            let sigint = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
            let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
            sigint.setEventHandler { stop.signal() }
            sigterm.setEventHandler { stop.signal() }
            sigint.resume()
            sigterm.resume()
            if let player = target.spawned {
                player.terminationHandler = { _ in stop.signal() }
            }
            Thread.detachNewThread {
                while let line = readLine(strippingNewline: true) {
                    let cmd = line.trimmingCharacters(in: .whitespaces)
                    if cmd == "quit" { stop.signal(); return }
                    if cmd == "finalize" {
                        // ポーズ検出(セグメンター)が節境界で送る強制 finalize。
                        let through = timeBox.cmtime()
                        Task {
                            do { try await analyzer.finalize(through: through) }
                            catch { diag(["phase": "finalize_error", "error": "\(error)"]) }
                        }
                    }
                }
                // stdin EOF では停止しない(orchestrator 起動時は stdin が閉じるため)。
            }
            emit(["type": "ready", "locale": localeId, "dst_hz": dstFmt.sampleRate,
                  "tap_hz": tapFmt.sampleRate, "source": target.label])

            await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
                DispatchQueue.global().async { stop.wait(); c.resume() }
            }
            _ = sigint
            _ = sigterm

            // 9) 後片付け(Core Audio を先に止める)+ フラッシュ(最大 1.5s で必ず終了)。
            AudioDeviceStop(aggID, procID)
            AudioDeviceDestroyIOProcID(aggID, procID)
            AudioHardwareDestroyAggregateDevice(aggID)
            AudioHardwareDestroyProcessTap(tapID)
            if let player = target.spawned, player.isRunning { player.terminate() }
            cont.finish()
            let flush = Task { try? await analyzer.finalizeAndFinishThroughEndOfInput(); _ = await consumer.value }
            Task { try? await Task.sleep(nanoseconds: 1_500_000_000); emit(["type": "bye", "note": "flush_timeout"]); exit(0) }
            _ = await flush.value
            emit(["type": "bye"])
            exit(0)
        } catch {
            diag(["phase": "error", "error": "\(error)"])
            target.spawned?.terminate()
            exit(1)
        }
    }
}
