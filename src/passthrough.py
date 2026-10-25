"""地声パススルー — Phase 3-I(会議自動化)。英語OFF中、マイク生声を BlackHole(Zoomマイク)へ流す。

Zoom のマイクは常に「BlackHole 2ch」を指したまま運用するため、英語通訳 OFF のときに何も
送らないと相手側が無音になる。本モジュールは OFF の間だけ
    システム既定入力(ヘッドホンのマイク)→ BlackHole(仮想マイク)
を低遅延で転送し、コントロールパネルのボタンを「地声 ⇄ 英語翻訳」の完全な切替にする。

- 英語ON(通訳中)は muted=True で無音を書く(生声と英語TTSの二重送出を防ぐ)。
- 入力は既定入力に追従(ヘッドホン銘柄非依存。tts.py の既定出力フォールバックと同じ思想)。
- 開始できない場合は翻訳機能に影響させず無効のまま続行する(理由をログに残す)。
- サンプルレートは config値 → 入力デバイス既定値 の順で試す(BTヘッドセット対策)。

単体検証(マイク/Zoom 不要。440Hzトーン注入 → BlackHole ループバック RMS で経路+ミュートを実証):
    spikes/.venv-ml/bin/python src/passthrough.py --tone
実マイク検証(話しながら。前半=転送(OFF相当)/後半=ミュート(ON相当)で RMS 比較):
    spikes/.venv-ml/bin/python src/passthrough.py --test 10
地声だけを流し続ける(v2 G: GUI で「無効」のまま会議に入ったとき watcher が起動する。SIGTERM で停止):
    spikes/.venv-ml/bin/python src/passthrough.py --serve
"""
from __future__ import annotations

import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import Metrics  # noqa: E402
from tts import resolve_device  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


class Passthrough:
    """マイク→BlackHole の低遅延転送(sounddevice 双方向ストリーム)。

    pipeline.py が --to-zoom かつ --panel/--hotkey のとき生成し、
    paused(英語OFF)⇔ muted を `set_muted(not paused)` で連動させる。
    """

    def __init__(self, cfg: dict, metrics: Metrics | None = None):
        pc = cfg.get("passthrough", {})
        self.enabled = bool(pc.get("enabled", True))
        self.gain = float(pc.get("gain", 1.0))
        self.rate_cfg = int(pc.get("samplerate", 48000))
        # MLX(翻訳/TTS)のGPU負荷中でも取りこぼさないよう太めのバッファ(1024f@48k≈21ms)。
        # 会話用途では合計 ~50-100ms の遅延は知覚されない。実測: low+auto では input overflow 11回/8s。
        self.blocksize = int(pc.get("blocksize", 1024))
        self.in_name = pc.get("input_device", "")              # 空=システム既定入力
        self.out_name = cfg.get("audio", {}).get("virtual_mic", "BlackHole 2ch")
        self.m = metrics
        self.muted = True                                       # 安全側の初期値(pipeline が paused に合わせて設定)
        self.tone_hz = 0.0                                      # >0 なら indata の代わりに正弦波(--tone 検証用)
        self.rate = 0
        self.in_dev: int | None = None
        self.out_dev: int | None = None
        self._stream = None
        self._tone_phase = 0.0
        self._status_count = 0                                  # コールバックの over/underflow 回数
        self._last_status = ""
        self._in_sq = 0.0                                       # 入力RMS集計(検証用)
        self._in_n = 0

    def _emit(self, ev: str, **kw) -> None:
        if self.m:
            self.m.emit("PASS", ev, **kw)
        print(f"[passthrough] {ev} {kw}", file=sys.stderr, flush=True)

    # sounddevice コールバック(CoreAudio スレッド)。重い処理・print はしない。
    def _cb(self, indata, outdata, frames, _t, status) -> None:
        if status:
            self._status_count += 1
            self._last_status = str(status)
        self._in_sq += float((indata ** 2).sum())
        self._in_n += frames
        if self.muted:
            outdata.fill(0.0)
            return
        if self.tone_hz > 0.0:
            import numpy as np
            ph = self._tone_phase + (2.0 * np.pi * self.tone_hz / self.rate) * np.arange(1, frames + 1)
            outdata[:] = (0.3 * np.sin(ph)).astype("float32")[:, None]
            self._tone_phase = float(ph[-1] % (2.0 * np.pi))
            return
        outdata[:] = indata[:, :1] * self.gain                  # (frames,1)→(frames,2) ブロードキャスト

    def start(self) -> bool:
        """転送を開始。失敗しても例外は投げず False(翻訳本体を止めない)。"""
        if not self.enabled:
            self._emit("passthrough_disabled", reason="config [passthrough].enabled=false")
            return False
        import sounddevice as sd
        self.in_dev = resolve_device(self.in_name, "input")     # None=既定入力
        self.out_dev = resolve_device(self.out_name, "output")
        if self.out_dev is None:
            self._emit("passthrough_disabled", reason=f"virtual_mic '{self.out_name}' が出力に見つからない")
            return False
        # サンプルレート候補: config → 入力デバイス既定(BTヘッドセットが 48k を拒む場合の保険)。
        rates = [self.rate_cfg]
        try:
            d = sd.query_devices(self.in_dev if self.in_dev is not None else sd.default.device[0])
            rates.append(int(d["default_samplerate"]))
        except Exception:
            pass
        last_err = ""
        for rate in dict.fromkeys(rates):
            try:
                st = sd.Stream(device=(self.in_dev, self.out_dev), samplerate=rate,
                               channels=(1, 2), dtype="float32", latency="high",
                               blocksize=self.blocksize, callback=self._cb)
                st.start()
                self._stream, self.rate = st, rate
                in_label = "既定入力" if self.in_dev is None else str(self.in_dev)
                self._emit("passthrough_start", in_dev=in_label, out_dev=self.out_dev,
                           rate=rate, muted=self.muted, gain=self.gain)
                return True
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
        self._emit("passthrough_disabled", reason=last_err)
        return False

    def set_muted(self, muted: bool) -> None:
        if muted == self.muted:
            return
        self.muted = muted
        self._emit("passthrough_mute", muted=muted)

    def take_input_rms(self) -> float:
        """直近区間のマイク入力 RMS(検証・レベル表示用)。読み出すとリセット。"""
        import math
        rms = math.sqrt(self._in_sq / self._in_n) if self._in_n else 0.0
        self._in_sq, self._in_n = 0.0, 0
        return rms

    def stop(self) -> None:
        """転送停止。PortAudio の stop() は CoreAudio HAL の DeviceStop ミューテックスと
        デッドロックすることがある(実測: FinishStoppingStream ⇄ startStopCallback の相互待ち)ため、
        watchdog スレッドで abort(即時・ドレインなし)→close を実行し、3秒で諦める。
        諦めた場合もプロセス終了時に OS が解放するので実害はない(ハングだけは絶対にしない)。"""
        st = self._stream
        if st is None:
            return
        self._stream = None
        self.muted = True                                       # 終端は無音(切断ノイズ防止)

        def _close() -> None:
            try:
                st.abort()
            except Exception:
                pass
            try:
                st.close()
            except Exception:
                pass

        import threading
        t = threading.Thread(target=_close, daemon=True, name="passthrough-close")
        t.start()
        t.join(timeout=3.0)
        self._emit("passthrough_stop", clean=not t.is_alive(),
                   cb_status_count=self._status_count, last_status=self._last_status)


def load_config() -> dict:
    with (Path(__file__).resolve().parent / "config.toml").open("rb") as f:
        cfg = tomllib.load(f)
    cfg["_root"] = str(ROOT)
    return cfg


def _record_bh_rms(seconds: float) -> float:
    """BlackHole 入力(ループバック)を録音して RMS を返す(相手に届く音の代理計測)。"""
    import numpy as np
    import sounddevice as sd
    cfg = load_config()
    bh_in = resolve_device(cfg["audio"].get("virtual_mic", "BlackHole 2ch"), "input")
    if bh_in is None:
        return -1.0
    rec = sd.rec(int(seconds * 48000), samplerate=48000, channels=1, dtype="float32", device=bh_in)
    sd.wait()
    a = np.asarray(rec).flatten()
    return float(np.sqrt(np.mean(a ** 2))) if a.size else 0.0


def _verify_tone(m: Metrics) -> None:
    """--tone: マイク不要の決定的検証。トーン注入→BlackHole RMS(転送時 >0.01 / ミュート時 <0.002)。"""
    cfg = load_config()
    pt = Passthrough(cfg, m)
    pt.tone_hz = 440.0
    pt.muted = False
    if not pt.start():
        print("[passthrough] 開始できず(BlackHole 未検出?)", file=sys.stderr)
        return
    time.sleep(0.3)                                             # ストリーム安定待ち
    rms_on = _record_bh_rms(1.2)
    pt.set_muted(True)
    time.sleep(0.2)
    rms_muted = _record_bh_rms(1.2)
    pt.stop()
    ok = rms_on > 0.01 and 0.0 <= rms_muted < 0.002
    m.emit("PASS", "tone_verify", ok=ok, rms_on=round(rms_on, 4), rms_muted=round(rms_muted, 4),
           rate=pt.rate, out_dev=pt.out_dev)
    print(f"[passthrough] tone_verify ok={ok}  転送時rms={rms_on:.4f}(>0.01)  "
          f"ミュート時rms={rms_muted:.4f}(<0.002)  rate={pt.rate}", file=sys.stderr)


def _verify_mic(m: Metrics, seconds: float) -> None:
    """--test: 実マイクで検証。前半=転送(英語OFF相当)/後半=ミュート(英語ON相当)。話しながら実行する。"""
    cfg = load_config()
    pt = Passthrough(cfg, m)
    pt.muted = False
    if not pt.start():
        print("[passthrough] 開始できず(BlackHole 未検出?)", file=sys.stderr)
        return
    half = max(2.0, seconds / 2)
    print(f"[passthrough] 前半 {half:.0f}秒: 転送中(話してください)…", file=sys.stderr, flush=True)
    time.sleep(0.3)
    pt.take_input_rms()
    bh1 = _record_bh_rms(half)
    in1 = pt.take_input_rms()
    pt.set_muted(True)
    print(f"[passthrough] 後半 {half:.0f}秒: ミュート中(話し続けてください)…", file=sys.stderr, flush=True)
    bh2 = _record_bh_rms(half)
    in2 = pt.take_input_rms()
    pt.stop()
    ok = bh1 > 0.003 and bh2 < 0.002 and in1 > 0.003
    m.emit("PASS", "mic_verify", ok=ok, bh_rms_pass=round(bh1, 4), bh_rms_muted=round(bh2, 4),
           mic_rms_pass=round(in1, 4), mic_rms_muted=round(in2, 4), rate=pt.rate)
    print(f"[passthrough] mic_verify ok={ok}  転送時: mic={in1:.4f}→BlackHole={bh1:.4f}  "
          f"ミュート時: mic={in2:.4f}→BlackHole={bh2:.4f}", file=sys.stderr)


def _serve(m: Metrics) -> int:
    """--serve: 翻訳なしで地声を BlackHole へ流し続ける。SIGTERM/SIGINT/SIGHUP で止める。

    開始できなければ 2 で終わる(watcher はプロセスの終了で失敗を知る)。"""
    import signal
    import threading
    pt = Passthrough(load_config(), m)
    pt.muted = False
    stop = threading.Event()
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, lambda *_: stop.set())
    if not pt.start():
        return 2
    while not stop.wait(1.0):
        pass
    pt.stop()
    return 0


if __name__ == "__main__":
    cfg0 = load_config()
    if "--serve" in sys.argv:
        mm = Metrics.from_config(cfg0, run_tag="bypass")
        rc = _serve(mm)
        mm.close()
        sys.exit(rc)
    mm = Metrics.from_config(cfg0, run_tag="passthru")
    if "--tone" in sys.argv:
        _verify_tone(mm)
    else:
        nums = [a for a in sys.argv[1:] if a.replace(".", "", 1).isdigit()]
        _verify_mic(mm, float(nums[0]) if nums else 10.0)
    mm.close()
