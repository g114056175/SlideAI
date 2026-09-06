#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_FILE="${PROJECT_ROOT}/runtime/ports.env"
STATE_FRONTEND_PORT=""
STATE_BACKEND_PORT=""
if [[ -f "${STATE_FILE}" ]]; then
  STATE_FRONTEND_PORT="$(awk -F= '$1=="FRONTEND_PORT"{print $2}' "${STATE_FILE}")"
  STATE_BACKEND_PORT="$(awk -F= '$1=="BACKEND_PORT"{print $2}' "${STATE_FILE}")"
fi
FRONTEND_BASE="${SLIDEAI_FRONTEND_BASE:-http://127.0.0.1:${STATE_FRONTEND_PORT:-5174}}"
API_BASE="${SLIDEAI_API_BASE:-${FRONTEND_BASE}}"
FULL=0
PDF_PATH=""
RUN_ID=""
TEMP_DIR=""

say() { printf '%s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }

cleanup() {
  if [[ -n "${RUN_ID}" ]]; then
    curl -fsS -X DELETE "${API_BASE}/api/video-runs/${RUN_ID}" >/dev/null 2>&1 || true
  fi
  if [[ -n "${TEMP_DIR}" && "${TEMP_DIR}" == /tmp/slideai-smoke.* ]]; then
    rm -rf "${TEMP_DIR}"
  fi
}
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --full) FULL=1; shift ;;
    --pdf) PDF_PATH="${2:-}"; shift 2 ;;
    -h|--help)
      cat <<'EOF'
Usage:
  scripts/smoke_test.sh
  scripts/smoke_test.sh --pdf /path/to/test.pdf
  scripts/smoke_test.sh --full --pdf /path/to/test.pdf

Default checks HTTP, project history and LAN-style CORS without creating data.
Providing --pdf also checks auth-free upload, persistent run creation and the
first page image without loading model services.
Full mode runs PDF -> LLM -> TTS -> ASR -> alignment -> subtitle/no-subtitle
render -> persistent merge, then deletes its temporary run.
EOF
      exit 0
      ;;
    *) fail "unknown argument: $1" ;;
  esac
done

for command_name in curl jq; do
  command -v "${command_name}" >/dev/null 2>&1 || fail "missing command: ${command_name}"
done

curl -fsS "${FRONTEND_BASE}/video-abstract-lab" >/dev/null \
  || fail "lab frontend unavailable: ${FRONTEND_BASE}/video-abstract-lab"
curl -fsS "${API_BASE}/api/health" | jq -e '.status == "healthy"' >/dev/null \
  || fail "backend health failed"
curl -fsS "${API_BASE}/api/video-runs" | jq -e '.runs | type == "array"' >/dev/null \
  || fail "video-run history failed"
curl -fsS "${API_BASE}/api/llm/status" | jq -e 'has("configured") and has("provider")' >/dev/null \
  || fail "LLM status failed"

cors_code="$(
  curl -sS -o /dev/null -w '%{http_code}' -X OPTIONS \
    -H 'Origin: http://192.168.1.10:5174' \
    -H 'Access-Control-Request-Method: POST' \
    "${API_BASE}/api/video-abstract"
)"
[[ "${cors_code}" == "200" || "${cors_code}" == "204" ]] \
  || fail "private-LAN CORS failed (${cors_code})"
say "PASS: frontend, backend, project history, LLM status and CORS"

if [[ -z "${PDF_PATH}" ]]; then
  [[ "${FULL}" == "0" ]] && exit 0
  fail "--full requires an existing --pdf file"
fi
[[ -f "${PDF_PATH}" ]] || fail "PDF not found: ${PDF_PATH}"
command -v file >/dev/null 2>&1 || fail "missing command: file"

TEMP_DIR="$(mktemp -d /tmp/slideai-smoke.XXXXXX)"
upload_json="${TEMP_DIR}/upload.json"
upload_code="$(
  curl -sS -o "${upload_json}" -w '%{http_code}' \
    -H 'Authorization: Bearer deliberately-invalid-smoke-token' \
    -F "file=@${PDF_PATH};type=application/pdf" \
          "${API_BASE}/api/video-abstract"
)"
[[ "${upload_code}" == "200" ]] || fail "local-only PDF upload failed (${upload_code}): $(jq -r '.detail // empty' "${upload_json}")"
RUN_ID="$(jq -r '.run_id // empty' "${upload_json}")"
[[ -n "${RUN_ID}" ]] || fail "upload returned no run_id"

slide_image="${TEMP_DIR}/slide.jpg"
curl -fsS "${API_BASE}/api/video-runs/${RUN_ID}/pages/0/image" -o "${slide_image}" \
  || fail "page image failed"
file "${slide_image}" | grep -Eq 'JPEG|PNG' || fail "page image is invalid"
say "PASS: auth-free PDF upload, persistent run and page image"

[[ "${FULL}" == "1" ]] || exit 0
command -v ffprobe >/dev/null 2>&1 || fail "missing command: ffprobe"

if curl -fsS "${API_BASE}/api/llm/status" | jq -e '.configured == true' >/dev/null; then
  curl -fsS -H 'Content-Type: application/json' \
    -d '{"pages":[0],"scope":"current","source":"pdf","language":"zh","overwrite":true}' \
    "${API_BASE}/api/video-runs/${RUN_ID}/scripts/generate" \
    | jq -e '.updated_pages | length > 0' >/dev/null \
    || fail "LLM script generation failed"
fi

ref_audio="${PROJECT_ROOT}/backend/app/static/ref_voices/YunJhe_中文-男.mp3"
ref_text='大家好，今天我們要來探討語音克隆的技術，這是一段測試的文字，我想看一下音色效果如何，他會如何複製這個聲音。'
tts_text='這是一段 SlideAI 自動健康檢查語音，用來確認完整工作流程正常。'
tts_audio="${TEMP_DIR}/tts.wav"
tts_headers="${TEMP_DIR}/tts.headers"
curl -fsS -D "${tts_headers}" -o "${tts_audio}" \
  -F "text=${tts_text}" \
  -F 'voice=YunJhe_中文-男' \
  -F 'speed=1.0' \
  -F "reference_text=${ref_text}" \
  -F "reference_audio=@${ref_audio};type=audio/mpeg" \
  "${API_BASE}/api/video-runs/${RUN_ID}/pages/0/tts" \
  || fail "persistent TTS failed"
variant_id="$(awk 'BEGIN{IGNORECASE=1}/^x-variant-id:/{gsub("\r",""); print $2}' "${tts_headers}")"
[[ -n "${variant_id}" ]] || fail "TTS returned no variant ID"
audio_duration="$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "${tts_audio}")"
awk -v n="${audio_duration}" 'BEGIN{exit !(n > 0)}' || fail "TTS output has no duration"

asr_json="${TEMP_DIR}/asr.json"
curl -fsS -F "reference_audio=@${tts_audio};type=audio/wav" \
  "${API_BASE}/api/video-abstract/reference-asr" -o "${asr_json}" \
  || fail "reference ASR failed"
jq -e '.text | length > 0' "${asr_json}" >/dev/null || fail "ASR returned empty text"

align_json="${TEMP_DIR}/align.json"
curl -fsS \
  -F "text=${tts_text}" \
  -F 'language=zh' \
  -F 'alignment_mode=scripted' \
  -F 'split_min_chars=6' \
  -F 'split_max_chars=18' \
  -F "tts_id=${variant_id}" \
  -F "variant_id=${variant_id}" \
  "${API_BASE}/api/video-runs/${RUN_ID}/pages/0/align" -o "${align_json}" \
  || fail "forced alignment failed"
jq -e '.segments | length > 0' "${align_json}" >/dev/null || fail "alignment returned no segments"

segments="$(jq -c '.segments' "${align_json}")"
rendered_video="${TEMP_DIR}/rendered.mp4"
curl -fsS -o "${rendered_video}" \
  -F "segments_json=${segments}" \
  -F 'subtitle_mode=burn' \
  -F 'subtitle_style=bg-dark' \
  -F 'enable_highlight=true' \
  -F 'font_size=54' \
  -F 'enable_background=true' \
  -F 'bg_color=#000000' \
  -F 'bg_opacity=68' \
  -F 'margin_v=96' \
  -F 'align_backend=qwen3-forced-aligner' \
  -F "run_id=${RUN_ID}" \
  -F 'page_index=0' \
  -F 'variant_label=automated-smoke-test' \
  -F "tts_id=${variant_id}" \
  -F "align_id=${variant_id}" \
  -F "variant_id=${variant_id}" \
  "${API_BASE}/api/video-abstract/render-subtitle-ass-video" \
  || fail "persistent subtitle render failed"
ffprobe -v error "${rendered_video}" >/dev/null || fail "rendered MP4 is invalid"

curl -fsS \
  "${API_BASE}/api/video-runs/${RUN_ID}/pages/0/variants/${variant_id}/subtitles.srt" \
  -o "${TEMP_DIR}/subtitles.srt" \
  || fail "sidecar SRT download failed"
[[ -s "${TEMP_DIR}/subtitles.srt" ]] || fail "sidecar SRT is empty"

no_subtitle_video="${TEMP_DIR}/no-subtitle.mp4"
curl -fsS -o "${no_subtitle_video}" \
  -F 'segments_json=[]' \
  -F 'subtitle_mode=none' \
  -F "run_id=${RUN_ID}" \
  -F 'page_index=0' \
  -F "variant_id=${variant_id}" \
  "${API_BASE}/api/video-abstract/render-subtitle-ass-video" \
  || fail "no-subtitle render failed"
ffprobe -v error "${no_subtitle_video}" >/dev/null || fail "no-subtitle MP4 is invalid"

merged_video="${TEMP_DIR}/merged.mp4"
curl -fsS -o "${merged_video}" \
  -F 'page_indexes_json=[0]' \
  -F "variant_ids_json={\"0\":\"${variant_id}\"}" \
  -F 'response_mode=video' \
  "${API_BASE}/api/video-runs/${RUN_ID}/exports/merge-selected" \
  || fail "persistent merge failed"
ffprobe -v error "${merged_video}" >/dev/null || fail "merged MP4 is invalid"

say "PASS: PDF, LLM, TTS, ASR, forced alignment, SRT, subtitle/no-subtitle render and merge"
