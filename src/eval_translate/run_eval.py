"""eval_translate/run_eval.py — 訳の質の比較(PLAN_v2 §7b の Step 1)。

同じ文を、今の PLaMo と Claude(Opus 5.5・Sonnet 5.5・Haiku 5.5 × 文脈なし・あり)で訳し、訳文・最初の
トークンまでの時間・全体の時間・トークン数(費用の見込み)を logs/eval_translate/<run_id>.jsonl に書く。
訳の良し悪しは make_rating_page.py の比較ページで 利用者が付ける(どの訳か伏せて並べる)。

向き(--direction):
  ja-en … 相手に聞こえる英語(自分の日本語 → 英語の読み上げ)。入力の組は bench と phrases
  en-ja … 字幕(相手の英語 → 日本語)。入力の組は subs
入力の組:
  bench   … ベンチの会話の日本語 24 文(src/bench/audio/timeline.json・作り例)。連続した会話なので、文脈ありの
            条件では直前の 6 発言(相手の英語も含む)を渡す
  phrases … よく使う言い回し(phrases_ja.txt)。文脈ありの条件では話し手のプロフィールと会議の目的・用語集だけ
  subs    … ベンチの会話の相手の英語 24 文(同じ timeline.json)。文脈ありは未対応
  ベンチの台本は時間を埋めるために繰り返している(49 番目の発言から 2 周目)。2 周目の同じ文は除く(10/25 の
  2 回目から。1 回目 20261025_074426 は bench 32 文で、25〜32 番は 1〜8 番と同じ文)。
Claude の指示文(--prompt):
  ja-en … v1 = 自然な言い回しへ組み替える(1 回目)。v2 = 短く・言われたことだけ(1 回目の評価で、短い訳・足さない訳が
          選ばれ、言い換えや付け足しは選ばれなかったため)
  en-ja … v1 だけ

使い方(プロジェクトの直下で):
    spikes/.venv-ml/bin/python src/eval_translate/run_eval.py [--direction ja-en|en-ja] [--prompt v1|v2]
        [--only plamo|claude] [--models opus,sonnet,haiku] [--contexts both|noctx|ctx] [--limit N]
API キーは ~/.transcue/anthropic_api_key(1 行)か、環境変数 ANTHROPIC_API_KEY。
Claude の呼び出しは 1 件ずつ順番に行う(会議と同じく、並列にしない遅延を測る)。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# huggingface のオフライン設定は mlx_lm の import より前に(先に import すると PLaMo を ~/.cache へ落とす。2026-10-25)
os.environ.setdefault("HF_HOME", str(ROOT / "models-cache" / "huggingface"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("ZRT_WIRE_WEIGHTS", "0")

import argparse  # noqa: E402
import asyncio  # noqa: E402
import gc  # noqa: E402
import json  # noqa: E402
import statistics  # noqa: E402
import time  # noqa: E402
import tomllib  # noqa: E402
from collections import defaultdict  # noqa: E402

sys.path.insert(0, str(ROOT / "src"))
HERE = Path(__file__).resolve().parent
OUT = ROOT / "logs" / "eval_translate"
STATE = Path.home() / ".transcue"
KEY_FILE = STATE / "anthropic_api_key"
CONTEXT_TURNS = 6

# 遅延を優先した設定(訳は短い)。Opus 5.5 は思考を切れないので effort だけ下げる。
# 評価では fallbacks(断られたときに別のモデルで続ける)を付けない: 付けるとモデルごとの比較が混ざる。断られたら記録する
MODELS = {
    "opus": {"id": "claude-opus-5-5", "thinking": None},
    "sonnet": {"id": "claude-sonnet-5-5", "thinking": {"type": "between_tools"}},
    "haiku": {"id": "claude-haiku-5-5", "thinking": {"type": "disabled"}},
}
# 費用の見込み(USD / 100 万トークン: 入力・出力・キャッシュ読み)。キャッシュ書きは入力の 1.25 倍。Haiku のキャッシュ読みは推定
PRICE = {"claude-opus-5-5": (4.0, 20.0, 0.20), "claude-sonnet-5-5": (2.0, 10.0, 0.20),
         "claude-haiku-5-5": (0.10, 0.50, 0.01)}

SYSTEM_BASE = """You are the user's interpreter in a live business meeting held over Zoom. The user speaks Japanese. \
You turn each Japanese utterance into the English that is read aloud (text-to-speech) to the other participants as the \
user's own words.

How to translate:
- Convey what the user means, the way a fluent bilingual professional would say it in English. Do not translate word \
for word: restructure sentences, choose natural business English, and use idiomatic equivalents for Japanese set \
phrases (for example よろしくお願いします, お世話になっております, 持ち帰って検討します), or drop them when an English \
speaker would not say anything there.
- Keep the user's intent, level of certainty, and politeness: polite but not stiff, with no added flattery.
- Speak as the user, in the first person. Never address the user or comment on the text.
- Japanese often omits subjects and objects. Recover them from the conversation when it is given; when it is unclear, \
choose the most neutral reading.
- The text comes from speech recognition: ignore fillers (えー, あの, ん) and fix obvious recognition errors. If the \
text is a fragment cut off mid-sentence, translate only what is there, naturally, without inventing the rest.
- Keep it short enough to say aloud comfortably, usually one or two sentences.

The user message may contain <conversation_so_far> with the most recent turns ([あなた] is the user, [相手] is the \
other side). Use it only to understand the context. Translate only the text inside <translate>, and output only the \
English to be spoken: no quotes, notes, alternatives, or Japanese."""

# v2: 1 回目の評価(20261025_074426)で、短い訳・言われたことだけの訳が選ばれ、言い換えや付け足しは選ばれなかった。
# 例に評価の文(よろしくお願いします等)は使わない(答えを教えることになるため)
SYSTEM_JA_EN_V2 = """You are the user's interpreter in a live business meeting held over Zoom. The user speaks Japanese. \
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

# 字幕(相手の英語 → 日本語)。「直訳すぎる」は字幕のこと(10/25 利用者)
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

PROMPTS = {("ja-en", "v1"): SYSTEM_BASE, ("ja-en", "v2"): SYSTEM_JA_EN_V2, ("en-ja", "v1"): SYSTEM_EN_JA}
LANGS = {"ja-en": ("Japanese", "English"), "en-ja": ("English", "Japanese")}


def load_items(direction: str, limit: int | None) -> list[dict]:
    segs = json.loads((ROOT / "src" / "bench" / "audio" / "timeline.json").read_text(encoding="utf-8"))["segments"]
    side, set_name = ("ja", "bench") if direction == "ja-en" else ("en", "subs")
    items: list[dict] = []
    seen: set[str] = set()
    for i, s in enumerate(segs):
        # 台本の 2 周目の同じ文は除く(直前の会話も 1 周目のものだけになる)
        if s["side"] != side or s["text"] in seen:
            continue
        seen.add(s["text"])
        hist = [("あなた" if p["side"] == "ja" else "相手", p["text"]) for p in segs[max(0, i - CONTEXT_TURNS):i]]
        items.append({"id": f"{set_name}-{len(items) + 1:02d}", "set": set_name, "src": s["text"], "history": hist})
    if direction == "ja-en":
        phrases = [ln.strip() for ln in (HERE / "phrases_ja.txt").read_text(encoding="utf-8").splitlines()]
        phrases = [ln for ln in phrases if ln and not ln.startswith("#")]
        items += [{"id": f"phrase-{n:02d}", "set": "phrases", "src": ja, "history": []}
                  for n, ja in enumerate(phrases, 1)]
    if limit:
        # 組ごとに先頭 N 文(動作確認用)
        by_set: dict[str, list[dict]] = defaultdict(list)
        for it in items:
            by_set[it["set"]].append(it)
        items = [it for its in by_set.values() for it in its[:limit]]
    return items


def context_block(ctx: dict, set_name: str) -> str:
    lines = ["<speaker_profile>", ctx["speaker"]["profile"].strip(), "</speaker_profile>",
             "<meeting>", ctx["meeting"][set_name]["purpose"].strip(), "</meeting>"]
    glossary = ctx.get("glossary") or {}
    if glossary:
        lines += ["<preferred_translations>"] + [f"{ja} → {en}" for ja, en in glossary.items()] + \
                 ["</preferred_translations>"]
    return "\n".join(lines)


def request(item: dict, with_ctx: bool, ctx: dict, base: str) -> tuple[list[dict], str]:
    """system(キャッシュする固定部分)と user(毎回変わる部分)を組み立てる。base は指示文(PROMPTS)。"""
    if with_ctx:
        system = [{"type": "text", "text": base},
                  {"type": "text", "text": context_block(ctx, item["set"]), "cache_control": {"type": "ephemeral"}}]
    else:
        system = [{"type": "text", "text": base, "cache_control": {"type": "ephemeral"}}]
    parts = []
    if with_ctx and item["history"]:
        turns = "\n".join(f"[{who}] {text}" for who, text in item["history"])
        parts.append(f"<conversation_so_far>\n{turns}\n</conversation_so_far>")
    parts.append(f"<translate>{item['src']}</translate>")
    return system, "\n".join(parts)


async def claude_translate(client, mkey: str, system: list[dict], user: str) -> dict:
    m = MODELS[mkey]
    kw: dict = {"model": m["id"], "max_tokens": 1024, "system": system,
                "messages": [{"role": "user", "content": user}], "output_config": {"effort": "low"}}
    if m["thinking"]:
        kw["thinking"] = m["thinking"]
    t0 = time.perf_counter()
    ttft = None
    parts: list[str] = []
    async with client.messages.stream(**kw) as stream:
        async for text in stream.text_stream:
            if ttft is None:
                ttft = (time.perf_counter() - t0) * 1000
            parts.append(text)
        final = await stream.get_final_message()
    total = (time.perf_counter() - t0) * 1000
    u = final.usage
    usage = {"input": u.input_tokens, "cache_write": u.cache_creation_input_tokens or 0,
             "cache_read": u.cache_read_input_tokens or 0, "output": u.output_tokens}
    p_in, p_out, p_cr = PRICE[m["id"]]
    cost = (usage["input"] * p_in + usage["cache_write"] * p_in * 1.25 + usage["cache_read"] * p_cr
            + usage["output"] * p_out) / 1e6
    return {"out": "".join(parts).strip(), "ttft_ms": round(ttft, 1) if ttft is not None else None,
            "total_ms": round(total, 1), "usage": usage, "cost_usd": round(cost, 6),
            "stop_reason": final.stop_reason, "request_id": getattr(final, "_request_id", None)}


async def run_plamo(items: list[dict], direction: str, emit) -> None:
    from translate import Translator      # 本番と同じ Translator(local・重みは固定しない)
    cfg = tomllib.loads((ROOT / "src" / "config.toml").read_text(encoding="utf-8"))
    cfg["_root"] = str(ROOT)
    src, dst = LANGS[direction]
    tr = Translator(cfg)
    t = time.perf_counter()
    # 受信側(receive.py)と同じく、字幕の向きは英文で温める
    await tr.load(warmup_texts=Translator.WARMUP_EN_JA if direction == "en-ja" else None, src=src, dst=dst)
    print(f"[eval] PLaMo 読み込み {time.perf_counter() - t:.1f} s", file=sys.stderr)
    for it in items:
        t0 = time.perf_counter()
        # 本番の Translator.translate() が中で呼ぶのと同じ処理(初トークンの ms も受け取るため直接呼ぶ)
        out, first_ms = await asyncio.to_thread(tr._translate_sync, it["src"], src, dst)
        emit(it, "plamo", None, False, {"out": out, "ttft_ms": round(first_ms, 1) if first_ms else None,
                                        "total_ms": round((time.perf_counter() - t0) * 1000, 1)})
    del tr
    gc.collect()


async def run_claude(items: list[dict], models: list[str], conds: tuple[bool, ...], ctx: dict, base: str,
                     emit) -> None:
    import anthropic
    key = None if os.environ.get("ANTHROPIC_API_KEY") else KEY_FILE.read_text(encoding="utf-8").strip()
    client = anthropic.AsyncAnthropic(api_key=key, timeout=60.0, max_retries=2)
    for mkey in models:
        for with_ctx in conds:
            name = f"claude:{mkey}:{'ctx' if with_ctx else 'noctx'}"
            # 1 回目は接続とキャッシュの準備(記録しない)。会議中も同じ固定部分を使い回すので、温まった状態を測る
            system, user = request(items[0], with_ctx, ctx, base)
            try:
                await claude_translate(client, mkey, system, user)
            except anthropic.APIError as e:
                print(f"[eval] {name} の準備で失敗: {e}", file=sys.stderr)
            for it in items:
                system, user = request(it, with_ctx, ctx, base)
                try:
                    res = await claude_translate(client, mkey, system, user)
                except anthropic.APIError as e:
                    res = {"out": "", "error": f"{type(e).__name__}: {getattr(e, 'message', e)}"}
                emit(it, name, MODELS[mkey]["id"], with_ctx, res)
            print(f"[eval] {name} 完了", file=sys.stderr)


def pct(v: list[float], q: float) -> float | None:
    v = sorted(x for x in v if x is not None)
    return round(v[min(int(len(v) * q), len(v) - 1)]) if v else None


def summarize(rows: list[dict]) -> str:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["system"]].append(r)
    lines = ["| 方式 | 件数 | 最初のトークン p50 / p90 (ms) | 全体 p50 / p90 / 最大 (ms) | 100 節あたりの費用 | 失敗・断り |",
             "|---|---:|---|---|---:|---:|"]
    for name, rs in by.items():
        ok = [r for r in rs if not r.get("error")]
        ttft = [r.get("ttft_ms") for r in ok]
        total = [r.get("total_ms") for r in ok]
        cost = [r.get("cost_usd", 0.0) for r in ok]
        bad = sum(1 for r in rs if r.get("error") or r.get("stop_reason") == "refusal")
        per100 = f"${statistics.mean(cost) * 100:.3f}" if cost and name != "plamo" else "—"
        top = max((t for t in total if t is not None), default=None)
        lines.append(f"| {name} | {len(rs)} | {pct(ttft, .5)} / {pct(ttft, .9)} | {pct(total, .5)} / {pct(total, .9)} / "
                     f"{round(top) if top is not None else None} | {per100} | {bad} |")
    return "\n".join(lines)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--direction", choices=["ja-en", "en-ja"], default="ja-en")
    ap.add_argument("--prompt", choices=["v1", "v2"], default="v1", help="Claude の指示文の版")
    ap.add_argument("--only", choices=["plamo", "claude"])
    ap.add_argument("--models", default="opus,sonnet,haiku")
    ap.add_argument("--contexts", choices=["both", "noctx", "ctx"], default="both", help="Claude の文脈の条件")
    ap.add_argument("--limit", type=int, help="組ごとに先頭 N 文だけ(動作確認用)")
    args = ap.parse_args()
    base = PROMPTS.get((args.direction, args.prompt))
    conds = {"both": (False, True), "noctx": (False,), "ctx": (True,)}[args.contexts]
    if base is None:
        print(f"{args.direction} に指示文 {args.prompt} はありません。", file=sys.stderr)
        return 2
    if args.direction == "en-ja" and True in conds and args.only != "plamo":
        print("字幕(en-ja)の文脈ありは未対応です。--contexts noctx を付けてください。", file=sys.stderr)
        return 2
    try:
        state = json.loads((STATE / "watcher.json").read_text())
    except (OSError, ValueError):
        state = {}
    if state.get("state") == "ACTIVE":
        print("会議中(watcher が ACTIVE)なので始めません。", file=sys.stderr)
        return 3
    if args.only != "plamo" and not (os.environ.get("ANTHROPIC_API_KEY") or KEY_FILE.exists()):
        print(f"API キーがありません: {KEY_FILE}(1 行)か環境変数 ANTHROPIC_API_KEY", file=sys.stderr)
        return 2
    items = load_items(args.direction, args.limit)
    ctx = tomllib.loads((HERE / "context.toml").read_text(encoding="utf-8"))
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    run_id = time.strftime("%Y%m%d_%H%M%S")
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{run_id}.jsonl"
    rows: list[dict] = []
    fh = path.open("w", encoding="utf-8")

    def emit(item: dict, system: str, model: str | None, with_ctx: bool, res: dict) -> None:
        row = {"run_id": run_id, "item_id": item["id"], "set": item["set"], "direction": args.direction,
               "prompt": args.prompt if model else None, "src": item["src"], "history": item["history"],
               "system": system, "model": model, "context": with_ctx, **res}
        rows.append(row)
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        fh.flush()

    print(f"[eval] {run_id}: {args.direction} {len(items)} 文 / Claude "
          f"{f'{models} {args.contexts} 指示文 {args.prompt}' if args.only != 'plamo' else '—'}", file=sys.stderr)
    try:
        if args.only != "claude":
            await run_plamo(items, args.direction, emit)
        if args.only != "plamo":
            await run_claude(items, models, conds, ctx, base, emit)
    finally:
        fh.close()
    sets = " と ".join(dict.fromkeys(it["set"] for it in items))
    md = (f"# 訳の比較 {run_id}\n\n- 向き: {args.direction}・Claude の指示文: {args.prompt}\n"
          f"- 文: {len(items)}({sets})\n- 結果: {path}\n\n{summarize(rows)}\n")
    (OUT / f"{run_id}_summary.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
