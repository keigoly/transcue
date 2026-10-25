"""会議ログ(transcript)— Phase 3-D。発話の原文・訳文・時刻を JSONL でローカル保存する。

ARCHITECTURE §2「会議ログ: 発話原文・訳文・タイミングを JSONL でローカル保存(議事録の副産物。
外部送信なし)」。送信(日→英)と受信(英→日)の両方が **同じ日付ファイル**
`logs/transcript_<YYYYMMDD>.jsonl` に追記する(1 会議 = 1 ファイルに時系列で残る)。

metrics.py(デバッグ計測ログ)とは別物: こちらは人が読む議事録用の原文/訳文だけの綺麗なログ。

1 行 = 1 発話:
    {"ts_ms":…, "time":"14:03:21", "dir":"受信(英→日)",
     "src_lang":"English","dst_lang":"Japanese",
     "original":"The budget is …","translation":"予算は …"}
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


class Transcript:
    def __init__(self, path: Path | str, enabled: bool = True):
        self.enabled = enabled
        self.path = Path(path)
        self._fh = None
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 追記モード。送受信が別プロセスでも同じ日付ファイルに時系列で残す。
            self._fh = self.path.open("a", encoding="utf-8")

    @classmethod
    def from_config(cls, cfg: dict) -> "Transcript":
        tc = cfg.get("transcript", {})
        # ZRT_NO_TRANSCRIPT=1: 再現会議ベンチ(v2 A1)の文を議事録に混ぜない。
        enabled = bool(tc.get("enabled", True)) and not os.environ.get("ZRT_NO_TRANSCRIPT")
        log_dir = Path(cfg["_root"]) / tc.get("dir", "logs")
        stamp = time.strftime("%Y%m%d")  # 日付単位(1会議1ファイル)
        return cls(log_dir / f"transcript_{stamp}.jsonl", enabled=enabled)

    def add(self, direction: str, original: str, translation: str,
            src_lang: str = "", dst_lang: str = "") -> None:
        """1 発話を記録。direction は "送信(日→英)" / "受信(英→日)" など。"""
        if not self.enabled or not self._fh:
            return
        rec = {
            "ts_ms": int(time.time() * 1000),
            "time": time.strftime("%H:%M:%S"),
            "dir": direction,
            "src_lang": src_lang,
            "dst_lang": dst_lang,
            "original": original,
            "translation": translation,
        }
        self._fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if self._fh and not self._fh.closed:
            self._fh.close()
