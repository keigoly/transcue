"""eval_translate/make_rating_page.py — 訳を伏せて並べる比較ページ(ローカルの HTML)を作る。

run_eval.py の結果(logs/eval_translate/<run_id>.jsonl)から、文ごとに訳の候補を並べ替え(同じ訳は 1 つにまとめる)、
どの方式の訳かを伏せたページを作る。利用者は文ごとに「いちばん良い訳」を 1 つ選び、直訳すぎる・不自然な訳に
印を付ける。評価はブラウザに残り、「評価を保存」で ratings_<run_id>.json(ダウンロード)に書き出す。集計は score.py。
候補と方式の対応は <run_id>_cands.json に置き、ページには入れない。

使い方(プロジェクトの直下で):
    spikes/.venv-ml/bin/python src/eval_translate/make_rating_page.py [run_id] [--top N] [--skip id,...] [--open]
    --top N: 候補どうしの違いが大きい文から N 文に絞る(組ごとに同じ数・候補が 1 つの文は除く)。--skip: 除く文
"""
from __future__ import annotations

import json
import random
import re
import subprocess
import sys
from collections import defaultdict
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "logs" / "eval_translate"

PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>訳の比較 __RUN__</title>
<style>
:root { --bg:#fafaf8; --fg:#1d1d1b; --muted:#6b6b66; --card:#fff; --line:#e3e2dc; --pick:#e7f1ff; --bad:#fdecea; }
@media (prefers-color-scheme: dark) { :root { --bg:#161615; --fg:#ecebe6; --muted:#9a9993; --card:#1f1f1d;
  --line:#33332f; --pick:#1d2a3d; --bad:#3a2220; } }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.6 -apple-system, "Hiragino Sans", sans-serif; }
header { position:sticky; top:0; background:var(--bg); border-bottom:1px solid var(--line); padding:10px 16px;
  display:flex; gap:12px; align-items:center; flex-wrap:wrap; z-index:1; }
main { max-width:860px; margin:0 auto; padding:12px 16px 80px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px 14px; margin:12px 0; }
.src { font-size:17px; font-weight:600; margin-bottom:6px; }
.meta { color:var(--muted); font-size:12px; }
details { color:var(--muted); font-size:13px; margin:4px 0 8px; }
.cand { display:grid; grid-template-columns:auto 1fr auto; gap:8px; align-items:start; padding:6px 8px;
  border-radius:8px; border:1px solid transparent; }
.cand.picked { background:var(--pick); border-color:var(--line); }
.cand.flag { background:var(--bad); }
.cand label.bad { color:var(--muted); font-size:12px; white-space:nowrap; }
button { font:inherit; padding:6px 12px; border-radius:8px; border:1px solid var(--line); background:var(--card);
  color:var(--fg); cursor:pointer; }
.help { color:var(--muted); font-size:13px; }
</style></head><body>
<header><strong>訳の比較 __RUN__</strong><span id="prog"></span>
<button id="save">評価を保存(JSON)</button>
<span class="help">文ごとに「いちばん良い訳」を 1 つ選び、直訳すぎる・不自然な訳に印を付けてください。途中でやめても残ります。</span>
</header><main id="list"></main>
<script>
const RUN = "__RUN__";
const ITEMS = __ITEMS__;
const KEY = "zrt-rate-" + RUN;
const SETS = { bench: "ベンチの会話", phrases: "よく使う言い回し", subs: "字幕(相手の英語)" };
let state = {};
try { state = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { state = {}; }
function persist() { try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} render(); }
function render() {
  const done = ITEMS.filter(it => state[it.id] && state[it.id].best).length;
  document.getElementById("prog").textContent = `評価済み ${done} / ${ITEMS.length}`;
  for (const it of ITEMS) {
    const s = state[it.id] || {};
    for (const c of it.cands) {
      const row = document.getElementById(`${it.id}-${c.cid}`);
      row.classList.toggle("picked", s.best === c.cid);
      row.classList.toggle("flag", (s.bad || []).includes(c.cid));
      row.querySelector("input[type=radio]").checked = s.best === c.cid;
      row.querySelector("input[type=checkbox]").checked = (s.bad || []).includes(c.cid);
    }
  }
}
const list = document.getElementById("list");
for (const it of ITEMS) {
  const card = document.createElement("div"); card.className = "card";
  const src = document.createElement("div"); src.className = "src"; src.textContent = it.src; card.appendChild(src);
  const meta = document.createElement("div"); meta.className = "meta";
  meta.textContent = `${it.id}(${SETS[it.set] || it.set})`; card.appendChild(meta);
  if (it.history.length) {
    const d = document.createElement("details"); const sm = document.createElement("summary");
    sm.textContent = "直前の会話"; d.appendChild(sm);
    for (const [who, text] of it.history) { const p = document.createElement("div"); p.textContent = `[${who}] ${text}`; d.appendChild(p); }
    card.appendChild(d);
  }
  for (const c of it.cands) {
    const row = document.createElement("div"); row.className = "cand"; row.id = `${it.id}-${c.cid}`;
    const r = document.createElement("input"); r.type = "radio"; r.name = it.id; r.title = "いちばん良い訳";
    r.onchange = () => { state[it.id] = { ...(state[it.id] || {}), best: c.cid }; persist(); };
    const t = document.createElement("div"); t.textContent = c.text || "(訳なし)";
    const lb = document.createElement("label"); lb.className = "bad";
    const cb = document.createElement("input"); cb.type = "checkbox";
    cb.onchange = () => { const s = state[it.id] || {}; const bad = new Set(s.bad || []);
      cb.checked ? bad.add(c.cid) : bad.delete(c.cid); state[it.id] = { ...s, bad: [...bad] }; persist(); };
    lb.appendChild(cb); lb.appendChild(document.createTextNode(" 直訳・不自然"));
    row.appendChild(r); row.appendChild(t); row.appendChild(lb); card.appendChild(row);
  }
  list.appendChild(card);
}
document.getElementById("save").onclick = () => {
  const blob = new Blob([JSON.stringify({ run_id: RUN, ratings: state }, null, 1)], { type: "application/json" });
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = `ratings_${RUN}.json`; a.click();
};
render();
</script></body></html>
"""


def latest_run() -> str:
    runs = sorted(p.stem for p in OUT.glob("*.jsonl"))
    if not runs:
        sys.exit("結果がありません。先に run_eval.py を実行してください。")
    return runs[-1]


def tokens(text: str) -> list[str]:
    """違いを測る単位: 日本語(かな・漢字を含む)は文字、英語は小文字の語。"""
    if any("぀" <= ch <= "ヿ" or "一" <= ch <= "鿿" for ch in text):
        return [ch for ch in text if ch.isalnum()]
    return re.findall(r"\w+(?:'\w+)?", text.lower())


def spread(texts: list[str]) -> float:
    """候補どうしの違い: 最も違う 2 つの訳で、語(日本語は文字)の並びが一致しない割合。0 = 同じ・1 = 重なりなし。"""
    toks = [tokens(t) for t in texts]
    return max((1 - SequenceMatcher(None, a, b).ratio() for a, b in combinations(toks, 2)), default=0.0)


def main() -> int:
    argv = sys.argv[1:]
    opt = lambda name: argv[argv.index(name) + 1] if name in argv else None
    pos = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] not in ("--top", "--skip"))]
    run_id = pos[0] if pos else latest_run()
    top = int(opt("--top") or 0)
    skip = set((opt("--skip") or "").split(",")) - {""}
    rows = [json.loads(ln) for ln in (OUT / f"{run_id}.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]
    by_item: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_item[r["item_id"]].append(r)
    items, cands_map, spreads = [], {}, {}
    for item_id, rs in by_item.items():
        groups: dict[str, list[str]] = defaultdict(list)       # 同じ訳(空白を詰めて比べる)は 1 つの候補にまとめる
        for r in rs:
            groups[" ".join((r.get("out") or "").split())].append(r["system"])
        texts = list(groups)
        spreads[item_id] = spread(texts)
        random.Random(f"{run_id}:{item_id}").shuffle(texts)     # どの方式か分からない順に(毎回同じ順)
        cands = [{"cid": f"c{n}", "text": t} for n, t in enumerate(texts, 1)]
        # ページに出さない文の分も置く(絞る前に付けた評価も score.py で集計できるように)
        cands_map[item_id] = {c["cid"]: groups[c["text"]] for c in cands}
        # 原文は src(10/25 の 2 回目から。1 回目までは ja)
        items.append({"id": item_id, "set": rs[0]["set"], "src": rs[0].get("src") or rs[0]["ja"],
                      "history": rs[0]["history"], "cands": cands})
    items = [it for it in items if it["id"] not in skip]
    if top:     # 違いの小さい文は、どれを選んでも差が出ないので外す
        sets = sorted({it["set"] for it in items})
        keep: set[str] = set()
        for s in sets:
            ids = [it["id"] for it in items if it["set"] == s and spreads[it["id"]] > 0]
            keep.update(sorted(ids, key=lambda i: -spreads[i])[: top // len(sets)])    # 同点は台本の順
        items = [it for it in items if it["id"] in keep]
    (OUT / f"{run_id}_cands.json").write_text(json.dumps(cands_map, ensure_ascii=False, indent=1), encoding="utf-8")
    page = OUT / f"{run_id}_rate.html"
    page.write_text(PAGE.replace("__RUN__", run_id).replace("__ITEMS__", json.dumps(items, ensure_ascii=False)),
                    encoding="utf-8")
    print(f"比較ページ: {page}({len(items)} 文)")
    if "--open" in sys.argv:
        subprocess.run(["open", str(page)], check=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
