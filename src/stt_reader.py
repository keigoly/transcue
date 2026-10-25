"""STT ヘルパー(Swift)の起動と JSON ストリーム受信 — 1-B/1-C の境界。

Swift helper を asyncio subprocess で起動し、stdout の JSONL を dict イベントとして流す。
stdin に "finalize" / "quit" を送れる(1-C セグメンターがポーズ検出時に finalize を叩く)。
stderr(helper の診断)は別タスクで drain し、バッファ詰まりでのデッドロックを防ぐ。
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator, Callable, Optional


class STTHelper:
    def __init__(self, helper_bin: str, locale: str = "ja-JP",
                 on_diag: Optional[Callable[[dict], None]] = None,
                 extra_args: Optional[list[str]] = None):
        self.helper_bin = helper_bin
        self.locale = locale
        self.on_diag = on_diag
        # 送信方向(stt_helper, マイク)は locale のみ。受信方向(tap_helper)は音源指定
        # (--file / --pid / --name)を追加で渡す。両者はプロトコル互換なので同じ reader で扱える。
        self.extra_args = list(extra_args) if extra_args else []
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._stderr_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            self.helper_bin, self.locale, *self.extra_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for raw in self.proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if not line or self.on_diag is None:
                continue
            try:
                d = json.loads(line)
                # helper の診断は "phase" を持つ。Metrics.emit の引数 phase と衝突して TypeError になり、
                # この読み取りタスクごと止まっていた(2026-10-24 まで stt_diag は 0 件)。名前を変えて渡す。
                if isinstance(d, dict) and "phase" in d:
                    d["helper_phase"] = d.pop("phase")
                self.on_diag(d)
            except json.JSONDecodeError:
                pass
            except Exception:  # noqa: BLE001 — 診断の記録に失敗しても stderr の読み取りは続ける
                pass

    async def events(self) -> AsyncIterator[dict]:
        """helper stdout の認識イベント(ready/partial/final/bye)を dict で逐次返す。"""
        assert self.proc and self.proc.stdout
        async for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

    async def send(self, cmd: str) -> None:
        if self.proc and self.proc.stdin and not self.proc.stdin.is_closing():
            self.proc.stdin.write((cmd + "\n").encode())
            await self.proc.stdin.drain()

    async def finalize(self) -> None:
        await self.send("finalize")

    async def stop(self) -> None:
        """"quit" を送って穏当に終了。応答なければ SIGTERM。マイクを確実に解放する。"""
        if not self.proc:
            return
        try:
            await self.send("quit")
        except (ConnectionError, BrokenPipeError):
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self._stderr_task:
            self._stderr_task.cancel()
