#!/bin/bash
# transcue ワンクリック起動
# 送信(日本語 → 英語音声を Zoom へ)+ 受信(相手の英語 → 日本語字幕ウィンドウ)を同時に起動する。
#
# 使い方:
#   ・Finder でこのファイルをダブルクリック、または
#   ・ターミナルで  ./run_meeting.command  [受信音源PID]
#   受信PID を省略すると Zoom アプリ(--name zoom)をタップする。字幕が出ないときは
#     src/tap_helper/tap-helper --list
#   を相手が話している間に実行し、♪ が付いた zoom 系の PID を引数で渡す(例: ./run_meeting.command 6785)。
#
#   終了: このウィンドウで Ctrl+C(送信・受信とも安全に停止し、マイク/タップを解放する)。
#         ⚠️ ウィンドウを閉じるだけだとマイクが残ることがあるので、なるべく Ctrl+C で止める。
#
# 事前設定(Zoom): マイク = BlackHole 2ch / マイクモード = ミュージシャン用オリジナルサウンド /
#                  スピーカー = ヘッドホン。詳細は docs/history/2E_CHECKLIST.md。

PROJ="$(cd "$(dirname "$0")" && pwd)"
PY="$PROJ/spikes/.venv-ml/bin/python"
DUR=28800   # 8時間(会議中ずっと)。実際は Ctrl+C で終了する。

cd "$PROJ" || { echo "プロジェクトが見つかりません: $PROJ"; exit 1; }
if [ ! -x "$PY" ]; then
  echo "venv python が見つかりません: $PY"
  echo "→ bash spikes/setup_env.sh で venv を作り直してください。"
  exit 1
fi
mkdir -p logs

# 受信の音源: 引数があれば --pid、なければ config [receive].tap_mode(既定 = 送信以外の全体タップ)。
# ⚠️ 2026-10-24: 以前は「なければ --name zoom」で、config の tap_mode を上書きしていた。以前の会議では
#    自動起動の受信が --name zoom で 85 分間字幕 0 件、手動の全体タップ(--exclude-pid)で 46 件だった。
if [ -n "$1" ]; then RECV_SRC="--pid $1"; else RECV_SRC=""; fi

# 翻訳の方式(v2 A2-1): "server" なら翻訳サーバ(PLaMo を 1 つだけ載せる)を送受信より先に起動し、送受信はそれを
# 共有する(送受信は受け口が開く = 読み込みが終わるまで待つ)。config [translate].mode(ZRT_TRANSLATE_MODE で上書き)。
# 読めなければ従来どおり "local"(送受信がそれぞれモデルを持つ)。
MODE=$("$PY" -c 'import os, tomllib; print(os.environ.get("ZRT_TRANSLATE_MODE") or tomllib.load(open("src/config.toml", "rb"))["translate"].get("mode", "local"))' 2>/dev/null)
[ -n "$MODE" ] || MODE=local
TRS=""
if [ "$MODE" = "server" ]; then
  "$PY" src/translate_server.py "$DUR" >logs/run_translator.log 2>&1 &
  TRS=$!
fi

echo "▶ 送信(日→英音声)と 受信(英→日字幕)を起動します…(翻訳: $MODE)"
# --start-off: 英語OFF(赤・地声パススルー)で開始。ボタン/⌃⌥P で英語翻訳へ切替(Phase 3-I)。
"$PY" src/pipeline.py "$DUR" --to-zoom --panel --start-off  >logs/run_send.log 2>&1 &
SEND=$!
"$PY" src/receive.py "$DUR" --window $RECV_SRC   >logs/run_recv.log 2>&1 &
RECV=$!
# 重さの記録(v2 A1): プロセスごとのメモリ・CPU・GPU を logs/resmon_*.jsonl へ。
# config [resmon].enabled = false なら resmon はすぐ終わる。
"$PY" src/resmon.py "$DUR" >logs/run_resmon.log 2>&1 &
RES=$!

# Ctrl+C / 終了シグナルで両方を安全停止。
# ⚠️ SIGINT ではなく SIGTERM を送る: 非対話シェルで & 起動した子(pipeline/receive)は
#    SIGINT が SIG_IGN にされるため kill -INT では止まらない。pipeline/receive は SIGTERM を
#    捕まえて finally でマイク/タップを解放する(add_signal_handler)。
cleanup() {
  echo
  echo "停止中…"
  kill -TERM "$SEND" "$RECV" "$RES" $TRS 2>/dev/null
  wait "$SEND" "$RECV" "$RES" $TRS 2>/dev/null
  echo "停止しました。"
  exit 0
}
trap cleanup INT TERM HUP

# 起動確認(モデルロードに数十秒。両プロセスが生きているか数秒後にチェック)。
sleep 5
if kill -0 "$SEND" 2>/dev/null && kill -0 "$RECV" 2>/dev/null && { [ -z "$TRS" ] || kill -0 "$TRS" 2>/dev/null; }; then
  echo "✅ 起動中(送信 PID=$SEND / 受信 PID=$RECV${TRS:+ / 翻訳サーバ PID=$TRS})。モデルロードに ~20〜30秒かかります。"
  echo "   ・受信字幕はフローティングウィンドウに表示(ドラッグ移動 / エッジでリサイズ / ⌃⌥L で固定⇄移動)。"
  echo "   ・起動直後は 赤🔴英語OFF(地声がそのまま相手へ)。ボタンクリック or ⌃⌥P で 緑🔊英語ON(英語翻訳)。"
  echo "   ・初回の一言だけ翻訳に ~30秒(ウォームアップ)。以降は 1〜3秒。"
  echo "   ・ログ: logs/run_send.log(送信) / logs/run_recv.log(受信)"
  echo "   ・終了: このウィンドウで Ctrl+C"
else
  echo "⚠️ 起動に失敗した可能性があります。ログを確認してください:"
  echo "   tail -n 40 logs/run_send.log ; tail -n 40 logs/run_recv.log ; tail -n 40 logs/run_translator.log"
fi

# 送信・受信の両方が終了する(または Ctrl+C)まで待つ。resmon は待たない(8 時間動き続けるため、
# 送受信が落ちても launcher が生き残り、watcher が再起動できなくなる)。終わったら resmon も止める。
# 翻訳サーバ(server モード)が止まったら送受信とも翻訳できないので、全部止めて終わる(watcher が起動し直す)。
# bash 3.2 には wait -n が無いので 2 秒ごとに確かめる(sleep を & と wait にして、止める信号ですぐ trap が動くように)。
RC=0
while kill -0 "$SEND" 2>/dev/null || kill -0 "$RECV" 2>/dev/null; do
  if [ -n "$TRS" ] && ! kill -0 "$TRS" 2>/dev/null; then
    echo "⚠️ 翻訳サーバが止まりました。送受信も止めます(ログ: logs/run_translator.log)。"
    kill -TERM "$SEND" "$RECV" 2>/dev/null
    wait "$SEND" "$RECV" 2>/dev/null
    RC=3
    break
  fi
  sleep 2 &
  wait $!
done
kill -TERM "$RES" $TRS 2>/dev/null
wait "$RES" $TRS 2>/dev/null
exit $RC
