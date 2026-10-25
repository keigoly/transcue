"""翻訳段 — 1-D。PLaMo-2-translate(MLX 4bit)で節を翻訳(spike3 を関数化)。

- 同期の mlx `stream_generate` を `asyncio.to_thread` でオフロードし、イベントループを塞がない。
- 早期stop(`<|plamo:op|>`)で節翻訳を短縮(spike3 実績: 中央値~1.3s / 初トークン~0.5s)。
- モデルは共有 HF キャッシュから **オフライン**(HF_HUB_OFFLINE=1)ロード。外部通信なし。

単体実行(マイク不要。モデルロード数秒+数節の翻訳):
    source spikes/.venv-ml/bin/activate
    python src/translate.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import Metrics, mlx_mem, new_rid  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# PLaMo-2-translate の指示フォーマット(spike3 実績)。
PROMPT = (
    "<|plamo:op|>dataset\n"
    "translation\n"
    "<|plamo:op|>input lang={src}\n"
    "{text}\n"
    "<|plamo:op|>output lang={dst}\n"
)


def _gpu_tick() -> None:
    """GPU を起こすだけの小さな計算(モデルには触らない)。"""
    import mlx.core as mx
    mx.eval(mx.ones((8,)) + 1)


class Translator:
    def __init__(self, cfg: dict, metrics: Metrics | None = None):
        tc = cfg["translate"]
        self.model_name = tc["model"]
        self.max_tokens = int(tc.get("max_tokens", 128))
        self.stop = tc.get("early_stop_marker", "<|plamo:op|>")
        self.hf_home = str(Path(cfg["_root"]) / "models-cache" / "huggingface")
        self.m = metrics
        self._model = None
        self._tok = None
        self._lock = asyncio.Lock()
        self._last_used: float | None = None      # 最後に翻訳した時刻(keep-warm 判定用)
        self._last_wake = 0.0                     # 最後に GPU を起こした時刻(wake。v2 A2-2)
        self._keepwarm_task: asyncio.Task | None = None
        self._load_ms: dict = {}                  # translate_load の内訳(import / 重み読み込み。v2 A1)
        # v2 A2-2: 重みを常時固定する([translate].wire_weights。ZRT_WIRE_WEIGHTS=1 で上書き)
        self.wire = os.environ.get("ZRT_WIRE_WEIGHTS", str(tc.get("wire_weights", False))).lower() in ("1", "true")

    def _load_sync(self) -> None:
        # 共有キャッシュから完全ローカルにロード(再DL・外部通信なし)。
        os.environ.setdefault("HF_HOME", self.hf_home)
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        t = time.perf_counter()
        from mlx_lm import load
        self._load_ms["import_ms"] = round((time.perf_counter() - t) * 1000)
        t = time.perf_counter()
        self._model, self._tok = load(self.model_name, tokenizer_config={"trust_remote_code": True})
        self._load_ms["weights_ms"] = round((time.perf_counter() - t) * 1000)
        if self.wire:
            # mlx_lm の stream_generate は生成のたびに wired limit を上げ、終わると「入る前の値」に戻す(既定 0 →
            # 生成の合間に重みが追い出される。A1: 1〜5 s 空くだけで初トークン 573 → 1,817 ms)。入る前の値を
            # ここで上げておけば、合間も固定されたままになる。生成の前に 1 回だけ設定する(生成中に変えない)。
            import mlx.core as mx
            limit = int(mx.device_info()["max_recommended_working_set_size"])
            mx.set_wired_limit(limit)
            self._load_ms["wired_limit_mb"] = limit // 2**20

    def _translate_sync(self, text: str, src: str, dst: str) -> tuple[str, float | None]:
        from mlx_lm import stream_generate
        prompt = PROMPT.format(src=src, dst=dst, text=text)
        out = ""
        first_ms: float | None = None
        t = time.perf_counter()
        for resp in stream_generate(self._model, self._tok, prompt=prompt, max_tokens=self.max_tokens):
            if first_ms is None:
                first_ms = (time.perf_counter() - t) * 1000
            out += resp.text
            if self.stop in out:
                break
        return out.split(self.stop)[0].strip(), first_ms

    # 既定 warmup(ja→en)。実発話に近い長さで Metal カーネル/メモリを事前確保し、
    # 初回実翻訳の遅延(実会議で ~30s を観測)をロード時間側へ移す(spike7 実測)。
    WARMUP_JA_EN = [
        "おはようございます、本日はお時間をいただきありがとうございます。",
        "設計フェーズは先週完了しており、実装は来週月曜日から開始する予定です。",
    ]
    # 受信方向(en→ja)用。receive.py が load(warmup_texts=..., src=..., dst=...) で渡す。
    WARMUP_EN_JA = [
        "Good morning, thank you for taking the time to meet with us today.",
        "The design phase was completed last week, and implementation starts next Monday.",
    ]

    async def load(self, warmup_texts: list[str] | None = None,
                   src: str = "Japanese", dst: str = "English") -> None:
        """モデルロード + warmup。受信方向は warmup_texts(英文)と src/dst を渡して同様に温める。"""
        t0 = time.perf_counter()
        await asyncio.to_thread(self._load_sync)
        texts = warmup_texts if warmup_texts is not None else self.WARMUP_JA_EN
        warm_ms: list[int] = []
        for w in texts:
            t_w = time.perf_counter()
            await asyncio.to_thread(self._translate_sync, w, src, dst)
            warm_ms.append(round((time.perf_counter() - t_w) * 1000))
        self._last_used = time.monotonic()
        if self.m:
            self.m.emit("TRANSLATE", "translate_load",
                        ms=round((time.perf_counter() - t0) * 1000), model=self.model_name,
                        warmup_n=len(texts), warmup_ms=warm_ms, **self._load_ms, **mlx_mem())

    async def translate(self, rid: str, text: str, src: str = "Japanese", dst: str = "English") -> str:
        async with self._lock:  # MLX モデルへの同時アクセスを避ける(節は逐次処理)
            try:
                if self.m:
                    with self.m.timed(rid, "translate", src=text, engine="plamo") as ex:
                        out, first_ms = await asyncio.to_thread(self._translate_sync, text, src, dst)
                        ex["out"] = out
                        ex["first_token_ms"] = round(first_ms, 1) if first_ms is not None else None
                    return out
                out, _ = await asyncio.to_thread(self._translate_sync, text, src, dst)
                return out
            finally:
                self._last_used = time.monotonic()

    # --- keep-warm: 無音が続くとモデルが GPU 上でコールドに戻り、次の1発目が遅くなる
    # (実測: 6分放置後の初翻訳 first_token 8.9s)。idle 中も小さなダミー翻訳でモデルを温存する。
    async def keepwarm_once(self, src: str, dst: str, text: str = "OK") -> None:
        if self._model is None:
            return
        async with self._lock:
            t = time.perf_counter()
            await asyncio.to_thread(self._translate_sync, text, src, dst)
            ms = round((time.perf_counter() - t) * 1000, 1)
        self._last_used = time.monotonic()
        if self.m:
            self.m.emit("TRANSLATE", "keepwarm", note="idle warm", ms=ms)

    # --- wake(v2 A2-2): 休止明けの最初の GPU 処理は 0.1〜1.4 s かかり(空き 3 s → 0.13 s、30 s 以上 → 1.4 s)、
    # それが初トークンに乗っていた。重みの追い出しではない(固定しても同じ)。話し始め(新しい節の最初の partial)で
    # 小さな計算を 1 回して起こしておけば、節が確定するころには起きている。無音の間は何もしない(keep-warm の代わり)。
    def wake(self) -> None:
        now = time.monotonic()
        if self._model is None or self._lock.locked() or now - self._last_wake < 1.0 \
                or (self._last_used is not None and now - self._last_used < 1.0):
            return                                        # 生成中・直後は起きている
        self._last_wake = now
        asyncio.ensure_future(self._wake())

    async def _wake(self) -> None:
        async with self._lock:
            t = time.perf_counter()
            await asyncio.to_thread(_gpu_tick)
            ms = round((time.perf_counter() - t) * 1000, 1)
        if self.m:
            self.m.emit("TRANSLATE", "wake", ms=ms)

    def start_keepwarm(self, check_s: float, idle_s: float, src: str, dst: str) -> None:
        """idle_s 秒 翻訳が無ければ check_s ごとにダミー翻訳でモデルを温存する背景タスク。"""
        idle_s = float(os.environ.get("ZRT_KEEPWARM_IDLE_S", idle_s))   # ベンチで止める(0。v2 A2-2)
        if idle_s <= 0:
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(check_s)
                if self._last_used is None:
                    continue
                if (time.monotonic() - self._last_used) < idle_s:
                    continue                              # 直近に実翻訳あり → 温かいので不要
                try:
                    await self.keepwarm_once(src, dst)
                except asyncio.CancelledError:
                    raise
                except Exception:                         # 温存失敗で本体を止めない
                    pass

        self._keepwarm_task = asyncio.ensure_future(_loop())

    async def stop_keepwarm(self) -> None:
        if self._keepwarm_task:
            self._keepwarm_task.cancel()
            self._keepwarm_task = None

    async def translate_stream(self, rid: str, text: str, src: str = "Japanese", dst: str = "English"):
        """部分訳(累積テキスト)を逐次 yield する非同期ジェネレータ(Phase 3 ストリーム字幕用)。

        同期の mlx `stream_generate` をワーカースレッドで回し、トークンが確定するたびに
        `loop.call_soon_threadsafe` で asyncio.Queue へ橋渡しする。最後に yield した値が確定訳。
        first_token / 全体 ms を metrics に記録(phase="translate", streamed=true)。
        送信方向(pipeline.py)は従来の translate() を使う — 本メソッドは受信字幕の低遅延表示専用。
        """
        from mlx_lm import stream_generate
        async with self._lock:  # MLX モデルへの同時アクセスを避ける(節は逐次処理)
            loop = asyncio.get_running_loop()
            q: asyncio.Queue = asyncio.Queue()
            sentinel = object()
            stop = self.stop
            prompt = PROMPT.format(src=src, dst=dst, text=text)
            max_tokens, model, tok = self.max_tokens, self._model, self._tok
            box: dict = {}

            def worker() -> None:
                try:
                    out = ""
                    t = time.perf_counter()
                    for resp in stream_generate(model, tok, prompt=prompt, max_tokens=max_tokens):
                        if "first_ms" not in box:
                            box["first_ms"] = (time.perf_counter() - t) * 1000
                        out += resp.text
                        loop.call_soon_threadsafe(q.put_nowait, out.split(stop)[0])  # 累積の部分訳
                        if stop in out:
                            break
                    box["final"] = out.split(stop)[0].strip()
                except BaseException as e:  # noqa: BLE001  ワーカー例外も伝える
                    box["error"] = f"{type(e).__name__}: {e}"
                finally:
                    loop.call_soon_threadsafe(q.put_nowait, sentinel)

            t0 = time.perf_counter()
            fut = loop.run_in_executor(None, worker)
            last = ""
            try:
                while True:
                    item = await q.get()
                    if item is sentinel:
                        break
                    last = item
                    yield item
            finally:
                await fut  # ワーカー完了を待つ(消費側が早期終了しても回収)
                self._last_used = time.monotonic()
            if self.m:
                self.m.emit(rid, "translate", src=text, out=box.get("final", last.strip()),
                            engine="plamo", streamed=True,
                            first_token_ms=round(box["first_ms"], 1) if "first_ms" in box else None,
                            ms=round((time.perf_counter() - t0) * 1000, 1),
                            ok="error" not in box, error=box.get("error"))


def load_config() -> dict:
    with (Path(__file__).resolve().parent / "config.toml").open("rb") as f:
        cfg = tomllib.load(f)
    cfg["_root"] = str(ROOT)
    return cfg


def server_sock_path(cfg: dict) -> str:
    """翻訳サーバの受け口(v2 A2-1)。ZRT_TRANSLATOR_SOCK > [translate].server_sock。"""
    p = os.environ.get("ZRT_TRANSLATOR_SOCK") or cfg["translate"].get(
        "server_sock", "~/.transcue/translator.sock")
    return str(Path(p).expanduser())


def wake_on_speech(cfg: dict) -> bool:
    """話し始めで GPU を起こすか([translate].wake_on_speech。ZRT_WAKE_ON_SPEECH=1 で上書き。v2 A2-2)。"""
    return os.environ.get("ZRT_WAKE_ON_SPEECH",
                          str(cfg["translate"].get("wake_on_speech", False))).lower() in ("1", "true")


def make_translator(cfg: dict, m: Metrics | None, role: str):
    """[translate].mode(ZRT_TRANSLATE_MODE で上書き)が "server" なら翻訳サーバの使い手、既定は自前のモデル。
    送信(role="send")の依頼は受信より先に処理される(v2 A2-1)。
    [translate].<role>_engine(ZRT_RECV_ENGINE / ZRT_SEND_ENGINE で上書き)が "claude" なら、訳は Claude API に頼み、
    上の PLaMo を控えにする(v2 §7b)。"""
    mode = os.environ.get("ZRT_TRANSLATE_MODE") or cfg["translate"].get("mode", "local")
    if mode == "server":
        from translate_client import RemoteTranslator
        tr = RemoteTranslator(cfg, m, prio=0 if role == "send" else 1)
    else:
        tr = Translator(cfg, m)
    engine = os.environ.get(f"ZRT_{role.upper()}_ENGINE") or cfg["translate"].get(f"{role}_engine", "plamo")
    if engine == "claude":
        from translate_claude import ClaudeTranslator
        return ClaudeTranslator(cfg, m, role, fallback=tr)
    return tr


async def _main() -> None:
    cfg = load_config()
    m = Metrics.from_config(cfg, run_tag="translate1d")
    tr = Translator(cfg, m)
    print("[translate] PLaMo ロード中(初回は数秒)…", file=sys.stderr)
    await tr.load()
    samples = [
        "何度も繰り返す必要があります",              # 1-C の実出力
        "本日はお時間をいただきありがとうございます",
        "来週の水曜日に打ち合わせを設定できますか",
        "設計フェーズは先週完了しており",            # 節フラグメント
    ]
    for s in samples:
        rid = new_rid()
        out = await tr.translate(rid, s)
        print(f"  {s}  →  {out}", file=sys.stderr)
    m.emit("SUMMARY", "translate_summary", n=len(samples))
    print(f"[translate] 完了。log={m.path}", file=sys.stderr)
    m.close()


if __name__ == "__main__":
    asyncio.run(_main())
