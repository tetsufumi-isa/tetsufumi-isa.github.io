# 変数の設定
REPO_DIR="/home/teisa/github/tetsufumi-isa.github.io"
PROJECT_DIR="${REPO_DIR}/projects/podcast-converter"
LOGS_DIR="${PROJECT_DIR}/logs"
MAX_LOG_DAYS=100

# 日付形式の設定（YYYY-MM-DD）
TODAY=$(date +"%Y-%m-%d")
LOG_FILE="${LOGS_DIR}/podcast_${TODAY}.log"

# ログディレクトリがなければ作成
mkdir -p "${LOGS_DIR}"

# 実行時間をログに記録
echo "===== $(date): ポッドキャスト更新開始 =====" >> "${LOG_FILE}"

# 作業ディレクトリに移動
cd "${REPO_DIR}"

# 仮想環境を有効化
source "${PROJECT_DIR}/podcast_env/bin/activate"

# スクリプトを実行
python projects/podcast-converter/code/podcast_update.py >> "${LOG_FILE}" 2>&1

# 終了ステータスを確認
if [ $? -eq 0 ]; then
    echo "$(date): 正常に完了しました" >> "${LOG_FILE}"
else
    echo "$(date): エラーが発生しました" >> "${LOG_FILE}"
fi

# 仮想環境を無効化
deactivate

# 古いログファイルを削除（100日以上前のもの）
find "${LOGS_DIR}" -name "podcast_*.log" -type f -mtime +${MAX_LOG_DAYS} -delete

# ログの要約
echo "$(date): ログクリーンアップ完了。${MAX_LOG_DAYS}日以上前のログファイルを削除しました。" >> "${LOG_FILE}"
echo "===== $(date): ポッドキャスト更新終了 =====" >> "${LOG_FILE}"

# 最新のログファイル一覧表示
echo "最新のログファイル："
ls -lh "${LOGS_DIR}" | grep "podcast_" | tail -n 5