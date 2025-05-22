# TV録画サーバー Docker構成

## 概要

Mirakurun + EPGStation + MariaDBを使用した地上波・BS録画サーバーのDocker構成です。

## 構成

- **Mirakurun**: チューナー（PX-Q1UD×4）とB-CASカードリーダーの管理
- **EPGStation**: 番組表取得、録画スケジュール管理、Web UI
- **MariaDB**: EPGStationのデータベース

## 前提条件

### ハードウェア
- PX-Q1UDチューナー × 4台（/dev/dvb/adapter0〜3として認識済み）
- SCMカードリーダー SCR3310（B-CASカード読み取り用）
- 大容量ストレージ（録画データ保存用）

### ソフトウェア
- Docker（v26.1.3以上）
- docker-compose（v1.29.2以上）
- ユーザーがvideoグループとdockerグループに所属

## セットアップ手順

### 1. 設定ファイルの確認
```bash
# 現在のディレクトリ構造を確認
tree .

# 期待される構造:
# tv-recorder-stack/
# ├── docker-compose.yml
# ├── .env
# ├── README.md
# ├── mirakurun/
# │   ├── config/
# │   └── data/
# ├── epgstation/
# │   ├── config/
# │   └── data/
# ├── mysql/
# │   └── data/
# └── recorded/
```

### 2. Dockerイメージの取得
```bash
# 公式イメージを事前にダウンロード（オプション）
docker pull chinachu/mirakurun:latest
docker pull l3tnun/epgstation:latest
docker pull mariadb:10.11
```

### 3. サービスの起動
```bash
# バックグラウンドで全サービスを起動
docker-compose up -d

# ログの確認
docker-compose logs -f
```

### 4. 初期設定

#### Mirakurun設定
1. ブラウザで http://localhost:40772 にアクセス
2. チューナー設定とチャンネルスキャンを実行

#### EPGStation設定
1. ブラウザで http://localhost:8888 にアクセス
2. 初期設定ウィザードに従って設定

## 運用コマンド

### サービス管理
```bash
# 全サービス起動
docker-compose up -d

# 全サービス停止
docker-compose down

# 特定サービスの再起動
docker-compose restart mirakurun
docker-compose restart epgstation

# ログ確認
docker-compose logs -f [サービス名]
```

### 状態確認
```bash
# サービス状態確認
docker-compose ps

# リソース使用状況確認
docker stats

# コンテナ内部アクセス（デバッグ用）
docker-compose exec mirakurun bash
docker-compose exec epgstation bash
```

### メンテナンス
```bash
# イメージ更新
docker-compose pull
docker-compose up -d

# 古いイメージ削除
docker image prune

# データベースバックアップ
docker-compose exec mysql mysqldump -u epgstation -p epgstation > backup.sql
```

## ポート情報

- **40772**: Mirakurun Web UI・API
- **8888**: EPGStation Web UI
- **8889**: EPGStation API（オプション）

## データ保存場所

- **録画ファイル**: `./recorded/`
- **設定ファイル**: `./mirakurun/config/`, `./epgstation/config/`
- **データベース**: `./mysql/data/`
- **ログファイル**: `./epgstation/data/logs/`

## トラブルシューティング

### チューナーが認識されない場合
```bash
# デバイス確認
ls -la /dev/dvb/

# 権限確認
groups $USER

# サービス再起動
docker-compose restart mirakurun
```

### EPGStationが起動しない場合
```bash
# データベース接続確認
docker-compose logs mysql
docker-compose logs epgstation

# 設定ファイル確認
docker-compose exec epgstation cat /app/config/config.yml
```

### 録画ができない場合
```bash
# Mirakurun状態確認
curl http://localhost:40772/api/status

# ディスク容量確認
df -h ./recorded/

# ログ確認
docker-compose logs -f epgstation
```

## 参考リンク

- [Mirakurun GitHub](https://github.com/Chinachu/Mirakurun)
- [EPGStation GitHub](https://github.com/l3tnun/EPGStation)
- [Docker Hub - Mirakurun](https://hub.docker.com/r/chinachu/mirakurun)
- [Docker Hub - EPGStation](https://hub.docker.com/r/l3tnun/epgstation)