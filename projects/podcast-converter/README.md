# YouTube→ポッドキャスト変換システム

## 構成
- `code/`: スクリプトと設定ファイル
- `data/`: RSSフィードとメディアファイル

## 使い方
1. 仮想環境を有効化: `source ../../podcast_env/bin/activate`
2. スクリプトを実行: `python code/podcast_update.py`

## 依存関係
- Python 3.12+
- yt-dlp, boto3, configparser