"""翻訳サーバの使い手 — v2 A2-1。Translator と同じ使い方(load / translate / translate_stream / keep-warm)で、
翻訳は translate_server.py に頼む。モデルを読み込まないので、このプロセスは PLaMo(5 GB)を持たない。

- load(): サーバの受け口が開く(= モデルの読み込みと warmup が済む)まで待ってつなぐ。warmup はサーバ側で済んでいる。
- keep-warm はサーバが 1 つだけ回すので、ここの start_keepwarm / stop_keepwarm は何もしない。
- metrics の translate には、使い手から見た ms(待ち + 生成 + 受け渡し)と、サーバ側の内訳
  (queue_ms = 順番待ち・gen_ms = 生成・gen_first_token_ms = 生成開始から初トークン)を残す。
- サーバが落ちたら、その後の翻訳は空(ok=false)を返す(呼び手はその節を飛ばす)。つなぎ直しはしない。
"""
from __future__ import annotations

import asyncio
import json
import time

from metrics import Metrics
from translate import Translator, server_sock_path


class RemoteTranslator:
    WARMUP_JA_EN = Translator.WARMUP_JA_EN
    WARMUP_EN_JA = Translator.WARMUP_EN_JA

    def __init__(self, cfg: dict, metrics: Metrics | None = None, prio: int = 1):
        self.sock = server_sock_path(cfg)
        self.m = metrics
        self.prio = prio                                   # 0 = 送信(優先)/ 1 = 受信
        self.connect_timeout_s = float(cfg["translate"].get("server_connect_timeout_s", 600))
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Queue] = {}
        self._rx_task: asyncio.Task | None = None

    async def load(self, warmup_texts: list[str] | None = None,
                   src: str = "Japanese", dst: str = "English") -> None:
        t0 = time.perf_counter()
        deadline = time.monotonic() + self.connect_timeout_s
        while True:
            try:
                self._reader, self._writer = await asyncio.open_unix_connection(self.sock)
                break
            except OSError:                                # まだ読み込み中(ソケットが無い・受け付けない)
                if time.monotonic() > deadline:
                    raise RuntimeError(f"翻訳サーバにつながりません: {self.sock}")
                await asyncio.sleep(0.5)
        self._rx_task = asyncio.ensure_future(self._rx())
        if self.m:
            self.m.emit("TRANSLATE", "translate_load", ms=round((time.perf_counter() - t0) * 1000),
                        remote=True, sock=self.sock)

    async def _rx(self) -> None:
        try:
            while line := await self._reader.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                q = self._pending.get(msg.get("id"))
                if q:
                    q.put_nowait(msg)
        except (ConnectionError, OSError):
            pass
        for q in list(self._pending.values()):             # 切れた: 待っている依頼を終わらせる
            q.put_nowait({"done": True, "ok": False, "error": "server_disconnected", "out": ""})

    async def _request(self, rid: str, text: str, src: str, dst: str, stream: bool):
        if self._writer is None or self._writer.is_closing() or (self._rx_task and self._rx_task.done()):
            raise ConnectionError("translator_not_connected")
        q: asyncio.Queue = asyncio.Queue()
        self._pending[rid] = q
        try:
            self._writer.write((json.dumps({"op": "translate", "id": rid, "text": text, "src": src, "dst": dst,
                                            "prio": self.prio, "stream": stream}, ensure_ascii=False)
                                + "\n").encode())
            await self._writer.drain()
            while True:
                msg = await q.get()
                yield msg
                if msg.get("done"):
                    return
        finally:
            self._pending.pop(rid, None)

    def _emit(self, rid: str, text: str, final: dict, t0: float, first_ms: float | None, **extra) -> None:
        if not self.m:
            return
        self.m.emit(rid, "translate", src=text, out=final.get("out", ""), engine="plamo", remote=True,
                    first_token_ms=round(first_ms, 1) if first_ms is not None else None,
                    gen_first_token_ms=final.get("first_token_ms"), queue_ms=final.get("queue_ms"),
                    gen_ms=final.get("gen_ms"), ms=round((time.perf_counter() - t0) * 1000, 1),
                    ok=bool(final.get("ok")), error=final.get("error"), **extra)

    async def translate(self, rid: str, text: str, src: str = "Japanese", dst: str = "English") -> str:
        t0 = time.perf_counter()
        final: dict = {}
        try:
            async for msg in self._request(rid, text, src, dst, stream=False):
                final = msg
        except (ConnectionError, OSError) as e:
            final = {"ok": False, "error": f"{type(e).__name__}: {e}", "out": ""}
        ft = final.get("first_token_ms")
        # 一括の依頼では部分訳が来ないので、使い手から見た初トークン = 順番待ち + 生成開始から初トークン
        self._emit(rid, text, final, t0, (final.get("queue_ms") or 0) + ft if ft is not None else None)
        return final.get("out", "") if final.get("ok") else ""

    async def translate_stream(self, rid: str, text: str, src: str = "Japanese", dst: str = "English"):
        t0 = time.perf_counter()
        final: dict = {}
        first_ms: float | None = None
        try:
            async for msg in self._request(rid, text, src, dst, stream=True):
                if msg.get("done"):
                    final = msg
                    break
                if first_ms is None:
                    first_ms = (time.perf_counter() - t0) * 1000
                yield msg.get("partial", "")
        except (ConnectionError, OSError) as e:
            final = {"ok": False, "error": f"{type(e).__name__}: {e}", "out": ""}
        self._emit(rid, text, final, t0, first_ms, streamed=True)

    def wake(self) -> None:
        """話し始めを翻訳サーバに知らせる(サーバが GPU を起こす。返事は待たない。v2 A2-2)。"""
        if self._writer is None or self._writer.is_closing():
            return
        try:
            self._writer.write(b'{"op": "wake"}\n')
        except (ConnectionError, OSError):
            pass

    def start_keepwarm(self, check_s: float, idle_s: float, src: str, dst: str) -> None:
        return None                                        # keep-warm はサーバ側で 1 つだけ

    async def stop_keepwarm(self) -> None:
        return None
