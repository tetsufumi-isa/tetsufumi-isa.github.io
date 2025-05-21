#!/bin/bash

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
echo "実行環境: USER=$(whoami), PWD=$(pwd), PATH=${PATH}" >> "${LOG_FILE}"

# 作業ディレクトリに移動
cd "${REPO_DIR}"
echo "作業ディレクトリ: $(pwd)" >> "${LOG_FILE}"

# 仮想環境の確認
VENV_ACTIVATE="${PROJECT_DIR}/podcast_env/bin/activate"
echo "仮想環境の確認: ${VENV_ACTIVATE}" >> "${LOG_FILE}"
if [ ! -f "${VENV_ACTIVATE}" ]; then
    echo "エラー: 仮想環境のactivateスクリプトが見つかりません: ${VENV_ACTIVATE}" >> "${LOG_FILE}"
    exit 1
fi

# 仮想環境を有効化
echo "仮想環境を有効化します..." >> "${LOG_FILE}"
source "${VENV_ACTIVATE}"
if [ -z "$VIRTUAL_ENV" ]; then
    echo "エラー: 仮想環境の有効化に失敗しました" >> "${LOG_FILE}"
    exit 1
fi
echo "仮想環境の有効化に成功しました: ${VIRTUAL_ENV}" >> "${LOG_FILE}"

# 仮想環境のPythonを確認
echo "Pythonバージョン: $(python --version 2>&1)" >> "${LOG_FILE}"
echo "Pythonパス: $(which python)" >> "${LOG_FILE}"

# yt-dlpの有無を確認
YT_DLP_PATH=$(which yt-dlp 2>/dev/null)
if [ -z "${YT_DLP_PATH}" ]; then
    echo "警告: yt-dlpコマンドが見つかりません。インストールが必要かもしれません。" >> "${LOG_FILE}"
else
    echo "yt-dlpのパス: ${YT_DLP_PATH}" >> "${LOG_FILE}"
    echo "yt-dlpバージョン: $(yt-dlp --version 2>&1)" >> "${LOG_FILE}"
fi

# スクリプトファイルの確認
SCRIPT_PATH="${PROJECT_DIR}/code/podcast_update.py"
if [ ! -f "${SCRIPT_PATH}" ]; then
    echo "エラー: Pythonスクリプトが見つかりません: ${SCRIPT_PATH}" >> "${LOG_FILE}"
    exit 1
fi

# スクリプトを実行
echo "Pythonスクリプト実行開始: $(date)" >> "${LOG_FILE}"
if python "${SCRIPT_PATH}" >> "${LOG_FILE}" 2>&1; then
    EXIT_CODE=0
    echo "Pythonスクリプト実行成功: 終了コード=${EXIT_CODE}" >> "${LOG_FILE}"
else
    EXIT_CODE=$?
    echo "Pythonスクリプト実行失敗: 終了コード=${EXIT_CODE}" >> "${LOG_FILE}"
fi

# 仮想環境を無効化
deactivate
echo "仮想環境を無効化しました" >> "${LOG_FILE}"

# 終了ステータスを確認
if [ ${EXIT_CODE} -eq 0 ]; then
    echo "$(date): 正常に完了しました" >> "${LOG_FILE}"
else
    echo "$(date): エラーが発生しました" >> "${LOG_FILE}"
fi

# 古いログファイルを削除（100日以上前のもの）
find "${LOGS_DIR}" -name "podcast_*.log" -type f -mtime +${MAX_LOG_DAYS} -delete

# ログの要約
echo "$(date): ログクリーンアップ完了。${MAX_LOG_DAYS}日以上前のログファイルを削除しました。" >> "${LOG_FILE}"
echo "===== $(date): ポッドキャスト更新終了 =====" >> "${LOG_FILE}"

# 最新のログファイル一覧表示
echo "最新のログファイル："
ls -lh "${LOGS_DIR}" | grep "podcast_" | tail -n 5

# スクリプトの終了ステータスを返す
exit ${EXIT_CODE}