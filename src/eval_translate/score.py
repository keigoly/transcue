"""eval_translate/score.py — 比較ページの評価を方式ごとに集計する(PLAN_v2 §7b の Step 1)。

ratings_<run_id>.json(比較ページの「評価を保存」)と <run_id>_cands.json(候補と方式の対応)から、方式ごとに
「いちばん良い訳」に選ばれた割合と「直訳・不自然」の印の割合を出す。同じ訳が複数の方式から出たときは、どの方式にも数える。
「偶然なら」は、文ごとに候補から 1 つを当てずっぽうで選んだときに選ばれる文数の見込み(候補が n 個なら 1/n ずつ)。
候補の数は文で違うので、選ばれた数はこれと比べて読む。
選ばれた訳が候補の中で最も短かった文の数も出す(長さは日本語は文字数、英語は語数。1 回目の評価で、短い訳ほど
選ばれていたため)。遅延と費用の表は run_eval.py の <run_id>_summary.md を見る。

使い方(プロジェクトの直下で):
    spikes/.venv-ml/bin/python src/eval_translate/score.py [run_id] [--ratings <path>]
    (--ratings を省くと ~/Downloads/ratings_<run_id>.json を読む)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "logs" / "eval_translate"


def size(text: str) -> int:
    """訳の長さ: 日本語(かな・漢字を含む)は文字数、英語は語数。"""
    if any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" for ch in text):
        return len(text.replace(" ", ""))
    return len(text.split())


def main() -> int:
    argv = sys.argv[1:]
    ratings_path = Path(argv[argv.index("--ratings") + 1]).expanduser() if "--ratings" in argv else None
    pos = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] != "--ratings")]
    run_id = pos[0] if pos else sorted(p.stem for p in OUT.glob("*.jsonl"))[-1]
    ratings_path = ratings_path or Path.home() / "Downloads" / f"ratings_{run_id}.json"
    cands = json.loads((OUT / f"{run_id}_cands.json").read_text(encoding="utf-8"))
    ratings = json.loads(ratings_path.read_text(encoding="utf-8"))["ratings"]
    rows = [json.loads(ln) for ln in (OUT / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    sets = {r["item_id"]: r["set"] for r in rows}
    outs = {(r["item_id"], r["system"]): " ".join((r.get("out") or "").split()) for r in rows}

    stat: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: {"rated": 0, "best": 0, "bad": 0, "chance": 0.0})
    short: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0])   # 範囲 → [最も短い訳が選ばれた文, 偶然なら, 文]
    for item_id, r in ratings.items():
        if not r.get("best") or item_id not in cands:
            continue
        cmap = cands[item_id]
        systems = {s for ss in cmap.values() for s in ss}
        lens = {cid: size(outs.get((item_id, ss[0]), "")) for cid, ss in cmap.items()}
        lo = min(lens.values())
        for scope in ("全体", sets.get(item_id, "?")):
            for s in systems:
                stat[(scope, s)]["rated"] += 1
                stat[(scope, s)]["chance"] += 1 / len(cmap)
            for s in cmap.get(r["best"], []):
                stat[(scope, s)]["best"] += 1
            for cid in r.get("bad", []):
                for s in cmap.get(cid, []):
                    stat[(scope, s)]["bad"] += 1
            sh = short[scope]
            sh[0] += lens.get(r["best"]) == lo
            sh[1] += sum(1 for v in lens.values() if v == lo) / len(lens)
            sh[2] += 1

    lines = [f"# 訳の比較の評価 {run_id}", "", f"- 評価した文: {sum(1 for r in ratings.values() if r.get('best'))}"]
    for scope, (hit, exp, n) in sorted(short.items(), key=lambda kv: kv[0] != "全体"):
        lines.append(f"- 選ばれた訳が候補の中で最も短かった文({scope}): {hit} / {n}(偶然なら {exp:.1f})")
    lines += ["", "| 範囲 | 方式 | 評価した文 | いちばん良い | 偶然なら | 直訳・不自然 |", "|---|---|---:|---:|---:|---:|"]
    for (scope, s), v in sorted(stat.items(), key=lambda kv: (kv[0][0] != "全体", kv[0][0], -kv[1]["best"])):
        n = v["rated"] or 1
        lines.append(f"| {scope} | {s} | {v['rated']} | {v['best']}({v['best'] / n:.0%}) | {v['chance']:.1f} | "
                     f"{v['bad']}({v['bad'] / n:.0%}) |")
    md = "\n".join(lines) + "\n"
    (OUT / f"{run_id}_score.md").write_text(md, encoding="utf-8")
    print(md)
    return 0


if __name__ == "__main__":
    sys.exit(main())
