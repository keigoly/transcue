// tapmeter — 受信タップ診断: 「タップに音が来ているか」を STT 非依存で音量(RMS)で測る。
//
// 目的: 字幕が出ないとき、原因が (a)タップに音が来ていない のか
//       (b)音は来ているが認識されない のかを切り分ける。
//
//   tapmeter --pid 4496 12         … 指定プロセスをタップして12秒計測
//   tapmeter --exclude-pid 36803 12 … システム全体をタップ(自分のTTSプロセスだけ除外)して計測
//
// ビルド: swiftc -O tapmeter.swift -o tapmeter

import AVFoundation
import CoreAudio
import Foundation

func out(_ s: String) { print(s); fflush(stdout) }

let args = CommandLine.arguments
var mode = "pid", targetPID: pid_t = 0
if let i = args.firstIndex(of: "--pid"), i + 1 < args.count { mode = "pid"; targetPID = pid_t(args[i+1]) ?? 0 }
if let i = args.firstIndex(of: "--exclude-pid"), i + 1 < args.count { mode = "exclude"; targetPID = pid_t(args[i+1]) ?? 0 }
let dur = Double(args.last ?? "12") ?? 12

func procObject(_ pid: pid_t) -> AudioObjectID? {
    var p = pid
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyTranslatePIDToProcessObject,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var obj = AudioObjectID(0)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    let st = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr,
                                        UInt32(MemoryLayout<pid_t>.size), &p, &size, &obj)
    return (st == noErr && obj != 0) ? obj : nil
}

func tapFormat(_ tapID: AudioObjectID) -> AVAudioFormat? {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioTapPropertyFormat, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var asbd = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    guard AudioObjectGetPropertyData(tapID, &addr, 0, nil, &size, &asbd) == noErr else { return nil }
    return AVAudioFormat(streamDescription: &asbd)
}

// 1) タップ記述
let desc: CATapDescription
if mode == "exclude" {
    guard let obj = procObject(targetPID) else { out("除外PIDの解決に失敗: \(targetPID)"); exit(2) }
    desc = CATapDescription(stereoGlobalTapButExcludeProcesses: [obj])
    out("モード: システム全体タップ(除外 pid=\(targetPID))")
} else {
    guard let obj = procObject(targetPID) else { out("PIDの解決に失敗: \(targetPID)"); exit(2) }
    desc = CATapDescription(stereoMixdownOfProcesses: [obj])
    out("モード: プロセスタップ(pid=\(targetPID))")
}
desc.name = "zrt-tapmeter"
desc.isPrivate = true

var tapID = AudioObjectID(0)
var st = AudioHardwareCreateProcessTap(desc, &tapID)
guard st == noErr, tapID != 0 else { out("tap_create 失敗 status=\(st)"); exit(3) }
guard let fmt = tapFormat(tapID) else { out("tap_format 取得失敗"); exit(4) }
out("tap_create OK  \(Int(fmt.sampleRate))Hz \(fmt.channelCount)ch")

// 2) 集約デバイス
let aggDict: [String: Any] = ["uid": UUID().uuidString, "name": "zrt-tapmeter-agg",
                              "private": 1, "taps": [["uid": desc.uuid.uuidString]]]
var aggID = AudioObjectID(0)
st = AudioHardwareCreateAggregateDevice(aggDict as CFDictionary, &aggID)
guard st == noErr, aggID != 0 else { out("aggregate 失敗 status=\(st)"); exit(5) }

// 3) IOProc で RMS/peak を集計
final class Meter: @unchecked Sendable {
    var sumSq = 0.0; var n = 0; var peak: Float = 0; var frames = 0
    let lock = NSLock()
}
let meter = Meter()
var procID: AudioDeviceIOProcID?
st = AudioDeviceCreateIOProcIDWithBlock(&procID, aggID, nil) { _, inData, _, _, _ in
    guard let buf = AVAudioPCMBuffer(pcmFormat: fmt, bufferListNoCopy: inData, deallocator: nil),
          buf.frameLength > 0, let ch = buf.floatChannelData else { return }
    var s = 0.0; var pk: Float = 0
    let nf = Int(buf.frameLength), nc = Int(buf.format.channelCount)
    for c in 0..<nc {
        let p = ch[c]
        for i in 0..<nf { let v = p[i]; s += Double(v * v); if abs(v) > pk { pk = abs(v) } }
    }
    meter.lock.lock()
    meter.sumSq += s; meter.n += nf * nc; meter.frames += nf
    if pk > meter.peak { meter.peak = pk }
    meter.lock.unlock()
}
guard st == noErr, let procID else { out("ioproc 失敗 status=\(st)"); exit(6) }
st = AudioDeviceStart(aggID, procID)
out("device_start status=\(st) — \(Int(dur))秒 計測開始")
out("秒  : RMS      peak     フレーム  判定")

var elapsed = 0.0
while elapsed < dur {
    Thread.sleep(forTimeInterval: 1.0); elapsed += 1
    meter.lock.lock()
    let rms = meter.n > 0 ? (meter.sumSq / Double(meter.n)).squareRoot() : 0
    let pk = meter.peak, fr = meter.frames
    meter.sumSq = 0; meter.n = 0; meter.peak = 0; meter.frames = 0
    meter.lock.unlock()
    let verdict = fr == 0 ? "音声フレーム未着" : (rms > 0.001 ? "★音声あり" : "無音(フレームは来ている)")
    out(String(format: "%3.0fs : %.6f  %.4f  %7d  %@", elapsed, rms, pk, fr, verdict))
}
AudioDeviceStop(aggID, procID)
AudioDeviceDestroyIOProcID(aggID, procID)
AudioHardwareDestroyAggregateDevice(aggID)
AudioHardwareDestroyProcessTap(tapID)
