// audio-procs — 音声を使っているプロセスの一覧(PID・アプリ名・入力中 / 出力中・使っている機器)。
//
// v3 Step 1(Google Meet 対応・PLAN_v3.md §2.2): 会議アプリ(Zoom・Chrome の Meet)が BlackHole を仮想マイクとして
// 読んでいるかを見て、会議の検知の手がかりにできるかを調べる。Core Audio のプロセス情報
// (kAudioHardwarePropertyProcessObjectList と kAudioProcessProperty*)を読むだけで、タップは張らない(権限は要らない)。
// 本番の tap-helper は作り直すと「システム音声録音」の許可がやり直しになるおそれがあるので、別の道具にした。
//
//   audio-procs              … 表で出す(入 = 入力中・出 = 出力中)
//   audio-procs --json       … 1 行 1 プロセスの JSON
//   audio-procs --watch <秒> … その間隔で読み、現れた・入出力が変わった・消えたプロセスだけを JSON で出す(Ctrl-C で終わる)
//
// ビルド: swiftc -parse-as-library -O audioprocs.swift -o audio-procs

import AppKit
import CoreAudio
import Darwin
import Foundation

struct Proc: Equatable {
    var pid: pid_t
    var uid: Int                  // 持ち主のアカウント。ほかのユーザーが同時にログインしているとその人のプロセスも見えるので、
                                  // 会議の検知では自分のアカウントのプロセスだけを見る
    var name: String
    var bundle: String
    var input: Bool
    var output: Bool
    var inDevices: [String]
    var outDevices: [String]

    var json: [String: Any] {
        ["pid": Int(pid), "uid": uid, "mine": uid == Int(getuid()), "name": name, "bundle": bundle, "in": input,
         "out": output, "in_devices": inDevices, "out_devices": outDevices]
    }
}

func address(_ sel: AudioObjectPropertySelector, _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal)
    -> AudioObjectPropertyAddress {
    AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: kAudioObjectPropertyElementMain)
}

func uint32(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> UInt32 {
    var addr = address(sel)
    var val: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    return AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &val) == noErr ? val : 0
}

func string(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector) -> String {
    var addr = address(sel)
    var unmanaged: Unmanaged<CFString>?
    var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
    guard AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &unmanaged) == noErr, let u = unmanaged else { return "" }
    return u.takeRetainedValue() as String
}

func objectIDs(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector,
               _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> [AudioObjectID] {
    var addr = address(sel, scope)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(obj, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &ids) == noErr else { return [] }
    return ids
}

func pidOf(_ obj: AudioObjectID) -> pid_t {
    var addr = address(kAudioProcessPropertyPID)
    var val: pid_t = -1
    var size = UInt32(MemoryLayout<pid_t>.size)
    return AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &val) == noErr ? val : -1
}

// 持ち主の uid とプロセス名(sysctl。ほかのアカウントのプロセスでも引ける)。引けなければ uid = -1。
func kinfo(_ pid: pid_t) -> (uid: Int, comm: String) {
    var info = kinfo_proc()
    var size = MemoryLayout<kinfo_proc>.stride
    var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
    guard pid > 0, sysctl(&mib, 4, &info, &size, nil, 0) == 0, size > 0 else { return (-1, "") }
    let comm = withUnsafePointer(to: &info.kp_proc.p_comm) {
        $0.withMemoryRebound(to: CChar.self, capacity: Int(MAXCOMLEN) + 1) { String(cString: $0) }
    }
    return (Int(info.kp_eproc.e_ucred.cr_uid), comm)
}

// アプリ名。自分のアカウントの GUI アプリは表示名、それ以外(Chrome のヘルパー・ほかのアカウント)はプロセス名。
func nameOf(_ pid: pid_t, comm: String) -> String {
    if pid > 0, let n = NSRunningApplication(processIdentifier: pid)?.localizedName { return n }
    return comm
}

func snapshot() -> [Proc] {
    objectIDs(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList).map { obj in
        let pid = pidOf(obj)
        let k = kinfo(pid)
        let devices = { (scope: AudioObjectPropertyScope) in
            objectIDs(obj, kAudioProcessPropertyDevices, scope).map { string($0, kAudioObjectPropertyName) }
        }
        return Proc(pid: pid, uid: k.uid, name: nameOf(pid, comm: k.comm), bundle: string(obj, kAudioProcessPropertyBundleID),
                    input: uint32(obj, kAudioProcessPropertyIsRunningInput) != 0,
                    output: uint32(obj, kAudioProcessPropertyIsRunningOutput) != 0,
                    inDevices: devices(kAudioObjectPropertyScopeInput),
                    outDevices: devices(kAudioObjectPropertyScopeOutput))
    }
}

func printJSON(_ fields: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys]),
       let s = String(data: d, encoding: .utf8) {
        print(s)
        fflush(stdout)
    }
}

func printTable(_ procs: [Proc]) {
    let rows = procs.sorted { (($0.input || $0.output) ? 0 : 1, $0.name.lowercased()) < (($1.input || $1.output) ? 0 : 1, $1.name.lowercased()) }
    print("  PID    uid  入 出  アプリ名 (bundle id)  — 入力機器 / 出力機器(* = 自分のアカウント)")
    for p in rows {
        let devs = (p.inDevices.isEmpty && p.outDevices.isEmpty) ? ""
            : "  — 入: \(p.inDevices.joined(separator: ", ")) / 出: \(p.outDevices.joined(separator: ", "))"
        print(String(format: "  %-5d  %3d%@ %@  %@  %@ (%@)%@", p.pid, p.uid, p.uid == Int(getuid()) ? "*" : " ",
                     p.input ? "●" : "·", p.output ? "●" : "·",
                     p.name.isEmpty ? "(名称不明)" : p.name, p.bundle.isEmpty ? "-" : p.bundle, devs))
    }
}

@main
struct AudioProcs {
    static func main() {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--watch") {
            let interval = i + 1 < args.count ? (Double(args[i + 1]) ?? 1) : 1
            var last: [pid_t: Proc] = [:]
            while true {
                let now = Dictionary(snapshot().map { ($0.pid, $0) }, uniquingKeysWith: { a, _ in a })
                let t = Int(Date().timeIntervalSince1970 * 1000)
                for (pid, p) in now where last[pid] != p {
                    printJSON(p.json.merging(["event": last[pid] == nil ? "appear" : "change", "t_ms": t]) { a, _ in a })
                }
                for (pid, p) in last where now[pid] == nil {
                    printJSON(p.json.merging(["event": "gone", "t_ms": t]) { a, _ in a })
                }
                last = now
                Thread.sleep(forTimeInterval: interval)
            }
        }
        let procs = snapshot()
        if args.contains("--json") {
            procs.forEach { printJSON($0.json) }
        } else {
            printTable(procs)
        }
    }
}
