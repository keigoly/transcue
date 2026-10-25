#!/bin/bash
# TransCue の Swift の部品と GUI(TransCue.app)をビルドする。
set -euo pipefail
cd "$(dirname "$0")/src"
# build <フォルダ> <ソース> <出力> [swiftc の追加オプション…]
build() { local d=$1 src=$2 out=$3; shift 3; (cd "$d" && swiftc "$@" -O "$src" -o "$out" && echo "built: src/$d/$out"); }
build stt_helper main.swift stt-helper -parse-as-library
build tap_helper main.swift tap-helper -parse-as-library
build tap_helper audioprocs.swift audio-procs -parse-as-library
build control_panel main.swift control-panel -parse-as-library
build subtitle_window main.swift subtitle-window -parse-as-library
build hotkey_helper main.swift hotkey-helper -parse-as-library
build transcribe_file main.swift transcribe-file -parse-as-library
build audio_device main.swift set-input
(cd app && ./build.sh)
