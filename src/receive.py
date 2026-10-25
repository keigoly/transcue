"""Phase 2-C: 受信方向オーケストレータ(相手の英語 → 日本語字幕)。

Zoom出力(process tap)→ en-US STT(tap-helper)→ セグメンター(再利用)→ PLaMo en→ja翻訳
→ ターミナル日本語字幕。送信方向 pipeline.py の「鏡」。UI は Phase 3、まずターミナル出力。

tap-helper は stt_helper とプロトコル互換なので stt_reader / segmenter は無改修で再利用する。
翻訳は translate.py の Translator を src="English" / dst="Japanese" で使う(引数対応済)。
requestId 付き JSONL で各段 ms を計測、E2E(節確定→日本語字幕)の分布(p50/p90)を出す。

字幕は stdout(クリーンな字幕ストリーム)、計測ログは stderr(metrics echo)に分離する。

実行(テスト: 英語音声ファイルをタップ。マイク/Zoom不要):
    source spikes/.venv-ml/bin/activate
    python src/receive.py 20 --file spikes/spike1_speechanalyzer/test_en.aiff

実行(実運用: Zoom 会議の相手音声をタップ):
    python src/receive.py 60            # 既定 target=[--name zoom]
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import statistics
import sys
import time
import tomllib
from pathlib import Path

_T_START = time.time()   # プロセス起動 ≒ ここ(boot の boot_ms = 起動から準備完了まで。v2 A1)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import Metrics  # noqa: E402
from stt_reader import STTHelper  # noqa: E402
from segmenter import Segmenter  # noqa: E402
from translate import Translator, make_translator, wake_on_speech  # noqa: E402
from transcript import Transcript  # noqa: E402
from hotkey import HotkeyListener  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class SubtitleSink:
    """Phase 3-C フローティング字幕ウィンドウ(Swift NSPanel)への供給。JSON 行を stdin へ書く。"""

    def __init__(self, bin_path: str):
        self.bin_path = bin_path
        self.proc: asyncio.subprocess.Process | None = None
        self._drain: asyncio.Task | None = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            self.bin_path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._drain = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for _ in self.proc.stderr:
            pass  # ウィンドウ診断は捨てる(バッファ詰まり防止)

    async def send(self, en: str, ja: str) -> None:
        if self.proc and self.proc.stdin and not self.proc.stdin.is_closing():
            line = json.dumps({"type": "subtitle", "en": en, "ja": ja}, ensure_ascii=False) + "\n"
            try:
                self.proc.stdin.write(line.encode())
                await self.proc.stdin.drain()
            except (ConnectionError, BrokenPipeError):
                pass

    async def stop(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.is_closing():
                self.proc.stdin.write(b'{"type":"quit"}\n')
                await self.proc.stdin.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            self.proc.terminate()
        if self._drain:
            self._drain.cancel()


def load_config() -> dict:
    with (Path(__file__).resolve().parent / "config.toml").open("rb") as f:
        cfg = tomllib.load(f)
    cfg["_root"] = str(ROOT)
    return cfg


def parse_args(cfg: dict) -> tuple[float, list[str]]:
    """CLI: [seconds] [--file <path> | --pid <n> | --name <substr> | --exclude-pid <n>]。

    --exclude-pid: システム全体をタップし、その PID(自分の TTS 再生プロセス)だけ除外する。
    Zoom は版によって会議音声の描画プロセスが変わるため、全体タップの方が確実に拾える。

    音源指定が無ければ config [receive].target を使う。--file はパスを絶対化する
    (helper は別 CWD で afplay を起動しうるため)。
    """
    argv = sys.argv[1:]
    max_s = 25.0
    override: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--file", "--pid", "--name", "--exclude-pid") and i + 1 < len(argv):
            val = argv[i + 1]
            if a == "--file":
                val = str(Path(val).resolve())
            override += [a, val]
            i += 2
            continue
        try:
            max_s = float(a)
        except ValueError:
            pass
        i += 1
    if override:
        return max_s, override
    rc = cfg["receive"]
    if rc.get("tap_mode", "process") == "global_exclude_sender":
        # システム全体タップ。除外するのは送信側(TTSを再生するプロセス)= 自分の英語音声の再認識を防ぐ。
        # 送信が居なければ自分自身を除外(受信は音を鳴らさないので実質フル・グローバルタップ)。
        import os, subprocess
        pid = os.getpid()
        try:
            r = subprocess.run(["pgrep", "-f", "src/pipeline.py"], capture_output=True, text=True, timeout=5)
            pids = [int(x) for x in r.stdout.split() if x.isdigit()]
            if pids:
                pid = pids[0]
        except Exception:
            pass
        return max_s, ["--exclude-pid", str(pid)]
    return max_s, list(rc.get("target", ["--name", "zoom"]))


def _pctl(vals: list[float], q: float) -> float:
    o = sorted(vals)
    return o[min(len(o) - 1, int(round(q * (len(o) - 1))))]


def summarize(first_lat: list[float], full_lat: list[float], m: Metrics) -> None:
    """ストリーム字幕の2遅延を集計: 先行表示(節確定→最初の日本語トークン)と確定(→確定訳完了)。"""
    if not full_lat:
        m.emit("SUMMARY", "e2e_recv_summary", n=0, note="stream")
        return
    m.emit("SUMMARY", "e2e_recv_summary", n=len(full_lat),
           first_shown_p50_ms=round(statistics.median(first_lat), 1),
           first_shown_p90_ms=round(_pctl(first_lat, 0.9), 1),
           full_p50_ms=round(statistics.median(full_lat), 1),
           full_p90_ms=round(_pctl(full_lat, 0.9), 1),
           budget_ms="先行~650 / 確定~2300", note="stream: 先行表示 vs 確定")


async def main() -> None:
    cfg = load_config()
    max_s, target = parse_args(cfg)
    rc = cfg["receive"]
    src_lang = rc.get("src_lang", "English")
    dst_lang = rc.get("dst_lang", "Japanese")
    m = Metrics.from_config(cfg, run_tag="receive2c")
    transcript = Transcript.from_config(cfg)

    print(f"[receive] PLaMo ロード中(初回 ~30s)…  source={target}", file=sys.stderr)
    translator = make_translator(cfg, m, "recv")   # mode=server なら翻訳サーバを共有(v2 A2-1)
    # en→ja 方向を実発話長 ×2 でウォームアップ(初節が warmup コストを被らないように。spike7)。
    await translator.load(warmup_texts=Translator.WARMUP_EN_JA, src=src_lang, dst=dst_lang)
    # keep-warm: 相手が長く黙った後の1発目もコールドにしない(en→ja 方向で温存)。
    tcfg = cfg["translate"]
    translator.start_keepwarm(float(tcfg.get("keepwarm_check_s", 4)),
                              float(tcfg.get("keepwarm_idle_s", 12)), src_lang, dst_lang)

    # --feed <音声ファイル>: タップの代わりにファイルを実時間で英語 STT へ流す(v2 A1 の再現会議ベンチ)。
    # 受け手は stt-helper(送信と同じ helper。--feed は tap-helper には無い)。
    feed = str(Path(sys.argv[sys.argv.index("--feed") + 1]).resolve()) \
        if "--feed" in sys.argv and sys.argv.index("--feed") + 1 < len(sys.argv) else None
    if feed:
        # ZRT_FEED_GATE(ベンチだけが設定): helper は流し始める前に go を待つ(送受信の流し始めを揃える。v2 A2-0)
        gate = os.environ.get("ZRT_FEED_GATE")
        target = ["--feed", feed] + (["--feed-gate", gate] if gate else [])
    helper = STTHelper(str(ROOT / (cfg["stt"]["helper_bin"] if feed else rc["helper_bin"])), rc["lang"],
                       on_diag=lambda d: m.emit("RECV_STT", "stt_diag", **d),
                       extra_args=target)
    await helper.start()
    # 受信方向のセグメンター上書き([receive] の同名キーを [segmenter] にマージ)。
    # English は auto-final 主体。ポーズ強制閾値を partial 間隔より長くして文中誤切りを防ぐ。
    # ⚠️ ここに列挙したキーだけが [receive] から上書きされる。新しいしきい値を [segmenter] に
    #    足したら、受信側の値も必ずここに足すこと(足し忘れると受信が送信用の値を拾う)。
    seg_over = {k: rc[k] for k in ("pause_finalize_ms", "pause_finalize_sentence_ms",
                                   "pause_finalize_incomplete_ms",
                                   "finalize_poll_ms", "carry_incomplete_tail", "carry_max_wait_ms",
                                   "max_clause_chars", "backchannel") if k in rc}
    cfg_seg = {**cfg, "segmenter": {**cfg["segmenter"], **seg_over}}
    seg = Segmenter(helper, cfg_seg, m)
    if wake_on_speech(cfg):   # 話し始めで翻訳の GPU を起こす(keep-warm の代わり。v2 A2-2)
        seg.on_speech = translator.wake
    first_lat: list[float] = []   # 節確定 → 最初の日本語トークン表示(先行表示遅延)
    full_lat: list[float] = []    # 節確定 → 確定訳完了

    sink: SubtitleSink | None = None
    if "--window" in sys.argv:
        sink = SubtitleSink(str(ROOT / rc.get("window_bin", "src/subtitle_window/subtitle-window")))
        await sink.start()
        print("[receive] フローティング字幕ウィンドウ ON", file=sys.stderr)

    hotkey: HotkeyListener | None = None
    if "--hotkey" in sys.argv:
        async def _on_pause(paused: bool) -> None:
            print(f"[receive] {'⏸ 一時停止(字幕停止)' if paused else '▶ 再開'}", file=sys.stderr, flush=True)
            if sink:
                await sink.send("", "⏸ 一時停止中" if paused else "")
        hk = cfg.get("hotkey", {})
        hk_args: list[str] = []
        if "--emit-toggles" in sys.argv:  # テスト用: 実キーなしで toggle を自動発火
            i = sys.argv.index("--emit-toggles")
            if i + 1 < len(sys.argv):
                hk_args = ["--emit-toggles", sys.argv[i + 1]]
        hotkey = HotkeyListener(str(ROOT / hk.get("helper_bin", "src/hotkey_helper/hotkey-helper")),
                                args=hk_args, on_change=_on_pause)
        await hotkey.start()
        print(f"[receive] ホットキー ON({hk.get('combo', '⌃⌥P')} で一時停止/再開)", file=sys.stderr)

    m.emit("BOOT", "boot", note="receive (en→ja streaming subtitle)", max_s=max_s,
           source=" ".join(target), stages="tap|stt_en|segment|plamo_en_ja|stream_subtitle",
           translate_engine=getattr(translator, "engine", "plamo"),       # v2 §7b: Claude なら モデル名
           boot_ms=round((time.time() - _T_START) * 1000), bench=os.environ.get("ZRT_BENCH_ID"))
    print(f"[receive] 準備完了。{max_s:.0f}秒 受信字幕(英語→日本語, ストリーム表示)…", file=sys.stderr)

    async def run() -> None:
        async for rid, clause, t_clause in seg.clauses():
            if hotkey and hotkey.paused:
                continue                                       # 一時停止中は翻訳/字幕をスキップ
            print(f"🇬🇧 {clause}", flush=True)
            ja = ""
            first_ms: float | None = None
            updates = 0
            # PLaMo のトークン確定ごとに部分訳を受け取り、同じ行を \r で更新(ストリーム表示)。
            async for partial in translator.translate_stream(rid, clause, src=src_lang, dst=dst_lang):
                ja = partial
                updates += 1
                if first_ms is None and partial.strip():
                    first_ms = (time.perf_counter() - t_clause) * 1000
                print(f"\r🇯🇵 {ja}\033[K", end="", flush=True)  # \033[K = 行末クリア
                if sink:
                    await sink.send(clause, ja)                    # フローティング字幕もストリーム更新
            print(flush=True)  # 確定で改行
            if not ja.strip():
                continue
            full_ms = (time.perf_counter() - t_clause) * 1000
            m.emit(rid, "e2e_recv_stream", first_shown_ms=round(first_ms, 1) if first_ms else None,
                   full_ms=round(full_ms, 1), updates=updates, en=clause, ja=ja.strip(),
                   note="clause_final→(first_token|full)")
            transcript.add("受信(英→日)", clause, ja.strip(), src_lang, dst_lang)
            first_lat.append(first_ms if first_ms is not None else full_ms)
            full_lat.append(full_ms)

    # SIGTERM/SIGINT で run を cancel → finally で確実に tap/字幕を解放する。
    # run_meeting.command は receive を & 起動するため子の SIGINT が SIG_IGN にされる。
    # add_signal_handler で SIG_IGN を上書きし、SIGTERM でも確実に停止(自動停止の要)。
    loop = asyncio.get_running_loop()
    run_task = asyncio.ensure_future(run())
    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(_sig, run_task.cancel)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await asyncio.wait_for(run_task, timeout=max_s)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass
    finally:
        await translator.stop_keepwarm()  # keep-warm タスクを止める
        await helper.stop()  # tap を確実に解放
        if sink:
            await sink.stop()  # 字幕ウィンドウを閉じる
        if hotkey:
            await hotkey.stop()  # ホットキー helper を止める
    summarize(first_lat, full_lat, m)
    transcript.close()
    print(f"[receive] 停止。字幕数={len(full_lat)}  会議ログ={transcript.path if transcript.enabled else 'off'}", file=sys.stderr)
    m.close()


if __name__ == "__main__":
    asyncio.run(main())
