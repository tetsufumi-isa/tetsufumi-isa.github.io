#!/usr/bin/env python3
"""
YouTube→ポッドキャスト変換スクリプト（改良版）
----------------------------------------
機能：
1. YouTube再生リストやチャンネルから音声を抽出
2. MP3形式に変換してローカルに保存
3. Cloudflare R2にアップロード
4. 期限切れファイルをローカルとR2から削除
5. Apple Podcast形式のRSSフィードを生成
6. GitHubにpush
"""

import os
import subprocess
import json
import re
import glob
from datetime import datetime, timedelta
import email.utils
import boto3
from botocore.client import Config
import logging
import concurrent.futures
import time
import configparser
import sys
import threading

# スクリプトのディレクトリを取得
script_dir = os.path.dirname(os.path.abspath(__file__))
# プロジェクトディレクトリを取得 (code の親ディレクトリ)
project_dir = os.path.dirname(script_dir)  # codeの1階層上がプロジェクトディレクトリ
# logs ディレクトリのパスを作成
logs_dir = os.path.join(project_dir, "logs")
# logs ディレクトリがなければ作成
os.makedirs(logs_dir, exist_ok=True)

# 日付形式の設定（YYYY-MM-DD）
today = datetime.now().strftime("%Y-%m-%d")
# 日別のログファイルパスを作成
log_file = os.path.join(logs_dir, f"podcast_{today}.log")

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 古いログファイルのクリーンアップ (100日以上前のログを削除)
MAX_LOG_DAYS = 100
try:
    log_pattern = os.path.join(logs_dir, "podcast_*.log")
    for log_path in glob.glob(log_pattern):
        try:
            # ファイル名から日付を抽出 (podcast_YYYY-MM-DD.log)
            file_date_str = os.path.basename(log_path).replace("podcast_", "").replace(".log", "")
            file_date = datetime.strptime(file_date_str, "%Y-%m-%d")
            # 100日以上前のログファイルを削除
            if (datetime.now() - file_date).days > MAX_LOG_DAYS:
                os.remove(log_path)
                logger.info(f"古いログファイルを削除しました: {log_path}")
        except Exception as e:
            logger.error(f"ログファイルの処理中にエラーが発生: {log_path} - {e}")
except Exception as e:
    logger.error(f"ログクリーンアップ中にエラーが発生: {e}")

# エラーメッセージの日本語化関数
def translate_youtube_error(error_msg):
    """YouTubeのエラーメッセージを日本語に変換する"""
    if "Premieres in" in error_msg:
        days = re.search(r'in (\d+) days', error_msg)
        if days:
            return f"📅 プレミア公開待ち（{days.group(1)}日後）"
        else:
            return f"📅 プレミア公開待ち"
    elif "Private video" in error_msg:
        return "🔒 非公開動画です"
    elif "Video unavailable" in error_msg or "removed by the uploader" in error_msg:
        return "❌ 動画が削除されています"
    elif "This video is not available" in error_msg:
        return "⛔ この動画は利用できません"
    else:
        return f"⚠️ {error_msg}"  # その他のエラーはそのまま表示

# yt-dlpの同時実行数を制限するためのセマフォ
yt_dlp_semaphore = threading.Semaphore(2)  # 最大2つのyt-dlpプロセスを同時実行

# 設定ファイル読み込み
def load_config():
    # スクリプトファイルのディレクトリパスを取得
    config_path = os.path.join(script_dir, 'config.ini')
    
    if os.path.exists(config_path):
        config = configparser.ConfigParser()
        config.read(config_path, encoding='utf-8')
        return config
    else:
        # 設定ファイルがない場合はエラーメッセージを表示して終了
        error_msg = f"エラー: 設定ファイル '{config_path}' が見つかりません。"
        logger.error(error_msg)
        sys.exit(1)

# R2クライアント初期化
def init_r2_client(config):
    session = boto3.session.Session()
    client = session.client(
        's3',
        region_name='auto',
        endpoint_url=config['R2']['endpoint'],
        aws_access_key_id=config['R2']['access_key'],
        aws_secret_access_key=config['R2']['secret_key'],
        config=Config(signature_version='s3v4')
    )
    return client

# R2へアップロード
def upload_to_r2(client, local_path, remote_path, bucket):
    try:
        with open(local_path, 'rb') as f:
            client.upload_fileobj(f, bucket, remote_path, ExtraArgs={'ACL': 'public-read'})
        return True
    except Exception as e:
        logger.error(f"R2アップロード失敗: {e}")
        return False

# R2からファイル削除
def delete_from_r2(client, remote_path, bucket):
    try:
        client.delete_object(Bucket=bucket, Key=remote_path)
        return True
    except Exception as e:
        logger.error(f"R2削除失敗: {e}")
        return False

# R2内のファイル一覧取得
def list_r2_files(client, prefix, bucket):
    try:
        response = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
        if 'Contents' in response:
            return [item['Key'] for item in response['Contents']]
        return []
    except Exception as e:
        logger.error(f"R2ファイル一覧取得失敗: {e}")
        return []

# 番組処理関数
def process_program(args):
    folder_name, playlist_url, config, r2_client = args

    # データディレクトリ内のチャンネルディレクトリを指定
    data_dir = os.path.join(project_dir, "data")
    output_dir = os.path.join(data_dir, folder_name)
    os.makedirs(output_dir, exist_ok=True)

    r2_bucket = config['R2']['bucket']
    min_duration = int(config['Settings']['min_duration'])
    max_duration = int(config['Settings']['max_duration'])
    expire_days = int(config['Settings']['expire_days'])
    max_items = config['Settings'].get('max_items', '15')
    look_back_days = int(config['Settings'].get('look_back_days', '4'))

    # 番組名を設定から取得
    display_name = config['Channels'].get(folder_name, folder_name)

    logger.info(f"🎙️ 番組処理開始: {folder_name} ({display_name})")

    threshold_date = int((datetime.today() - timedelta(days=look_back_days)).strftime('%Y%m%d'))

    # 動画リスト取得
    try:
        result = subprocess.run([
            "yt-dlp", "--flat-playlist", "-J",
            "--playlist-items", f"1-{max_items}",
            playlist_url
        ], capture_output=True, text=True, check=True)

        playlist = json.loads(result.stdout)
        entries = playlist.get("entries", [])
        logger.info(f"🎞️ 対象動画数: {len(entries)}")
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ 動画ID取得失敗: {folder_name} - {translate_youtube_error(e.stderr)}")
        return
    except json.JSONDecodeError:
        logger.error(f"❌ 動画リストJSONパース失敗: {folder_name}")
        return
    except Exception as e:
        logger.error(f"❌ 予期せぬエラー: {folder_name} - {e}")
        return

    downloaded = 0
    durations = {}

    # 既存MP3のメタデータ読み込み（すでにダウンロード済みの場合に使用）
    for filename in os.listdir(output_dir):
        if filename.endswith(".mp3"):
            filepath = os.path.join(output_dir, filename)
            try:
                # ffprobeで動画の長さを取得
                probe_result = subprocess.run([
                    os.path.join(config['Paths']['ffmpeg_path'], "ffprobe"),
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "json",
                    filepath
                ], capture_output=True, text=True, check=True)

                probe_data = json.loads(probe_result.stdout)
                duration = float(probe_data['format']['duration'])
                durations[filename] = int(duration)
            except:
                # 取得できない場合は推定値として1時間を設定
                durations[filename] = 3600

    # 各動画を処理
    for entry in entries:
        if not entry or not entry.get("id"):
            continue
        video_id = entry["id"]
        url = f"https://www.youtube.com/watch?v={video_id}"

        try:
            meta = subprocess.run(["yt-dlp", "-J", url], capture_output=True, text=True, check=True)
            data = json.loads(meta.stdout)

            title = data.get("title")
            upload_date = data.get("upload_date")
            duration = data.get("duration")

            if not all([title, upload_date, duration]):
                logger.warning(f"⚠️ メタデータ不足: {url}")
                continue

            if int(upload_date) < threshold_date:
                logger.debug(f"⏭️ 古い動画をスキップ: {upload_date} < {threshold_date}")
                continue

            if not (min_duration <= duration <= max_duration):
                logger.debug(f"⏭️ 長さ条件外をスキップ: {duration}秒")
                continue

            mmdd = datetime.strptime(upload_date, "%Y%m%d").strftime("%m-%d")
            safe_title = "".join(c for c in title if c not in r'<>:"/\\|?*').replace("#", " ")
            filename = f"{mmdd}：{safe_title[:40]}.mp3"
            filepath = os.path.join(output_dir, filename)

            if os.path.exists(filepath):
                logger.info(f"⏭️ スキップ（既存）: {filename}")
                durations[filename] = duration
                continue

            logger.info(f"⬇️ ダウンロード: {filename}")
            with yt_dlp_semaphore:  # セマフォを使用して同時実行数を制限
                dl_result = subprocess.run([
                    "yt-dlp", "-x",
                    "--audio-format", "mp3",
                    "--ffmpeg-location", config['Paths']['ffmpeg_path'],
                    "--postprocessor-args", "-b:a 128k",
                    "-o", filepath,
                    url
                ], capture_output=True, text=True)

            if dl_result.returncode == 0:
                downloaded += 1
                durations[filename] = duration

                # R2へアップロード
                logger.info(f"☁️ アップロード: {filename}")
                remote_path = f"{folder_name}/{filename}"
                upload_success = upload_to_r2(r2_client, filepath, remote_path, r2_bucket)
                if not upload_success:
                    logger.warning(f"⚠️ アップロード処理でエラーが発生しました: {filename}")
            else:
                logger.error(f"⚠️ ダウンロード失敗: {title}\n{translate_youtube_error(dl_result.stderr)}")
        except subprocess.CalledProcessError as e:
            logger.error(f"⚠️ yt-dlp実行エラー: {translate_youtube_error(e.stderr)}")
            continue
        except json.JSONDecodeError:
            logger.error(f"⚠️ JSONパース失敗: {url}")
            continue
        except Exception as e:
            logger.error(f"⚠️ 予期せぬエラー: {e}")
            continue

    logger.info(f"✅ ダウンロード完了（{downloaded} 件）")

    # 古いファイル削除（ローカルとR2の両方）
    logger.info(f"🧹 古いファイル削除: {folder_name}")

    # R2の現在のファイル一覧を取得
    r2_files = list_r2_files(r2_client, f"{folder_name}/", r2_bucket)
    r2_filenames = [os.path.basename(path) for path in r2_files]

    for filename in os.listdir(output_dir):
        if filename.endswith(".mp3") and "：" in filename:
            try:
                mmdd = filename.split("：")[0]
                file_date = datetime.strptime(mmdd, "%m-%d").replace(year=datetime.now().year)

                # 日付が古い場合
                if datetime.now() - file_date > timedelta(days=expire_days):
                    # ローカルファイル削除
                    local_path = os.path.join(output_dir, filename)
                    if os.path.exists(local_path):
                        os.remove(local_path)
                        logger.info(f"🗑️ ローカル削除: {filename}")

                    # R2ファイル削除
                    if filename in r2_filenames:
                        remote_path = f"{folder_name}/{filename}"
                        delete_success = delete_from_r2(r2_client, remote_path, r2_bucket)
                        if delete_success:
                            logger.info(f"🗑️ R2削除: {remote_path}")
            except Exception as e:
                logger.error(f"⚠️ ファイル削除エラー: {filename} - {e}")
                continue

    # RSS生成
    logger.info(f"📄 feed.xml 生成: {folder_name}")
    rss_items = []

    # 現在のディレクトリ内のMP3ファイルを日付順（新しい順）にソート
    mp3_files = [f for f in os.listdir(output_dir) if f.endswith(".mp3")]
    mp3_files.sort(reverse=True)  # 新しい順

    max_rss_items = int(config['Settings'].get('max_rss_items', '30'))

    for filename in mp3_files:
        if len(rss_items) >= max_rss_items:
            break

        try:
            base = os.path.splitext(filename)[0]
            if "：" not in base:
                continue

            mmdd, raw_title = base.split("：", 1)
            title = raw_title.strip()
            today = datetime.strptime(mmdd, "%m-%d").replace(year=datetime.now().year)
            pubdate = email.utils.format_datetime(today)
            filepath = os.path.join(output_dir, filename)
            filesize = os.path.getsize(filepath)
            fileurl = f"{config['R2']['public_base_url']}/{folder_name}/{filename}"

            duration_sec = durations.get(filename, 0)
            h, m, s = duration_sec // 3600, (duration_sec % 3600) // 60, duration_sec % 60
            duration_str = f"{h:02}:{m:02}:{s:02}"

            item = f"""
    <item>
      <title>{title}</title>
      <enclosure url=\"{fileurl}\" length=\"{filesize}\" type=\"audio/mpeg\" />
      <guid>{fileurl}</guid>
      <pubDate>{pubdate}</pubDate>
      <itunes:duration>{duration_str}</itunes:duration>
    </item>"""
            rss_items.append(item)
        except Exception as e:
            logger.error(f"⚠️ RSS項目エラー: {filename} ({e})")
            continue

    rss_feed = f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<rss version=\"2.0\" xmlns:itunes=\"http://www.itunes.com/dtds/podcast-1.0.dtd\">
  <channel>
    <title>{display_name}</title>
    <link>{config['R2']['public_base_url']}/{folder_name}/</link>
    <description>{display_name} の音声Podcast</description>
    <language>ja</language>
    <itunes:category text="News"/>
    <lastBuildDate>{email.utils.format_datetime(datetime.utcnow())}</lastBuildDate>
    {''.join(rss_items)}
  </channel>
</rss>
"""

    with open(os.path.join(output_dir, "feed.xml"), "w", encoding="utf-8") as f:
        f.write(rss_feed)
    logger.info(f"✅ RSS生成完了: {folder_name}")

    return folder_name

def main():
    try:
        logger.info("🚀 YouTube→ポッドキャスト変換 開始")

        # 設定読み込み
        config = load_config()

        # 設定から並列処理数を読み込み
        max_workers = int(config['Settings'].get('max_workers', '3'))
        max_yt_dlp = int(config['Settings'].get('max_yt_dlp_processes', '2'))

        # yt-dlpの同時実行数制限用セマフォを設定
        global yt_dlp_semaphore
        yt_dlp_semaphore = threading.Semaphore(max_yt_dlp)

        # R2クライアント初期化
        r2_client = init_r2_client(config)

        # 設定からチャンネルとプレイリスト情報を取得
        channels = dict(config['Channels'].items())
        playlists = dict(config['Playlists'].items())

        # 並列処理で各番組を処理
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for folder_name, playlist_url in playlists.items():
                futures.append(executor.submit(process_program, (folder_name, playlist_url, config, r2_client)))

            # 完了を待機
            for future in concurrent.futures.as_completed(futures):
                try:
                    folder_name = future.result()
                    if folder_name:
                        logger.info(f"✅ 番組処理完了: {folder_name}")
                except Exception as e:
                    logger.error(f"⚠️ 番組処理中にエラーが発生: {e}")

        # Git push（feed.xml のみ）
        logger.info("\n🚀 GitHubにpush中...")
        
        ## リポジトリのルートディレクトリを取得
        repo_dir = os.path.dirname(os.path.dirname(os.path.dirname(script_dir)))
        
        git_commands = [
            ["git", "-C", repo_dir, "pull"],
            ["git", "-C", repo_dir, "add", "projects/podcast-converter/data/*/feed.xml"],
            ["git", "-C", repo_dir, "commit", "-m", "auto: update all feeds"],
            ["git", "-C", repo_dir, "push"]
        ]

        for cmd in git_commands:
            result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8')
            if result.returncode != 0:
                logger.error(f"⚠️ Gitエラー: {' '.join(cmd)}")
                logger.error(result.stderr)
                break
            else:
                logger.info(f"✅ {' '.join(cmd)} 完了")

        logger.info("🎉 全処理完了！")

    except Exception as e:
        logger.critical(f"❌ 致命的エラー: {e}", exc_info=True)
        return 1

    return 0

if __name__ == "__main__":
    sys.exit(main())