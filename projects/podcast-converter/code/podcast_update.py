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

def load_config():
    try:
        # スクリプトファイルのディレクトリパスを取得
        print(f"スクリプトディレクトリ: {script_dir}")
        config_path = os.path.join(script_dir, 'config.ini')
        print(f"設定ファイルパス: {config_path}")
        
        # ファイルの存在確認
        file_exists = os.path.exists(config_path)
        print(f"設定ファイルは存在する: {file_exists}")
        
        if file_exists:
            # ファイルが読み取り可能か確認
            try:
                with open(config_path, 'r', encoding='utf-8') as test_file:
                    first_line = test_file.readline()
                print(f"ファイル読み取りテスト成功: 最初の行 = {first_line}")
            except Exception as read_error:
                print(f"ファイル読み取りテスト失敗: {str(read_error)}")
            
            # 設定を読み込む
            config = configparser.ConfigParser(inline_comment_prefixes=('#', ';'))
            config.read(config_path, encoding='utf-8')
            
            # 正しく読み込めたか確認
            if len(config.sections()) > 0:
                print(f"設定セクション: {config.sections()}")
            else:
                print("警告: 設定ファイルからセクションを読み込めませんでした")
            
            return config
        else:
            # 設定ファイルがない場合
            error_msg = f"エラー: 設定ファイル '{config_path}' が見つかりません。"
            print(error_msg)  # 標準出力にも出力
            logger.error(error_msg)
            
            # 現在のディレクトリの内容を表示
            print(f"現在のディレクトリ内容: {os.listdir(script_dir)}")
            
            # 親ディレクトリも確認
            parent_dir = os.path.dirname(script_dir)
            print(f"親ディレクトリ: {parent_dir}")
            print(f"親ディレクトリ内容: {os.listdir(parent_dir)}")
            
            sys.exit(1)
    except Exception as e:
        print(f"設定読み込み中に予期せぬエラー: {str(e)}")
        print(f"エラータイプ: {type(e).__name__}")
        import traceback
        traceback.print_exc()
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
    
    # ローカルファイルとR2ファイルの保存期間を別々に取得
    local_expire_days = int(config['Settings'].get('local_expire_days', '10'))
    r2_expire_days = int(config['Settings'].get('r2_expire_days', '28'))
    
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

    # 古いファイル削除（ローカルとR2のパラメータを別々に適用）
    logger.info(f"🧹 古いファイル削除: {folder_name}")

    # R2の現在のファイル一覧を取得
    r2_files = list_r2_files(r2_client, f"{folder_name}/", r2_bucket)
    r2_filenames = [os.path.basename(path) for path in r2_files]

    # 現在の日時
    now = datetime.now()

    # ローカルファイル処理
    logger.info(f"🧹 ローカルファイル確認中（{local_expire_days}日以上経過したものを削除）")
    for filename in os.listdir(output_dir):
        if filename.endswith(".mp3") and "：" in filename:
            try:
                mmdd = filename.split("：")[0]
                file_date = datetime.strptime(mmdd, "%m-%d").replace(year=now.year)
                
                # 年をまたいだ場合（例：現在1月で、ファイルが12月の場合）
                if file_date > now and mmdd.startswith("12") and now.month < 2:
                    file_date = file_date.replace(year=now.year - 1)
                
                # ローカルファイルの期限確認
                if (now - file_date).days > local_expire_days:
                    # ローカルファイル削除
                    local_path = os.path.join(output_dir, filename)
                    if os.path.exists(local_path):
                        os.remove(local_path)
                        logger.info(f"🗑️ ローカル削除: {filename}")
            except Exception as e:
                logger.error(f"⚠️ ローカルファイル削除エラー: {filename} - {e}")

    # R2ファイル処理（別の基準で削除）
    logger.info(f"🧹 R2ファイル確認中（{r2_expire_days}日以上経過したものを削除）")
    for filename in r2_filenames:
        if filename.endswith(".mp3") and "：" in filename:
            try:
                mmdd = filename.split("：")[0]
                file_date = datetime.strptime(mmdd, "%m-%d").replace(year=now.year)
                
                # 年をまたいだ場合（例：現在1月で、ファイルが12月の場合）
                if file_date > now and mmdd.startswith("12") and now.month < 2:
                    file_date = file_date.replace(year=now.year - 1)
                
                # R2ファイルの期限確認
                if (now - file_date).days > r2_expire_days:
                    # R2ファイル削除
                    remote_path = f"{folder_name}/{filename}"
                    delete_success = delete_from_r2(r2_client, remote_path, r2_bucket)
                    if delete_success:
                        logger.info(f"🗑️ R2削除: {remote_path}")
            except Exception as e:
                logger.error(f"⚠️ R2ファイル削除エラー: {filename} - {e}")

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

    # RSSファイルを保存
    rss_path = os.path.join(output_dir, "feed.xml")
    with open(rss_path, "w", encoding="utf-8") as f:
        f.write(rss_feed)
    logger.info(f"✅ RSS生成完了: {folder_name}")

    # RSSファイルをR2にアップロード
    logger.info(f"☁️ RSSをR2にアップロード中: {folder_name}/feed.xml")
    remote_feed_path = f"{folder_name}/feed.xml"
    feed_upload_success = upload_to_r2(r2_client, rss_path, remote_feed_path, r2_bucket)
    if feed_upload_success:
        logger.info(f"✅ RSSのR2アップロード完了: {remote_feed_path}")
        # RSSファイルのR2 URL（参考用に表示）
        rss_url = f"{config['R2']['public_base_url']}/{remote_feed_path}"
        logger.info(f"📡 RSS URL: {rss_url}")
    else:
        logger.error(f"❌ RSSのR2アップロード失敗: {remote_feed_path}")

    return folder_name

def main():
    try:
        # 最初のログ出力前にプリントを追加
        print("スクリプト実行開始: " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
        logger.info("🚀 YouTube→ポッドキャスト変換 開始")

        # 設定読み込み
        print("設定ファイル読み込み開始...")
        config = load_config()
        print("設定ファイル読み込み完了")

        # 設定から並列処理数を読み込み
        max_workers = int(config['Settings'].get('max_workers', '3'))
        max_yt_dlp = int(config['Settings'].get('max_yt_dlp_processes', '2'))

        # yt-dlpの同時実行数制限用セマフォを設定
        global yt_dlp_semaphore
        yt_dlp_semaphore = threading.Semaphore(max_yt_dlp)

        # R2クライアント初期化
        print("R2クライアント初期化開始...")
        r2_client = init_r2_client(config)
        print("R2クライアント初期化完了")

        # 設定からチャンネルとプレイリスト情報を取得
        channels = dict(config['Channels'].items())
        playlists = dict(config['Playlists'].items())
        print(f"処理対象チャンネル数: {len(channels)}")
        print(f"処理対象プレイリスト数: {len(playlists)}")

        # 並列処理で各番組を処理
        print(f"並列処理開始（最大ワーカー数: {max_workers}）...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = []
            for folder_name, playlist_url in playlists.items():
                print(f"番組タスク追加: {folder_name}")
                futures.append(executor.submit(process_program, (folder_name, playlist_url, config, r2_client)))

            # 完了を待機
            print(f"全タスク投入完了、完了を待機中（タスク数: {len(futures)}）...")
            for future in concurrent.futures.as_completed(futures):
                try:
                    folder_name = future.result()
                    if folder_name:
                        logger.info(f"✅ 番組処理完了: {folder_name}")
                        print(f"番組処理完了: {folder_name}")
                except Exception as e:
                    error_msg = f"⚠️ 番組処理中にエラーが発生: {e}"
                    logger.error(error_msg)
                    print(error_msg)
        
        print("全ての並列処理が完了しました")
        logger.info("🎉 全処理完了！")

    except Exception as e:
        error_message = f"❌ 致命的エラー: {str(e)}"
        print(error_message)  # 標準出力に出力
        print(f"エラー詳細: {type(e).__name__}")
        
        # トレースバックも標準出力に出力
        import traceback
        print("トレースバック:")
        traceback.print_exc()
        
        # ロガーにも記録
        try:
            logger.critical(error_message, exc_info=True)
        except Exception as log_error:
            print(f"ログ記録中にさらにエラー発生: {str(log_error)}")
        
        return 1

    return 0

if __name__ == "__main__":
    sys.exit(main())