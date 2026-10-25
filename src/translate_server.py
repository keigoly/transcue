"""翻訳サーバ — v2 A2-1: 送受信で翻訳モデル(PLaMo)を 1 つだけ載せて共有する。

送信(pipeline.py)と受信(receive.py)は、[translate] mode = "server"(ZRT_TRANSLATE_MODE=server で上書き)のとき、
翻訳をこのサーバに頼む(translate_client.RemoteTranslator)。モデルの読み込み・warmup・keep-warm はここで 1 回だけ。
A1 のベンチでは、送受信がそれぞれ PLaMo を持って定常 11.8 GB、相手側の生成と重なると翻訳 p50 が +0.8〜1.2 s だった。

- 受け口: Unix ソケット([translate] server_sock。ZRT_TRANSLATOR_SOCK で上書き)。モデルを読み終えてから開く
  (つながった = 使える)。
- 1 行 1 JSON。依頼 {"op":"translate","id","text","src","dst","prio","stream"} → 返事 {"id","partial"}…(stream のみ)
  → {"id","done":true,"out","first_token_ms","gen_ms","queue_ms","ok","error"}。{"op":"ping"} → {"pong":true}。
  {"op":"wake"}(返事なし): 話し始めの知らせ。GPU を起こす(v2 A2-2。Translator.wake)。
- 生成は 1 つずつ。待ちは prio の小さい順(送信 0 → 受信 1)、同じ prio は来た順。生成中の打ち切りはしない。

単体実行: spikes/.venv-ml/bin/python src/translate_server.py [max_s]
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import signal
import sys
import time
from pathlib import Path

_T_START = time.time()   # boot_ms = 起動から受け口を開くまで

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import Metrics, mlx_mem  # noqa: E402
from translate import Translator, load_config, server_sock_path  # noqa: E402


async def _send(writer: asyncio.StreamWriter, obj: dict) -> bool:
    """1 行送る。相手が切れていたら False(生成は続けて捨てる)。"""
    if writer.is_closing():
        return False
    try:
        writer.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        await writer.drain()
        return True
    except (ConnectionError, OSError):
        return False


async def main() -> None:
    cfg = load_config()
    max_s = float(sys.argv[1]) if len(sys.argv) > 1 else 28800.0
    m = Metrics.from_config(cfg, run_tag="translator")
    sock = server_sock_path(cfg)
    tr = Translator(cfg, m)
    print(f"[translator] PLaMo ロード中… sock={sock}", file=sys.stderr, flush=True)
    await tr.load()
    tcfg = cfg["translate"]
    tr.start_keepwarm(float(tcfg.get("keepwarm_check_s", 4)), float(tcfg.get("keepwarm_idle_s", 12)),
                      "Japanese", "English")

    q: asyncio.PriorityQueue = asyncio.PriorityQueue()
    seq = itertools.count()
    served = {0: 0, 1: 0}

    async def serve_one(req: dict, writer: asyncio.StreamWriter, t_enq: float) -> None:
        rid = req.get("id") or "?"
        t0 = time.perf_counter()
        first_ms: float | None = None
        last = ""
        ok, err = True, None
        alive = True
        try:
            async for partial in tr.translate_stream(rid, req.get("text", ""), src=req.get("src", "Japanese"),
                                                     dst=req.get("dst", "English")):
                if first_ms is None:
                    first_ms = round((time.perf_counter() - t0) * 1000, 1)
                last = partial
                if req.get("stream") and alive:
                    alive = await _send(writer, {"id": rid, "partial": partial})
        except Exception as e:  # noqa: BLE001 — 1 件の失敗でサーバを止めない
            ok, err = False, f"{type(e).__name__}: {e}"
        await _send(writer, {"id": rid, "done": True, "out": last.strip(), "first_token_ms": first_ms,
                             "gen_ms": round((time.perf_counter() - t0) * 1000, 1),
                             "queue_ms": round((t0 - t_enq) * 1000, 1), "ok": ok, "error": err})

    async def worker() -> None:
        while True:
            prio, _, req, writer, t_enq = await q.get()
            served[min(prio, 1)] += 1
            await serve_one(req, writer, t_enq)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                try:
                    req = json.loads(line)
                except ValueError:
                    continue
                if req.get("op") == "ping":
                    await _send(writer, {"pong": True})
                elif req.get("op") == "wake":              # 話し始め: GPU を起こす(生成中なら何もしない)
                    tr.wake()
                elif req.get("op") == "translate":
                    prio = req.get("prio", 1)
                    await q.put((prio if isinstance(prio, int) else 1, next(seq), req, writer, time.perf_counter()))
        except (ConnectionError, OSError):
            pass

    Path(sock).parent.mkdir(parents=True, exist_ok=True)
    if os.path.exists(sock):
        os.unlink(sock)                       # 前回の残り(落ちたサーバのソケット)
    server = await asyncio.start_unix_server(handle, path=sock)
    os.chmod(sock, 0o600)
    m.emit("BOOT", "boot", note="translator (shared PLaMo)", sock=sock, max_s=max_s,
           boot_ms=round((time.time() - _T_START) * 1000), bench=os.environ.get("ZRT_BENCH_ID"), **mlx_mem())
    print("[translator] 準備完了", file=sys.stderr, flush=True)

    wtask = asyncio.ensure_future(worker())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await asyncio.wait_for(stop.wait(), timeout=max_s)
    except asyncio.TimeoutError:
        pass
    finally:
        server.close()
        wtask.cancel()
        await tr.stop_keepwarm()
        try:
            os.unlink(sock)
        except FileNotFoundError:
            pass
        m.emit("SUMMARY", "translator_summary", served_send=served[0], served_recv=served[1])
        m.close()


if __name__ == "__main__":
    asyncio.run(main())
