// stt_helper — Phase 1-B: ライブマイク・ストリーミング STT 常駐 CLI(macOS 26 SpeechAnalyzer)。
//
// 既定の音声入力(AirPods 等)を AVAudioEngine で常時取得し、認識結果を stdout に JSONL で
// 1 行ずつ流す:
//   {"type":"ready","locale":"ja-JP","dst_hz":16000}
//   {"type":"partial","text":"…","start_s":0.0,"end_s":1.2,"t_ms":…}
//   {"type":"final","text":"…","start_s":0.0,"end_s":1.2,"t_ms":…}
//   {"type":"bye"}
// 診断・ライフサイクルは stderr へ(stdout は純粋なイベント列に保つ = 1-C の reader が読みやすい)。
// stdin プロトコル(1 行 1 コマンド): "quit" でフラッシュして終了。
// ※ ja の mid-stream 強制 finalize(ポーズ検出→finalize(through:))は 1-C セグメンターが駆動する。
//
// ビルド: swiftc -parse-as-library -O main.swift -o stt-helper
// 実行:   ./stt-helper ja-JP        (初回はマイク許可ダイアログが出る → 許可)
// ベンチ: ./stt-helper ja-JP --feed <音声ファイル>
//         マイクの代わりにファイルを実時間(100 ms ずつ)で流す(v2 A1 の再現会議用)。流し終えたら
//         通常の停止と同じくフラッシュして bye を出して終わる。
//         --feed-gate <path> を足すと、<path>.<locale>.waiting を作って待ち、<path> ができた瞬間に
//         流し始める(v2 A2-0。送受信の流し始めを揃える。<path> は run_bench.py が作る)。

import AVFoundation
import CoreMedia
import Foundation
import Speech

// stdout: 認識イベント。
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
// NSLock で内部同期しているため @unchecked Sendable が妥当。
final class TimeBox: @unchecked Sendable {
    private let lock = NSLock()
    private var seconds: Double = 0
    func add(_ s: Double) { lock.lock(); seconds += s; lock.unlock() }
    func cmtime() -> CMTime { lock.lock(); let s = seconds; lock.unlock(); return CMTime(seconds: s, preferredTimescale: 1000) }
}

// --feed: 音声ファイルを実時間で analyzer へ流す(マイク経路と同じ変換・同じ timeBox)。
// 流し終えたら done(呼び手が通常の停止 = フラッシュして bye に進む)。
func startFeed(path: String, dstFmt: AVAudioFormat, cont: AsyncStream<AnalyzerInput>.Continuation,
               timeBox: TimeBox, gate: String?, tag: String, done: @escaping @Sendable () -> Void) {
    Thread.detachNewThread {
        defer { done() }
        guard let file = try? AVAudioFile(forReading: URL(fileURLWithPath: path)) else {
            diag(["phase": "feed_error", "reason": "open_failed", "path": path])
            return
        }
        let inFmt = file.processingFormat
        guard let conv = AVAudioConverter(from: inFmt, to: dstFmt) else {
            diag(["phase": "feed_error", "reason": "no_converter"])
            return
        }
        let chunk = AVAudioFrameCount(max(1, Int(inFmt.sampleRate / 10)))   // 100 ms ずつ
        // --feed-gate: ファイルと変換を用意し終えてから待つ(go の後はすぐ流れる)。
        if let gate = gate {
            FileManager.default.createFile(atPath: "\(gate).\(tag).waiting", contents: nil)
            diag(["phase": "feed_wait", "gate": gate])
            let w0 = Date()
            while !FileManager.default.fileExists(atPath: gate) { Thread.sleep(forTimeInterval: 0.01) }
            diag(["phase": "feed_go", "waited_ms": Int(Date().timeIntervalSince(w0) * 1000)])
        }
        diag(["phase": "feed_start", "path": path, "in_hz": inFmt.sampleRate,
              "duration_s": (Double(file.length) / inFmt.sampleRate * 10).rounded() / 10])
        let t0 = Date()
        var fed = 0.0
        while file.framePosition < file.length {
            guard let inBuf = AVAudioPCMBuffer(pcmFormat: inFmt, frameCapacity: chunk) else { break }
            do {
                try file.read(into: inBuf, frameCount: chunk)
            } catch {
                diag(["phase": "feed_error", "reason": "\(error)"])
                break
            }
            if inBuf.frameLength == 0 { break }
            let ratio = dstFmt.sampleRate / inFmt.sampleRate
            let cap = AVAudioFrameCount(Double(inBuf.frameLength) * ratio) + 1024
            if let outBuf = AVAudioPCMBuffer(pcmFormat: dstFmt, frameCapacity: cap) {
                var used = false
                var convErr: NSError?
                _ = conv.convert(to: outBuf, error: &convErr) { _, status in
                    if used { status.pointee = .noDataNow; return nil }
                    used = true
                    status.pointee = .haveData
                    return inBuf
                }
                if convErr == nil && outBuf.frameLength > 0 {
                    cont.yield(AnalyzerInput(buffer: outBuf))
                    timeBox.add(Double(outBuf.frameLength) / dstFmt.sampleRate)
                }
            }
            fed += Double(inBuf.frameLength) / inFmt.sampleRate
            let wait = fed - Date().timeIntervalSince(t0)   // 実時間に合わせる
            if wait > 0 { Thread.sleep(forTimeInterval: wait) }
        }
        diag(["phase": "feed_end", "fed_s": (fed * 10).rounded() / 10,
              "wall_s": (Date().timeIntervalSince(t0) * 10).rounded() / 10])
    }
}

@main
struct STTHelper {
    static func main() async {
        // 引数: [locale] [--feed <file> [--feed-gate <path>]]
        var positional: [String] = []
        var feedPath: String? = nil
        var feedGate: String? = nil
        var ai = 1
        let argv = CommandLine.arguments
        while ai < argv.count {
            if argv[ai] == "--feed", ai + 1 < argv.count {
                feedPath = argv[ai + 1]
                ai += 2
                continue
            }
            if argv[ai] == "--feed-gate", ai + 1 < argv.count {
                feedGate = argv[ai + 1]
                ai += 2
                continue
            }
            positional.append(argv[ai])
            ai += 1
        }
        let localeId = positional.first ?? "ja-JP"
        do {
            let supported = await SpeechTranscriber.supportedLocales
            func bcp(_ l: Locale) -> String { l.identifier(.bcp47) }
            guard supported.contains(where: { bcp($0) == localeId }) else {
                diag(["phase": "abort", "reason": "locale_not_supported", "target": localeId])
                exit(2)
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
                exit(3)
            }

            let (inputStream, cont) = AsyncStream.makeStream(of: AnalyzerInput.self)
            let analyzer = SpeechAnalyzer(modules: [transcriber])

            // 認識結果 → stdout イベント。
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

            let timeBox = TimeBox()
            // ライブマイク取得(AVAudioEngine)。tap 内で dstFmt へ変換して analyzer へ流す。
            // --feed のときはマイクを開かない(ready の後に startFeed で流す)。
            var engine: AVAudioEngine? = nil
            if feedPath == nil {
                let eng = AVAudioEngine()
                let inputNode = eng.inputNode
                let inFmt = inputNode.outputFormat(forBus: 0)
                guard let conv = AVAudioConverter(from: inFmt, to: dstFmt) else {
                    diag(["phase": "abort", "reason": "no_converter"])
                    exit(4)
                }
                inputNode.installTap(onBus: 0, bufferSize: 4096, format: inFmt) { inBuf, _ in
                    let ratio = dstFmt.sampleRate / inFmt.sampleRate
                    let cap = AVAudioFrameCount(Double(inBuf.frameLength) * ratio) + 1024
                    guard let outBuf = AVAudioPCMBuffer(pcmFormat: dstFmt, frameCapacity: cap) else { return }
                    var used = false
                    var convErr: NSError?
                    _ = conv.convert(to: outBuf, error: &convErr) { _, status in
                        if used { status.pointee = .noDataNow; return nil }
                        used = true
                        status.pointee = .haveData
                        return inBuf
                    }
                    if convErr == nil && outBuf.frameLength > 0 {
                        cont.yield(AnalyzerInput(buffer: outBuf))
                        timeBox.add(Double(outBuf.frameLength) / dstFmt.sampleRate)
                    }
                }
                eng.prepare()
                try eng.start()
                engine = eng
            }

            // 停止トリガ: SIGINT/SIGTERM(orchestrator の kill / Ctrl-C 用)と stdin "quit"/EOF。
            let stop = DispatchSemaphore(value: 0)
            signal(SIGINT, SIG_IGN)
            signal(SIGTERM, SIG_IGN)
            let sigint = DispatchSource.makeSignalSource(signal: SIGINT, queue: .global())
            let sigterm = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .global())
            sigint.setEventHandler { stop.signal() }
            sigterm.setEventHandler { stop.signal() }
            sigint.resume()
            sigterm.resume()
            Thread.detachNewThread {
                while let line = readLine(strippingNewline: true) {
                    let cmd = line.trimmingCharacters(in: .whitespaces)
                    if cmd == "quit" { stop.signal(); return }
                    if cmd == "finalize" {
                        // ポーズ検出(1-C セグメンター)が節境界で送る強制 finalize。
                        let through = timeBox.cmtime()
                        Task {
                            do { try await analyzer.finalize(through: through) }
                            catch { diag(["phase": "finalize_error", "error": "\(error)"]) }
                        }
                    }
                }
                // stdin EOF では停止しない(バックグラウンド/orchestrator 起動時は stdin が閉じるため)。
                // 停止は SIGINT / SIGTERM で行う。
            }
            emit(["type": "ready", "locale": localeId, "dst_hz": dstFmt.sampleRate])
            if let path = feedPath {
                startFeed(path: path, dstFmt: dstFmt, cont: cont, timeBox: timeBox, gate: feedGate,
                          tag: localeId) { stop.signal() }
            }

            await withCheckedContinuation { (c: CheckedContinuation<Void, Never>) in
                DispatchQueue.global().async { stop.wait(); c.resume() }
            }
            _ = sigint
            _ = sigterm

            // 後片付け + フラッシュ(最大 1.5s。finalize がハングしても必ず終了する)。
            engine?.stop()
            engine?.inputNode.removeTap(onBus: 0)
            cont.finish()
            let flush = Task { try? await analyzer.finalizeAndFinishThroughEndOfInput(); _ = await consumer.value }
            Task { try? await Task.sleep(nanoseconds: 1_500_000_000); emit(["type": "bye", "note": "flush_timeout"]); exit(0) }
            _ = await flush.value
            emit(["type": "bye"])
            exit(0)
        } catch {
            diag(["phase": "error", "error": "\(error)"])
            exit(1)
        }
    }
}
