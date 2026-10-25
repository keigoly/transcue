"""bench/report.py — v2 A1: 再現会議ベンチ 1 回分の内訳表を作る。

logs/bench/<id>.json(run_bench.py の記録)から、同じ bench_id の送信(pipeline1f_*)・受信(receive2c_*)・
resmon_* のログを探し、次を Markdown(logs/bench/<id>.md)と JSON(<id>_summary.json)に出す。
  - メモリ: 役割ごとの最大(phys_footprint)と、システム全体(スワップの増え方・圧縮・GPU が使うメモリ)
  - CPU・GPU・消費電力: 「読み込み中」「会話中」「無音(待機)」の時間帯ごと
  - 起動: 送信・受信それぞれの import / 重み / warmup / 準備完了までの時間
  - 遅延: 送信 e2e・翻訳・TTS、受信の先行表示・確定、無音明けの 1 発目(H3)
  - keep-warm: 回数と 1 回あたりの時間(H2)
  - import だけの時間とメモリ(H4)
  - 開始時の環境(ディスク・スワップ・逼迫・ユーザーごとの RSS)と、送受信の流し始めの差(v2 A2-0)
  - 送信の繰り越し: 待ちの上限と、単独で出した回数・話の再開までの時間(2026-10-25)

使い方:
    spikes/.venv-ml/bin/python src/bench/report.py <bench_id>
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LOGS = ROOT / "logs"
OUT = LOGS / "bench"
IDLE_MARGIN_S = 25.0   # 無音に入ってから、最後の翻訳・TTS が終わるまでの余裕


def load_jsonl(p: Path) -> list[dict]:
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def find_run(pattern: str, boot_phase: str, bench_id: str, t_start_ms: int) -> list[dict]:
    for f in sorted(LOGS.glob(pattern)):
        try:
            stamp = time.mktime(time.strptime(f.stem.split("_", 1)[1], "%Y%m%d_%H%M%S"))
        except ValueError:
            continue
        if stamp * 1000 < t_start_ms - 60_000:
            continue
        ev = load_jsonl(f)
        if any(e.get("phase") == boot_phase and e.get("bench") == bench_id for e in ev):
            return ev
    return []


def pct(vals: list[float], q: float) -> float | None:
    if not vals:
        return None
    o = sorted(vals)
    return o[min(len(o) - 1, int(round(q * (len(o) - 1))))]


def first(ev: list[dict], phase: str, **match) -> dict:
    for e in ev:
        if e.get("phase") == phase and all(e.get(k) == v for k, v in match.items()):
            return e
    return {}


def n(v, digits: int = 0) -> str:
    if v is None:
        return "—"
    if isinstance(v, float) and digits:
        return f"{v:,.{digits}f}"
    return f"{round(v):,}"


def by_role(samples: list[dict]) -> list[dict]:
    """1 回の採取の中で同じ役割のプロセス(venv の python は親子 2 つになる)を足し合わせる。"""
    rows = []
    for s in samples:
        agg: dict[str, dict] = defaultdict(lambda: {"fp": 0.0, "cpu": 0.0, "mj": 0.0, "has_cpu": False})
        for p in s.get("procs", []):
            a = agg[p["role"]]
            a["fp"] += p.get("fp_mb", 0)
            if "cpu_pct" in p:
                a["cpu"] += p["cpu_pct"]
                a["mj"] += p.get("mj", 0)
                a["has_cpu"] = True
        rows.append({"ts": s["ts_ms"], "roles": agg, "sys": s.get("sys", {}),
                     "zrt_fp": s.get("zrt_fp_mb", 0), "zrt_cpu": s.get("zrt_cpu_pct", 0)})
    return rows


def window(rows: list[dict], t0: float, t1: float) -> dict | None:
    ss = [r for r in rows if t0 <= r["ts"] <= t1]
    if len(ss) < 2:
        return None
    dur = (ss[-1]["ts"] - ss[0]["ts"]) / 1000 or 1.0
    cpu: dict[str, list[float]] = defaultdict(list)
    mj: dict[str, float] = defaultdict(float)
    for r in ss[1:]:   # 先頭の採取の差分は窓の外の時間を含む
        for role, a in r["roles"].items():
            if a["has_cpu"]:
                cpu[role].append(a["cpu"])
                mj[role] += a["mj"]
    gpu = [r["sys"]["gpu_util"] for r in ss if r["sys"].get("gpu_util") is not None]
    return {"dur_s": round(dur), "samples": len(ss),
            "zrt_cpu_avg": round(statistics.mean(r["zrt_cpu"] for r in ss[1:]), 1),
            "gpu_avg": round(statistics.mean(gpu), 1) if gpu else None, "gpu_max": max(gpu) if gpu else None,
            "cpu_avg": {k: round(statistics.mean(v), 1) for k, v in cpu.items()},
            "watts": {k: round(v / 1000 / dur, 2) for k, v in mj.items()}}


def side_stats(ev: list[dict], t_idle_end: float | None, recv: bool) -> dict:
    tr = [e for e in ev if e.get("phase") == "translate" and e.get("ok")]
    kw = [e for e in ev if e.get("phase") == "keepwarm"]
    d = {"translations": len(tr), "keepwarm": len(kw),
         "keepwarm_ms_p50": pct([e["ms"] for e in kw if "ms" in e], 0.5),
         "translate_ms_p50": pct([e["ms"] for e in tr], 0.5), "translate_ms_p90": pct([e["ms"] for e in tr], 0.9),
         "first_token_ms_p50": pct([e["first_token_ms"] for e in tr if e.get("first_token_ms")], 0.5),
         # v2 §7b: 訳した先ごとの節の数と、Claude から PLaMo に回した理由(古い結果は PLaMo だけ)
         "engines": dict(Counter(e.get("engine", "plamo") for e in tr)),
         "fallbacks": dict(Counter(e.get("reason") for e in ev if e.get("phase") == "translate_fallback"))}
    if recv:
        st = [e for e in ev if e.get("phase") == "e2e_recv_stream"]
        d.update({"clauses": len(st),
                  "first_shown_p50": pct([e["first_shown_ms"] for e in st if e.get("first_shown_ms")], 0.5),
                  "first_shown_p90": pct([e["first_shown_ms"] for e in st if e.get("first_shown_ms")], 0.9),
                  "full_p50": pct([e["full_ms"] for e in st if e.get("full_ms")], 0.5),
                  "full_p90": pct([e["full_ms"] for e in st if e.get("full_ms")], 0.9),
                  "full_max": max([e["full_ms"] for e in st if e.get("full_ms")], default=None)})
        after = [e for e in st if t_idle_end and e["ts_ms"] >= t_idle_end]
        if after:
            d["after_idle"] = {"first_shown_ms": after[0].get("first_shown_ms"), "full_ms": after[0].get("full_ms")}
    else:
        e2e = [e for e in ev if e.get("phase") == "e2e"]
        tts = [e for e in ev if e.get("phase") == "tts" and e.get("ok")]
        d.update({"clauses": len(e2e), "e2e_p50": pct([e["ms"] for e in e2e], 0.5),
                  "e2e_p90": pct([e["ms"] for e in e2e], 0.9), "e2e_max": max([e["ms"] for e in e2e], default=None),
                  "tts_ms_p50": pct([e["ms"] for e in tts], 0.5), "tts_ms_max": max([e["ms"] for e in tts], default=None)})
        after = [e for e in e2e if t_idle_end and e["ts_ms"] >= t_idle_end]
        if after:
            rid = after[0]["rid"]
            t = first(ev, "translate", rid=rid)
            d["after_idle"] = {"e2e_ms": after[0]["ms"], "translate_ms": t.get("ms"),
                               "first_token_ms": t.get("first_token_ms")}
    return d


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    bid = sys.argv[1]
    man = json.loads((OUT / f"{bid}.json").read_text(encoding="utf-8"))
    t0 = man["t_start_ms"]
    send = find_run("pipeline1f_*.jsonl", "boot", bid, t0)
    recv = find_run("receive2c_*.jsonl", "boot", bid, t0)
    res = find_run("resmon_*.jsonl", "res_boot", bid, t0)
    # --shared(v2 A2-1): 翻訳サーバのログ(モデルの読み込みと keep-warm はこちらに出る)
    srv = find_run("translator_*.jsonl", "boot", bid, t0) if man.get("shared") else []
    rows = by_role([e for e in res if e.get("phase") == "res_sample"])

    def feed_start(ev: list[dict]) -> float | None:
        e = first(ev, "stt_diag", helper_phase="feed_start")
        return e.get("t_ms")

    idle = man.get("idle_audio_s") or {}
    fs_send, fs_recv = feed_start(send), feed_start(recv)
    boot_send, boot_recv = first(send, "boot"), first(recv, "boot")
    t_ready = max(boot_send.get("ts_ms", 0), boot_recv.get("ts_ms", 0)) or None
    # 会話の始まり = 流し始め(A2-0 から送受信で揃う)。流し始めが取れない古い結果は準備完了の時刻
    t_talk = max(fs_send, fs_recv) if fs_send and fs_recv else t_ready
    # 音声の時刻 → 実時刻(送受信で流し始めがずれるので、無音は両方が黙っている区間をとる)
    t_idle0 = t_idle1 = None
    if fs_send and fs_recv and idle:
        t_idle0 = max(fs_send, fs_recv) + (idle["start_s"] + IDLE_MARGIN_S) * 1000
        t_idle1 = min(fs_send, fs_recv) + idle["end_s"] * 1000
    t_end = man.get("t_end_ms")
    windows = {
        "読み込み中": window(rows, man["t_launch_ms"], t_ready) if t_ready else None,
        "会話中(前半)": window(rows, t_talk, t_idle0) if t_talk and t_idle0 else None,
        "無音(待機)": window(rows, t_idle0, t_idle1) if t_idle0 and t_idle1 else None,
        "会話中(後半)": window(rows, t_idle1, t_end) if t_idle1 and t_end else None,
    }
    peak: dict[str, float] = defaultdict(float)
    for r in rows:
        for role, a in r["roles"].items():
            peak[role] = max(peak[role], a["fp"])
    sysv = [r["sys"] for r in rows]

    def sys_series(k: str) -> list[float]:
        return [s[k] for s in sysv if s.get(k) is not None]

    swap = sys_series("swap_used_mb")
    summary = {
        "bench_id": bid, "label": man.get("label"), "aborted": man.get("aborted"),
        "zoom_at_start": man.get("zoom_at_start"),
        "env_start": man.get("env_start"), "sys_start": sysv[0] if sysv else None,
        "feed": {"offset_ms": round(fs_send - fs_recv) if fs_send and fs_recv else None,
                 "wait_ms": man.get("feed_wait_ms")},
        "memory": {"peak_by_role_mb": {k: round(v) for k, v in sorted(peak.items(), key=lambda x: -x[1])},
                   "zrt_peak_mb": round(max((r["zrt_fp"] for r in rows), default=0)),
                   "swap_start_mb": swap[0] if swap else None, "swap_max_mb": max(swap) if swap else None,
                   "compressed_max_mb": max(sys_series("compressed_mb"), default=None),
                   "free_min_mb": min(sys_series("free_mb"), default=None),
                   "pressure_max": max(sys_series("pressure"), default=None),
                   "gpu_mem_max_mb": max(sys_series("gpu_mem_mb"), default=None)},
        "startup": {
            "send": {"boot_ms": boot_send.get("boot_ms"),
                     "translate_load": {k: first(send, "translate_load").get(k) for k in
                                        ("ms", "import_ms", "weights_ms", "warmup_ms", "mlx_active_mb", "mlx_peak_mb")},
                     "tts_load": {k: first(send, "tts_load").get(k) for k in
                                  ("ms", "import_ms", "weights_ms", "mlx_active_mb", "mlx_peak_mb")}},
            "recv": {"boot_ms": boot_recv.get("boot_ms"),
                     "translate_load": {k: first(recv, "translate_load").get(k) for k in
                                        ("ms", "import_ms", "weights_ms", "warmup_ms", "mlx_active_mb", "mlx_peak_mb")}},
            **({"translator": {"boot_ms": first(srv, "boot").get("boot_ms"),
                               "translate_load": {k: first(srv, "translate_load").get(k) for k in
                                                  ("ms", "import_ms", "weights_ms", "warmup_ms", "mlx_active_mb",
                                                   "mlx_peak_mb", "wired_limit_mb")}}} if srv else {})},
        "wired": man.get("wired"),
        # v2 A2-2: 話し始めの起こし(翻訳は wake、送信の TTS は tts_wake)の回数と所要 ms
        "wake": {k: {"n": len(v), "p50": pct(v, 0.5), "max": max(v, default=None)}
                 for k, v in (("翻訳", [e["ms"] for ev in (srv, send, recv) for e in ev if e.get("phase") == "wake"]),
                              ("TTS", [e["ms"] for e in send if e.get("phase") == "tts_wake"])) if v},
        "windows": windows,
        "translator": ({"keepwarm": sum(1 for e in srv if e.get("phase") == "keepwarm"),
                        "keepwarm_ms_p50": pct([e["ms"] for e in srv if e.get("phase") == "keepwarm" and "ms" in e],
                                               0.5)} if srv else None),
        "send": side_stats(send, t_idle1, recv=False),
        "recv": side_stats(recv, t_idle1, recv=True),
        "keepwarm_in_idle": {k: sum(1 for e in ev if e.get("phase") == "keepwarm" and t_idle0 and t_idle1
                                    and t_idle0 <= e["ts_ms"] <= t_idle1)
                             for k, ev in (("send", send), ("recv", recv), ("translator", srv))},
        "queue_ms": {k: {"p50": pct(q, 0.5), "p90": pct(q, 0.9), "max": max(q, default=None)}
                     for k, ev in (("send", send), ("recv", recv))
                     if (q := [e["queue_ms"] for e in ev if e.get("phase") == "translate"
                               and e.get("queue_ms") is not None])},
        "imports": man.get("imports"),
        # 送信の繰り越し(2026-10-25): 待ちの上限(古い結果は記録なし)・単独で出した回数・話の再開まで
        "carry": {"wait_ms": man.get("carry_wait_ms"),
                  "flush": {r: sum(1 for e in send if e.get("phase") == "carry_flush" and e.get("reason") == r)
                            for r in ("timeout", "bye")},
                  **{k: first(send, "segmenter_trace_summary").get(k)
                     for k in ("carry_resume_ms", "carry_left_chars")}},
        "found": {"send": bool(send), "recv": bool(recv), "resmon": bool(res),
                  **({"translator": bool(srv)} if man.get("shared") else {})},
    }
    (OUT / f"{bid}_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    md = render(summary)
    (OUT / f"{bid}.md").write_text(md, encoding="utf-8")
    print(md)
    return 0 if all(summary["found"].values()) else 1


def render(s: dict) -> str:
    L = [f"# 再現会議ベンチ {s['bench_id']}(label={s['label']})", ""]
    if s.get("aborted"):
        L += [f"**中止: {s['aborted']}**(以下は途中までの値)", ""]
    if not all(s["found"].values()):
        L += [f"**ログが揃っていません: {s['found']}**", ""]
    env0, sys0, feed = s.get("env_start") or {}, s.get("sys_start") or {}, s.get("feed") or {}
    if env0 or sys0:
        users = " / ".join(f"{u} {g} GB" for u, g in (env0.get("rss_gb_by_user") or {}).items())
        L += [f"- 開始時の環境: ディスク空き {n(env0.get('disk_free_gb'), 1)} GB・スワップ {n(sys0.get('swap_used_mb'))} MB・"
              f"空きメモリ {n(sys0.get('free_mb'))} MB・圧縮 {n(sys0.get('compressed_mb'))} MB・"
              f"逼迫レベル {sys0.get('pressure', '—')}" + (f"・ユーザーごとの RSS: {users}" if users else "")]
    if feed.get("offset_ms") is not None:
        waits = ", ".join(f"{k} {n(v)} ms" for k, v in (feed.get("wait_ms") or {}).items())
        L += [f"- 流し始めの差(送信 − 受信): {n(feed['offset_ms'])} ms" + (f"(待ち合わせ: {waits})" if waits else "")]
    c = s.get("carry") or {}
    if c.get("carry_resume_ms"):
        r = c["carry_resume_ms"]
        L += [f"- 送信の繰り越し: 待ちの上限 {n(c.get('wait_ms'))} ms(0 = 無効)・単独で出した {c['flush']['timeout']} 回・"
              f"話の再開まで p50 {n(r.get('p50'))} / 最大 {n(r.get('max'))} ms(n={r.get('n')})・"
              f"停止で残った {n(c.get('carry_left_chars'))} 文字"]
    if env0 or sys0 or feed.get("offset_ms") is not None or c.get("carry_resume_ms"):
        L += [""]
    m = s["memory"]
    L += ["## メモリ(phys_footprint の最大)", "", "| 役割 | 最大 MB |", "|---|---:|"]
    L += [f"| {k} | {n(v)} |" for k, v in m["peak_by_role_mb"].items()]
    L += ["", f"- 翻訳スタック合計の最大: **{n(m['zrt_peak_mb'])} MB**(Zoom を除く)",
          f"- スワップ使用: 開始 {n(m['swap_start_mb'])} MB → 最大 {n(m['swap_max_mb'])} MB / "
          f"圧縮メモリ最大 {n(m['compressed_max_mb'])} MB / 空き最小 {n(m['free_min_mb'])} MB / "
          f"逼迫レベル最大 {m['pressure_max']}(1 正常・2 警告・4 危険)",
          f"- GPU が使うメモリの最大: {n(m['gpu_mem_max_mb'])} MB(システム全体)", ""]
    st = s["startup"]
    L += ["## 起動(ms)", "", "| | 準備完了まで | 翻訳: 計 | import | 重み | warmup | TTS: 計 | import | 重み |",
          "|---|---:|---:|---:|---:|---|---:|---:|---:|"]
    for side in ("send", "recv", "translator"):
        if side not in st:
            continue
        b = st[side]
        tl = b["translate_load"]
        tt = b.get("tts_load", {})
        L.append(f"| {({'send': '送信', 'recv': '受信'}).get(side, '翻訳サーバ')} | {n(b['boot_ms'])} | {n(tl.get('ms'))} | "
                 f"{n(tl.get('import_ms'))} | {n(tl.get('weights_ms'))} | {tl.get('warmup_ms') or '—'} | "
                 f"{n(tt.get('ms'))} | {n(tt.get('import_ms'))} | {n(tt.get('weights_ms'))} |")
    L += ["", f"- MLX のメモリ(読み込み直後): 送信 翻訳 {n(st['send']['translate_load'].get('mlx_active_mb'))} MB"
          f" → +TTS {n(st['send']['tts_load'].get('mlx_active_mb'))} MB / 受信 "
          f"{n(st['recv']['translate_load'].get('mlx_active_mb'))} MB"
          + (f" / 翻訳サーバ {n(st['translator']['translate_load'].get('mlx_active_mb'))} MB"
             if "translator" in st else ""), ""]
    if "translator" in st:
        L += ["- 送受信の「翻訳: 計」は、翻訳サーバの準備(読み込み + warmup)を待った時間。", ""]
    if s.get("wired"):
        wl = (st.get("translator") or {}).get("translate_load", {}).get("wired_limit_mb")
        L += [f"- 重みを常時固定(上限 {n(wl)} MB)・keep-warm なし(v2 A2-2)。", ""]
    L += ["## CPU・GPU・消費電力(時間帯ごと)", "",
          "| 時間帯 | 秒 | スタック CPU% | GPU% 平均/最大 | send CPU% | recv CPU% | 翻訳サーバ CPU% | send W | recv W |",
          "|---|---:|---:|---|---:|---:|---:|---:|---:|"]
    for name, w in s["windows"].items():
        if not w:
            L.append(f"| {name} | — | — | — | — | — | — | — | — |")
            continue
        L.append(f"| {name} | {w['dur_s']} | {w['zrt_cpu_avg']} | {w['gpu_avg']} / {w['gpu_max']} | "
                 f"{w['cpu_avg'].get('send', '—')} | {w['cpu_avg'].get('recv', '—')} | "
                 f"{w['cpu_avg'].get('translator', '—')} | "
                 f"{w['watts'].get('send', '—')} | {w['watts'].get('recv', '—')} |")
    L += ["", "W は CPU 側の消費電力(GPU は含まない)。CPU% は 100 = 1 コアぶん。", ""]
    se, re_ = s["send"], s["recv"]
    L += ["## 遅延(ms)", "", "| | 節 | p50 | p90 | 最大 | 翻訳 p50 / p90 | 初トークン p50 | 無音明けの 1 発目 |",
          "|---|---:|---:|---:|---:|---|---:|---|"]
    ai = se.get("after_idle") or {}
    L.append(f"| 送信 e2e | {se['clauses']} | {n(se.get('e2e_p50'))} | {n(se.get('e2e_p90'))} | "
             f"{n(se.get('e2e_max'))} | {n(se['translate_ms_p50'])} / {n(se['translate_ms_p90'])} | "
             f"{n(se['first_token_ms_p50'])} | e2e {n(ai.get('e2e_ms'))}・初トークン {n(ai.get('first_token_ms'))} |")
    ai = re_.get("after_idle") or {}
    L.append(f"| 受信 確定 | {re_['clauses']} | {n(re_.get('full_p50'))} | {n(re_.get('full_p90'))} | "
             f"{n(re_.get('full_max'))} | {n(re_['translate_ms_p50'])} / {n(re_['translate_ms_p90'])} | "
             f"{n(re_['first_token_ms_p50'])} | 先行表示 {n(ai.get('first_shown_ms'))}・確定 {n(ai.get('full_ms'))} |")
    L += ["", f"- 受信の先行表示 p50 {n(re_.get('first_shown_p50'))} / p90 {n(re_.get('first_shown_p90'))}・"
          f"送信の TTS p50 {n(se.get('tts_ms_p50'))} / 最大 {n(se.get('tts_ms_max'))}"]
    for name, side in (("送信", se), ("受信", re_)):
        eng, fb = side.get("engines") or {}, side.get("fallbacks") or {}
        if fb or set(eng) - {"plamo"}:
            L += [f"- {name}の訳: " + "・".join(f"{k} {v} 節" for k, v in eng.items())
                  + (f"(PLaMo に回した理由: " + "・".join(f"{k} {v} 回" for k, v in fb.items()) + ")" if fb else "")]
    qm = s.get("queue_ms") or {}
    if qm:
        L += ["- 翻訳サーバの順番待ち(ms): " + "・".join(
            f"{({'send': '送信', 'recv': '受信'})[k]} p50 {n(v['p50'])} / p90 {n(v['p90'])} / 最大 {n(v['max'])}"
            for k, v in qm.items()) + "(遅延の翻訳・初トークンはこの待ちを含む)"]
    L += [""]
    kw = s["keepwarm_in_idle"]
    tr = s.get("translator") or {}
    L += ["## keep-warm(空回し)", "",
          f"- 回数: 送信 {se['keepwarm']}・受信 {re_['keepwarm']}(うち無音の区間で 送信 {kw['send']}・受信 {kw['recv']})"
          + (f"・翻訳サーバ {tr['keepwarm']}(うち無音 {kw.get('translator', 0)})" if tr else "")
          + f" / 実際の翻訳: 送信 {se['translations']}・受信 {re_['translations']}",
          f"- 1 回あたり(p50): 送信 {n(se['keepwarm_ms_p50'])} ms・受信 {n(re_['keepwarm_ms_p50'])} ms"
          + (f"・翻訳サーバ {n(tr.get('keepwarm_ms_p50'))} ms" if tr else ""), ""]
    if s.get("wake"):
        L += ["- 話し始めの起こし(v2 A2-2): " + "・".join(
            f"{k} {v['n']} 回(1 回 p50 {n(v['p50'])} ms / 最大 {n(v['max'])} ms)" for k, v in s["wake"].items()), ""]
    if s.get("imports"):
        L += ["## import だけの時間とメモリ(H4)", "", "| import | 秒 | 最大 footprint MB | モジュール数 | torch |",
              "|---|---:|---:|---:|---|"]
        for r in s["imports"]:
            L.append(f"| {r['what']} | {r['wall_s']} | {n(r['peak_fp_mb'])} | {n(r['modules'])} | "
                     f"{'読み込む' if r['torch'] else '—'} |")
        L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    sys.exit(main())
