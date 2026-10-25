// set-input — システム既定 *入力* デバイスを名前(部分一致)で切り替える小ツール。
//
// 目的: ヘッドホン(Bluetooth)を再接続すると macOS が既定入力までヘッドホンのマイクに
// 変えてしまい、英語TTSの自己モニタをそのマイクが拾って STT が再認識する「自己ループ」を招く。
// 本ツールで既定入力を内蔵マイクへ戻す(出力はヘッドホンのまま=高音質A2DPを維持)。
//
// 使い方:
//   set-input --list                 入力デバイス一覧と現在の既定を表示
//   set-input "MacBook"              名前に含むデバイスを既定入力にする(部分一致・大小無視)
//
// ビルド: swiftc -O main.swift -o set-input

import CoreAudio
import Foundation

func devName(_ id: AudioObjectID) -> String {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioObjectPropertyName, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var name: CFString = "" as CFString
    var size = UInt32(MemoryLayout<CFString>.size)
    let st = withUnsafeMutablePointer(to: &name) {
        AudioObjectGetPropertyData(id, &addr, 0, nil, &size, $0)
    }
    return st == noErr ? (name as String) : ""
}

func inputChannels(_ id: AudioObjectID) -> Int {
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioDevicePropertyStreamConfiguration, mScope: kAudioObjectPropertyScopeInput,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size),
                                               alignment: MemoryLayout<AudioBufferList>.alignment)
    defer { raw.deallocate() }
    guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, raw) == noErr else { return 0 }
    let abl = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
    return abl.reduce(0) { $0 + Int($1.mNumberChannels) }
}

func allDevices() -> [AudioObjectID] {
    let sys = AudioObjectID(kAudioObjectSystemObject)
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(sys, &addr, 0, nil, &size) == noErr else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(sys, &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func defaultInput() -> AudioObjectID {
    let sys = AudioObjectID(kAudioObjectSystemObject)
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultInputDevice, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var id = AudioObjectID(0)
    var size = UInt32(MemoryLayout<AudioObjectID>.size)
    _ = AudioObjectGetPropertyData(sys, &addr, 0, nil, &size, &id)
    return id
}

func setDefaultInput(_ id: AudioObjectID) -> OSStatus {
    let sys = AudioObjectID(kAudioObjectSystemObject)
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultInputDevice, mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var dev = id
    return AudioObjectSetPropertyData(sys, &addr, 0, nil, UInt32(MemoryLayout<AudioObjectID>.size), &dev)
}

let args = CommandLine.arguments
let cur = defaultInput()
let inputs = allDevices().filter { inputChannels($0) > 0 }

if args.count < 2 || args[1] == "--list" {
    print("入力デバイス(★=現在の既定):")
    for d in inputs {
        print("  \(d == cur ? "★" : " ") \(devName(d))  (\(inputChannels(d))ch)")
    }
    exit(0)
}

let needle = args[1].lowercased()
guard let target = inputs.first(where: { devName($0).lowercased().contains(needle) }) else {
    FileHandle.standardError.write("入力デバイスが見つかりません: \(args[1])\n".data(using: .utf8)!)
    exit(2)
}
if target == cur {
    print("既に既定入力です: \(devName(target))")
    exit(0)
}
let st = setDefaultInput(target)
if st == noErr {
    print("既定入力を変更: \(devName(cur)) → \(devName(target))")
} else {
    FileHandle.standardError.write("変更に失敗 status=\(st)\n".data(using: .utf8)!)
    exit(3)
}
