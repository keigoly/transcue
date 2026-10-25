"""Step 3(効果測定)— 会議ログを改修前の基準値と突き合わせる。

Step 1 で確定した基準値(2026-09-21 改修前)と、
改修後の実会議ログを同じ物差しで比べる。**推測せず数値で判断するための道具。**

使い方:
    spikes/.venv-ml/bin/python src/analyze_step3.py            # logs/ の全ログ
    spikes/.venv-ml/bin/python src/analyze_step3.py --since 2026-09-22
    spikes/.venv-ml/bin/python src/analyze_step3.py logs/pipeline1f_20260922_*.jsonl

見るべき順:
  1. 節の完結性   … 基準 未完結 73.4%(送信)/ 13.3%(受信)より下がったか
  2. 無駄な翻訳   … 基準 58%(送信)/ 12%(受信)より下がったか
  3. e2e レイテンシ … 悪化していないか(送信 p50 1.5〜1.8s が目安)
  4. 節の出方     … 節数が極端に少なければ切れていない(Step 2 で踏んだ失敗)
"""
from __future__ import annotations

import argparse
import glob
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from segmenter import _INCOMPLETE_TAILS, tail_kind  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# 改修(Step 2-b)が入った日。これより前のログは「改修前」なので混ぜて読むと誤る。
FIX_DATE = "20260921"

# Step 1 で確定した改修前の基準値(docs/history/src_DEVELOPMENT_history.md「改修前の基準値」)
BASELINE = {
    "send": {"label": "送信(日→英)", "clauses": 304, "incomplete_pct": 73.4, "waste_pct": 58.0},
    "recv": {"label": "受信(英→日)", "clauses": 210, "incomplete_pct": 13.3, "waste_pct": 12.0},
}


def _load(paths: list[str]) -> list[dict]:
    ev = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    ev.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return ev


def _pctl(v: list[float], q: float) -> float:
    s = sorted(v)
    return s[min(int(len(s) * q), len(s) - 1)]


def _arrow(now: float, base: float, lower_is_better: bool = True) -> str:
    d = now - base
    good = (d < 0) if lower_is_better else (d > 0)
    if abs(d) < 0.05:
        return "→ 横ばい"
    return f"{'✅' if good else '⚠️'} {d:+.1f}pt"


def report(paths: list[str], key: str) -> None:
    b = BASELINE[key]
    ev = _load(paths)
    segs = [e for e in ev if e.get("phase") == "segment"]
    if not segs:
        print(f"\n### {b['label']}: 該当ログなし")
        return
    trs = {e["rid"]: e for e in ev
           if e.get("phase") == "translate" and isinstance(e.get("ms"), (int, float))}
    e2e = [e["ms"] for e in ev
           if e.get("phase") in ("e2e", "e2e_recv", "e2e_recv_stream")
           and isinstance(e.get("ms"), (int, float))]
    summaries = [e for e in ev if e.get("phase") == "segmenter_trace_summary"]

    n = len(segs)
    kinds = Counter()
    ms_by = Counter()
    ex = defaultdict(list)
    for s in segs:
        # ログに tail_kind があればそれを使う(改修後)。無ければ今の分類器で当てる(改修前ログ)。
        k = s.get("tail_kind") or tail_kind(s.get("text", ""))
        kinds[k] += 1
        if len(ex[k]) < 2:
            ex[k].append(s.get("text", "")[:40])
        t = trs.get(s["rid"])
        if t:
            ms_by[k] += t["ms"]

    inc = sum(kinds[k] for k in _INCOMPLETE_TAILS)
    inc_pct = inc / n * 100
    tot_ms = sum(ms_by.values())
    waste_ms = sum(ms_by[k] for k in _INCOMPLETE_TAILS)
    waste_pct = (waste_ms / tot_ms * 100) if tot_ms else 0.0

    days = sorted({Path(p).name.split("_")[1] for p in paths})
    span = f"{days[0]}〜{days[-1]}" if days else "?"
    mixed = days and days[0] <= FIX_DATE <= days[-1] and days[0] != days[-1]
    print(f"\n{'=' * 74}\n### {b['label']}   節 {n} 件 / ログ {len(paths)} 本 ({span})\n{'=' * 74}")
    if mixed:
        print(f"  ⚠️ 改修日({FIX_DATE})をまたぐログが混ざっています。効果を見るなら")
        print(f"     --since で改修後だけに絞ってください(例: --since 2026-09-22)。")
    print(f"  {'末尾種別':<22}{'件数':>6}{'割合':>8}{'翻訳秒':>9}")
    for k, c in kinds.most_common():
        mark = "⚠️" if k in _INCOMPLETE_TAILS else "  "
        sample = f"   {ex[k][0]!r}" if ex[k] else ""
        print(f"  {mark}{k:<20}{c:>6}{c / n * 100:>7.1f}%{ms_by[k] / 1000:>8.1f}s{sample}")
    print(f"  {'-' * 70}")
    print(f"  ① 未完結な節   {inc:>4} 件 = {inc_pct:5.1f}%   "
          f"(基準 {b['incomplete_pct']}%)  {_arrow(inc_pct, b['incomplete_pct'])}")
    print(f"  ② 無駄な翻訳   {waste_ms / 1000:6.1f}s = {waste_pct:5.1f}%   "
          f"(基準 {b['waste_pct']}%)  {_arrow(waste_pct, b['waste_pct'])}")
    if e2e:
        print(f"  ③ e2e 遅延     p50 {statistics.median(e2e):.0f}ms / "
              f"p90 {_pctl(e2e, 0.9):.0f}ms  (n={len(e2e)})")
    print(f"  ④ 節の出方     {n} 件 / 基準 {b['clauses']} 件"
          f"{'   ⚠️ 極端に少ない = 切れていない疑い' if n < b['clauses'] * 0.15 else ''}")

    for s in summaries[-1:]:
        g = s.get("chunk_gap") or {}
        print(f"\n  最新 run の設定と実測:")
        print(f"    chunk_gap n={g.get('n')} p50={g.get('p50')}ms max={g.get('max')}ms")
        print(f"    閾値 文末={s.get('threshold_sentence_ms')} 完結={s.get('threshold_ms')} "
              f"未完結={s.get('threshold_incomplete_ms')} 繰越={s.get('carry_incomplete_tail')}")
        tbg = s.get("threshold_below_gap")
        if tbg:
            print("    ⚠️ threshold_below_gap: true = 完結の閾値がまだチャンク間ギャップより短い")
        print(f"    finalize 強制={s.get('final_forced')} 自動={s.get('final_auto')}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="対象ログ(省略時は logs/ 全体)")
    ap.add_argument("--since", help="YYYY-MM-DD 以降のログだけ")
    a = ap.parse_args()

    def pick(tag: str) -> list[str]:
        if a.files:
            return [f for f in a.files if tag in Path(f).name]
        fs = sorted(glob.glob(str(ROOT / "logs" / f"{tag}_*.jsonl")))
        if a.since:
            stamp = a.since.replace("-", "")
            fs = [f for f in fs if Path(f).name.split("_")[1] >= stamp]
        return fs

    send, recv = pick("pipeline1f") + pick("segmenter1c"), pick("receive2c")
    if not send and not recv:
        print("対象ログがありません。会議を 1 回行ってから実行してください。")
        return
    report(send, "send")
    report(recv, "recv")
    print("\n判断の目安: ① と ② が基準より下がり、③ が悪化していなければ改修は成功。")
    print("            ④ が極端に少なければ閾値が長すぎる(Step 2 で踏んだ失敗)。")


if __name__ == "__main__":
    main()
