"""Phase 1-F: 統合パイプライン(実段結線)。

マイク → 日本語STT(Swift helper)→ セグメンター → PLaMo翻訳 → Kokoro TTS → スピーカー
を一本に結線し、節ごとに requestId 付き JSONL で各段 ms を計測、E2E(節確定→英語音声ready)
の分布(p50/p90)を出す。1-A のスタブ結線を実段に差し替えたもの。

⚠️ ヘッドホン必須: TTS 出力がマイクに回り込むとエコーループになる(ARCHITECTURE.md §2)。

実行(短時間ライブ E2E。既定 25 秒で自動終了・マイク解放):
    source spikes/.venv-ml/bin/activate
    python src/pipeline.py 25
"""
from __future__ import annotations

import asyncio
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
from translate import make_translator, wake_on_speech  # noqa: E402
from tts import TTS  # noqa: E402
from transcript import Transcript  # noqa: E402
from hotkey import HotkeyListener  # noqa: E402
from passthrough import Passthrough  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    with (Path(__file__).resolve().parent / "config.toml").open("rb") as f:
        cfg = tomllib.load(f)
    cfg["_root"] = str(ROOT)
    return cfg


def summarize(vals: list[float], m: Metrics, note: str) -> None:
    if not vals:
        m.emit("SUMMARY", "e2e_summary", n=0, note=note)
        return
    o = sorted(vals)
    p90 = o[min(len(o) - 1, int(round(0.9 * (len(o) - 1))))]
    m.emit("SUMMARY", "e2e_summary", n=len(o), p50_ms=round(statistics.median(o), 1),
           p90_ms=round(p90, 1), max_ms=round(o[-1], 1), budget_ms="800-1800", note=note)


async def main() -> None:
    argv = sys.argv[1:]
    to_zoom = "--to-zoom" in argv                       # Phase 2 送信: TTS を BlackHole(Zoom マイク)へ
    start_off = "--start-off" in argv                   # Phase 3-I: 英語OFF(赤・地声)状態で開始
    nums = [a for a in argv if a.replace(".", "", 1).isdigit()]
    max_s = float(nums[0]) if nums else 25.0
    # --feed <音声ファイル>: マイクの代わりにファイルを実時間で STT へ流す(v2 A1 の再現会議ベンチ)。
    feed = str(Path(argv[argv.index("--feed") + 1]).resolve()) \
        if "--feed" in argv and argv.index("--feed") + 1 < len(argv) else None
    cfg = load_config()
    m = Metrics.from_config(cfg, run_tag="pipeline1f")
    transcript = Transcript.from_config(cfg)

    # --to-zoom で TTS 出力先を virtual_mic(BlackHole)へ。Zoom のマイク入力を BlackHole にすると相手へ英語 TTS。
    # フラグなし(既定)はシステム既定出力=自己モニタ(Phase 1 挙動、無変更)。
    tts_out = cfg["audio"].get("virtual_mic") if to_zoom else None

    # --- パネル/ホットキー + 地声パススルーを「モデルロードより先に」開始(Phase 3-I) ---
    # 会議参加→自動起動フローで、ロード(~20-30s)の間も ①ボタンが即表示され ②英語OFF(地声)が
    # 相手に届く状態を成立させる(旧順序ではロード完了まで相手が無音だった。spike7 の知見)。
    hotkey: HotkeyListener | None = None
    passthrough: Passthrough | None = None
    if "--hotkey" in sys.argv or "--panel" in sys.argv:
        def _on_pause(paused: bool) -> None:
            # 英語OFF(paused)= 地声をパススルー / 英語ON = パススルーをミュート(TTSのみ相手へ)。
            if passthrough:
                passthrough.set_muted(not paused)
            print(f"[pipeline] {'⏸ 英語OFF(地声を相手へ)' if paused else '▶ 英語ON(英語TTSを相手へ)'}", file=sys.stderr, flush=True)
        hk = cfg.get("hotkey", {})
        use_panel = "--panel" in sys.argv                        # GUI ボタン版 or ホットキーのみ
        bin_path = hk.get("panel_bin", "src/control_panel/control-panel") if use_panel \
            else hk.get("helper_bin", "src/hotkey_helper/hotkey-helper")
        # --start-off: 英語OFF(paused=True)で開始。パネルにも --start-off を渡して赤で起動させる
        # (パネルの ready {"on":...} でも同期されるので、フラグと表示が食い違わない)。
        # --only-app: config [hotkey].panel_only_app が非空なら、その bundle(カンマ区切りで複数)が最前面のときだけ
        # ボタンを表示(Zoom の会議画面と Meet のタブ)。ホットキー(非パネル)には渡さない。
        panel_args: list[str] = []
        if use_panel:
            if start_off:
                panel_args.append("--start-off")
            only_app = hk.get("panel_only_app", "")
            if only_app:
                panel_args += ["--only-app", only_app]
                titles = hk.get("panel_meeting_titles", "")
                if titles:  # 最前面ウィンドウのタイトルで会議窓のみに絞る(Workplace ホーム除外)
                    panel_args += ["--meeting-title", titles]
        hotkey = HotkeyListener(str(ROOT / bin_path), args=panel_args or None, on_change=_on_pause,
                                paused=start_off)
        await hotkey.start()
        state0 = "英語OFF(地声)で開始" if start_off else "英語ONで開始"
        print(f"[pipeline] {'コントロールパネル(GUI: クリック/⌃⌥Pで英語ON/OFF)' if use_panel else 'ホットキー(⌃⌥P)'} ON"
              f" — {state0}", file=sys.stderr)
        if to_zoom:
            # 地声パススルー(Phase 3-I): 英語OFF中はマイク生声を BlackHole へ流し、
            # ボタンを「地声 ⇄ 英語翻訳」の完全切替にする(Zoom マイクは BlackHole のまま)。
            passthrough = Passthrough(cfg, m)
            passthrough.muted = not hotkey.paused
            passthrough.start()

    print("[pipeline] モデルロード中(PLaMo + Kokoro, ~20-30s。この間もボタン/地声は有効)…", file=sys.stderr)
    translator = make_translator(cfg, m, "send")   # mode=server なら翻訳サーバを共有(v2 A2-1)
    await translator.load()          # 実発話長 warmup ×2 込み(初回実翻訳の ~30s を防ぐ。spike7)
    # keep-warm: 無音中もモデルを温存し、ON にして久々に話す1発目も高速に保つ(実測 8.9s→~1s)。
    tcfg = cfg["translate"]
    translator.start_keepwarm(float(tcfg.get("keepwarm_check_s", 4)),
                              float(tcfg.get("keepwarm_idle_s", 12)), "Japanese", "English")
    tts = TTS(cfg, m, output_device=tts_out)
    await tts.load()
    tts.start_player()

    # ZRT_FEED_GATE(ベンチだけが設定): helper は流し始める前に go を待つ(送受信の流し始めを揃える。v2 A2-0)
    gate = os.environ.get("ZRT_FEED_GATE")
    helper = STTHelper(str(ROOT / cfg["stt"]["helper_bin"]), cfg["stt"]["lang"],
                       on_diag=lambda d: m.emit("STT", "stt_diag", **d),
                       extra_args=(["--feed", feed] + (["--feed-gate", gate] if gate else [])) if feed else None)
    await helper.start()
    seg = Segmenter(helper, cfg, m)
    if wake_on_speech(cfg):   # 話し始めで翻訳と TTS の GPU を起こす(keep-warm の代わり。英語 OFF の間は起こさない。v2 A2-2)
        seg.on_speech = lambda: None if (hotkey and hotkey.paused) else (translator.wake(), tts.wake())
    e2e: list[float] = []

    route = f"→Zoom(BlackHole,out={tts.out_dev})" if to_zoom else "→自己モニタ(既定出力)"
    m.emit("BOOT", "boot", note="live E2E", max_s=max_s, to_zoom=to_zoom, tts_out_dev=tts.out_dev,
           stages="mic|stt|segment|plamo|kokoro|speaker", boot_ms=round((time.time() - _T_START) * 1000),
           feed=feed, bench=os.environ.get("ZRT_BENCH_ID"))
    print(f"[pipeline] 準備完了。{max_s:.0f}秒 日本語で話してください(⚠️ヘッドホン必須){route}…", file=sys.stderr)

    async def run() -> None:
        async for rid, clause, t_clause in seg.clauses():
            if hotkey and hotkey.paused:
                continue                                     # 一時停止中は翻訳/TTS送出をスキップ(相手に流さない)
            en = await translator.translate(rid, clause)     # metrics: translate ms/first_token
            if not en:
                continue
            transcript.add("送信(日→英)", clause, en, "Japanese", "English")
            audio, sr = await tts.synth(rid, en)             # metrics: tts ms/first_audio
            tts.play_audio(rid, audio, sr)                   # 背景で順次再生
            dt = (time.perf_counter() - t_clause) * 1000
            m.emit(rid, "e2e", ms=round(dt, 1), src=clause, en=en, note="clause_final→synth_done")
            e2e.append(dt)
            mon = "🔊 " if tts._monitor_active() else ""   # 自己モニタ(送出中)インジケータ
            print(f"  {mon}[{clause}] → {en}", file=sys.stderr)

    # SIGTERM/SIGINT で run を cancel → finally で確実にマイク/TTS/タップを解放する。
    # run_meeting.command は pipeline を & (バックグラウンド)起動するため、非対話シェルでは
    # 子プロセスの SIGINT が SIG_IGN にされる。add_signal_handler は継承した SIG_IGN も上書きし、
    # かつ SIGINT を無視される SIGTERM でも確実に止められるよう両方を捕まえる(自動停止の要)。
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
        await helper.stop()          # マイクを確実に解放
        await tts.stop_player()
        if passthrough:
            passthrough.stop()       # 地声パススルーを止める(BlackHole解放)
        if hotkey:
            await hotkey.stop()      # ホットキー helper を止める
    summarize(e2e, m, "clause_final→synth_done")
    transcript.close()
    print(f"[pipeline] 停止(マイクOFF)。節数={len(e2e)}  log={m.path}", file=sys.stderr)
    m.close()


if __name__ == "__main__":
    asyncio.run(main())
