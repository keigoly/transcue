"""bench/run_bench.py — v2 A1: 再現会議ベンチ(同じ音声を送受信に流して重さを測る)。

src/bench/audio/{ja,en}.m4a(make_audio.py で作る)を、送信(pipeline.py --feed)と受信(receive.py --feed)へ
同時に流し、resmon でメモリ・CPU・GPU を記録する。実会議と同じく 2 つのプロセスが同時にモデルを読み込む。
終わったら report.py で内訳表を出す(logs/bench/<id>.md)。

流し始めは送受信で揃える(v2 A2-0): 両方の stt-helper が準備を終えて待機に入ったら(`<id>_go.<locale>.waiting`)、
`<id>_go` を作って同時に流し始めさせる(`ZRT_FEED_GATE`)。音声は日英が交互に話す会話なので、ずれると本来は
重ならない翻訳が重なる(A1 の 1 本目は 38 s ずれた)。開始時の環境(ディスク・スワップ・逼迫・ユーザーごとの RSS)も
記録する(A/B の比較で環境差を見分けるため)。

--shared(v2 A2-1): 翻訳サーバ(translate_server.py)を先に起動し、送受信はそれを共有する(PLaMo は 1 つ)。
付けないときは送受信がそれぞれモデルを持つ(従来)。どちらも ZRT_TRANSLATE_MODE で明示するので、
config.toml の [translate].mode の既定が変わっても比較の条件は変わらない。

--wired(v2 A2-2): 翻訳モデルの重みを常時固定し(ZRT_WIRE_WEIGHTS=1)、keep-warm を止める(ZRT_KEEPWARM_IDLE_S=0)。
付けないときは ZRT_WIRE_WEIGHTS=0 で固定しない(keep-warm は A1 の基準と同じ 12 s。config には任せない)。
--wake(v2 A2-2): 話し始めで GPU を起こし(ZRT_WAKE_ON_SPEECH=1)、keep-warm を止める。付けないときは起こさない。
--carry-wait-ms N(2026-10-25): セグメンターの繰り越しの待ちの上限(ZRT_CARRY_MAX_WAIT_MS)。既定 0 = 無効も
明示して渡す(config の既定が変わっても比較の条件は変わらない)。
--recv-engine / --send-engine(2026-10-25・v2 §7b): 訳を頼む先(plamo | claude。ZRT_RECV_ENGINE / ZRT_SEND_ENGINE)。
既定 plamo も明示して渡す。claude でも PLaMo は控えとして読み込む(翻訳サーバの条件は変わらない)。

安全のため:
- 翻訳スタック(pipeline / receive / translate_server)が動いていたら始めない(会議中の可能性)。
- Zoom が動いていたら始めない。途中で Zoom が起動したら止める(--allow-zoom で許可。一人会議で測るとき)。
  送信の英語 TTS は BlackHole(= Zoom のマイク)へ出るので、会議中に流すと相手に聞こえるため。
- 議事録(logs/transcript_*.jsonl)には書かない(ZRT_NO_TRANSCRIPT=1)。

実行(所要 ~13 分。モデル 2 つで 10 GB 超を使う):
    spikes/.venv-ml/bin/python src/bench/run_bench.py [--label nozoom] [--allow-zoom] [--interval 2]
                                                      [--with-ui] [--no-imports] [--shared] [--wired] [--wake]
                                                      [--carry-wait-ms N]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
AUDIO = Path(__file__).resolve().parent / "audio"
OUT = ROOT / "logs" / "bench"
PY = sys.executable


PS_TIMEOUTS = 0


def _ps(fmt: str) -> list[str] | None:
    """ps の各行。メモリが逼迫すると ps 自体が 5 秒以上止まる(2026-10-24 の 1 回目はこの時間切れで落ちた)ので、
    時間切れは None(不明)で返す。"""
    global PS_TIMEOUTS
    try:
        return subprocess.run(["ps", "-axww", "-o", fmt], capture_output=True, text=True,
                              timeout=15).stdout.splitlines()
    except subprocess.TimeoutExpired:
        PS_TIMEOUTS += 1
        print("[bench] ps が 15 s 応答しません(メモリ逼迫)。この回の確認は見送ります", file=sys.stderr, flush=True)
        return None


def stack_running() -> bool | None:
    lines = _ps("args=")
    return None if lines is None else any(re.search(r"src/(pipeline|receive|translate_server)\.py", a)
                                          for a in lines)


def zoom_running() -> bool | None:
    """Zoom 本体(zoom.us)か会議プロセス(CptHost)が動いているか。自動更新(ZoomUpdater)は数えない。
    ps が時間切れなら None(不明)。"""
    lines = _ps("comm=")
    if lines is None:
        return None
    for c in (c.strip() for c in lines):
        if c.startswith("/Applications/zoom.us.app/") and c.rsplit("/", 1)[-1] in ("zoom.us", "CptHost"):
            return True
    return False


def measure_imports() -> list[dict]:
    """H4: import だけの時間とメモリ(新しいプロセスで 1 回ずつ。/usr/bin/time -l の peak memory footprint)。"""
    rows = []
    for name, code in (("python のみ", "pass"), ("mlx.core", "import mlx.core"),
                       ("mlx_lm(翻訳)", "from mlx_lm import load"),
                       ("mlx_audio(TTS)", "from mlx_audio.tts.utils import load_model")):
        t = time.perf_counter()
        r = subprocess.run(["/usr/bin/time", "-l", PY, "-c", code], capture_output=True, text=True, cwd=ROOT)
        wall = time.perf_counter() - t
        fp = re.search(r"(\d+)\s+peak memory footprint", r.stderr)
        rss = re.search(r"(\d+)\s+maximum resident set size", r.stderr)
        mods = subprocess.run([PY, "-c", f"{code}\nimport sys\nprint(len(sys.modules), 'torch' in sys.modules)"],
                              capture_output=True, text=True, cwd=ROOT).stdout.split()
        rows.append({"what": name, "wall_s": round(wall, 2),
                     "peak_fp_mb": round(int(fp.group(1)) / 2**20) if fp else None,
                     "max_rss_mb": round(int(rss.group(1)) / 2**20) if rss else None,
                     "modules": int(mods[0]) if mods else None, "torch": mods[1] == "True" if len(mods) > 1 else None,
                     "ok": r.returncode == 0})
    return rows


def env_snapshot() -> dict:
    """開始時の環境。ディスクの空き・スワップ・逼迫レベル・ユーザーごとの RSS(0.5 GB 以上)。"""
    snap: dict = {"disk_free_gb": round(shutil.disk_usage("/System/Volumes/Data").free / 2**30, 1)}
    try:
        sw = subprocess.run(["sysctl", "-n", "vm.swapusage", "kern.memorystatus_vm_pressure_level"],
                            capture_output=True, text=True, timeout=15).stdout.splitlines()
        used = re.search(r"used = ([\d.]+)M", sw[0]) if sw else None
        snap["swap_used_mb"] = round(float(used.group(1))) if used else None
        snap["pressure"] = int(sw[1]) if len(sw) > 1 and sw[1].strip().isdigit() else None
        by_user: dict[str, float] = defaultdict(float)
        for line in subprocess.run(["ps", "-axo", "rss=,user="], capture_output=True, text=True,
                                   timeout=15).stdout.splitlines():
            kb, _, user = line.strip().partition(" ")
            if kb.isdigit():
                by_user[user.strip()] += int(kb) / 2**20
        snap["rss_gb_by_user"] = {u: round(g, 1) for u, g in sorted(by_user.items(), key=lambda x: -x[1])
                                  if g >= 0.5}
    except (subprocess.TimeoutExpired, OSError, ValueError):
        pass
    return snap


def stop(p: subprocess.Popen | None, timeout: float = 20.0) -> None:
    if p is None or p.poll() is not None:
        return
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait(timeout=5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="nozoom", help="結果に付ける名前(例: nozoom / zoom)")
    ap.add_argument("--allow-zoom", action="store_true", help="Zoom が動いていても測る(一人会議での計測用)")
    ap.add_argument("--interval", type=float, default=2.0, help="resmon の採取間隔(秒)")
    ap.add_argument("--with-ui", action="store_true", help="字幕ウィンドウも出す(実会議に近づける)")
    ap.add_argument("--no-imports", action="store_true", help="import だけの計測(H4)を省く")
    ap.add_argument("--shared", action="store_true", help="翻訳サーバ 1 つを送受信で共有する(v2 A2-1)")
    ap.add_argument("--wired", action="store_true", help="重みを常時固定し、keep-warm を止める(v2 A2-2)")
    ap.add_argument("--wake", action="store_true", help="話し始めで GPU を起こし、keep-warm を止める(v2 A2-2)")
    ap.add_argument("--carry-wait-ms", type=int, default=0,
                    help="繰り越しの待ちの上限 ms(0 = 無効。セグメンター 2026-10-25 Step 2)")
    ap.add_argument("--recv-engine", choices=("plamo", "claude"), default="plamo",
                    help="字幕(英→日)の訳を頼む先(v2 §7b。claude でも PLaMo は控えとして読み込む)")
    ap.add_argument("--send-engine", choices=("plamo", "claude"), default="plamo",
                    help="英語(日→英)の訳を頼む先(同上)")
    args = ap.parse_args()

    ja, en, tl_path = AUDIO / "ja.m4a", AUDIO / "en.m4a", AUDIO / "timeline.json"
    if not (ja.exists() and en.exists() and tl_path.exists()):
        print("音声がありません。先に: spikes/.venv-ml/bin/python src/bench/make_audio.py", file=sys.stderr)
        return 2
    running, zoom = stack_running(), zoom_running()
    if running is None or zoom is None:
        print("ps が応答しないため、翻訳スタックと Zoom が止まっていることを確かめられません。始めません。",
              file=sys.stderr)
        return 3
    if running:
        print("翻訳スタック(pipeline / receive / translate_server)が動いています。会議中かもしれないので始めません。",
              file=sys.stderr)
        return 3
    if zoom and not args.allow_zoom:
        print("Zoom が動いています。送信の英語 TTS が Zoom に流れるため始めません(一人会議なら --allow-zoom)。",
              file=sys.stderr)
        return 3
    timeline = json.loads(tl_path.read_text(encoding="utf-8"))
    duration = float(timeline["duration_s"])
    bench_id = time.strftime("%Y%m%d_%H%M%S")
    OUT.mkdir(parents=True, exist_ok=True)
    gate = OUT / f"{bench_id}_go"                # 流し始めの合図(両方の helper が待機に入ったら作る)
    env = {**os.environ, "ZRT_BENCH_ID": bench_id, "ZRT_NO_TRANSCRIPT": "1", "ZRT_FEED_GATE": str(gate),
           "ZRT_TRANSLATE_MODE": "server" if args.shared else "local",
           # 本番(会議中に watcher が起動する)の受け口とぶつからないよう、ベンチ専用の受け口にする
           "ZRT_TRANSLATOR_SOCK": str(Path.home() / ".transcue" / "translator-bench.sock"),
           "ZRT_WIRE_WEIGHTS": "1" if args.wired else "0",
           "ZRT_WAKE_ON_SPEECH": "1" if args.wake else "0",
           # keep-warm も明示する: --wired / --wake なら止める。付けなければ A1 の基準と同じ 12 s(旧既定。
           # 2026-10-25 に本番の config は 0 になったので、config に任せると基準の条件が変わってしまう)
           "ZRT_KEEPWARM_IDLE_S": "0" if args.wired or args.wake else "12",
           "ZRT_CARRY_MAX_WAIT_MS": str(args.carry_wait_ms),
           "ZRT_RECV_ENGINE": args.recv_engine, "ZRT_SEND_ENGINE": args.send_engine,
           # 操作パネルの「クラウド翻訳」の切り替えは見ない(画面の状態で比較の条件が変わらないように)
           "ZRT_CLOUD_SWITCH_FILE": ""}
    manifest: dict = {"bench_id": bench_id, "label": args.label, "allow_zoom": args.allow_zoom,
                      "zoom_at_start": zoom, "with_ui": args.with_ui, "interval_s": args.interval,
                      "shared": args.shared, "wired": args.wired, "wake": args.wake,
                      "carry_wait_ms": args.carry_wait_ms,
                      "recv_engine": args.recv_engine, "send_engine": args.send_engine,
                      "audio": {"ja": str(ja), "en": str(en), "sha256_16": timeline.get("sha256_16")},
                      "idle_audio_s": timeline.get("idle"), "env_start": env_snapshot()}

    if not args.no_imports:
        print("[bench] import だけの時間とメモリを測ります(H4)…", file=sys.stderr, flush=True)
        manifest["imports"] = measure_imports()

    max_s = str(int(duration + 240))          # モデル読み込みぶんの余裕。helper が bye を出せば先に終わる
    logs = {k: (OUT / f"{bench_id}_{k}.log").open("w")
            for k in ("send", "recv", "resmon") + (("translator",) if args.shared else ())}
    procs: dict[str, subprocess.Popen] = {}
    print(f"[bench] {bench_id} 開始(label={args.label}・音声 {duration:.0f} s + 読み込み)…", file=sys.stderr, flush=True)
    manifest["t_start_ms"] = int(time.time() * 1000)
    procs["resmon"] = subprocess.Popen([PY, str(SRC / "resmon.py"), str(int(duration + 600)), "--interval",
                                        str(args.interval)], cwd=ROOT, env=env, stdout=logs["resmon"],
                                       stderr=subprocess.STDOUT)
    time.sleep(args.interval + 0.5)           # 読み込み前の状態を 1 回は採る
    manifest["t_launch_ms"] = int(time.time() * 1000)
    if args.shared:                           # 送受信は受け口が開く(読み込みが終わる)までつながるのを待つ
        procs["translator"] = subprocess.Popen([PY, str(SRC / "translate_server.py"), str(int(duration + 600))],
                                               cwd=ROOT, env=env, stdout=logs["translator"],
                                               stderr=subprocess.STDOUT)
    procs["send"] = subprocess.Popen([PY, str(SRC / "pipeline.py"), max_s, "--to-zoom", "--feed", str(ja)],
                                     cwd=ROOT, env=env, stdout=logs["send"], stderr=subprocess.STDOUT)
    procs["recv"] = subprocess.Popen([PY, str(SRC / "receive.py"), max_s, "--feed", str(en)]
                                     + (["--window"] if args.with_ui else []),
                                     cwd=ROOT, env=env, stdout=logs["recv"], stderr=subprocess.STDOUT)
    aborted, tick = None, 0
    try:
        while any(procs[k].poll() is None for k in ("send", "recv")):
            # ps は逼迫時に重いので Zoom の確認は 5 秒ごと。時間切れ(None)はその回を見送る
            if not args.allow_zoom and tick % 5 == 0 and zoom_running():
                aborted = "Zoom が起動したため中止"
                break
            if time.time() * 1000 - manifest["t_launch_ms"] > (duration + 900) * 1000:
                aborted = "時間切れ"
                break
            if "translator" in procs and procs["translator"].poll() is not None:
                aborted = "翻訳サーバが止まった"
                break
            if not gate.exists():
                waiting = sorted(OUT.glob(f"{gate.name}.*.waiting"))
                if len(waiting) >= 2:                  # 送受信とも待機中 → 同時に流し始めさせる
                    gate.touch()
                    go = int(time.time() * 1000)
                    manifest["feed_go_ms"] = go
                    manifest["feed_wait_ms"] = {w.name.split(".")[-2]: go - int(w.stat().st_mtime * 1000)
                                                for w in waiting}
                    print("[bench] 送受信とも準備完了。同時に流し始めます", file=sys.stderr, flush=True)
                elif any(procs[k].poll() is not None for k in ("send", "recv")):
                    aborted = "送信か受信が、流し始める前に終了"
                    break
            tick += 1
            time.sleep(1.0)
    except KeyboardInterrupt:
        aborted = "Ctrl+C"
    finally:
        if aborted:
            print(f"[bench] {aborted}。送受信を止めます。", file=sys.stderr, flush=True)
        for k in ("send", "recv", "translator"):
            stop(procs.get(k))
        manifest["t_end_ms"] = int(time.time() * 1000)
        stop(procs.get("resmon"))
        for f in logs.values():
            f.close()
        for f in (gate, *OUT.glob(f"{gate.name}.*.waiting")):
            f.unlink(missing_ok=True)
    manifest["aborted"] = aborted
    manifest["ps_timeouts"] = PS_TIMEOUTS
    manifest["rc"] = {k: p.returncode for k, p in procs.items()}
    (OUT / f"{bench_id}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[bench] 終了 rc={manifest['rc']} → 集計します", file=sys.stderr, flush=True)
    r = subprocess.run([PY, str(Path(__file__).resolve().parent / "report.py"), bench_id], cwd=ROOT)
    return 1 if aborted else r.returncode


if __name__ == "__main__":
    sys.exit(main())
