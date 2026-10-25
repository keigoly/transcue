"""Claude API の翻訳 — v2 §7b(訳の質)。Translator / RemoteTranslator と同じ使い方(load / translate /
translate_stream / wake / keep-warm)で、訳は Claude に頼み、PLaMo(fallback)を控えに置く。

- 2026-10-25 の評価(src/eval_translate/)で、字幕(英→日)は Haiku 5.5、英語(日→英)は Sonnet 5.5 に決めた。
  指示文と設定は評価したときのまま(字幕 20261025_100746・英語 20261025_100939)。変えたら評価を取り直す。
  送るのは節の文だけで、直前の会話や話し手の情報は送らない(評価で文脈なしを選んだ)。
- 英語(送信・translate())は、主のモデル(Sonnet)と控えのモデル(Haiku・`<role>_backup_model`)に同時に頼み、
  主が first_token_timeout_ms 以内に返し始めて訳し終えればその訳、でなければ控えの訳を使う(Sonnet は評価で
  54 回中 15 回、初トークンまで 4.5 s ほど待たされた)。字幕(translate_stream())は主のモデルだけ。
- 次のときは、その節を PLaMo で訳す: API キーが無い・訳が出始めるまでに first_token_timeout_ms を超えた・
  出そろうまでに total_timeout_ms を超えた・API の失敗・断られた(refusal)・訳が空(英語は主と控えの両方で)。
  続けて cooldown_after_failures 回失敗したら、cooldown_s 秒は Claude を使わない(通信が切れている間に毎節待たない)。
  キーが通らない(401 / 403)ときは、その後ずっと PLaMo(会議の途中でキーは直らない)。
- metrics: Claude で訳せた節は phase="translate"(engine = 訳を使ったモデル・usage・cost_usd。英語は主と控えの
  内訳 primary / backup と、控えを使ったか hedged)。PLaMo に回した節は phase="translate_fallback"(reason と
  Claude 側でかかった ms)を残し、続けて PLaMo の "translate" が出る。どの節も "translate" は 1 つ。
- PLaMo の読み込み・keep-warm・wake は PLaMo に任せる(控えをいつでも使えるように、今までどおり温めておく)。
- 操作パネル(src/app の TransCue)の「クラウド翻訳」を OFF にすると、Claude を一切呼ばずに PLaMo で訳す
  (cloud.json。発言ごとに読むので、会議中に切り替えても次の発言から効く。OFF のまま始めたら起動時の温めも送らない)。
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from metrics import Metrics

# 評価した指示文(src/eval_translate/run_eval.py の SYSTEM_JA_EN_V2 と SYSTEM_EN_JA と同じ文)
SYSTEM_JA_EN = """You are the user's interpreter in a live business meeting held over Zoom. The user speaks Japanese. \
You turn each Japanese utterance into the English that is read aloud (text-to-speech) to the other participants as the \
user's own words.

How to translate:
- Say only what the user said, about as briefly as the Japanese says it. Do not add anything that was not said: no \
extra thanks, greetings, apologies, explanations, promises, or lead-ins such as "Okay," or "So,".
- Stay close to the user's wording and sentence order whenever that is correct, natural English, so the user can \
recognize their own words. Reword only where a literal rendering would be wrong, unclear, or unnatural.
- Japanese courtesy formulas (greetings, thanks, set closings) often have no direct English equivalent. Use the \
shortest plain equivalent, such as "Hello." or "Thank you.", and do not expand them into longer pleasantries.
- Keep the user's intent, level of certainty, and politeness.
- Speak as the user, in the first person. Never address the user or comment on the text.
- Japanese often omits subjects and objects. Supply only what English grammar needs, choosing the most neutral \
reading, and do not add details that were not said.
- The text comes from speech recognition: ignore fillers (えー, あの, ん) and fix obvious recognition errors. If the \
text is a fragment cut off mid-sentence, translate only what is there, without inventing the rest.

The user message may contain <conversation_so_far> with the most recent turns ([あなた] is the user, [相手] is the \
other side). Use it only to understand the context. Translate only the text inside <translate>, and output only the \
English to be spoken: no quotes, notes, alternatives, or Japanese."""

SYSTEM_EN_JA = """You write Japanese subtitles for the user, a Japanese speaker in a live business meeting held over \
Zoom. Each English utterance from the other participants becomes one subtitle that the user reads at a glance while \
listening.

How to translate:
- Write what a Japanese professional would naturally say in the same meeting, not a word-for-word translation. \
Avoid translationese: unneeded subjects and pronouns (私は, あなたは, 彼は, それは), literal renderings of English \
idioms, and stiff written forms.
- Keep the meaning and nuance (certainty, requests, agreement, concerns) and keep it short. Do not add anything that \
was not said.
- Use the polite spoken style (です・ます) consistently.
- Keep names, numbers, dates, and times exact. Write technical terms the way Japanese engineers usually write them.
- The text comes from speech recognition: fix obvious recognition errors when the intended word is clear. If the \
text is a fragment cut off mid-sentence, translate only what is there, without inventing the rest.

The user message may contain <conversation_so_far> with the most recent turns ([あなた] is the user, [相手] is the \
other side). Use it only to understand the context. Translate only the text inside <translate>, and output only the \
Japanese subtitle: no quotes, notes, alternatives, or English."""

SYSTEMS = {("Japanese", "English"): SYSTEM_JA_EN, ("English", "Japanese"): SYSTEM_EN_JA}
# 評価と同じ設定(遅延を優先)。Sonnet 5.5 は思考を切れない
THINKING = {"claude-haiku-5-5": {"type": "disabled"}, "claude-sonnet-5-5": {"type": "between_tools"}}
# 費用の見込み(USD / 100 万トークン: 入力・出力・キャッシュ読み。run_eval.py と同じ。Haiku のキャッシュ読みは推定)
PRICE = {"claude-sonnet-5-5": (2.0, 10.0, 0.20), "claude-haiku-5-5": (0.10, 0.50, 0.01)}


class CloudSwitch:
    """操作パネルが書く「クラウド翻訳」(cloud.json)。無ければ ON(切り替えを使う前と同じ)。
    読めないとき(壊れたファイルなど)は直前の値を保つ。path が無ければ常に ON(ベンチは条件を自分で決める)。"""

    def __init__(self, path: Path | None):
        self.path = path
        self.value = True

    def read(self) -> bool:
        if self.path is None:
            return True
        try:
            self.value = bool(json.loads(self.path.read_text(encoding="utf-8")).get("cloud", True))
        except FileNotFoundError:
            self.value = True
        except (OSError, ValueError, AttributeError):
            pass
        return self.value


class ClaudeTranslator:
    def __init__(self, cfg: dict, metrics: Metrics | None, role: str, fallback):
        cc = cfg.get("claude", {})
        self.model = cc.get(f"{role}_model", "claude-haiku-5-5")
        self.backup_model = cc.get(f"{role}_backup_model")    # 主が遅いときに使う訳(英語だけ)
        self.engine = self.model
        self.fallback = fallback                           # PLaMo(Translator か RemoteTranslator)
        self.m = metrics
        self.max_tokens = int(cc.get("max_tokens", 256))
        self.first_timeout_s = float(cc.get("first_token_timeout_ms", 2000)) / 1000
        self.total_timeout_s = float(cc.get("total_timeout_ms", 5000)) / 1000
        self.cooldown_after = int(cc.get("cooldown_after_failures", 3))
        self.cooldown_s = float(cc.get("cooldown_s", 60))
        self.key_file = Path(cc.get("api_key_file", "~/.transcue/anthropic_api_key")).expanduser()
        # ZRT_CLOUD_SWITCH_FILE で上書き(空 = 切り替えを見ない。ベンチ用)
        switch = os.environ.get("ZRT_CLOUD_SWITCH_FILE", cc.get("cloud_switch_file", "~/.transcue/cloud.json"))
        self.cloud = CloudSwitch(Path(switch).expanduser() if switch else None)
        self._cloud_on: bool | None = None                 # 直前に見たクラウド翻訳の ON/OFF(変わったら記録する)
        self._client = None
        self._off: str | None = None                       # Claude を使わない理由(no_api_key など)。None = 使う
        self._fails = 0                                    # 続けて失敗した回数
        self._cool_until = 0.0

    def _request(self, model: str, text: str, src: str, dst: str) -> dict:
        kw: dict = {"model": model, "max_tokens": self.max_tokens,
                    "system": [{"type": "text", "text": SYSTEMS[(src, dst)], "cache_control": {"type": "ephemeral"}}],
                    "messages": [{"role": "user", "content": f"<translate>{text}</translate>"}],
                    "output_config": {"effort": "low"}}
        if model in THINKING:
            kw["thinking"] = THINKING[model]
        return kw

    def _error(self, e: BaseException) -> str:
        """失敗の種類(metrics の reason)。キーが通らないときは、その後ずっと Claude を使わない。"""
        import anthropic
        if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            self._off = "auth_error"
            return "auth_error"
        if isinstance(e, anthropic.APITimeoutError):       # APIConnectionError の一種なので先に見る
            return "api_timeout"
        if isinstance(e, anthropic.APIConnectionError):
            return "connection_error"
        if isinstance(e, anthropic.APIStatusError):
            return f"status_{e.status_code}"
        return type(e).__name__

    async def load(self, warmup_texts: list[str] | None = None,
                   src: str = "Japanese", dst: str = "English") -> None:
        await self.fallback.load(warmup_texts=warmup_texts, src=src, dst=dst)    # 控えを先に用意する
        t0 = time.perf_counter()
        error = None
        try:
            import anthropic
        except ImportError:
            self._off = "no_sdk"
        if self._off is None:
            key = os.environ.get("ANTHROPIC_API_KEY") or (
                self.key_file.read_text(encoding="utf-8").strip() if self.key_file.exists() else "")
            if key:
                self._client = anthropic.AsyncAnthropic(api_key=key, max_retries=0, timeout=self.total_timeout_s)
            else:
                self._off = "no_api_key"
        self._cloud_on = self.cloud.read()
        if self._off is None and self._cloud_on:
            # 1 回目は接続の準備(TLS・指示文のキャッシュ)。最初の節が遅れないよう、ここで 1 回ずつ訳しておく
            text = (warmup_texts or self.fallback.WARMUP_JA_EN)[0]
            models = [m for m in (self.model, self.backup_model) if m]
            for r in await asyncio.gather(*(asyncio.wait_for(self._warm(m, text, src, dst), self.total_timeout_s)
                                            for m in models), return_exceptions=True):
                if isinstance(r, TimeoutError):
                    error = error or "warmup_timeout"
                elif isinstance(r, Exception):             # 温めの失敗では止めない(節ごとに PLaMo へ回る)
                    error = error or self._error(r)
        if self.m:
            self.m.emit("TRANSLATE", "claude_load", ms=round((time.perf_counter() - t0) * 1000), engine=self.model,
                        backup=self.backup_model, ok=self._off is None and error is None, error=self._off or error,
                        cloud=self._cloud_on)

    async def _warm(self, model: str, text: str, src: str, dst: str) -> None:
        async with self._client.messages.stream(**self._request(model, text, src, dst)) as stream:
            async for _ in stream.text_stream:
                pass

    async def _produce(self, q: asyncio.Queue, kw: dict) -> None:
        """Claude の訳を q へ流す(別タスク。cancel で通信を閉じる)。"""
        try:
            async with self._client.messages.stream(**kw) as stream:
                async for delta in stream.text_stream:
                    q.put_nowait(("delta", delta))
                q.put_nowait(("final", await stream.get_final_message()))
        except Exception as e:  # noqa: BLE001  失敗は受け手に渡して PLaMo に回す
            q.put_nowait(("error", e))

    async def _stream_once(self, model: str, text: str, src: str, dst: str, res: dict):
        """1 回頼む。部分訳(累積)を yield し、終わったら res に結果を入れる(reason が None なら訳せた・out・ms・
        first_token_ms・usage・cost_usd)。metrics と失敗の数は呼び手が扱う。"""
        t0 = time.perf_counter()
        q: asyncio.Queue = asyncio.Queue()
        task = asyncio.ensure_future(self._produce(q, self._request(model, text, src, dst)))
        out, first_ms, final, reason = "", None, None, None
        try:
            while final is None and reason is None:
                limit = self.total_timeout_s if first_ms is not None else self.first_timeout_s
                try:
                    kind, val = await asyncio.wait_for(q.get(), max(limit - (time.perf_counter() - t0), 0.001))
                except TimeoutError:
                    reason = "total_timeout" if first_ms is not None else "first_token_timeout"
                    break
                if kind == "delta":
                    if first_ms is None:
                        first_ms = (time.perf_counter() - t0) * 1000
                    out += val
                    yield out
                elif kind == "final":
                    final = val
                else:
                    reason = self._error(val)
        finally:
            if not task.done():
                task.cancel()                              # 時間切れ・受け手の中断: 通信を閉じる
            await asyncio.gather(task, return_exceptions=True)
        if reason is None and final.stop_reason == "refusal":
            reason = "refusal"
        if reason is None and not out.strip():
            reason = "empty"
        res.update(model=model, reason=reason, out=out.strip(), shown=out,
                   first_token_ms=round(first_ms, 1) if first_ms is not None else None,
                   ms=round((time.perf_counter() - t0) * 1000, 1))
        if final is not None:
            u = final.usage
            usage = {"input": u.input_tokens, "cache_write": u.cache_creation_input_tokens or 0,
                     "cache_read": u.cache_read_input_tokens or 0, "output": u.output_tokens}
            p_in, p_out, p_cr = PRICE.get(model, (0.0, 0.0, 0.0))
            cost = (usage["input"] * p_in + usage["cache_write"] * p_in * 1.25 + usage["cache_read"] * p_cr
                    + usage["output"] * p_out) / 1e6
            res.update(stop_reason=final.stop_reason, usage=usage, cost_usd=round(cost, 6))
        if reason is None and out != out.strip():
            yield out.strip()

    def _skipped(self, rid: str, text: str) -> str | None:
        """Claude を使わない間(キーが無い・クラウド翻訳が OFF・休止中など)なら、その理由を残して返す。"""
        on = self.cloud.read()
        if on != self._cloud_on:
            if self.m:
                self.m.emit("TRANSLATE", "cloud_switch", on=on, engine=self.model)
            self._cloud_on = on
        reason = self._off or (None if on else "cloud_off") \
            or ("cooldown" if time.monotonic() < self._cool_until else None)
        if reason and self.m:
            self.m.emit(rid, "translate_fallback", src=text, engine=self.model, reason=reason)
        return reason

    def _record(self, rid: str, text: str, res: dict, **extra) -> None:
        """訳せた節は "translate"、PLaMo に回す節は "translate_fallback" を残し、続けて失敗した回数を数える。"""
        reason = res.get("reason")
        if reason is None:
            self._fails = 0
        elif reason not in ("refusal", "empty"):           # 断り・空の訳は通信の問題ではないので数えない
            self._fails += 1
            if self._fails >= self.cooldown_after:
                self._cool_until = time.monotonic() + self.cooldown_s
        if not self.m:
            return
        if reason is None:
            self.m.emit(rid, "translate", src=text, out=res["out"], engine=res["model"], streamed=True,
                        first_token_ms=res["first_token_ms"], ms=res["ms"], ok=True, error=None,
                        stop_reason=res.get("stop_reason"), usage=res.get("usage"), cost_usd=res.get("cost_usd"),
                        **extra)
        else:
            self.m.emit(rid, "translate_fallback", src=text, engine=res.get("model", self.model), reason=reason,
                        ms=res.get("ms"), first_token_ms=res.get("first_token_ms"), shown=res.get("shown"),
                        cooldown=self._fails >= self.cooldown_after, **extra)

    async def _claude_stream(self, rid: str, text: str, src: str, dst: str, box: dict):
        """主のモデルの部分訳(累積)を yield する。終わったら box["reason"] に PLaMo へ回す理由(None = 訳せた)。"""
        reason = self._skipped(rid, text)
        if reason:
            box["reason"] = reason
            return
        res: dict = {}
        try:
            async for partial in self._stream_once(self.model, text, src, dst, res):
                yield partial
        except Exception as e:  # noqa: BLE001  想定外の失敗でも字幕を止めない(PLaMo に回す)
            res.update(model=self.model, reason=f"unexpected:{type(e).__name__}")
        box["reason"] = res["reason"]
        self._record(rid, text, res)

    async def _translate_hedged(self, rid: str, text: str, src: str, dst: str) -> str | None:
        """主と控えに同時に頼み、主 → 控えの順に、訳せた最初の訳を返す。主は first_token_timeout_ms 以内に
        返し始めなければ失敗になるので、主が遅いときはその時点で控えの訳(たいてい出そろっている)を使う。
        どちらも失敗したら None(呼び手が PLaMo で訳す)。"""
        t0 = time.perf_counter()
        runs: dict[str, dict] = {self.model: {}, self.backup_model: {}}

        async def run(model: str) -> None:
            try:
                async for _ in self._stream_once(model, text, src, dst, runs[model]):
                    pass
            except Exception as e:  # noqa: BLE001  想定外の失敗でも読み上げを止めない
                runs[model].update(model=model, reason=f"unexpected:{type(e).__name__}")

        tasks = [asyncio.ensure_future(run(m)) for m in runs]
        used = None
        try:
            for model, task in zip(runs, tasks):
                await task                                 # どちらも自分の時間切れで必ず終わる
                if runs[model].get("reason", "cancelled") is None:
                    used = model
                    break
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()                          # 使わない方の通信を閉じる
            await asyncio.gather(*tasks, return_exceptions=True)
        primary, backup = runs[self.model], runs[self.backup_model]
        detail = {name: {k: r.get(k) for k in ("first_token_ms", "ms", "reason")}
                  for name, r in (("primary", primary), ("backup", backup))}
        ms = round((time.perf_counter() - t0) * 1000, 1)   # 待った時間(主の時間切れを待った分を含む)
        if used:
            cost = sum(r.get("cost_usd") or 0 for r in runs.values())
            self._record(rid, text, {**runs[used], "ms": ms, "cost_usd": round(cost, 6)},
                         hedged=used != self.model, **detail)
            return runs[used]["out"]
        self._record(rid, text, {**primary, "ms": ms}, backup_reason=backup.get("reason"), **detail)
        return None

    async def translate_stream(self, rid: str, text: str, src: str = "Japanese", dst: str = "English"):
        """部分訳(累積)を yield する。最後に yield した値が確定訳。Claude が途中で失敗したら PLaMo の部分訳が続く。"""
        box: dict = {}
        async for partial in self._claude_stream(rid, text, src, dst, box):
            yield partial
        if box["reason"]:
            async for partial in self.fallback.translate_stream(rid, text, src, dst):
                yield partial

    async def translate(self, rid: str, text: str, src: str = "Japanese", dst: str = "English") -> str:
        """一括で訳す(送信の読み上げ用)。控えのモデルがあれば主と同時に頼む。Claude で訳せなければ PLaMo。"""
        if self.backup_model:
            out = None if self._skipped(rid, text) else await self._translate_hedged(rid, text, src, dst)
            return out if out is not None else await self.fallback.translate(rid, text, src, dst)
        box: dict = {}
        out = ""
        async for out in self._claude_stream(rid, text, src, dst, box):
            pass
        return await self.fallback.translate(rid, text, src, dst) if box["reason"] else out

    def wake(self) -> None:
        self.fallback.wake()

    def start_keepwarm(self, check_s: float, idle_s: float, src: str, dst: str) -> None:
        self.fallback.start_keepwarm(check_s, idle_s, src, dst)

    async def stop_keepwarm(self) -> None:
        await self.fallback.stop_keepwarm()
