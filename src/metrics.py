"""JSONL メトリクスロガー — Phase 1 の「見える化」土台(DEVELOPMENT.md Step 1)。

パイプラインの各フェーズは 1 行の JSONL を吐く: rid / phase / ms / ok / error (+任意フィールド)。
最適化の前に、まずどの段で何 ms かかっているかを requestId 付きで計測できる状態にする。

使い方:
    m = Metrics.from_config(cfg)
    rid = new_rid()
    with m.timed(rid, "translate") as ex:      # 例外時は ok=false, error=... で自動記録
        ex["src"] = clause
        ex["out"] = do_translate(clause)
    m.emit(rid, "e2e", ms=e2e_ms)              # 単発イベントの記録
"""
from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

# rid/phase/ts_ms は emit が必ず埋める予約キー。timed の ms/ok/error も同様。
_RESERVED = ("ts_ms", "rid", "phase")


def new_rid() -> str:
    """1 発話(節)を追跡するための短い requestId。"""
    return uuid.uuid4().hex[:8]


def mlx_mem() -> dict:
    """MLX(Metal)のメモリ使用量(MB)。mlx を読み込み済みのプロセスでだけ意味がある(v2 A1)。"""
    try:
        import mlx.core as mx
        mb = 2 ** 20
        return {"mlx_active_mb": round(mx.get_active_memory() / mb),
                "mlx_peak_mb": round(mx.get_peak_memory() / mb),
                "mlx_cache_mb": round(mx.get_cache_memory() / mb)}
    except Exception:  # noqa: BLE001 — 計測の失敗で本体を止めない
        return {}


class Metrics:
    def __init__(self, log_path: Path | str, echo_stderr: bool = True):
        self.path = Path(log_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = self.path.open("a", encoding="utf-8")
        self.echo = echo_stderr

    @classmethod
    def from_config(cls, cfg: dict, run_tag: str = "pipeline") -> "Metrics":
        mc = cfg.get("metrics", {})
        root = Path(cfg["_root"])
        log_dir = root / mc.get("log_dir", "logs")
        # 秒精度のファイル名(Date.now は使わず時刻はプロセス起動時刻でよい)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return cls(log_dir / f"{run_tag}_{stamp}.jsonl", echo_stderr=mc.get("echo_stderr", True))

    def emit(self, rid: str, phase: str, **fields) -> dict:
        rec = {"ts_ms": int(time.time() * 1000), "rid": rid, "phase": phase}
        for k in _RESERVED:
            fields.pop(k, None)  # 予約キーは呼び出し側の値で上書きさせない
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
        if self.echo:
            print(line, file=sys.stderr, flush=True)
        return rec

    @contextmanager
    def timed(self, rid: str, phase: str, **fields):
        """フェーズの所要 ms を計測し、終了時(例外含む)に 1 行記録する。

        yield される dict に追記したフィールドも一緒に記録される。
        例外は握りつぶさず再送出する(冪等性・可観測性の両立)。
        """
        extra: dict = {}
        t0 = time.perf_counter()
        ok = True
        err = None
        try:
            yield extra
        except BaseException as e:  # ok=false を必ず残してから再送出
            ok = False
            err = f"{type(e).__name__}: {e}"
            raise
        finally:
            ms = round((time.perf_counter() - t0) * 1000, 1)
            payload = dict(fields)
            payload.update(extra)
            payload["ms"] = ms
            payload["ok"] = ok
            payload["error"] = err
            self.emit(rid, phase, **payload)

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()
