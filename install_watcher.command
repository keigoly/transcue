#!/bin/bash
# meeting_watcher の LaunchAgent 登録(ログイン時自動常駐)— Phase 3-I Step 2d
#
# 使い方:
#   ・Finder でダブルクリック(=install)、またはターミナルで:
#       ./install_watcher.command            # 登録して今すぐ開始(ログイン時も自動開始)
#       ./install_watcher.command uninstall  # 解除(常駐停止+ログイン時自動開始もやめる)
#       ./install_watcher.command status     # 状態確認
#
# 仕組み: ~/Library/LaunchAgents/local.transcue.watcher.plist を生成して launchctl で登録。
#   watcher は Zoom会議参加を検知すると run_meeting.command を自動起動し、退出で自動停止する。
#   ⚠️ 初回はマイク/システム音声録音の許可プロンプトが出ることがある(許可すること)。

set -u
LABEL="local.transcue.watcher"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PROJ="$(cd "$(dirname "$0")" && pwd)"
PY="$PROJ/spikes/.venv-ml/bin/python"
GUI="gui/$(id -u)"
CMD="${1:-install}"

status() {
  if launchctl print "$GUI/$LABEL" >/dev/null 2>&1; then
    echo "✅ 登録済み・常駐中: $LABEL"
    launchctl print "$GUI/$LABEL" 2>/dev/null | grep -E "state|pid" | head -3
    "$PY" "$PROJ/src/meeting_watcher.py" --status 2>/dev/null || true
  else
    echo "⛔ 未登録: $LABEL(./install_watcher.command で登録)"
  fi
}

case "$CMD" in
  install)
    if [ ! -x "$PY" ]; then
      echo "venv python が見つかりません: $PY"
      echo "→ bash spikes/setup_env.sh で venv を作り直してください。"
      exit 1
    fi
    mkdir -p "$HOME/Library/LaunchAgents" "$PROJ/logs"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$PROJ/src/meeting_watcher.py</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJ</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict>
    <key>Crashed</key><true/>
  </dict>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>StandardOutPath</key><string>$PROJ/logs/watcher_agent.log</string>
  <key>StandardErrorPath</key><string>$PROJ/logs/watcher_agent.log</string>
  <key>ThrottleInterval</key><integer>10</integer>
</dict>
</plist>
EOF
    plutil -lint "$PLIST" || { echo "plist が不正です"; exit 1; }
    launchctl bootout "$GUI/$LABEL" 2>/dev/null   # 旧登録があれば入れ替え
    launchctl bootstrap "$GUI" "$PLIST" || { echo "登録に失敗しました"; exit 1; }
    echo "✅ 登録しました(ログイン時も自動で常駐します)。"
    sleep 1
    status
    ;;
  uninstall)
    launchctl bootout "$GUI/$LABEL" 2>/dev/null && echo "常駐を停止しました。" || echo "(常駐していませんでした)"
    rm -f "$PLIST" && echo "✅ 解除しました: $PLIST"
    ;;
  status)
    status
    ;;
  *)
    echo "使い方: $0 [install|uninstall|status]"; exit 1;;
esac
