"""bench/make_audio.py — v2 A1: 再現会議ベンチの音声(日本語・英語の 2 本)を作る。

macOS の `say`(Kyoko / Samantha)で 1 文ずつ合成し、交互に話す打ち合わせの形に並べる。
文は一般的な打ち合わせを想定して作ったもの(実際の会議の文は使わない)。

並び(既定 600 s):
  0 s〜     会話(日本語の番 → 英語の番 → … 各番 2 文)   … PART_A_END_S まで
  無音      IDLE_S 秒(待機中の負荷と、長い無音の後の 1 発目の遅れを測る)
  再開〜    会話                                      … 末尾 TAIL_S 秒は無音
出力: src/bench/audio/ja.m4a・en.m4a(AAC 64 kbps・24 kHz・モノラル)と timeline.json(各文の位置)。
同じ入力で何度も比べるため、一度作ったら作り直さない(--force で作り直す)。

実行:
    spikes/.venv-ml/bin/python src/bench/make_audio.py [--force]
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

OUT = Path(__file__).resolve().parent / "audio"
SR = 24000
DURATION_S = 600.0
PART_A_END_S = 150.0
IDLE_S = 240.0
TAIL_S = 15.0
GAP_SENTENCE_S = 0.7     # 同じ人の文と文の間
GAP_TURN_S = 2.0         # 話し手が替わるときの間
VOICES = {"ja": "Kyoko", "en": "Samantha"}

JA = [
    "お疲れさまです。今日は来月のリリース計画について確認させてください。",
    "まず、テスト環境の準備は予定どおり今週中に終わる見込みです。",
    "ただ、性能試験の結果がまだ揃っていないので、少し様子を見たいと思っています。",
    "その点について、そちらのチームのご意見を伺えますか。",
    "はい、承知しました。",
    "では、来週の水曜日までに資料を共有します。",
    "もう一つ確認ですが、ログの保存期間は九十日で問題ないでしょうか。",
    "移行作業は週末の夜間に行う予定で、作業時間はおよそ三時間を見込んでいます。",
    "万が一のときは、元の構成にすぐ戻せるように手順を用意しています。",
    "なるほど、それなら安心です。",
    "費用については、当初の見積もりから大きな変更はありません。",
    "ただし、追加のライセンスが必要になった場合は、別途ご相談させてください。",
    "次回の打ち合わせは、来週の木曜日の午後でいかがでしょうか。",
    "ありがとうございます。それで進めましょう。",
    "先日いただいたご質問ですが、社内で確認したところ、仕様どおりの動作でした。",
    "画面の表示が遅い件は、キャッシュの設定を見直すことで改善できそうです。",
    "修正版は今月末までにお渡しできると思います。",
    "少し補足させてください。",
    "利用者向けの説明会は、リリースの一週間前に開く予定です。",
    "質問があれば、チャットでいつでもご連絡ください。",
    "承知しました。こちらでも確認しておきます。",
    "スケジュールに遅れが出そうな場合は、早めにお知らせします。",
    "以上です。何か気になる点はありますか。",
    "本日はお時間をいただき、ありがとうございました。",
]
EN = [
    "Thanks for setting this up. We reviewed the test plan yesterday.",
    "Overall it looks good, but we have a concern about the database migration.",
    "Could you share the rollback procedure before the release?",
    "Sure, that makes sense.",
    "We would also like to run the load test one more time next week.",
    "How long do you expect the downtime to be?",
    "Three hours should be fine for us, as long as it happens over the weekend.",
    "Our security team asked whether the logs are encrypted at rest.",
    "If possible, please send us the updated architecture diagram.",
    "Got it, thank you.",
    "On our side, the user training materials are almost ready.",
    "We are planning to invite about fifty people to the first session.",
    "One more thing, the budget for the additional licenses has been approved.",
    "Let's keep the Thursday afternoon slot for the next meeting.",
    "I think the cache change you mentioned should solve the performance issue.",
    "Please let us know if anything changes in the schedule.",
    "That sounds great.",
    "We will review the document and come back to you by Friday.",
    "Is there anything else we need to prepare from our end?",
    "Just to confirm, the release date is still the fifteenth, right?",
    "Okay, we will update the internal announcement accordingly.",
    "Could you repeat the last point about the license?",
    "No, I think we covered everything today.",
    "Thanks again for your time, and have a good weekend.",
]


def synth(text: str, voice: str, tmp: Path) -> np.ndarray:
    f = tmp / "s.wav"
    subprocess.run(["say", "-v", voice, "-o", str(f), "--file-format=WAVE", f"--data-format=LEI16@{SR}", text],
                   check=True)
    a, sr = sf.read(str(f), dtype="float32")
    assert sr == SR, sr
    return a if a.ndim == 1 else a.mean(axis=1)


def main() -> int:
    force = "--force" in sys.argv
    OUT.mkdir(parents=True, exist_ok=True)
    if (OUT / "timeline.json").exists() and not force:
        print(f"既にあります: {OUT}(作り直すなら --force)")
        return 0
    n = int(DURATION_S * SR)
    tracks = {"ja": np.zeros(n, dtype=np.float32), "en": np.zeros(n, dtype=np.float32)}
    segments = []
    idx = {"ja": 0, "en": 0}
    texts = {"ja": JA, "en": EN}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        t = 1.0
        side = "ja"
        resume_s = PART_A_END_S + IDLE_S
        while True:
            turn = []
            for _ in range(2):
                i = idx[side] % len(texts[side])
                idx[side] += 1
                turn.append((texts[side][i], synth(texts[side][i], VOICES[side], tmp)))
            turn_len = sum(len(a) for _, a in turn) / SR + GAP_SENTENCE_S
            # 前半の終わり・全体の終わりを越える番は入れない(無音区間と末尾の無音を保つ)。
            if t < PART_A_END_S and t + turn_len > PART_A_END_S:
                t = resume_s
            if t + turn_len > DURATION_S - TAIL_S:
                break
            for text, a in turn:
                s = int(t * SR)
                tracks[side][s:s + len(a)] += a
                segments.append({"side": side, "start_s": round(t, 2), "end_s": round(t + len(a) / SR, 2),
                                 "text": text})
                t += len(a) / SR + GAP_SENTENCE_S
            t += GAP_TURN_S - GAP_SENTENCE_S
            side = "en" if side == "ja" else "ja"
        sha = {}
        for name, a in tracks.items():
            wav = tmp / f"{name}.wav"
            sf.write(str(wav), np.clip(a, -1, 1), SR, subtype="PCM_16")
            m4a = OUT / f"{name}.m4a"
            subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", "-b", "64000", str(wav), str(m4a)], check=True)
            sha[name] = hashlib.sha256(m4a.read_bytes()).hexdigest()[:16]
    speech_end_a = max(s["end_s"] for s in segments if s["start_s"] < PART_A_END_S)
    resume = min(s["start_s"] for s in segments if s["start_s"] >= PART_A_END_S)
    timeline = {"duration_s": DURATION_S, "sample_rate": SR, "voices": VOICES,
                "idle": {"start_s": round(speech_end_a, 2), "end_s": resume},
                "counts": {k: sum(1 for s in segments if s["side"] == k) for k in ("ja", "en")},
                "sha256_16": sha, "generated": time.strftime("%Y-%m-%d %H:%M:%S"), "segments": segments}
    (OUT / "timeline.json").write_text(json.dumps(timeline, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"作成: {OUT}  文の数={timeline['counts']}  無音={timeline['idle']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
