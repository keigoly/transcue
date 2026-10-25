# TransCue

Real-time two-way interpretation for online meetings on macOS (Zoom and Google Meet).
Your Japanese is spoken to the other participants in English, and their English appears as Japanese subtitles.
Speech recognition and speech synthesis run on your Mac; translation runs on your Mac (PLaMo) or, optionally, on Claude.

会議の音声を双方向でリアルタイムに訳す Mac の道具です。自分の日本語は英語の合成音声で相手に届け、相手の英語は日本語の字幕で
画面に出します。対応する会議アプリは Zoom と Google Meet(Chrome)。音声認識と読み上げは Mac の中で行い、翻訳は Mac の中
(PLaMo)か、選べば Claude(クラウド)で行います。開発中(v0.1)です。

## 仕組み

- **送信**: マイク → 音声認識(SpeechAnalyzer・日本語)→ 節に区切る → 翻訳 → 英語の読み上げ(Kokoro)→ 仮想マイク(BlackHole 2ch)
  → 会議アプリのマイク。生の声は相手に送らない(英語ボタンを OFF にした間だけ地声を流す)。
- **受信**: Mac で鳴っている音(自分の読み上げを除く。Core Audio のタップ)→ 音声認識(英語)→ 翻訳 → 日本語の字幕の窓。
  会議の間だけ動くので、会議中に流した動画などの音も字幕になる。
- **自動起動**: 常駐の監視(watcher)が会議を検知して起動し、退出で止める。Zoom は会議に入ると、Meet などは会議アプリのマイクに
  BlackHole 2ch を選んでいると検知する。

## 必要なもの

- macOS 26 以降・Apple Silicon・メモリ 16 GB 以上を推奨(翻訳モデル PLaMo が約 5 GB)
- [BlackHole 2ch](https://github.com/ExistentialAudio/BlackHole)(仮想オーディオデバイス。別に入れる)
- Python 3.12 と [uv](https://github.com/astral-sh/uv)・Xcode Command Line Tools(Swift)
- Zoom、または Chrome で Google Meet

## セットアップ

```bash
git clone https://github.com/keigoly/transcue.git && cd transcue
bash spikes/setup_env.sh                            # Python の環境と src/config.toml(見本から)を作る
./build_all.sh                                      # Swift の部品と TransCue.app をビルドする
HF_HOME="$PWD/models-cache/huggingface" spikes/.venv-ml/bin/hf download mlx-community/plamo-2-translate
HF_HOME="$PWD/models-cache/huggingface" spikes/.venv-ml/bin/hf download prince-canuma/Kokoro-82M
./install_watcher.command                           # ログイン時に監視を常駐させる
```

初回は macOS がマイクとシステム音声の録音の許可を求めます。画面収録の許可もあれば、英語ボタンは会議の窓だけに出ます
(無ければ Zoom・Chrome が前面のときに出ます)。PLaMo は読み込みのときにモデルに含まれるコードを実行します(`trust_remote_code`)。

## 使い方

1. **会議アプリのマイクとスピーカーを設定する**(最初の 1 回)

   | 会議アプリ | マイク | スピーカー | ほか |
   |---|---|---|---|
   | Zoom | BlackHole 2ch | ヘッドホン | マイクモードは「オリジナルサウンド」 |
   | Google Meet(Chrome) | BlackHole 2ch(会議の「設定」→「音声」) | ヘッドホン | Chrome に meet.google.com のマイクの使用を許可する |

2. **会議に入る**: 数秒で翻訳が起動し、英語ボタンと字幕が出ます。退出すると止まります。
3. **英語の ON/OFF**: 会議画面の英語ボタン(⌃⌥P)。OFF の間は地声がそのまま届きます。
   ボタンは Zoom の会議の窓か Chrome の Meet のタブが前面のときに出ます(⌃⌥P はいつでも効きます)。
4. **TransCue の画面**(ログイン時に開く): 電源ボタン = 有効/無効、クラウド翻訳(Claude)の ON/OFF、設定(モデルと API キーの有無)。
5. **記録**: 会議の発言と訳は `logs/` のファイル(議事録と計測のログ)に残ります。このファイルはどこにも送りません。要らなくなったら `logs/` を消してください。

## クラウド翻訳(Claude・任意)

既定は OFF で、すべて Mac の中(PLaMo)で訳します。Claude で訳すと、訳が自然になり、遅延も短くなります(字幕は Haiku、英語は Sonnet)。

1. `src/config.toml` の `[translate] recv_engine` と `send_engine` を `"claude"` にする。
2. Anthropic の API キーを `~/.transcue/anthropic_api_key` に置く(1 行・本人だけが読める権限に)。
3. TransCue の画面で「クラウド翻訳」を ON にする。

ON の間は、訳す文字起こし(会議中に Mac で鳴った音を含む)が Anthropic に送られます。つながらない・遅い・断られたときは、その発言だけ Mac の中で訳します。

## ライセンス

このリポジトリのコードは [MIT](./LICENSE)。使うものはそれぞれのライセンスに従ってください(どれも同梱していません):

- PLaMo-2-translate: PLaMo Community License(業務で使うには Preferred Networks への登録が必要。条件はモデルのページを参照)
- Kokoro-82M: Apache-2.0
- BlackHole: GPL-3.0
- Claude API: Anthropic の利用規約
