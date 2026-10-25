"""meeting_watcher — Phase 3-I: 会議(Zoom・Meet など)の参加/退出を検知し、翻訳スタックを自動起動/停止する常駐監視。

ユーザー操作ゼロで:
  Zoom会議に参加 → run_meeting.command(送信+受信+字幕+英語ON/OFFパネル)を自動起動
                    (パネルは --start-off: 赤=地声パススルーで開始。クリックで英語翻訳へ)
  会議から退出   → SIGINT で全スタックを安全停止(マイク/タップ解放、パネル自動クローズ)

v2 G(2026-10-24): GUI(src/app の「TransCue」)の有効/無効に従う。
  - GUI が ~/.transcue/enabled.json に有効/無効を書き、毎ポーリングで読む(無ければ有効)。
  - 無効のまま会議に入ったら、翻訳スタックの代わりに地声パススルーだけを起動する(BYPASS)。
    Zoom のマイクは BlackHole のままなので、何も流さないと相手には無音になるため。
  - 会議中に無効にするとスタックを止めて BYPASS へ、有効にすると BYPASS を止めてスタックを起動する。
  - 自分の状態と心拍を ~/.transcue/watcher.json に毎ポーリング書く(GUI が表示する)。
  - 起動時(= ログイン時)に GUI を開く(config [gui].open_at_boot)。

検知シグナル(config [watcher].signal。"+" でつなぐと、どれか 1 つで会議中):
  "cpthost"     … /Applications/zoom.us.app/ 配下に CptHost プロセスが存在 = 会議中(既定。spike6 で実測確定)
  "virtual_mic" … 自分のアカウントのプロセスが BlackHole(仮想マイク)を使って入力中 = 会議中(v3 Step 2・
                  会議アプリを選ばない。Chrome の Meet で、ミュート中も続き・退出で 1 s 以内に止まることを確認)
  "file:<path>" … テスト用: そのファイルが存在すれば会議中とみなす(状態機械の単体検証)

状態機械:
  IDLE(待機)→[join連続n回]→ ACTIVE(会議中・スタック稼働)→[leave連続m回]→ 停止 → IDLE
  - 無効のときは join で BYPASS(地声パススルーだけ稼働)へ。会議中の有効/無効の切替で ACTIVE ⇄ BYPASS。
  - IDLE で手動起動の pipeline を検知したら EXTERNAL(関与しない)へ。退出で IDLE に戻る。
  - ACTIVE/BYPASS 中に子プロセスが死んだら max_relaunch 回まで再起動、超えたら FAILED(退出まで待つ)。
    FAILED 中に有効/無効を切り替えると IDLE に戻してやり直す。

ポーリングごとのログは出さない(常駐でログ肥大するため)。遷移・起動・停止・切替のみ JSONL(logs/watcher_*.jsonl)。
状態ファイル watcher.json は毎ポーリング上書きする(1 ファイル・数百バイト。プロジェクトの外に置く)。

実行:
  常駐(フォアグラウンド): spikes/.venv-ml/bin/python src/meeting_watcher.py
  状態確認(1回だけ)   : spikes/.venv-ml/bin/python src/meeting_watcher.py --status
  テスト用上書き         : --signal file:/tmp/x --launcher /path/to/stub.sh(config より優先)
                           --state-dir /tmp/st --bypass-cmd "/path/to/stub2.sh" --no-gui
LaunchAgent 登録(ログイン時自動常駐)は install_watcher.command で行う。
"""
from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from metrics import Metrics  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ZOOM_APP = "/Applications/zoom.us.app/"
AUDIO_PROCS = ROOT / "src" / "tap_helper" / "audio-procs"   # v3 Step 2: 音声プロセスの診断(Core Audio)
VIRTUAL_MIC = "BlackHole"                                    # 会議アプリのマイクに選ぶ仮想デバイス(名前の一部)
# このプロジェクトのプロセスの目印(コマンド行)。相対パスで起動した python も venv の名前で分かる
OWN_MARKERS = (str(ROOT), "spikes/.venv-ml/")
# GUI ⇄ watcher の状態ファイルの置き場所(プロジェクトの外)。
STATE_DIR = Path.home() / ".transcue"


def load_config() -> dict:
    with (Path(__file__).resolve().parent / "config.toml").open("rb") as f:
        cfg = tomllib.load(f)
    cfg["_root"] = str(ROOT)
    return cfg


def cpthost_running() -> bool:
    """zoom.us.app 配下の CptHost プロセスがある = Zoom の会議中。"""
    try:
        out = subprocess.run(["ps", "-axwwo", "comm="], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return False
    for comm in out.splitlines():
        comm = comm.strip()
        if comm.startswith(ZOOM_APP) and comm.rsplit("/", 1)[-1] == "CptHost":
            return True
    return False


def virtual_mic_users() -> list[dict]:
    """BlackHole(仮想マイク)を使って入力中の、自分のアカウントのプロセス = 会議アプリ(v3 Step 2)。
    Core Audio のプロセス情報(src/tap_helper/audio-procs)を読む。入力の機器の一覧は「入力の口を持つ機器」を返すので、
    マイクを読みながら BlackHole に書くプロセス(地声パススルーなど)も載る → このプロジェクトのプロセスは除く。
    同じ Mac にほかのユーザーがログインしていると、その人のプロセスも見えるので除く。読めなければ空(安全側)。"""
    try:
        out = subprocess.run([str(AUDIO_PROCS), "--json"], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    users = []
    for line in out.splitlines():
        try:
            p = json.loads(line)
        except ValueError:
            continue
        if not (p.get("mine") and p.get("in") and any(VIRTUAL_MIC in d for d in p.get("in_devices", []))):
            continue
        try:
            args = subprocess.run(["ps", "-o", "args=", "-p", str(p["pid"])], capture_output=True, text=True,
                                  timeout=5).stdout
        except Exception:
            args = ""
        if not any(mark in args for mark in OWN_MARKERS):
            users.append(p)
    return users


def in_meeting(sig_src: str) -> bool:
    """会議中か判定する(シグナル種別は config で切替・"+" でつなぐとどれか 1 つで会議中。判定失敗は False=安全側)。"""
    if sig_src.startswith("file:"):
        return Path(sig_src[5:]).exists()
    kinds = sig_src.split("+")
    return ("cpthost" in kinds and cpthost_running()) or ("virtual_mic" in kinds and bool(virtual_mic_users()))


def external_pipeline_running() -> bool:
    """手動起動の翻訳スタック(pipeline.py)が既に動いていないか(二重起動防止)。"""
    try:
        r = subprocess.run(["pgrep", "-f", "src/pipeline.py"], capture_output=True, text=True,
                           timeout=5)
        return bool(r.stdout.strip())
    except Exception:
        return False


class EnabledFlag:
    """GUI が書く有効/無効(enabled.json)。無ければ有効(GUI を使う前と同じ挙動)。
    読めないとき(壊れたファイルなど)は直前の値を保つ。"""

    def __init__(self, path: Path):
        self.path = path
        self.value = True

    def read(self) -> bool:
        try:
            self.value = bool(json.loads(self.path.read_text(encoding="utf-8")).get("enabled", True))
        except FileNotFoundError:
            self.value = True
        except (OSError, ValueError, AttributeError):
            pass
        return self.value


class StatusFile:
    """watcher の状態と心拍(watcher.json)。GUI が 1 s ごとに読む。書けなくても watcher は止めない。"""

    def __init__(self, path: Path, m: Metrics):
        self.path = path
        self.m = m
        self._warned = False

    def write(self, **fields) -> None:
        rec = {"v": 1, "heartbeat_ms": int(time.time() * 1000), "watcher_pid": os.getpid(), **fields}
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)   # 置き換えは一瞬(GUI が書きかけを読まない)
        except OSError as e:
            if not self._warned:         # 毎ポーリング同じ失敗を記録しない
                self._warned = True
                self.m.emit("WATCH", "status_write", ok=False, error=f"{type(e).__name__}: {e}")


def open_gui(app: Path, m: Metrics) -> None:
    """GUI(TransCue.app)を開く。開いていれば何もしない(open の挙動)。前面には出さない(-g)。"""
    if not app.exists():
        m.emit("WATCH", "gui_open", ok=False, app=str(app), error="not found(src/app/build.sh で組み立てる)")
        return
    try:
        r = subprocess.run(["open", "-g", str(app)], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=15)
        m.emit("WATCH", "gui_open", ok=r.returncode == 0, app=str(app), error=r.stderr.strip() or None)
    except (OSError, subprocess.TimeoutExpired) as e:
        m.emit("WATCH", "gui_open", ok=False, app=str(app), error=f"{type(e).__name__}: {e}")


class Stack:
    """子プロセス(run_meeting.command か地声パススルー)の管理(起動/生存確認/安全停止)。"""

    def __init__(self, cmd: list[str], log_dir: Path, m: Metrics, tag: str = "run", label: str = "スタック"):
        self.cmd = cmd
        self.log_dir = log_dir
        self.m = m
        self.tag = tag                 # ログ名 watcher_<tag>_*.log と JSONL の tag
        self.label = label             # 標準エラーの表示名
        self.proc: subprocess.Popen | None = None
        self.pgid: int | None = None   # 起動時に確定(launcher が死んでも group を掃討できるよう保持)
        self.log_path: Path | None = None

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def launch(self) -> bool:
        t0 = time.perf_counter()
        self.log_path = self.log_dir / f"watcher_{self.tag}_{datetime.now():%Y%m%d_%H%M%S}.log"

        def _reset_signals() -> None:
            # 親(watcher)が非対話シェルの & や launchd 経由で SIGINT=SIG_IGN を継承していると、
            # 子 bash は「起動時に無視されていたシグナルは trap できない」(POSIX)ため
            # run_meeting.command の INT trap が無効化される。子側で必ず既定に戻す。
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            signal.signal(signal.SIGTERM, signal.SIG_DFL)

        try:
            logf = self.log_path.open("w")
            self.proc = subprocess.Popen(
                self.cmd, cwd=str(ROOT), stdin=subprocess.DEVNULL,
                stdout=logf, stderr=subprocess.STDOUT, start_new_session=True,
                preexec_fn=_reset_signals)
            # start_new_session=True で launcher はグループ/セッションリーダー(pgid=pid)。
            # 起動直後に確定して保持する(退出停止時に launcher が既に死んでいても group を掃討できる)。
            try:
                self.pgid = os.getpgid(self.proc.pid)
            except ProcessLookupError:
                self.pgid = self.proc.pid
            self.m.emit("WATCH", "stack_launch", ok=True, tag=self.tag, pid=self.proc.pid, pgid=self.pgid,
                        ms=round((time.perf_counter() - t0) * 1000, 1), log=str(self.log_path))
            print(f"[watcher] {self.label}起動 pid={self.proc.pid} log={self.log_path.name}",
                  file=sys.stderr, flush=True)
            return True
        except Exception as e:  # noqa: BLE001
            self.m.emit("WATCH", "stack_launch", ok=False, tag=self.tag, error=f"{type(e).__name__}: {e}")
            print(f"[watcher] {self.label}起動失敗: {e}", file=sys.stderr, flush=True)
            return False

    def stop(self, timeout_s: float = 15.0) -> None:
        """プロセスグループ全体を SIGTERM で安全停止 → 残れば SIGKILL に段階昇格。

        launcher は start_new_session=True でグループ/セッションリーダーなので、
        os.killpg(pgid, ...) で launcher + pipeline + receive + それらの子(パネル/字幕/helper)を
        一括で確実に止められる。& 起動の子は SIGINT を無視するため必ず SIGTERM を使う
        (pipeline/receive は SIGTERM を捕まえて finally でマイク/タップを解放する)。"""
        if self.proc is None:
            return
        t0 = time.perf_counter()
        # 起動時に確定した pgid を使う(launcher が先に死んでいても group の子を掃討できる)。
        pgid = self.pgid if self.pgid is not None else self.proc.pid
        if self.proc.poll() is not None:
            # launcher は死んでいても、孤児化した子(同一 group)が残っていれば掃討する。
            self._sweep(pgid)
            self.proc = None
            return

        def _killpg(sig: int) -> None:
            try:
                os.killpg(pgid, sig)
            except ProcessLookupError:
                pass

        how = "TERM"
        _killpg(signal.SIGTERM)
        try:
            self.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            how = "KILL"
            _killpg(signal.SIGKILL)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self._sweep(pgid)   # SIGKILL 後も別グループへ逃げた子が無いか最終掃討
        self.m.emit("WATCH", "stack_stop", ok=True, tag=self.tag, how=how, pgid=pgid,
                    ms=round((time.perf_counter() - t0) * 1000, 1))
        print(f"[watcher] {self.label}停止({how}, {time.perf_counter() - t0:.1f}s)",
              file=sys.stderr, flush=True)
        self.proc = None

    def _sweep(self, pgid: int) -> None:
        """pgid グループに生き残りが居れば SIGKILL(孤児化・停止漏れの最終保険)。"""
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def status_once(sig_src: str, state_dir: Path) -> None:
    act = in_meeting(sig_src)
    ext = external_pipeline_running()
    enabled = EnabledFlag(state_dir / "enabled.json").read()
    print(f"signal={sig_src}  in_meeting={act}  pipeline_running={ext}  enabled={enabled}")
    if "virtual_mic" in sig_src.split("+"):
        for p in virtual_mic_users():
            print(f"  virtual_mic: pid {p['pid']} {p['name']} ({p['bundle'] or '-'}) 入力 {p['in_devices']}")
    try:
        print(f"watcher.json: {(state_dir / 'watcher.json').read_text(encoding='utf-8')}")
    except OSError:
        print("watcher.json: (なし)")


def _argv_opt(name: str) -> str | None:
    """テスト用 CLI 上書き(--signal / --launcher / --state-dir / --bypass-cmd)。config より優先する。"""
    argv = sys.argv[1:]
    if name in argv:
        i = argv.index(name)
        if i + 1 < len(argv):
            return argv[i + 1]
    return None


def main() -> None:
    cfg = load_config()
    wc = cfg.get("watcher", {})
    gc = cfg.get("gui", {})
    sig_src = _argv_opt("--signal") or wc.get("signal", "cpthost")
    state_dir = Path(_argv_opt("--state-dir") or os.environ.get("ZRT_STATE_DIR") or STATE_DIR).expanduser()
    if "--status" in sys.argv:
        status_once(sig_src, state_dir)
        return
    poll_s = float(wc.get("poll_s", 2.0))
    n_join = int(wc.get("debounce_join", 2))
    n_leave = int(wc.get("debounce_leave", 3))
    launcher = Path(_argv_opt("--launcher") or (ROOT / wc.get("launcher", "run_meeting.command")))
    max_relaunch = int(wc.get("max_relaunch", 1))
    bypass_on = bool(wc.get("bypass_when_disabled", True))
    bypass_arg = _argv_opt("--bypass-cmd")
    bypass_cmd = shlex.split(bypass_arg) if bypass_arg else \
        [sys.executable, str(ROOT / "src" / "passthrough.py"), "--serve"]
    m = Metrics.from_config(cfg, run_tag="watcher")
    stack = Stack([str(launcher)], ROOT / "logs", m)
    bypass = Stack(bypass_cmd, ROOT / "logs", m, tag="bypass", label="地声パススルー")
    flag = EnabledFlag(state_dir / "enabled.json")
    status = StatusFile(state_dir / "watcher.json", m)

    # launchd(SIGTERM)/Ctrl+C(SIGINT)/HUP どれでも、稼働中スタックを道連れにせず安全停止してから終了。
    # シグナルハンドラで Event を set し、ループの Event.wait() を即中断する(中断可能スリープ)。
    # ※ ハンドラで sys.exit() を投げる方式は subprocess.run 実行中などに取りこぼす実測があったため、
    #   Event 方式にして確実に停止させる。
    stop_event = threading.Event()

    def _request_stop(*_: object) -> None:
        stop_event.set()

    for _s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(_s, _request_stop)

    enabled = flag.read()
    m.emit("WATCH", "watcher_boot", signal=sig_src, poll_s=poll_s, debounce_join=n_join,
           debounce_leave=n_leave, launcher=str(launcher), max_relaunch=max_relaunch,
           enabled=enabled, bypass_when_disabled=bypass_on, state_dir=str(state_dir))
    print(f"[watcher] 常駐開始 signal={sig_src} poll={poll_s}s join×{n_join} leave×{n_leave}"
          f" enabled={enabled}", file=sys.stderr, flush=True)
    if gc.get("open_at_boot", True) and "--no-gui" not in sys.argv:
        open_gui(ROOT / gc.get("app", "src/app/TransCue.app"), m)

    state = "IDLE"
    join_streak = leave_streak = relaunches = 0
    since_ms = int(time.time() * 1000)
    act = False

    def publish(busy: str = "") -> None:
        """GUI 向けに状態を書く。busy はポーリングが止まる処理(スタック停止など)の説明。"""
        live = stack if stack.alive() else bypass if bypass.alive() else None
        status.write(state=state, enabled=enabled, in_meeting=act, since_ms=since_ms, busy=bool(busy),
                     detail=busy, child_pid=live.proc.pid if live and live.proc else None, poll_s=poll_s)

    def transition(new: str, **kw) -> None:
        nonlocal state, join_streak, leave_streak, since_ms
        m.emit("WATCH", "state", frm=state, to=new, **kw)
        print(f"[watcher] {state} → {new} {kw if kw else ''}", file=sys.stderr, flush=True)
        state = new
        join_streak = leave_streak = 0
        since_ms = int(time.time() * 1000)
        publish()

    def start_live(reason: str | None = None) -> None:
        """会議中の起動: 有効なら翻訳スタック、無効なら地声パススルー。手動起動の pipeline があれば関与しない。"""
        nonlocal relaunches
        kw = {"reason": reason} if reason else {}
        if external_pipeline_running():
            transition("EXTERNAL", reason="手動起動の pipeline を検知(関与しない)")
        elif enabled:
            if stack.launch():
                relaunches = 0
                transition("ACTIVE", **kw)
            else:
                transition("FAILED", reason="launch失敗")
        elif bypass.launch():
            relaunches = 0
            transition("BYPASS", reason=reason or "無効のため地声だけ")
        else:
            transition("FAILED", reason="地声パススルーの起動失敗")

    publish()
    try:
        while not stop_event.is_set():
            act = in_meeting(sig_src)
            now_enabled = flag.read()
            flag_changed = now_enabled != enabled
            if flag_changed:
                m.emit("WATCH", "enabled_change", enabled=now_enabled, state=state, in_meeting=act)
                print(f"[watcher] {'有効' if now_enabled else '無効'}に切替(state={state})",
                      file=sys.stderr, flush=True)
                enabled = now_enabled
            if state == "IDLE":
                # 無効かつ地声パススルーも使わない設定なら、会議に入っても何もしない。
                want = act and (enabled or bypass_on)
                join_streak = join_streak + 1 if want else 0
                if join_streak >= n_join:
                    start_live()
            elif state == "ACTIVE":
                leave_streak = leave_streak + 1 if not act else 0
                if leave_streak >= n_leave:
                    transition("IDLE", reason="会議退出")
                    publish(busy="翻訳を停止しています…")
                    stack.stop()
                elif not enabled:
                    publish(busy="翻訳を停止しています…")
                    stack.stop()
                    if bypass_on:
                        start_live(reason="会議中に無効化")
                    else:
                        transition("IDLE", reason="会議中に無効化")
                elif not stack.alive():
                    if relaunches < max_relaunch:
                        relaunches += 1
                        m.emit("WATCH", "stack_relaunch", n=relaunches)
                        print(f"[watcher] スタックが終了していたため再起動({relaunches}/{max_relaunch})",
                              file=sys.stderr, flush=True)
                        if not stack.launch():
                            transition("FAILED", reason="relaunch失敗")
                    else:
                        transition("FAILED", reason=f"再起動上限({max_relaunch})超過")
            elif state == "BYPASS":
                leave_streak = leave_streak + 1 if not act else 0
                if leave_streak >= n_leave:
                    transition("IDLE", reason="会議退出")
                    bypass.stop()
                elif enabled:
                    bypass.stop()
                    start_live(reason="会議中に有効化")
                elif not bypass.alive():
                    if relaunches < max_relaunch:
                        relaunches += 1
                        m.emit("WATCH", "bypass_relaunch", n=relaunches)
                        if not bypass.launch():
                            transition("FAILED", reason="地声パススルーの再起動失敗")
                    else:
                        transition("FAILED", reason=f"地声パススルーの再起動上限({max_relaunch})超過")
            elif state == "EXTERNAL":
                leave_streak = leave_streak + 1 if not act else 0
                if leave_streak >= n_leave:
                    transition("IDLE", reason="会議退出")
            elif state == "FAILED":
                leave_streak = leave_streak + 1 if not act else 0
                if leave_streak >= n_leave:
                    transition("IDLE", reason="会議退出")
                elif flag_changed:
                    transition("IDLE", reason="有効/無効の切替でやり直し")
            publish()
            stop_event.wait(poll_s)   # 中断可能スリープ: シグナルで即復帰
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        for child in (stack, bypass):
            if child.alive():
                print(f"[watcher] 終了処理: 稼働中の{child.label}を停止します…", file=sys.stderr, flush=True)
                child.stop()
        m.emit("WATCH", "watcher_exit", state=state)
        m.close()
        print("[watcher] 終了", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
