"""TTS 段 — 1-E。Kokoro-82M(mlx-audio)で英語音声を合成し、再生キューで発話する。

- 採用は mlx-audio 版(PHASE0: first-audio 220〜290ms。ONNX/CPU版は1秒超で不採用)。
- 同期の生成を `asyncio.to_thread` でオフロード(イベントループを塞がない)。
- 初回生成は pipeline 構築 + カーネルコンパイルで数秒 → load() で warmup 済ませる。
- 再生キュー: 節を順に発話。滞留時は speed を上げて追いつく(動的catch-up は 1-F 以降)。

単体実行(マイク不要。試聴用 wav を logs/tts1e/ に書き出す):
    source spikes/.venv-ml/bin/activate
    python src/tts.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import tomllib
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import Metrics, mlx_mem, new_rid  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _gpu_tick() -> None:
    """GPU を起こすだけの小さな計算(モデルには触らない。v2 A2-2。translate.py と同じ)。"""
    import mlx.core as mx
    mx.eval(mx.ones((8,)) + 1)


def resolve_device(name: str, kind: str) -> int | None:
    """デバイス名(部分一致・大小無視)を sounddevice の index に解決する。

    kind='output': TTS 再生先(Phase 2 送信は BlackHole=仮想マイク経由で Zoom へ)。
    kind='input' : 録音元(BlackHole ループバック検証用)。
    見つからなければ None(=システム既定デバイス。Phase 1 の挙動)。
    """
    if not name:
        return None
    import sounddevice as sd
    nl = name.lower()
    ch_key = "max_output_channels" if kind == "output" else "max_input_channels"
    for i, d in enumerate(sd.query_devices()):
        if nl in d["name"].lower() and d[ch_key] > 0:
            return i
    return None


class TTS:
    def __init__(self, cfg: dict, metrics: Metrics | None = None, output_device: str | None = None):
        tc = cfg["tts"]
        self.model_id = tc.get("model_id", "prince-canuma/Kokoro-82M")
        self.voice = tc.get("voice", "af_heart")
        self.lang_code = tc.get("lang_code", "a")
        self.base_speed = float(tc.get("speed", 1.0))
        self.hf_home = str(Path(cfg["_root"]) / "models-cache" / "huggingface")
        self.m = metrics
        # 再生先: 明示指定 > config[tts].output_device > None(システム既定=Phase 1 挙動)。
        # Phase 2 送信では virtual_mic(BlackHole)を指定し、英語 TTS を Zoom マイク入力へ送る。
        self.out_dev_name = output_device if output_device is not None else tc.get("output_device", "")
        self.out_dev = resolve_device(self.out_dev_name, "output")
        # 自己モニタ: 送信(out_dev=BlackHole)時、英語 TTS を自分のヘッドホンにも小音量で流す。
        mc = cfg.get("monitor", {})
        self.monitor_enabled = bool(mc.get("enabled", True))
        self.monitor_gain = float(mc.get("gain", 0.25))
        self.monitor_dev_name = mc.get("device", "") or cfg.get("audio", {}).get("speaker_device", "")
        self.monitor_dev = resolve_device(self.monitor_dev_name, "output")
        if self.monitor_dev is None and self.monitor_enabled:
            # 指定名(既定 "AirPods")が見つからない時は、実際に繋がっている既定出力へフォールバック
            # (ヘッドホンの銘柄に依存せず自己モニタが鳴るように)。out_dev(BlackHole)と同一なら無効化される。
            try:
                import sounddevice as sd
                dflt = sd.default.device[1]
                if isinstance(dflt, int) and dflt >= 0:
                    self.monitor_dev = dflt
                    self.monitor_dev_name = f"{sd.query_devices(dflt)['name']}(既定出力)"
            except Exception:
                pass
        self._model = None
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._player: asyncio.Task | None = None

    def _load_sync(self) -> None:
        os.environ.setdefault("HF_HOME", self.hf_home)
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        t = time.perf_counter()
        from mlx_audio.tts.utils import load_model
        self._load_ms = {"import_ms": round((time.perf_counter() - t) * 1000)}   # tts_load の内訳(v2 A1)
        t = time.perf_counter()
        self._model = load_model(self.model_id)
        self._load_ms["weights_ms"] = round((time.perf_counter() - t) * 1000)

    def _synth_sync(self, text: str, speed: float) -> tuple[np.ndarray, int, float | None]:
        chunks: list[np.ndarray] = []
        sr = 24000
        first_ms: float | None = None
        t = time.perf_counter()
        for seg in self._model.generate(text=text, voice=self.voice, speed=speed, lang_code=self.lang_code):
            if first_ms is None:
                first_ms = (time.perf_counter() - t) * 1000
            chunks.append(np.asarray(seg.audio, dtype=np.float32))
            sr = int(seg.sample_rate)
        audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
        return audio, sr, first_ms

    async def load(self) -> None:
        t0 = time.perf_counter()
        await asyncio.to_thread(self._load_sync)
        await asyncio.to_thread(self._synth_sync, "Hello.", self.base_speed)  # warmup(pipeline+kernel)
        if self.m:
            self.m.emit("TTS", "tts_load", ms=round((time.perf_counter() - t0) * 1000), model=self.model_id,
                        out_dev=self.out_dev, out_dev_name=self.out_dev_name or "(default)",
                        monitor_dev=self.monitor_dev, monitor_active=self._monitor_active(),
                        **getattr(self, "_load_ms", {}), **mlx_mem())

    async def synth(self, rid: str, text: str, speed: float | None = None) -> tuple[np.ndarray, int]:
        sp = speed if speed is not None else self.base_speed
        async with self._lock:  # MLX モデルの同時アクセスを避ける
            if self.m:
                with self.m.timed(rid, "tts", text=text) as ex:
                    audio, sr, first_ms = await asyncio.to_thread(self._synth_sync, text, sp)
                    ex["first_audio_ms"] = round(first_ms, 1) if first_ms is not None else None
                    ex["audio_s"] = round(len(audio) / sr, 2) if sr else 0
                    ex["voice"] = self.voice
                return audio, sr
            audio, sr, _ = await asyncio.to_thread(self._synth_sync, text, sp)
            return audio, sr

    def wake(self) -> None:
        """話し始めで GPU を起こす(v2 A2-2。翻訳の後の合成も休止明けにならないように)。合成中は何もしない。"""
        if self._model is None or self._lock.locked():
            return
        asyncio.ensure_future(self._wake())

    async def _wake(self) -> None:
        async with self._lock:
            t = time.perf_counter()
            await asyncio.to_thread(_gpu_tick)
            ms = round((time.perf_counter() - t) * 1000, 1)
        if self.m:
            self.m.emit("TTS", "tts_wake", ms=ms)

    async def speak(self, rid: str, text: str, speed: float | None = None) -> None:
        """合成してスピーカー(AirPods)へ再生。エコー防止はヘッドホン運用前提(ARCHITECTURE.md §2)。"""
        audio, sr = await self.synth(rid, text, speed)
        import sounddevice as sd
        await asyncio.to_thread(lambda: (sd.play(audio, sr, device=self.out_dev), sd.wait()))

    # --- 再生キュー(1-F: 合成済み音声を順に再生。合成は pipeline 側で計測する) ---
    def play_audio(self, rid: str, audio: np.ndarray, sr: int) -> None:
        self._queue.put_nowait((rid, audio, sr))

    def _monitor_active(self) -> bool:
        # 送信(out_dev=BlackHole 等)時に、別デバイス(ヘッドホン)へ小音量ミックスするときだけ有効。
        return (self.monitor_enabled and self.out_dev is not None
                and self.monitor_dev is not None and self.monitor_dev != self.out_dev)

    async def _play_dual(self, audio: np.ndarray, sr: int) -> None:
        """相手へ(out_dev=BlackHole, 等倍)と自分へ(monitor_dev, 小音量)を同時再生する。"""
        import sounddevice as sd

        def _stream_play(a: np.ndarray, dev, gain: float) -> None:
            aa = np.ascontiguousarray(a * gain if gain != 1.0 else a, dtype=np.float32)
            ch = 1 if aa.ndim == 1 else aa.shape[1]
            with sd.OutputStream(samplerate=sr, device=dev, channels=ch, dtype="float32") as st:
                st.write(aa)

        await asyncio.gather(
            asyncio.to_thread(_stream_play, audio, self.out_dev, 1.0),                    # 相手へ(等倍)
            asyncio.to_thread(_stream_play, audio, self.monitor_dev, self.monitor_gain),  # 自分へ(小音量)
        )

    async def _run_player(self) -> None:
        import sounddevice as sd
        while True:
            rid, audio, sr = await self._queue.get()
            try:
                if self._monitor_active():
                    await self._play_dual(audio, sr)                                      # 送出 + 自己モニタ
                else:
                    await asyncio.to_thread(lambda a=audio, s=sr: (sd.play(a, s, device=self.out_dev), sd.wait()))
            except Exception as e:  # 再生失敗で全体を止めない
                if self.m:
                    self.m.emit(rid, "tts_error", error=f"{type(e).__name__}: {e}")
            finally:
                self._queue.task_done()

    def start_player(self) -> None:
        if self._player is None:
            self._player = asyncio.create_task(self._run_player())

    async def stop_player(self) -> None:
        if self._player:
            self._player.cancel()
            self._player = None


def load_config() -> dict:
    with (Path(__file__).resolve().parent / "config.toml").open("rb") as f:
        cfg = tomllib.load(f)
    cfg["_root"] = str(ROOT)
    return cfg


async def _verify_blackhole(cfg: dict, m: Metrics) -> None:
    """2-D 見える化: 実 TTS 英語音声を BlackHole 出力へ流し、BlackHole 入力で同時録音して
    ループバック RMS を測る(= 相手側に届く Zoom マイク入力経路の実証)。マイク/Zoom不要。"""
    import numpy as np
    import sounddevice as sd
    vmic = cfg["audio"].get("virtual_mic", "BlackHole 2ch")
    out_dev = resolve_device(vmic, "output")
    in_dev = resolve_device(vmic, "input")
    print(f"[tts] BlackHole routing check: vmic='{vmic}'  out={out_dev}  in={in_dev}", file=sys.stderr)
    if out_dev is None or in_dev is None:
        m.emit("SEND2D", "blackhole_missing", vmic=vmic, out_dev=out_dev, in_dev=in_dev)
        print("[tts] BlackHole が出力/入力デバイスとして見つかりません(brew install blackhole-2ch)。", file=sys.stderr)
        return
    tts = TTS(cfg, m, output_device=vmic)
    print("[tts] Kokoro ロード中…", file=sys.stderr)
    await tts.load()
    rid = new_rid()
    text = "Hello, this is a test of the virtual microphone routing to Zoom."
    audio, sr = await tts.synth(rid, text)
    mono = np.asarray(audio, dtype=np.float32).reshape(-1, 1)

    def _playrec():
        rec = sd.playrec(mono, samplerate=sr, device=(in_dev, out_dev), channels=1, dtype="float32")
        sd.wait()
        return rec

    rec = np.asarray(await asyncio.to_thread(_playrec)).flatten()
    rms = float(np.sqrt(np.mean(rec ** 2))) if rec.size else 0.0
    peak = float(np.max(np.abs(rec))) if rec.size else 0.0
    ok = rms > 0.005 and peak > 0.02
    m.emit("SEND2D", "blackhole_loopback", ok=ok, vmic=vmic, out_dev=out_dev, in_dev=in_dev,
           audio_s=round(len(audio) / sr, 2), rms=round(rms, 4), peak=round(peak, 4))
    print(f"[tts] → BlackHole loopback  ok={ok}  rms={rms:.4f}  peak={peak:.4f}  "
          f"({len(audio) / sr:.1f}s TTS, out=[{out_dev}] in=[{in_dev}])", file=sys.stderr)
    print(f"[tts] log={m.path}", file=sys.stderr)


async def _verify_monitor(cfg: dict, m: Metrics) -> None:
    """3-F 見える化: 送出(main→BlackHole 等倍)+ 自己モニタ(monitor→ヘッドホン 小音量)の二重出力を検証。
    monitor 未解決(AirPods 未接続)ならテスト用に MacBook 出力へ代替。相手経路は BlackHole ループバックで確認。
    ⚠️ モニタ側デバイスへ小さなテスト音声が鳴ります。"""
    import numpy as np
    import sounddevice as sd
    vmic = cfg["audio"].get("virtual_mic", "BlackHole 2ch")
    tts = TTS(cfg, m, output_device=vmic)
    if tts.monitor_dev is None:  # AirPods 未接続時のテスト代替
        tts.monitor_dev = resolve_device("MacBook", "output")
        tts.monitor_dev_name = "MacBook(テスト代替)"
    print(f"[tts] self-monitor: out={tts.out_dev}({vmic}) monitor={tts.monitor_dev}({tts.monitor_dev_name}) "
          f"gain={tts.monitor_gain} active={tts._monitor_active()}", file=sys.stderr)
    print("[tts] Kokoro ロード中…", file=sys.stderr)
    await tts.load()
    rid = new_rid()
    audio, sr = await tts.synth(rid, "Testing the self monitor. You should hear this quietly.")
    full_peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    # (a) 二重出力(送出 + 自己モニタ)を実行
    t0 = time.perf_counter()
    dual_ok, dual_err = True, None
    try:
        await tts._play_dual(audio, sr)
    except Exception as e:
        dual_ok, dual_err = False, f"{type(e).__name__}: {e}"
    dual_ms = round((time.perf_counter() - t0) * 1000, 1)
    # (b) 相手経路(main→BlackHole)をループバックで確認
    mono = np.ascontiguousarray(audio, dtype=np.float32).reshape(-1, 1)

    def _playrec():
        r = sd.playrec(mono, samplerate=sr, device=(resolve_device(vmic, "input"), tts.out_dev),
                       channels=1, dtype="float32")
        sd.wait()
        return r

    rec = np.asarray(await asyncio.to_thread(_playrec)).flatten()
    main_rms = float(np.sqrt(np.mean(rec ** 2))) if rec.size else 0.0
    m.emit("SEND3F", "self_monitor", monitor_active=tts._monitor_active(), out_dev=tts.out_dev,
           monitor_dev=tts.monitor_dev, gain=tts.monitor_gain, dual_ok=dual_ok, dual_err=dual_err,
           dual_ms=dual_ms, full_peak=round(full_peak, 4),
           expected_monitor_peak=round(full_peak * tts.monitor_gain, 4), main_bh_rms=round(main_rms, 4))
    print(f"[tts] dual_ok={dual_ok} dual_ms={dual_ms}  相手→BlackHole rms={main_rms:.4f}  "
          f"自己モニタ_peak≈{full_peak * tts.monitor_gain:.4f}(={full_peak:.4f}×{tts.monitor_gain})", file=sys.stderr)
    if dual_err:
        print(f"[tts] dual error: {dual_err}", file=sys.stderr)


async def _main() -> None:
    cfg = load_config()
    if "--to-zoom" in sys.argv:
        # 2-D: BlackHole ルーティング検証(TTS→仮想マイク経路)。
        m = Metrics.from_config(cfg, run_tag="send2d")
        await _verify_blackhole(cfg, m)
        m.close()
        return
    if "--monitor-test" in sys.argv:
        # 3-F: 自己モニタ(二重出力)検証。
        m = Metrics.from_config(cfg, run_tag="send3f")
        await _verify_monitor(cfg, m)
        m.close()
        return
    import soundfile as sf
    m = Metrics.from_config(cfg, run_tag="tts1e")
    out_dir = ROOT / "logs" / "tts1e"
    out_dir.mkdir(parents=True, exist_ok=True)
    tts = TTS(cfg, m)
    print("[tts] Kokoro ロード中(初回warmupで数秒)…", file=sys.stderr)
    await tts.load()
    # 1-D の翻訳出力(英語)を合成
    clauses = [
        "must be repeated several times",
        "Thank you for your time today.",
        "Can you set up a meeting for next Wednesday?",
        "the design phase was completed last week",
    ]
    for i, text in enumerate(clauses):
        rid = new_rid()
        audio, sr = await tts.synth(rid, text)
        wav = out_dir / f"tts_{i}.wav"
        sf.write(str(wav), audio, sr)
        print(f"  {text}  →  {wav.name} ({len(audio)/sr:.1f}s)", file=sys.stderr)
    m.emit("SUMMARY", "tts_summary", n=len(clauses), out=str(out_dir))
    print(f"[tts] 完了。試聴wav={out_dir}  log={m.path}", file=sys.stderr)
    m.close()


if __name__ == "__main__":
    asyncio.run(_main())
