"""セグメンター — 1-C。STT の partial/final ストリームを翻訳に渡す「節」に切る。

方式(ARCHITECTURE.md §5 / §9「ポーズ検出 + finalize(through:)」):
  - partial のテキストが更新されるたびに last_change を更新。
  - pending な発話があり、last_change から pause_finalize_ms 無音(=partial更新なし)が
    続いたら helper に finalize を送る → Swift が analyzer.finalize(through:) で綺麗な final を返す。
  - final(強制/自動どちらも)を節候補として確定。相槌・極短は除外、長節は分割。
全判定を metrics に記録(requestId 付き)。しきい値は config.toml [segmenter]。

## 2026-09-21 Step 1 計装(挙動不変・計測フィールドの追加のみ)

実走ログの分類で、送信方向(ja)は節の 98% が強制 finalize 由来・66% が句点で終わらず、
語の途中でも切れている(例: 'ブラックオフツーというゲー')ことが判明した。しきい値
(pause_finalize_ms=500)と finalize 条件を直す前に、まず以下を数値で可視化する:

  - `partial_delta`  : partial 更新間隔の分布(節ごと p50/max)。500ms が妥当かの根拠
  - `final_recv`     : final が強制由来か自動か / finalize 送信→final 受信の待ち /
                       強制時に pending_text が final で書き換わったか(再スコアの有無)
  - `pause_finalize` : 発火時の直近更新間隔・partial 回数・末尾種別(語中/助詞/文末)
  - `segment`        : 末尾種別と前節からの間隔(=結合すべきだったかの判定材料)
  - `segmenter_trace_summary` : run 全体の更新間隔分布と末尾種別の内訳

末尾種別 `tail_kind` は「どこで切れたか」を機械可読にするための分類:
  sentence_end(文末記号) / comma(読点) / particle(助詞=明確に未完結) /
  mid_word(語中の疑い) / func_word(英語の機能語) / en_other / other / empty

## 2026-10-25 Step 1 計装(繰り越し・挙動不変)

A0 の代行で、句点の無い切れ端が無音をまたいで 108 s 繰り越され、翻訳されないまま停止で失われた。
繰り越しから次の確定までは正しい繰り越しでも 2.5〜7.2 s かかる(次の発話を言い終えるまで待つ)ため、
経過時間だけでは区別できない。繰り越した後に話が再開したか・いつ再開したかを測る:

  - `carry_tail`  : 連なった回数 chain(前置した切れ端ごとまた繰り越すと増える)
  - `carry_join`  : 前置した時点の経過 age_ms と、話が再開するまでの resume_ms(フィラーは数えない。
                    None = 再開せずに確定が来た)
  - `carry_flush` : 前置せず単独で出した(reason=bye は停止時の取りこぼし防止)
  - `segmenter_trace_summary` : carry_resume_ms / carry_age_ms の分布と、停止で残った切れ端

## 2026-10-25 Step 2(繰り越しの待ちの上限・既定は無効)

Step 1 のベンチ(20261025_060324)で、正しい繰り越しは 1.0〜1.1 s で話が再開した(音声認識の更新間隔と同じ。
実マイクで見た更新間隔の最大は 1.9 s)。A0 の 3 件は 98 s 後・8 s 後・再開なし。そこで、繰り越した後
carry_max_wait_ms だけ話が再開しなければ、前置を待たずに単独の節として出す(`carry_flush` reason=timeout)。
0 = 無効。

単体実行(短時間ライブ確認。マイク窓は既定 25 秒で自動終了):
    source spikes/.venv-ml/bin/activate
    python src/segmenter.py 25
"""
from __future__ import annotations

import asyncio
import os
import re
import statistics
import sys
import time
import tomllib
from collections.abc import Callable
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import Metrics, new_rid  # noqa: E402
from stt_reader import STTHelper  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
_PUNCT = "。!?！?、,．."

# --- Step 1 計装: 末尾種別の判定 -------------------------------------------------
# ⚠️ STT は節の途中にも句点を打つ(実ログ: 'こんにちは。私は。' 'いろいろと。' '今ゲーム。')。
# そのため「末尾が句点か」では完結判定にならない。句読点を剥がした"芯"の終わり方で分類し、
# 句点の有無は tail_punct として別に記録する(2 つは直交する事実)。
_SENT_END = "。．.!?！？"
_COMMA = "、,"
_TRIM = " \t。．.!?！？、,・…\"'’”)）]】"
# 日本語: 芯がこれで終わったら文が続く(明確に未完結)。長いものから照合する。
_JA_PARTICLE = ("から", "まで", "より", "ので", "けど", "たり", "ながら",
                "は", "が", "を", "に", "へ", "と", "で", "も", "の", "や", "て", "し")
# 日本語: 芯がこれで終わったら文として完結している(高精度側に寄せた保守的なリスト)
_JA_END_RE = re.compile(
    r"(ございます|でしょう|ください|ません|でした|ました|です|ます|である|した|する|ある|いる|"
    r"なる|できる|思う|だ|ね|よ)\Z")
# 英語: 芯がこれで終わったら文が続く
_EN_FUNC = {"and", "or", "but", "to", "of", "in", "on", "at", "for", "with", "the", "a", "an",
            "that", "is", "are", "was", "were", "by", "from", "as", "so", "if", "when", "will",
            "would", "can", "could", "about", "into", "than", "then", "we", "i", "it", "he",
            "she", "they", "you", "this", "these", "let", "me"}
_ASCII_RE = re.compile(r"[\x00-\x7f]+\Z")
_JA_CHAR_RE = re.compile(r"[ぁ-んァ-ヶ一-龥ー]\Z")


# 無音や息で音声認識が出すフィラー(「あ」「えー」「ん」「っ」)だけのテキスト。「い」「う」「お」は「いい」「うん」の
# ような本物の語になるので含めない。2026-10-25 のベンチで、無音 247 s の間に約 10.8 s ごとの「あ」が繰り越しで
# 23 文字溜まり、次の文の頭に付いた(v2 A2 の Step 3 で対処)。
_FILLER_RE = re.compile(r"[あぁえぇんっアァエェンッー〜~・…、。,.\s]+")


def is_filler(text: str) -> bool:
    """フィラーだけ(または空)か。繰り越さない・翻訳しない・GPU を起こさない判定に使う。"""
    t = (text or "").strip()
    return not t or bool(_FILLER_RE.fullmatch(t))


def tail_punct(text: str) -> bool:
    """末尾に文末記号が付いているか(完結性とは独立の事実として記録する)。"""
    t = (text or "").strip()
    return bool(t) and t[-1] in _SENT_END


def tail_kind(text: str) -> str:
    """節の"芯"(句読点を剥がした本体)の終わり方を分類する(計測専用・挙動には影響しない)。

    ja_end     … 用言/丁寧形で終わる = 完結
    particle   … 助詞で終わる       = 明確に未完結
    mid_word   … 助詞でも用言でもない仮名/漢字で終わる = 語中で切れた疑い
    func_word  … 英語の機能語で終わる = 明確に未完結
    en_other   … 英語のそれ以外
    empty      … 芯が無い(記号のみ)
    """
    t = (text or "").strip()
    core = t.strip(_TRIM)
    if not core:
        return "empty"
    if _ASCII_RE.match(core):  # 英語(受信方向)
        words = re.sub(r"[^\w\s\-]", "", core).strip().lower().split()
        return "func_word" if words and words[-1] in _EN_FUNC else "en_other"
    if _JA_END_RE.search(core):
        return "ja_end"
    for p in _JA_PARTICLE:
        if core.endswith(p):
            return "particle"
    if _JA_CHAR_RE.search(core):
        return "mid_word"
    return "other"


# partial 更新は二峰性になる(2026-09-21 実測・受信 file 経路):
#   ・同一音声チャンクの中での連続更新 … 1ms 未満(p50=0.3ms)
#   ・次の音声チャンクまでの間隔     … 約 960ms(p90=959 / max=965)
# 単一の p50/p90 では実態を表せないため、_MICRO_MS 未満の連続更新と、それ以上の
# 「チャンク間ギャップ」を分けて記録する。pause_finalize_ms が比較すべき相手は後者。
# 送信 ja=500ms がこのギャップより短ければ、発話のポーズではなくチャンクの切れ目で
# 発火してしまう(= 文中・語中での強制切断の疑い)。Step 2 の判断材料。
_MICRO_MS = 50.0

# Step 2(2026-09-21): この末尾で切ると文/語の途中で断ち切ることになる種別。
# 該当する間は pause_finalize_incomplete_ms まで finalize を待つ(下の _watch)。
_INCOMPLETE_TAILS = ("particle", "mid_word", "func_word", "empty")


# 文末記号の並び。⚠️ ASCII のピリオドは**数字に挟まれていない**ものだけを文末とみなす。
# そうしないと実測値の '1947年に 4.54だった' が '4.' と '54だった' に割れる(2026-09-21 実測)。
_SENT_END_RE = re.compile(r"[。．!?！？]+|(?<!\d)\.+(?!\d)")


def split_sentences(text: str) -> list[str]:
    """文末記号の直後で割る(長さに関係なく常に割る)。

    Step 2-b の要。finalize の応答は要求時点より先まで確定するため、節の末尾に
    次の文の頭がはみ出す(実測: '自然なら数がいる。' と判断 → '…いる。楽しみなら' が返る)。
    文単位に割っておけば、完全な文は完全な文のまま翻訳へ渡せる。
    """
    out, start = [], 0
    for m in _SENT_END_RE.finditer(text):
        piece = text[start:m.end()].strip()
        if piece:
            out.append(piece)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _dist(values: list[float]) -> dict:
    """計測値の分布(件数/p50/p90/max)。空なら None 埋め。"""
    if not values:
        return {"n": 0, "p50": None, "p90": None, "max": None}
    v = sorted(values)
    return {"n": len(v),
            "p50": round(statistics.median(v), 1),
            "p90": round(v[min(int(len(v) * 0.9), len(v) - 1)], 1),
            "max": round(v[-1], 1)}


class Segmenter:
    def __init__(self, helper: STTHelper, cfg: dict, m: Metrics):
        self.helper = helper
        self.m = m
        seg = cfg["segmenter"]
        self.backchannel = set(seg.get("backchannel", []))
        self.pause_ms = int(seg["pause_finalize_ms"])
        # Step 2: 末尾が明らかに未完結(助詞/語中/機能語/記号のみ)なら、この長さまで待つ。
        # 未設定なら pause_ms と同じ = 従来挙動(後方互換)。
        self.pause_incomplete_ms = int(seg.get("pause_finalize_incomplete_ms", self.pause_ms))
        # Step 2-a: 末尾に文末記号が付いていれば、そこは**安全な切り所**なので速く切る。
        # チャンク間ギャップ(~1秒)と短い発話ポーズは時間だけでは区別できないため、
        # 「時間で待つ」のではなく「切ってよい形になったら切る」に寄せるための段。
        self.pause_sentence_ms = int(seg.get("pause_finalize_sentence_ms", self.pause_ms))
        # Step 2: 監視の刻み。旧実装は pause_ms/2000 秒(=閾値の半分)で、閾値 500ms に対し
        # 実測 idle_ms p50=639ms と 139ms の行き過ぎがあった。固定の細かい刻みにして削る。
        self.poll_ms = float(seg.get("finalize_poll_ms", 100))
        self.max_chars = int(seg["max_clause_chars"])
        self._current = ""
        self._last_change = time.perf_counter()
        self._pending = False
        # v2 A2-2: 節の中で最初の、フィラーでない partial(= 話し始め)で呼ぶ。翻訳・TTS が GPU を起こすのに使う。
        # 既定は何もしない。_spoke は「この節では呼んだ」の印(final で戻す)。
        self.on_speech: Callable[[], None] | None = None
        self._spoke = False
        self._finalize_sent = False
        self._out: asyncio.Queue = asyncio.Queue()
        # --- Step 1 計装用の状態(挙動には影響しない) ---
        self._deltas: list[float] = []        # 現在の節の partial 更新間隔(全て)
        self._all_deltas: list[float] = []    # run 全体(全て)
        self._gaps: list[float] = []          # 現在の節のチャンク間ギャップ(>= _MICRO_MS)
        self._all_gaps: list[float] = []      # run 全体のギャップ
        self._finalize_t: float | None = None  # finalize を送った時刻
        self._pending_at_finalize = ""         # finalize 送信時点の partial
        self._prev_emit_t: float | None = None  # 直前に節を出した時刻
        # ⚠️ _finalize_sent は partial 到着で False に戻る(再発火抑止用・挙動上正しい)。
        #    そのため「この節で finalize を要求したか」の計測には使えない。節ごとに粘着する
        #    別フラグを持つ(2026-09-21: 5 回発火を forced=1 と誤記録していた欠陥の修正)。
        self._finalize_req = False
        self._finalize_req_n = 0               # この節で finalize を要求した回数
        # Step 2-b: 末尾の未完結な切れ端を次の節へ繰り越す。実測で、はみ出した部分は
        # 次の発話の先頭そのものだったため、前置すると文が正しく復元される
        # (例: '楽しみなら' + '1人に集中する。' → '楽しみなら1人に集中する。')。
        # 受信(en)は挙動を変えないため既定 False。
        self.carry_tail = bool(seg.get("carry_incomplete_tail", False))
        self._carry = ""
        # 繰り越しの計測(2026-10-25 Step 1・挙動は不変)。A0 の代行で、句点の無い切れ端が無音をまたいで 108 s
        # 繰り越され(前置した結果をまた繰り越して 3 回連なった)、翻訳されないまま停止で失われた。次の発話の頭
        # (正しい繰り越し)と発話の終わりを分けるため、繰り越してから話が再開するまでの時間を測る。
        self._carry_t = 0.0                         # 今の切れ端を繰り越した時刻
        self._carry_first_t = 0.0                   # 連なりの最初の繰り越しの時刻
        self._carry_chain = 0                       # 連なった回数(1 = 連なっていない)
        self._carry_resume_ms: float | None = None  # 繰り越し後、最初のフィラーでない partial まで(None = 再開していない)
        self._carry_resumes: list[float] = []       # run 全体: 前置された繰り越しの resume_ms
        self._carry_ages: list[float] = []          # run 全体: 繰り越し → 前置されるまで
        # Step 2(2026-10-25): 繰り越しの待ちの上限。繰り越した後この時間だけ話が再開しなければ(フィラーは数えない)、
        # 切れ端は次の発話の頭ではなく発話の終わりなので、前置を待たずに単独の節として出す(_watch)。
        # 0 = 無効(従来どおり次の確定まで待つ)。ベンチは ZRT_CARRY_MAX_WAIT_MS で条件を明示する。
        self.carry_max_wait_ms = float(os.environ.get("ZRT_CARRY_MAX_WAIT_MS", seg.get("carry_max_wait_ms", 0)))
        self._carry_timeouts = 0
        self._tail_counts: dict[str, int] = {}
        self._forced_n = 0
        self._auto_n = 0

    def _split(self, text: str) -> list[str]:
        if len(text) <= self.max_chars:
            return [text]
        parts, buf = [], ""
        for ch in text:
            buf += ch
            if ch in _PUNCT or len(buf) >= self.max_chars:
                parts.append(buf)
                buf = ""
        if buf:
            parts.append(buf)
        return [p.strip() for p in parts if p.strip()]

    async def _emit(self, text: str, ev: dict) -> None:
        had_carry = bool(self._carry)
        if had_carry:
            # Step 1 計装: 前置する時点の経過と、その間に話が再開していたか(None = 再開せずに確定が来た)
            now = time.perf_counter()
            age_ms = round((now - self._carry_t) * 1000, 1)
            self._carry_ages.append(age_ms)
            if self._carry_resume_ms is not None:
                self._carry_resumes.append(self._carry_resume_ms)
            self.m.emit("STT", "carry_join", chars=len(self._carry), age_ms=age_ms,
                        resume_ms=self._carry_resume_ms, chain=self._carry_chain,
                        chain_age_ms=round((now - self._carry_first_t) * 1000, 1))
        text = (self._carry + text).strip()
        self._carry = ""
        pieces = split_sentences(text) if self.carry_tail else [text]
        if self.carry_tail and pieces and not tail_punct(pieces[-1]):
            # 末尾が文として閉じていない = 次の発話の頭。繰り越して次に前置する。
            # ただし溜め込みすぎないよう、max_chars を超えたらそのまま出す。
            tail = pieces[-1]
            if is_filler(tail):
                # フィラーだけの切れ端は次の発話の頭ではない。繰り越すと無音の間に溜まるので捨てる。
                pieces.pop()
                self.m.emit("STT", "drop", reason="filler_tail", text=tail)
            elif len(tail) <= self.max_chars:
                pieces.pop()
                self._carry = tail
                # 前置した切れ端ごとまた繰り越すなら連なりが続く(途中に文の終わりがあれば、前の切れ端はそこで出ている)
                now = time.perf_counter()
                chained = had_carry and not pieces
                self._carry_chain = self._carry_chain + 1 if chained else 1
                if not chained:
                    self._carry_first_t = now
                self._carry_t = now
                self._carry_resume_ms = None
                self.m.emit("STT", "carry_tail", text=tail, chars=len(tail),
                            tail_kind=tail_kind(tail), chain=self._carry_chain)
        for piece in pieces:
            await self._emit_chunks(piece, ev)

    async def _emit_chunks(self, text: str, ev: dict) -> None:
        for chunk in self._split(text.strip()):
            rid = new_rid()
            drop = (not chunk) or (chunk in self.backchannel) or is_filler(chunk)
            # Step 1 計装: 末尾種別と前節からの間隔(結合すべきだったかの判定材料)
            now = time.perf_counter()
            gap_ms = round((now - self._prev_emit_t) * 1000, 1) if self._prev_emit_t else None
            tk = tail_kind(chunk)
            self._tail_counts[tk] = self._tail_counts.get(tk, 0) + 1
            self.m.emit(rid, "segment", text=chunk, chars=len(chunk), dropped=drop,
                        audio_start_s=ev.get("start_s"), audio_end_s=ev.get("end_s"),
                        tail_kind=tk, tail_punct=tail_punct(chunk), prev_gap_ms=gap_ms)
            self._prev_emit_t = now
            if drop:
                self.m.emit(rid, "drop", reason="filler" if is_filler(chunk) else "backchannel", text=chunk)
                continue
            await self._out.put((rid, chunk, time.perf_counter()))

    async def _consume(self) -> None:
        async for ev in self.helper.events():
            t = ev.get("type")
            if t == "partial":
                text = ev.get("text", "").strip()
                if text and text != self._current:
                    # 節の中で最初の、フィラーでない partial で 1 回だけ(無音中の「あ」では起こさない)
                    if self.on_speech and not self._spoke and not is_filler(text):
                        self._spoke = True
                        try:
                            self.on_speech()
                        except Exception:  # noqa: BLE001 — 起こし損ねても認識は止めない
                            pass
                    # Step 1 計装: 節の途中の更新間隔だけを拾う(節の先頭は間隔の意味がない)
                    now = time.perf_counter()
                    if self._pending:
                        d = (now - self._last_change) * 1000
                        self._deltas.append(d)
                        self._all_deltas.append(d)
                        if d >= _MICRO_MS:        # チャンク間ギャップだけを別に積む
                            self._gaps.append(d)
                            self._all_gaps.append(d)
                    # Step 1 計装(繰り越し): 繰り越してから最初の、フィラーでない partial = 話の再開
                    if self._carry and self._carry_resume_ms is None and not is_filler(text):
                        self._carry_resume_ms = round((now - self._carry_t) * 1000, 1)
                    self._current = text
                    self._last_change = now
                    self._pending = True
                    self._finalize_sent = False
            elif t == "final":
                text = ev.get("text", "").strip()
                # Step 1 計装: リセット前に「強制由来か」を捕まえる
                forced = self._finalize_req          # 粘着フラグで判定(_finalize_sent は不可)
                wait_ms = (round((time.perf_counter() - self._finalize_t) * 1000, 1)
                           if (forced and self._finalize_t) else None)
                pend = self._pending_at_finalize if forced else self._current
                d = _dist(self._deltas)
                g = _dist(self._gaps)
                self.m.emit("STT", "final_recv", forced=forced, finalize_wait_ms=wait_ms,
                            text=text, chars=len(text), tail_kind=tail_kind(text),
                            tail_punct=tail_punct(text),
                            pending_text=pend, rescored=(forced and text != pend),
                            partial_n=d["n"], delta_p50=d["p50"], delta_max=d["max"],
                            gap_n=g["n"], gap_p50=g["p50"], gap_max=g["max"],
                            finalize_req_n=self._finalize_req_n)
                if forced:
                    self._forced_n += 1
                else:
                    self._auto_n += 1
                self._deltas = []
                self._gaps = []
                self._finalize_req = False
                self._finalize_req_n = 0
                self._finalize_t = None
                self._pending_at_finalize = ""

                self._current = ""
                self._pending = False
                self._finalize_sent = False
                self._spoke = False
                if text:
                    await self._emit(text, ev)
            elif t == "ready":
                self.m.emit("STT", "stt_ready", locale=ev.get("locale"), dst_hz=ev.get("dst_hz"))
            elif t == "bye":
                break
        if self._carry:                      # 取りこぼし防止: 残った切れ端を最後に出す
            await self._flush_carry("bye")
        await self._out.put(None)

    async def _flush_carry(self, reason: str) -> None:
        """繰り越しを前置せず、単独の節として出す。"""
        leftover, self._carry = self._carry, ""
        self.m.emit("STT", "carry_flush", reason=reason, chars=len(leftover),
                    age_ms=round((time.perf_counter() - self._carry_t) * 1000, 1),
                    resume_ms=self._carry_resume_ms, chain=self._carry_chain)
        await self._emit_chunks(leftover, {})

    async def _watch(self) -> None:
        """ポーズを検出して finalize を要求する。

        Step 2(2026-09-21): 一律の閾値をやめ、**末尾の完結性で待ち時間を変える**。
        Step 1 実測で、旧 500ms は partial のチャンク間ギャップ(ja マイク実測
        p50=597ms / max=680ms)より短く、発話のポーズではなくチャンクの切れ目で
        毎回発火していた(idle_ms の 99.4% が閾値直上 300ms 幅に集中)。
        結果、節の 73.4% が助詞終わり・語中切断になり、翻訳時間の 58% を無駄にしていた。

        Step 2-a(同日・実測で再調整): 閾値を上げるだけでは解けないと判明した。
        ja マイク 2 回目の実測で chunk_gap は p50=977ms / max=1101ms(1 回目 n=4 の
        597/680 は標本が小さすぎた)。**チャンク間ギャップと短い発話ポーズは時間では
        区別できない**ため、待ちを伸ばすと今度は一切切れなくなり 12.5 秒が 1 節になった。
        そこで「時間で待つ」から「**切ってよい形になったら切る**」へ寄せ、3 段にする:

          - 末尾に文末記号(。.!?) … pause_sentence_ms で速く切る(安全な切り所)
          - 末尾が完結(用言終わり) … pause_ms
          - 末尾が未完結(助詞/語中) … pause_incomplete_ms まで待つ(続きを拾う)
        """
        interval = max(0.02, self.poll_ms / 1000)
        while True:
            await asyncio.sleep(interval)
            # Step 2(2026-10-25): 話が再開しないまま上限を超えた繰り越しは、発話の終わりとして単独で出す
            if (self.carry_max_wait_ms > 0 and self._carry and self._carry_resume_ms is None
                    and (time.perf_counter() - self._carry_t) * 1000 >= self.carry_max_wait_ms):
                self._carry_timeouts += 1
                await self._flush_carry("timeout")
            if self._pending and not self._finalize_sent:
                idle_ms = (time.perf_counter() - self._last_change) * 1000
                tk = tail_kind(self._current)
                tp = tail_punct(self._current)
                if tp:                                   # 文末記号 = 安全な切り所。速く切る
                    need, why = self.pause_sentence_ms, "sentence"
                elif tk in _INCOMPLETE_TAILS:            # 助詞/語中 = 切ると壊れる。待つ
                    need, why = self.pause_incomplete_ms, "incomplete"
                else:
                    need, why = self.pause_ms, "complete"
                if idle_ms >= need:
                    self._finalize_sent = True
                    self._finalize_req = True
                    self._finalize_req_n += 1
                    # Step 1 計装: 何を見て切ったのかを残す(末尾種別・直近間隔・partial 回数)
                    d = _dist(self._deltas)
                    g = _dist(self._gaps)
                    self._finalize_t = time.perf_counter()
                    self._pending_at_finalize = self._current
                    self.m.emit("STT", "pause_finalize", idle_ms=round(idle_ms),
                                pending_text=self._current, pending_chars=len(self._current),
                                tail_kind=tk, tail_punct=tp,
                                threshold_ms=self.pause_ms, required_ms=need, tier=why,
                                partial_n=d["n"], delta_p50=d["p50"], delta_max=d["max"],
                                gap_n=g["n"], gap_p50=g["p50"], gap_max=g["max"],
                                # ★ 閾値がチャンク間ギャップより短いと、発話のポーズではなく
                                #    チャンクの切れ目で発火する(語中切断の疑い)
                                below_gap=(g["p50"] is not None and self.pause_ms < g["p50"]))
                    await self.helper.finalize()

    def trace_summary(self) -> dict:
        """run 全体の計測サマリ(Step 1)。しきい値決定の根拠にする。"""
        g = _dist(self._all_gaps)
        return {"partial_delta": _dist(self._all_deltas),
                "chunk_gap": g,                      # ★ 閾値が比較すべき相手
                "threshold_ms": self.pause_ms,
                "threshold_sentence_ms": self.pause_sentence_ms,
                "threshold_incomplete_ms": self.pause_incomplete_ms,
                "poll_ms": self.poll_ms,
                "carry_incomplete_tail": self.carry_tail,
                "carry_max_wait_ms": self.carry_max_wait_ms,    # 0 = 無効
                "carry_timeouts": self._carry_timeouts,         # 上限を超えて単独で出した回数
                "carry_resume_ms": _dist(self._carry_resumes),  # 繰り越し → 話の再開(前置されたもの)
                "carry_age_ms": _dist(self._carry_ages),        # 繰り越し → 前置
                "carry_left_chars": len(self._carry),           # 停止で出せずに残った切れ端
                "carry_left_age_ms": (round((time.perf_counter() - self._carry_t) * 1000)
                                      if self._carry else None),
                "threshold_below_gap": (g["p50"] is not None and self.pause_ms < g["p50"]),
                "final_forced": self._forced_n,
                "final_auto": self._auto_n,
                "tail_kinds": dict(sorted(self._tail_counts.items(), key=lambda kv: -kv[1]))}

    async def clauses(self):
        """(rid, clause, clause_perf_t) を節ごとに yield。pipeline の segment 段と同じ契約。"""
        consumer = asyncio.create_task(self._consume())
        watcher = asyncio.create_task(self._watch())
        try:
            while True:
                item = await self._out.get()
                if item is None:
                    break
                yield item
        finally:
            watcher.cancel()
            consumer.cancel()
            # Step 1 計装: 停止時に必ずサマリを 1 行残す(途中終了でも取れるように finally)
            try:
                self.m.emit("SUMMARY", "segmenter_trace_summary", **self.trace_summary())
            except Exception:
                pass


def load_config() -> dict:
    with (Path(__file__).resolve().parent / "config.toml").open("rb") as f:
        cfg = tomllib.load(f)
    cfg["_root"] = str(ROOT)
    return cfg


async def _main() -> None:
    max_s = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0
    cfg = load_config()
    m = Metrics.from_config(cfg, run_tag="segmenter1c")
    helper_bin = str(ROOT / cfg["stt"]["helper_bin"])
    helper = STTHelper(helper_bin, cfg["stt"]["lang"],
                       on_diag=lambda d: m.emit("STT", "stt_diag", **d))
    print(f"[segmenter] listening {max_s:.0f}s … 話してみてください(節ごとに表示)", file=sys.stderr)
    await helper.start()
    seg = Segmenter(helper, cfg, m)
    clauses: list[str] = []

    async def consume():
        async for rid, clause, _t in seg.clauses():
            clauses.append(clause)
            print(f"  [clause {len(clauses):02d}] {clause}", file=sys.stderr)

    try:
        await asyncio.wait_for(consume(), timeout=max_s)
    except asyncio.TimeoutError:
        pass
    finally:
        await helper.stop()  # マイクを確実に解放
    m.emit("SUMMARY", "segmenter_summary", clauses=len(clauses))
    print(f"[segmenter] 停止(マイクOFF)。節数={len(clauses)}  log={m.path}", file=sys.stderr)
    m.close()


if __name__ == "__main__":
    asyncio.run(_main())
