// transcribe_file — 録音ファイルをオフライン文字起こし(実時間より高速)。
//
// 会議の録音(m4a/wav/aiff)を SpeechAnalyzer に直接流し込み、確定結果を JSONL で出す。
// 用途: 実機で取りこぼした発話の事後確認、認識品質の評価、議事録化。
//   transcribe-file <audio> [locale] > out.jsonl     (locale 既定 en-US)
//
// ビルド: swiftc -parse-as-library -O main.swift -o transcribe-file

import AVFoundation
import Foundation
import Speech

func emit(_ o: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: o, options: [.sortedKeys]),
       let s = String(data: d, encoding: .utf8) { print(s); fflush(stdout) }
}
func diag(_ s: String) { FileHandle.standardError.write(Data((s + "\n").utf8)) }

@main
struct T {
    static func main() async {
        let args = CommandLine.arguments
        guard args.count > 1 else { diag("usage: transcribe-file <audio> [locale]"); exit(1) }
        let path = args[1]
        let localeId = args.count > 2 ? args[2] : "en-US"
        let t0 = Date()
        do {
            let file = try AVAudioFile(forReading: URL(fileURLWithPath: path))
            let srcFmt = file.processingFormat
            let total = Double(file.length) / srcFmt.sampleRate
            diag("入力: \(Int(srcFmt.sampleRate))Hz \(srcFmt.channelCount)ch  長さ \(String(format: "%.1f", total/60))分  locale=\(localeId)")

            let transcriber = SpeechTranscriber(locale: Locale(identifier: localeId),
                                                transcriptionOptions: [],
                                                reportingOptions: [.volatileResults, .fastResults],
                                                attributeOptions: [.audioTimeRange])
            diag("step: transcriber作成OK")
            if let req = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                diag("step: asset install開始"); try await req.downloadAndInstall()
            }
            diag("step: asset OK")
            guard let dstFmt = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: [transcriber]) else {
                diag("no format"); exit(2)
            }
            guard let conv = AVAudioConverter(from: srcFmt, to: dstFmt) else { diag("no converter"); exit(3) }

            let (stream, cont) = AsyncStream.makeStream(of: AnalyzerInput.self)
            let analyzer = SpeechAnalyzer(modules: [transcriber])

            let consumer = Task {
                var n = 0
                for try await r in transcriber.results where r.isFinal {
                    n += 1
                    emit(["i": n,
                          "start_s": (CMTimeGetSeconds(r.range.start) * 100).rounded() / 100,
                          "end_s": (CMTimeGetSeconds(r.range.end) * 100).rounded() / 100,
                          "text": String(r.text.characters)])
                }
                return n
            }
            diag("step: analyzer.start前")
            try await analyzer.start(inputSequence: stream)
            diag("step: analyzer.start後")

            // ファイルを一括で読み込んで供給(実時間待ちなし)
            let chunk: AVAudioFrameCount = 16384
            while true {
                if file.framePosition >= file.length { break }
                guard let inBuf = AVAudioPCMBuffer(pcmFormat: srcFmt, frameCapacity: chunk) else { break }
                do { try file.read(into: inBuf, frameCount: chunk) } catch { diag("read終了: \(error)"); break }
                if inBuf.frameLength == 0 { break }
                let ratio = dstFmt.sampleRate / srcFmt.sampleRate
                let cap = AVAudioFrameCount(Double(inBuf.frameLength) * ratio) + 4096
                guard let outBuf = AVAudioPCMBuffer(pcmFormat: dstFmt, frameCapacity: cap) else { break }
                var used = false
                var err: NSError?
                _ = conv.convert(to: outBuf, error: &err) { _, st in
                    if used { st.pointee = .noDataNow; return nil }
                    used = true; st.pointee = .haveData; return inBuf
                }
                if err == nil && outBuf.frameLength > 0 { cont.yield(AnalyzerInput(buffer: outBuf)) }
            }
            cont.finish()
            diag("step: 供給完了 → finalize")
            try await analyzer.finalizeAndFinishThroughEndOfInput()
            diag("step: finalize完了")
            let n = try await consumer.value
            diag("完了: \(n)件  所要 \(String(format: "%.1f", Date().timeIntervalSince(t0)))秒 (音声 \(String(format: "%.1f", total/60))分)")
        } catch {
            diag("error: \(error)"); exit(9)
        }
    }
}
