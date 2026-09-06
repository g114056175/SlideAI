#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_AI_WORKSPACE="${SOURCE_AI_WORKSPACE:-$(cd "${PROJECT_ROOT}/../.." && pwd)/AI_Workspace}"
TARGET_ROOT="${1:-}"
INCLUDE_MODELS=0
INCLUDE_VIDEO_RUNS=0

usage() {
  cat <<USAGE
Usage: $0 <target-root> [--with-models] [--with-video-runs]

Create a runnable SlideAI handoff copy.

Default behavior copies:
  - SlideAI source only; persistent video-run artifacts are not included
  - no virtual environments, Node modules, or model checkpoints

Optional behavior copies:
  - data/video_runs demo artifacts with --with-video-runs
  - checkpoint weights in the standard models/ slots with --with-models

Options:
  --with-models    Copy checkpoint weights into SlideAI/models (never copies .venv).
  --with-video-runs Copy existing data/video_runs artifacts.
USAGE
}

if [[ -z "${TARGET_ROOT}" || "${TARGET_ROOT}" == "-h" || "${TARGET_ROOT}" == "--help" ]]; then
  usage
  exit 0
fi
shift || true

for arg in "$@"; do
  case "${arg}" in
    --with-models) INCLUDE_MODELS=1 ;;
    --with-video-runs) INCLUDE_VIDEO_RUNS=1 ;;
    *) echo "[ERROR] Unknown argument: ${arg}"; usage; exit 2 ;;
  esac
done

TARGET_ROOT="$(mkdir -p "${TARGET_ROOT}" && cd "${TARGET_ROOT}" && pwd)"
TARGET_SLIDEAI="${TARGET_ROOT}/SlideAI"
TARGET_MODELS="${TARGET_SLIDEAI}/models"

echo "[INFO] Source SlideAI: ${PROJECT_ROOT}"
echo "[INFO] Target root: ${TARGET_ROOT}"
echo "[INFO] INCLUDE_MODELS=${INCLUDE_MODELS}"
echo "[INFO] INCLUDE_VIDEO_RUNS=${INCLUDE_VIDEO_RUNS}"

RSYNC_EXCLUDES=(
  "--exclude=.git"
  "--exclude=runtime"
  "--exclude=.pytest_cache"
  "--exclude=backend/.env"
  "--exclude=deploy/models.env"
  "--exclude=frontend/.env*.local"
  "--exclude=logs"
  "--exclude=.backend8002.pid"
  "--exclude=.frontend5174.pid"
  "--exclude=**/__pycache__"
  "--exclude=backend/app/services/*.bak.*"
  "--exclude=backend/app/user_thumbnails"
  "--exclude=backend/.venv"
  "--exclude=.runtimes"
  "--exclude=backend/node_modules"
  "--exclude=frontend/node_modules"
  "--exclude=frontend/dist"
  "--exclude=models/**"
)

if [[ "${INCLUDE_VIDEO_RUNS}" != "1" ]]; then
  RSYNC_EXCLUDES+=("--exclude=data/video_runs/*")
fi

mkdir -p "${TARGET_SLIDEAI}"
rsync -a --delete "${RSYNC_EXCLUDES[@]}" "${PROJECT_ROOT}/" "${TARGET_SLIDEAI}/"
mkdir -p \
  "${TARGET_SLIDEAI}/data/video_runs" \
  "${TARGET_SLIDEAI}/models/tts" \
  "${TARGET_SLIDEAI}/models/asr" \
  "${TARGET_SLIDEAI}/models/alignment"
touch "${TARGET_SLIDEAI}/data/video_runs/.gitkeep"

if [[ "${INCLUDE_MODELS}" == "1" ]]; then
  declare -A MODEL_SLOTS=(
    ["${SOURCE_AI_WORKSPACE}/VoxCPM/models/VoxCPM2"]="${TARGET_MODELS}/tts/VoxCPM2"
    ["${SOURCE_AI_WORKSPACE}/QWEN3-TTS/Qwen3-TTS-12Hz-1.7B-Base"]="${TARGET_MODELS}/tts/Qwen3-TTS-12Hz-1.7B-Base"
    ["${SOURCE_AI_WORKSPACE}/QWEN3-ASR/Qwen3-ASR-1.7B"]="${TARGET_MODELS}/asr/Qwen3-ASR-1.7B"
    ["${SOURCE_AI_WORKSPACE}/QWEN3-ASR/Qwen3-ForcedAligner-0.6B"]="${TARGET_MODELS}/alignment/Qwen3-ForcedAligner-0.6B"
  )
  for source_path in "${!MODEL_SLOTS[@]}"; do
    target_path="${MODEL_SLOTS[${source_path}]}"
    if [[ -d "${source_path}" ]]; then
      echo "[INFO] Copying ${source_path} -> ${target_path}"
      mkdir -p "${target_path}"
      rsync -a --delete --exclude='.gitkeep' "${source_path}/" "${target_path}/"
    else
      echo "[WARN] Missing ${source_path}; skipped"
    fi
  done
fi

cp "${TARGET_SLIDEAI}/backend/.env.example" "${TARGET_SLIDEAI}/backend/.env"
cp "${TARGET_SLIDEAI}/deploy/models.env.example" "${TARGET_SLIDEAI}/deploy/models.env"

chmod +x \
  "${TARGET_SLIDEAI}/slideai.sh"

echo
echo "[DONE] Handoff copy created:"
echo "  ${TARGET_SLIDEAI}"
echo
echo "Next:"
echo "  cd ${TARGET_SLIDEAI}"
echo "  edit deploy/models.env"
echo "  ./slideai.sh"
