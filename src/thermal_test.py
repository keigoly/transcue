"""Phase 1-G: 連続負荷 熱・電力試験(fanless M5 のスロットリング確認)。

PLaMo翻訳 + Kokoro合成 を連続実行(worst-case 100%デューティ。実運用はバースト実行で
これより軽い)し、
  - 各反復の遅延(translate/tts ms)を記録 → 経時的に悪化すればスロットリングの実害兆候(主指標)
  - 定期的に `pmset -g therm`(sudo不要)をサンプル → CPU_Speed_Limit があれば throttle%(副指標)
を JSONL に残す。終了時に 5分窓ごとの遅延 p50 を集計し、最初と最後を比較して判定する。

※ 詳細な電力/周波数は `sudo powermetrics` が必要だが本環境の非対話 sudo では不可。
   実ワークロード遅延の経時変化が最も意味のある throttle 判定なのでそれを主指標とする。

実行(無人・sudo不要。既定 3600 秒。AC電源推奨):
    source spikes/.venv-ml/bin/activate
    python src/thermal_test.py 3600
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import Metrics, new_rid  # noqa: E402
from translate import Translator  # noqa: E402
from tts import TTS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

CLAUSES = [
    "本日はお時間をいただきありがとうございます",
    "まずプロジェクトの進捗についてご報告します",
    "設計フェーズは先週完了しており",
    "来週から実装に入る予定です",
    "予算については当初の見積もりから一割ほど増加する見込みです",
    "その提案には基本的に賛成なのですが",
    "納期を二週間ほど後ろ倒しにできないかと考えております",
    "週次の定例ミーティングを設けたいと考えています",
]


def load_config() -> dict:
    with (Path(__file__).resolve().parent / "config.toml").open("rb") as f:
        cfg = tomllib.load(f)
    cfg["_root"] = str(ROOT)
    return cfg


def sample_thermal() -> dict:
    try:
        out = subprocess.run(["pmset", "-g", "therm"], capture_output=True, text=True, timeout=5).stdout
    except Exception as e:
        return {"err": str(e)}
    limit = None
    for line in out.splitlines():
        mm = re.search(r"CPU_Speed_Limit\s*=?\s*(\d+)", line)
        if mm:
            limit = int(mm.group(1))
    return {"cpu_speed_limit": limit if limit is not None else 100, "raw": out.strip()[:160]}


def pct(vals: list[float], p: float) -> float | None:
    if not vals:
        return None
    o = sorted(vals)
    return round(o[min(len(o) - 1, int(round(p * (len(o) - 1))))], 1)


async def main() -> None:
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 3600.0
    cfg = load_config()
    m = Metrics.from_config(cfg, run_tag="thermal1g")
    print(f"[thermal] モデルロード中… (試験 {dur/60:.0f}分, AC電源推奨)", file=sys.stderr)
    tr = Translator(cfg, m)
    await tr.load()
    tts = TTS(cfg, m)
    await tts.load()

    m.emit("BOOT", "thermal_boot", duration_s=dur, duty="continuous(worst-case)")
    t0 = time.perf_counter()
    last_therm = 0.0
    last_print = 0.0
    records: list[tuple[float, float, float]] = []  # (elapsed_s, translate_ms, tts_ms)
    i = 0
    min_speed_limit = 100

    while (time.perf_counter() - t0) < dur:
        clause = CLAUSES[i % len(CLAUSES)]
        rid = new_rid()
        ts = time.perf_counter()
        en = await tr.translate(rid, clause)
        tr_ms = (time.perf_counter() - ts) * 1000
        ts = time.perf_counter()
        await tts.synth(rid, en)  # 合成のみ(再生しない)
        tt_ms = (time.perf_counter() - ts) * 1000
        elapsed = time.perf_counter() - t0
        records.append((elapsed, tr_ms, tt_ms))
        i += 1

        # 熱サンプル(30秒毎)
        if elapsed - last_therm >= 30:
            last_therm = elapsed
            th = sample_thermal()
            if th.get("cpu_speed_limit") is not None:
                min_speed_limit = min(min_speed_limit, th["cpu_speed_limit"])
            m.emit("THERM", "thermal_sample", elapsed_s=round(elapsed, 1), iters=i,
                    translate_ms=round(tr_ms, 1), tts_ms=round(tt_ms, 1), **th)
        # 進捗(5分毎)
        if elapsed - last_print >= 300:
            last_print = elapsed
            print(f"[thermal] {elapsed/60:.0f}分 / {dur/60:.0f}分  iters={i}  "
                  f"直近 translate={tr_ms:.0f}ms tts={tt_ms:.0f}ms  min_speed_limit={min_speed_limit}",
                  file=sys.stderr)

    # 5分窓ごとに translate+tts の合計遅延 p50 を集計
    win = 300.0
    buckets: dict[int, list[float]] = {}
    for el, a, b in records:
        buckets.setdefault(int(el // win), []).append(a + b)
    window_p50 = {k: pct(v, 0.5) for k, v in sorted(buckets.items())}
    first = window_p50.get(min(window_p50)) if window_p50 else None
    last = window_p50.get(max(window_p50)) if window_p50 else None
    degrade_pct = round((last / first - 1) * 100, 1) if (first and last) else None
    m.emit("SUMMARY", "thermal_summary", iters=i, duration_min=round((time.perf_counter() - t0) / 60, 1),
           window_p50_ms={str(k): v for k, v in window_p50.items()},
           first_window_p50_ms=first, last_window_p50_ms=last, degrade_pct=degrade_pct,
           min_cpu_speed_limit=min_speed_limit,
           verdict=("OK: 遅延劣化<15%かつ throttleなし"
                    if (degrade_pct is not None and degrade_pct < 15 and min_speed_limit >= 100)
                    else "要確認: 劣化 or throttle 検出"))
    print(f"[thermal] 完了。iters={i} first_p50={first}ms last_p50={last}ms "
          f"劣化={degrade_pct}% min_speed_limit={min_speed_limit}  log={m.path}", file=sys.stderr)
    m.close()


if __name__ == "__main__":
    asyncio.run(main())
