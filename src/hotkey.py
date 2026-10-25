"""グローバルホットキー(一時停止/再開)— Phase 3-E。

hotkey_helper(Carbon RegisterEventHotKey, 既定 ⌃⌥P)を起動し、`toggle` イベントで
`paused` を反転する。Accessibility 許可不要。receive.py / pipeline.py が `--hotkey` で使う。

orchestrator は毎節の頭で `listener.paused` を見て、True の間は翻訳/TTS送出/字幕をスキップする
(一時停止中の発話は送らない=プライベートな会話を相手に流さない)。
"""
from __future__ import annotations

import asyncio
import json
from typing import Callable, Optional


class HotkeyListener:
    def __init__(self, bin_path: str, args: Optional[list[str]] = None,
                 on_change: Optional[Callable[[bool], object]] = None,
                 paused: bool = False):
        self.bin_path = bin_path
        self.args = list(args) if args else []
        self.on_change = on_change  # (paused: bool) -> None | Awaitable
        self.paused = paused        # 初期状態(--start-off のパネルと合わせるときは True で生成)
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self.proc = await asyncio.create_subprocess_exec(
            self.bin_path, *self.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._tasks = [asyncio.create_task(self._read()), asyncio.create_task(self._drain_err())]

    async def _read(self) -> None:
        assert self.proc and self.proc.stdout
        async for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "toggle":
                self.paused = not self.paused
                if self.on_change:
                    res = self.on_change(self.paused)
                    if asyncio.iscoroutine(res):
                        await res
            elif obj.get("type") == "ready" and "on" in obj:
                # パネルの初期状態(--start-off 等)に同期(on=英語通訳中 ⇔ paused=False)。
                new_paused = not bool(obj["on"])
                if new_paused != self.paused:
                    self.paused = new_paused
                    if self.on_change:
                        res = self.on_change(self.paused)
                        if asyncio.iscoroutine(res):
                            await res

    async def _drain_err(self) -> None:
        assert self.proc and self.proc.stderr
        async for _ in self.proc.stderr:
            pass  # helper の診断は捨てる(バッファ詰まり防止)

    async def stop(self) -> None:
        if not self.proc:
            return
        try:
            if self.proc.stdin and not self.proc.stdin.is_closing():
                self.proc.stdin.write(b"quit\n")
                await self.proc.stdin.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            self.proc.terminate()
        for t in self._tasks:
            t.cancel()
