from backend.app.services.upload_limits import read_upload_limited
import os
import asyncio
import tempfile
import shutil
import json
import uuid
import logging
import threading
import time
import re
from pathlib import Path
from fastapi import APIRouter, UploadFile, File, Form, Request, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
import io
from pydantic import BaseModel, Field
from typing import Optional, List
import pypdf as PyPDF2
from backend.app.services.speech_providers import (
    align_subtitles,
    get_tts_provider_name,
    synthesize_tts_preview,
    transcribe_reference_audio,
    tts_requires_reference_text,
    warm_tts_provider,
)
from backend.app.services.artifact_store import get_video_run_store
from backend.app.services.ass_renderer import generate_ass_script
from backend.app.api.video_helpers import (
    apply_audio_speed as _apply_audio_speed,
    clamp_preview_speed as _clamp_preview_speed,
    is_local_only_mode as _is_local_only_mode,
    is_mock_mode as _is_mock_mode,
    split_user_script_to_pages as _split_user_script_to_pages,
    to_traditional_chinese_for_display as _to_traditional_chinese_for_display,
)
logger = logging.getLogger("video_abstract")
logging.basicConfig(level=logging.INFO)

router = APIRouter()
_PRESET_VOICE_DIR = Path(__file__).resolve().parents[1] / "static" / "ref_voices"
MAX_FILE_SIZE = int(os.getenv("MAX_FILE_SIZE", str(50 * 1024 * 1024)))


def _load_preset_reference_voice(voice_key: str) -> tuple[bytes, str, str] | None:
    """Resolve a built-in voice without trusting a client-supplied path."""
    key = str(voice_key or "").strip()
    if not key or key == "custom":
        return None
    try:
        manifest = json.loads((_PRESET_VOICE_DIR / "manifest.json").read_text(encoding="utf-8"))
        entry = manifest.get(key) or {}
        filename = Path(str(entry.get("file") or "")).name
        if not filename:
            return None
        audio_path = (_PRESET_VOICE_DIR / filename).resolve()
        if _PRESET_VOICE_DIR.resolve() not in audio_path.parents or not audio_path.is_file():
            return None
        return audio_path.read_bytes(), filename, str(entry.get("transcript") or "")
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _pregenerate_run_thumbnails_safe(run_id: str, pdf_path: str) -> None:
    """Best-effort page image cache for the persistent run store.

    This runs in a daemon thread after upload so the UI can enter the workspace
    immediately while previews become available progressively.
    """
    try:
        from backend.app.services.page_images import ensure_run_images
        ensure_run_images(get_video_run_store(), run_id)
    except Exception as exc:
        logger.warning("[UPLOAD][BG] Page images failed run=%s: %s", run_id, exc)



def _use_nano_voxcpm_tts() -> bool:
    return get_tts_provider_name() in {"voxcpm_nano", "nano_vllm", "voxcpm"}


@router.post("/api/video-abstract")
async def video_abstract_api(
    request: Request,
    file: UploadFile = File(...),
):
    """Create a persistent project; script generation has its own endpoint."""
    mock_mode = _is_mock_mode()

    if file:
        # The local-first application accepts PDF presentations only.
        if file.content_type and file.content_type.startswith("video/"):
            raise HTTPException(status_code=415, detail="目前僅支援 PDF 簡報")

        # Reserve the persistent run input path up front so the run copy is the
        # only canonical project PDF.
        file.file.seek(0, os.SEEK_END)
        pdf_size = file.file.tell()
        file.file.seek(0)
        if pdf_size <= 0:
            raise HTTPException(status_code=400, detail="PDF 檔案是空的")
        if pdf_size > MAX_FILE_SIZE:
            raise HTTPException(
                status_code=413,
                detail=f"PDF 檔案過大，請上傳 {MAX_FILE_SIZE // 1024 // 1024}MB 以下的檔案",
            )
        pdf_signature = file.file.read(5)
        file.file.seek(0)
        if pdf_signature != b"%PDF-":
            raise HTTPException(status_code=400, detail="檔案內容不是有效的 PDF")

        pdf_id = str(uuid.uuid4())
        run_store = get_video_run_store()
        reserved_run_id = run_store.new_run_id()
        project_name = file.filename or "source.pdf"
        pdf_name = run_store.safe_filename(project_name, "source.pdf")
        if not pdf_name.lower().endswith(".pdf"):
            pdf_name += ".pdf"
        run_input_dir = run_store.run_dir(reserved_run_id) / "input"
        run_input_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = str(run_input_dir / pdf_name)
        logger.info(f"[PDF UPLOAD] Saving canonical run PDF to {pdf_path}")
        with open(pdf_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
        logger.info(f"[PDF UPLOAD] PDF saved: {os.path.exists(pdf_path)} size={os.path.getsize(pdf_path) if os.path.exists(pdf_path) else 0}")

        # 僅在系統啟動時檢查 Poppler，這裡不下載/檢查。
        # 只產生 AI 文字，不產生影片
        try:
            form = await request.form()
            content_language = form.get('content_language') if 'content_language' in form else None
            voice_hint = form.get('voice') if 'voice' in form else None
            language_hint = form.get('language') if 'language' in form else None
            skip_llm_raw = str(form.get('skip_llm', '')).strip().lower()
            skip_llm = skip_llm_raw in {"1", "true", "yes", "on"}
            subtitle_source = str(form.get('subtitle_source', '')).strip()
            user_script = str(form.get('user_script', '') or '')
        except Exception:
            content_language = None
            voice_hint = None
            language_hint = None
            skip_llm = False
            subtitle_source = ""
            user_script = ""

        try:
            try:
                with open(pdf_path, "rb") as pdf_file:
                    reader = PyPDF2.PdfReader(pdf_file)
                    page_count = len(reader.pages)
                if page_count == 0:
                    raise ValueError("PDF 沒有頁面")
            except Exception as exc:
                raise HTTPException(status_code=400, detail="PDF 無法解析或沒有頁面") from exc

            if subtitle_source == "none":
                ai_texts = ["" for _ in range(page_count)]
                logger.info(f"[UPLOAD] subtitle_source=none, return {len(ai_texts)} empty scripts")
            elif subtitle_source == "user_input":
                ai_texts = _split_user_script_to_pages(user_script, page_count)
                logger.info(f"[UPLOAD] subtitle_source=user_input, parsed {len(ai_texts)} page scripts")
            else:
                ai_texts = ["" for _ in range(page_count)]
        except Exception:
            shutil.rmtree(run_store.run_dir(reserved_run_id), ignore_errors=True)
            raise

        # Filesystem manifests are the sole project record.
        project_id = None

        run_manifest = None
        run_id = None
        try:
            run_manifest = run_store.create_run(
                pdf_path=pdf_path,
                original_filename=project_name,
                scripts=ai_texts,
                settings={
                    "upload": {
                        "subtitle_source": subtitle_source or ("none" if skip_llm else ""),
                        "content_language": content_language,
                        "voice": voice_hint,
                        "language": language_hint,
                        "skip_llm": skip_llm,
                    }
                },
                pdf_id=pdf_id,
                project_id=project_id,
                source="web-upload",
                run_id=reserved_run_id,
            )
            run_id = str(run_manifest.get("run_id") or "")
            logger.info(f"[UPLOAD] Created video run id={run_id} name={project_name}")
            threading.Thread(
                target=_pregenerate_run_thumbnails_safe,
                args=(run_id, pdf_path),
                daemon=True,
            ).start()
        except Exception as run_err:
            logger.error(f"[UPLOAD] Failed to create persistent video run: {run_err}", exc_info=True)
            shutil.rmtree(run_store.run_dir(reserved_run_id), ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"建立專案資料失敗: {run_err}")

        return JSONResponse({
            "texts": ai_texts,
            "pdf_id": pdf_id,
            "project_id": project_id,
            "run_id": run_id,
            "run": run_manifest,
            # The lightweight app-only launcher intentionally has no LLM/model
            # services. Tell the SPA not to immediately start a second,
            # model-dependent request after the PDF upload has succeeded.
            "model_services_skipped": bool(mock_mode),
        })

@router.post("/api/video-abstract/tts-preview")
async def tts_preview_endpoint(
    text: str = Form(...),
    voice: str = Form("zh-TW-YunJheNeural"),
    speed: float = Form(1.0),
    reference_audio: Optional[UploadFile] = File(None),
    reference_text: str = Form(""),
):
    """
    TTS 試聽生成端點，透過部署時選定的 provider 進行語音克隆。
    """
    import os

    speed = _clamp_preview_speed(speed)
    out_fd, out_tmp = tempfile.mkstemp(prefix="slideai_tts_preview_", suffix=".mp3")
    os.close(out_fd)

    local_only_mode = _is_local_only_mode()

    ref_data = None
    file_suffix = ".wav"
    if reference_audio is not None:
        ref_data = await read_upload_limited(reference_audio)
        file_suffix = os.path.splitext(reference_audio.filename or "")[-1] or ".wav"

    if ref_data:
        try:
            provider = get_tts_provider_name()
            if tts_requires_reference_text() and not (reference_text or "").strip():
                raise HTTPException(
                    status_code=400,
                    detail=f"{provider} 語音克隆需要參考音檔逐字稿；請先使用「ASR 代填」或填入 reference text。",
                )
            ok, out_wav, reason = await asyncio.to_thread(
                synthesize_tts_preview,
                text=text,
                reference_audio_bytes=ref_data,
                reference_suffix=file_suffix,
                reference_text=reference_text or "",
            )
            if ok and out_wav and os.path.exists(out_wav):
                final_out = await asyncio.to_thread(_apply_audio_speed, out_wav, speed)
                return FileResponse(final_out, media_type="audio/wav", filename="tts_preview.wav")
            if local_only_mode:
                logger.error(f"[TTS Preview] LOCAL_ONLY mode and {provider} unavailable: {reason}")
                raise HTTPException(status_code=500, detail=f"LOCAL_ONLY 模式下 {provider} 失敗：{reason}")
            raise HTTPException(status_code=500, detail=f"{provider} 失敗：{reason}")
        except HTTPException:
            raise
        except Exception as e:
            if local_only_mode:
                logger.error(f"[TTS Preview] LOCAL_ONLY mode and TTS exception: {e}")
                raise HTTPException(status_code=500, detail=f"LOCAL_ONLY 模式下 TTS 例外：{str(e)}")
            raise HTTPException(status_code=500, detail=f"TTS 例外：{str(e)}")
    elif local_only_mode:
        raise HTTPException(status_code=400, detail="LOCAL_ONLY 模式下請提供參考音檔，才能使用本地 TTS 生成。")
    raise HTTPException(status_code=400, detail="請提供參考音檔以生成語音。EdgeTTS fallback 已停用。")


@router.post("/api/video-abstract/tts-warmup")
async def tts_warmup_endpoint():
    """
    Warm up the configured TTS provider when appropriate.
    The worker still shuts down automatically after the configured idle timeout.
    """
    return JSONResponse(await asyncio.to_thread(warm_tts_provider))


@router.post("/api/video-runs/{run_id}/pages/{page_index}/tts")
async def video_run_page_tts_endpoint(
    run_id: str,
    page_index: int,
    text: str = Form(...),
    voice: str = Form("zh-TW-YunJheNeural"),
    speed: float = Form(1.0),
    reference_audio: Optional[UploadFile] = File(None),
    reference_text: str = Form(""),
    selected_voice_key: str = Form(""),
    response_mode: str = Form("audio"),
):
    """Generate one page TTS, persist it immediately, and return the audio."""
    import os
    if page_index < 0:
        raise HTTPException(status_code=400, detail="page_index must be >= 0")
    ref_data = await read_upload_limited(reference_audio) if reference_audio is not None else None
    reference_filename = reference_audio.filename if reference_audio is not None else "reference.wav"
    current_settings = {}
    if not ref_data:
        try:
            manifest = get_video_run_store().load_manifest(run_id)
            current_settings = ((manifest.get("settings") or {}).get("current") or {})
            saved_ref = current_settings.get("reference_audio") or {}
            saved_ref_path = Path(str(saved_ref.get("path") or ""))
            if saved_ref_path.is_file():
                ref_data = await asyncio.to_thread(saved_ref_path.read_bytes)
                reference_filename = str(saved_ref.get("filename") or saved_ref_path.name)
        except FileNotFoundError:
            pass
    if not ref_data:
        # Batch rendering submits JSON and cannot attach the browser File on
        # every page.  Resolve built-in voices directly from the trusted
        # server manifest, using either the request or persisted selection.
        preset = await asyncio.to_thread(
            _load_preset_reference_voice,
            selected_voice_key or current_settings.get("selected_voice_key") or "",
        )
        if preset:
            ref_data, reference_filename, preset_transcript = preset
            if not (reference_text or "").strip():
                reference_text = str(current_settings.get("reference_text") or preset_transcript)
    if not ref_data:
        raise HTTPException(status_code=400, detail="請提供參考音檔以生成語音。")
    file_suffix = os.path.splitext(reference_filename or "")[-1] or ".wav"
    if tts_requires_reference_text() and not (reference_text or "").strip():
        raise HTTPException(
            status_code=400,
            detail=f"{get_tts_provider_name()} 語音克隆需要參考音檔逐字稿。",
        )
    ok, out_wav, reason = await asyncio.to_thread(
        synthesize_tts_preview,
        text=text,
        reference_audio_bytes=ref_data,
        reference_suffix=file_suffix,
        reference_text=reference_text or "",
    )
    if not ok or not out_wav or not os.path.exists(out_wav):
        raise HTTPException(status_code=500, detail=f"TTS 失敗：{reason}")
    final_out = await asyncio.to_thread(_apply_audio_speed, out_wav, speed)
    variant = get_video_run_store().record_page_variant_tts(
        run_id=run_id,
        page_index=page_index,
        audio_source_path=final_out,
        metadata={
            "text": text,
            "voice": voice,
            "speed": speed,
            # Keep the server-side voice identity with the variant.  This is
            # also the reliable fallback for a later chunk regeneration when
            # the browser no longer has the original File object.
            "selected_voice_key": selected_voice_key or current_settings.get("selected_voice_key") or "",
            "reference_text": reference_text or "",
        },
        label=f"web-page-{page_index + 1}",
    )
    stored_audio_path = variant["paths"]["audio"]
    for temporary_path in {str(out_wav), str(final_out)}:
        try:
            if Path(temporary_path).resolve() != Path(stored_audio_path).resolve():
                Path(temporary_path).unlink(missing_ok=True)
                shutil.rmtree(Path(temporary_path).with_suffix(".chunks"), ignore_errors=True)
        except Exception:
            pass
    if str(response_mode or "").strip().lower() == "json":
        return JSONResponse({
            "ok": True,
            "tts_id": variant["variant_id"],
            "variant_id": variant["variant_id"],
            "audio_url": f"/api/video-runs/{run_id}/pages/{page_index}/variants/{variant['variant_id']}/audio",
            "chunks": [
                {k: value for k, value in chunk.items() if k != "path"}
                for chunk in ((variant.get("tts") or {}).get("chunks") or [])
            ],
        })
    return FileResponse(
        stored_audio_path,
        media_type="audio/wav",
        filename=f"page_{page_index + 1}_tts.wav",
        headers={
            "X-TTS-Id": variant["variant_id"],
            "X-Variant-Id": variant["variant_id"],
        },
    )


@router.post("/api/video-runs/{run_id}/pages/{page_index}/variants/{variant_id}/chunks/{chunk_index}/regenerate")
async def regenerate_video_run_tts_chunk(
    run_id: str,
    page_index: int,
    variant_id: str,
    chunk_index: int,
    text: str = Form(""),
):
    """Regenerate one existing four-sentence chunk and create a new page variant."""
    from backend.app.services.tts_chunks import combine_tts_chunks

    store = get_video_run_store()
    try:
        manifest = store.load_manifest(run_id)
        variant = store.get_page_variant(
            run_id=run_id, page_index=page_index, variant_id=variant_id,
        )
    except (FileNotFoundError, IndexError):
        raise HTTPException(status_code=404, detail="找不到要修正的語音變體")

    tts_data = variant.get("tts") or {}
    chunks = list(tts_data.get("chunks") or [])
    if chunk_index < 0 or chunk_index >= len(chunks):
        raise HTTPException(status_code=404, detail="此語音沒有可局部重生的四句分段")
    for chunk in chunks:
        if not Path(str(chunk.get("path") or "")).is_file():
            raise HTTPException(status_code=409, detail="舊語音沒有保留完整 chunk，請先重新生成整頁")

    current_settings = ((manifest.get("settings") or {}).get("current") or {})
    reference = current_settings.get("reference_audio") or {}
    reference_path = Path(str(reference.get("path") or ""))
    reference_bytes: bytes | None = None
    reference_filename = "reference.wav"
    preset_transcript = ""
    if reference_path.is_file():
        reference_bytes = await asyncio.to_thread(reference_path.read_bytes)
        reference_filename = str(reference.get("filename") or reference_path.name)
    else:
        # Preset voices intentionally do not create a per-project uploaded
        # reference file.  The ordinary page-TTS route already resolves them
        # from the trusted server manifest; chunk regeneration must use the
        # exact same fallback rather than incorrectly requiring an upload.
        metadata_voice_key = str((tts_data.get("metadata") or {}).get("selected_voice_key") or "")
        preset = await asyncio.to_thread(
            _load_preset_reference_voice,
            metadata_voice_key or current_settings.get("selected_voice_key") or "",
        )
        if preset:
            reference_bytes, reference_filename, preset_transcript = preset
    if not reference_bytes:
        raise HTTPException(
            status_code=400,
            detail="找不到此語音變體的參考音檔或內建音色；請在語音設定重新選擇音色後再重生。",
        )
    reference_text = str(
        (tts_data.get("metadata") or {}).get("reference_text")
        or current_settings.get("reference_text")
        or preset_transcript
        or ""
    ).strip()
    if tts_requires_reference_text() and not reference_text:
        raise HTTPException(status_code=400, detail="此語音模型需要參考音檔逐字稿")

    replacement_text = str(text or chunks[chunk_index].get("text") or "").strip()
    if not replacement_text:
        raise HTTPException(status_code=400, detail="重生文字不可為空")
    ok, replacement_path, reason = await asyncio.to_thread(
        synthesize_tts_preview,
        text=replacement_text,
        reference_audio_bytes=reference_bytes,
        reference_suffix=Path(reference_filename).suffix or ".wav",
        reference_text=reference_text,
    )
    if not ok or not replacement_path or not os.path.isfile(replacement_path):
        raise HTTPException(status_code=500, detail=f"局部 TTS 重生失敗：{reason}")

    workspace = tempfile.mkdtemp(prefix="slideai_chunk_regen_")
    try:
        combined_path = Path(workspace) / "combined.wav"
        sources = []
        for index, chunk in enumerate(chunks):
            chunk_text = replacement_text if index == chunk_index else str(chunk.get("text") or "")
            chunk_path = replacement_path if index == chunk_index else str(chunk.get("path") or "")
            sources.append((chunk_text, chunk_path))
        combine_tts_chunks(
            sources,
            combined_path,
            silence_ms=float(tts_data.get("chunk_silence_ms") or 120.0),
        )
        metadata = dict(tts_data.get("metadata") or {})
        metadata.update({
            "regenerated_from_variant_id": variant_id,
            "regenerated_chunk_index": chunk_index,
        })
        new_variant = store.record_page_variant_tts(
            run_id=run_id,
            page_index=page_index,
            audio_source_path=combined_path,
            metadata=metadata,
            label=f"chunk-{chunk_index + 1}-retry",
        )
        return JSONResponse({
            "ok": True,
            "tts_id": new_variant["variant_id"],
            "variant_id": new_variant["variant_id"],
            "audio_url": f"/api/video-runs/{run_id}/pages/{page_index}/variants/{new_variant['variant_id']}/audio",
            "chunks": [
                {key: value for key, value in chunk.items() if key != "path"}
                for chunk in ((new_variant.get("tts") or {}).get("chunks") or [])
            ],
            # If the user edited this local segment, the subsequent full-page
            # forced alignment must receive the same script as the combined
            # audio.  Returning it avoids a subtle audio/text timeline drift.
            "page_text": "\n\n".join(text for text, _ in sources).strip(),
        })
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        try:
            Path(replacement_path).unlink(missing_ok=True)
            shutil.rmtree(Path(replacement_path).with_suffix(".chunks"), ignore_errors=True)
        except Exception:
            pass


@router.post("/api/video-runs/{run_id}/pages/{page_index}/align")
async def video_run_page_align_endpoint(
    run_id: str,
    page_index: int,
    text: str = Form(""),
    language: str = Form("auto"),
    alignment_mode: str = Form("auto"),
    split_min_chars: int = Form(10),
    split_max_chars: int = Form(32),
    enable_pause_split: bool = Form(False),
    pause_threshold_ms: int = Form(320),
    tts_id: str = Form(""),
    variant_id: str = Form(""),
):
    """Align one page audio, persist segments immediately, and return them."""
    if page_index < 0:
        raise HTTPException(status_code=400, detail="page_index must be >= 0")
    target_variant_id = variant_id or tts_id
    if not target_variant_id:
        raise HTTPException(status_code=400, detail="variant_id is required for persistent alignment")
    try:
        audio_path = get_video_run_store().get_variant_audio_path(
            run_id=run_id, page_index=page_index, variant_id=target_variant_id,
        )
    except (FileNotFoundError, IndexError):
        raise HTTPException(status_code=404, detail="找不到此變體的 TTS 音訊")
    audio_bytes = None
    audio_filename = audio_path.name
    audio_source_path = str(audio_path)
    result = await asyncio.to_thread(
        align_subtitles,
        text=text,
        audio_bytes=audio_bytes,
        audio_source_path=audio_source_path,
        audio_filename=audio_filename,
        language=language,
        alignment_mode=alignment_mode,
        split_min_chars=split_min_chars,
        split_max_chars=split_max_chars,
        enable_pause_split=enable_pause_split,
        pause_threshold_ms=pause_threshold_ms,
    )
    variant = get_video_run_store().record_page_variant_alignment(
        run_id=run_id,
        page_index=page_index,
        variant_id=target_variant_id,
        segments=result.segments,
        srt=result.srt,
        metadata={
            "text": text,
            "backend": result.backend,
            "tts_id": tts_id,
            "split_min_chars": split_min_chars,
            "split_max_chars": split_max_chars,
            "readable_chunks": result.readable_chunks or [],
            "source_text_preview": (result.source_text or "")[:300],
            "warning": result.warning or "",
            "match_ratio": result.match_ratio,
        },
    )
    align_id = variant["variant_id"]
    return JSONResponse(
        {
            "align_id": align_id,
            "variant_id": variant["variant_id"],
            "segments": result.segments,
            "srt": result.srt,
            "backend": result.backend,
            "audio_duration": result.audio_duration,
            "readable_chunks": result.readable_chunks or [],
            "warning": result.warning or "",
            "match_ratio": result.match_ratio,
        },
        headers={"X-Align-Id": align_id, "X-Variant-Id": variant["variant_id"]},
    )


@router.post("/api/video-abstract/reference-asr")
async def reference_asr_fill_endpoint(
    reference_audio: UploadFile = File(...),
):
    try:
        ref_data = await read_upload_limited(reference_audio)
        file_suffix = os.path.splitext(reference_audio.filename or "")[-1] or ".wav"
        ok, text, reason = await asyncio.to_thread(
            transcribe_reference_audio,
            ref_data,
            file_suffix,
        )
        if not ok:
            raise HTTPException(status_code=500, detail=reason)
        display_text = _to_traditional_chinese_for_display(str(text or "").strip())
        return JSONResponse({"text": display_text})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[Reference ASR] failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"本地 ASR 代填失敗: {str(e)}")


@router.post("/api/video-abstract/render-subtitle-ass-video")
async def render_subtitle_ass_video(
    segments_json: str = Form("[]"),
    subtitle_style: str = Form("bg-dark"),
    subtitle_mode: str = Form("burn"),
    enable_highlight: bool = Form(True),
    font_size: int = Form(54),
    enable_background: bool = Form(True),
    bg_color: str = Form("#000000"),
    bg_opacity: int = Form(68),
    margin_v: int = Form(96),
    align_backend: str = Form(""),
    run_id: str = Form(..., min_length=1),
    page_index: int = Form(..., ge=0),
    variant_label: str = Form(""),
    tts_id: str = Form(""),
    align_id: str = Form(""),
    variant_id: str = Form(""),
    tts_voice: str = Form(""),
    tts_speed: str = Form(""),
    selected_voice_key: str = Form(""),
    reference_text: str = Form(""),
):
    """(ASS 引擎極速渲染) Create a video with ASS subtitles from audio and an image."""
    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp(prefix="slideai_ass_temp_")
        store = get_video_run_store()
        target_variant_id = str(variant_id or tts_id or align_id or "").strip()
        if not isinstance(run_id, str) or not run_id.strip() or not isinstance(page_index, int) or page_index < 0 or not target_variant_id:
            raise HTTPException(status_code=422, detail="渲染必須指定專案、頁碼與既有音訊變體")
        try:
            audio_path = str(store.get_variant_audio_path(
                run_id=run_id, page_index=page_index, variant_id=target_variant_id,
            ))
            from backend.app.services.page_images import ensure_page_images
            pair = await asyncio.to_thread(ensure_page_images, store, run_id, page_index)
            slide_path = str(pair["slide"])
        except (FileNotFoundError, IndexError):
            raise HTTPException(status_code=404, detail="找不到專案頁面或變體音訊")

        try:
            segments_list = json.loads(segments_json or "[]")
        except (ValueError, TypeError):
            raise HTTPException(status_code=422, detail="字幕時間軸必須是有效 JSON")
        if not isinstance(segments_list, list) or len(segments_list) > 50000 or any(not isinstance(item, dict) for item in segments_list):
            raise HTTPException(status_code=422, detail="字幕時間軸必須是段落物件陣列（最多 50000 段）")

        subtitle_mode = str(subtitle_mode or "burn").strip().lower()
        if subtitle_mode not in {"none", "sidecar", "burn"}:
            raise HTTPException(status_code=400, detail="無效的字幕輸出模式")
        if subtitle_mode == "burn" and not segments_list:
            raise HTTPException(status_code=400, detail="缺少可用字幕時間軸")

        canvas_w, canvas_h = 1920, 1080

        ass_content = None
        ass_path = ""
        if subtitle_mode == "burn":
            is_qwen = "qwen" in align_backend.lower()
            ass_content = generate_ass_script(
                canvas_w, canvas_h, segments_list, subtitle_style, font_size,
                bg_opacity, enable_highlight, is_qwen, margin_v=margin_v,
                enable_background=enable_background, background_color=bg_color,
            )
            ass_path = os.path.join(temp_dir, "subtitles.ass")
            with open(ass_path, "w", encoding="utf-8") as f:
                f.write(ass_content)

        out_mp4 = os.path.join(temp_dir, "output.mp4")

        local_fonts_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "public", "vendor"))
        vf_chain = f"scale={canvas_w}:{canvas_h}:force_original_aspect_ratio=decrease,pad={canvas_w}:{canvas_h}:(ow-iw)/2:(oh-ih)/2"
        if subtitle_mode == "burn":
            ass_filter = f"ass={ass_path}"
            if os.path.isdir(local_fonts_dir):
                ass_filter += f":fontsdir={local_fonts_dir}"
            vf_chain += f",{ass_filter}"

        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-loop", "1", "-i", slide_path,
            "-i", audio_path,
            "-vf", vf_chain,
            "-c:v", "libx264",
            "-tune", "stillimage",
            "-c:a", "aac",
            "-shortest",
            "-pix_fmt", "yuv420p",
            out_mp4
        ]

        proc = await asyncio.create_subprocess_exec(
            *ffmpeg_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout_data, stderr_data = await asyncio.wait_for(proc.communicate(), timeout=1800)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            raise
        if proc.returncode != 0:
            err_text = (stderr_data or b"").decode(errors='ignore')
            logger.error(f"ASS FFmpeg Failed: {err_text}")
            detail = (err_text.strip().splitlines()[-1] if err_text.strip() else "ASS 渲染失敗")
            if len(detail) > 260:
                detail = detail[:260]
            raise HTTPException(status_code=500, detail=f"ASS 渲染失敗: {detail}")

        headers = {
            "Content-Disposition": "attachment; filename=subtitle_ass.mp4",
            "X-Subtitle-Render-Version": "ass-discrete-fast",
            "X-Subtitle-Style-Applied": subtitle_style,
        }
        persisted_video_path = ""
        if run_id and page_index >= 0:
            variant = store.record_page_variant(
                run_id=run_id,
                page_index=page_index,
                video_source_path=out_mp4,
                audio_bytes=None,
                slide_bytes=None,
                segments=segments_list,
                ass_content=ass_content,
                settings={
                    "subtitle_style": subtitle_style,
                    "subtitle_output_mode": subtitle_mode,
                    "enable_highlight": enable_highlight,
                    "font_size": font_size,
                    "enable_background": enable_background,
                    "bg_color": bg_color,
                    "bg_opacity": bg_opacity,
                    "margin_v": margin_v,
                    "align_backend": align_backend,
                    "tts_id": tts_id,
                    "align_id": align_id,
                    "tts_voice": tts_voice,
                    "tts_speed": tts_speed,
                    "selected_voice_key": selected_voice_key,
                    "reference_text": reference_text,
                },
                label=variant_label or f"web-page-{page_index + 1}",
                variant_id=target_variant_id,
            )
            headers["X-Variant-Id"] = variant.get("variant_id", "")
            persisted_video_path = str((variant.get("paths") or {}).get("video") or "")

        if persisted_video_path and os.path.isfile(persisted_video_path):
            return FileResponse(persisted_video_path, media_type="video/mp4", headers=headers)

        raise HTTPException(status_code=500, detail="渲染成果未成功保存")

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"ASS Render Error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


class BatchRenderJobRequest(BaseModel):
    page_indexes: List[int] = Field(default_factory=list)
    subtitle_mode: str = "burn"
    split_min_chars: int = 10
    split_max_chars: int = 32
    tts_voice: str = ""
    tts_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    selected_voice_key: str = ""
    reference_text: str = ""
    subtitle_settings: dict = Field(default_factory=dict)
    auto_merge: bool = False
    transitions_enabled: bool = False


class AgentVideoJobConfig(BaseModel):
    """Stable, intentionally small contract for unattended PDF rendering."""

    scripts: List[str]
    reference_text: str
    label: str = ""
    subtitle_mode: str = "burn"  # none | srt | burn
    tts_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    selected_voice_key: str = "custom"
    split_min_chars: int = Field(default=10, ge=4, le=80)
    split_max_chars: int = Field(default=32, ge=8, le=120)
    transitions_enabled: bool = False
    subtitle_settings: dict = Field(default_factory=dict)


@router.post("/api/agent/video-jobs", status_code=202)
async def create_agent_video_job(
    pdf: UploadFile = File(...),
    reference_audio: UploadFile = File(...),
    config_json: str = Form(...),
):
    """Submit PDF + reference voice + JSON and run the full video pipeline.

    The caller supplies exactly one script per PDF page.  The endpoint creates
    a persistent run, renders it through the shared FIFO GPU queue, and merges
    the selected page videos automatically.  It therefore behaves exactly like
    the WebUI pipeline without requiring a browser session.
    """
    try:
        config = AgentVideoJobConfig.model_validate_json(config_json)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"config_json 格式錯誤：{exc}")

    subtitle_mode = str(config.subtitle_mode or "burn").strip().lower()
    if subtitle_mode not in {"none", "srt", "burn"}:
        raise HTTPException(status_code=422, detail="subtitle_mode 必須是 none、srt 或 burn")
    if config.split_min_chars > config.split_max_chars:
        raise HTTPException(status_code=422, detail="split_min_chars 不可大於 split_max_chars")
    if tts_requires_reference_text() and not config.reference_text.strip():
        raise HTTPException(status_code=422, detail="VoxCPM2 語音克隆需要 reference_text")

    pdf_bytes = await read_upload_limited(pdf, MAX_FILE_SIZE)
    if not pdf_bytes or not pdf_bytes.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="pdf 必須是有效的 PDF 檔案")
    if len(pdf_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail=f"PDF 不可超過 {MAX_FILE_SIZE // 1024 // 1024}MB")
    reference_bytes = await read_upload_limited(reference_audio)
    if not reference_bytes:
        raise HTTPException(status_code=400, detail="reference_audio 不可為空")

    try:
        page_count = len(PyPDF2.PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"PDF 解析失敗：{exc}")
    scripts = [str(item or "").strip() for item in config.scripts]
    if len(scripts) != page_count:
        raise HTTPException(
            status_code=422,
            detail=f"scripts 必須與 PDF 頁數一致（PDF={page_count}，scripts={len(scripts)}）",
        )
    empty_pages = [index + 1 for index, text in enumerate(scripts) if not text]
    if empty_pages:
        raise HTTPException(status_code=422, detail=f"scripts 不可留空；空白頁：{empty_pages[:20]}")

    store = get_video_run_store()
    workspace = tempfile.mkdtemp(prefix="slideai_agent_submit_")
    try:
        source_path = Path(workspace) / "source.pdf"
        source_path.write_bytes(pdf_bytes)
        manifest = store.create_run(
            pdf_path=source_path,
            original_filename=pdf.filename or "agent-input.pdf",
            scripts=scripts,
            settings={"agent_request": config.model_dump()},
            source="agent-api",
        )
        run_id = str(manifest["run_id"])
        store.update_settings(
            run_id,
            {
                "selected_voice_key": config.selected_voice_key,
                "reference_text": config.reference_text,
                "tts_speed": config.tts_speed,
                "subtitle_mode": subtitle_mode,
                "subtitle": config.subtitle_settings,
                "has_reference_audio": True,
            },
            reference_audio=reference_bytes,
            reference_audio_name=reference_audio.filename or "reference.wav",
        )
        # The browser normally has time to create these while the user edits
        # scripts.  An agent submits immediately, so prepare all page images
        # before allowing the GPU job to start and avoid an asset race.
        persisted_pdf = str((store.load_manifest(run_id).get("paths") or {}).get("pdf") or "")
        await asyncio.to_thread(_pregenerate_run_thumbnails_safe, run_id, persisted_pdf)
        job = store.create_job(
            run_id=run_id,
            payload=BatchRenderJobRequest(
                page_indexes=list(range(page_count)),
                subtitle_mode=subtitle_mode,
                split_min_chars=config.split_min_chars,
                split_max_chars=config.split_max_chars,
                tts_speed=config.tts_speed,
                selected_voice_key=config.selected_voice_key,
                reference_text=config.reference_text,
                subtitle_settings=config.subtitle_settings,
                auto_merge=True,
                transitions_enabled=config.transitions_enabled,
            ).model_dump(),
        )
        job_id = str(job["job_id"])
        _start_batch_job_task(run_id, job_id)
        return JSONResponse(
            {
                "run_id": run_id,
                "job_id": job_id,
                "status": "queued",
                "status_url": f"/api/agent/video-jobs/{run_id}/{job_id}",
                "cancel_url": f"/api/video-runs/{run_id}/jobs/{job_id}/cancel",
            },
            status_code=202,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[Agent API] submit failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"建立 Agent 影片任務失敗：{exc}")
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


_BATCH_JOB_TASKS: dict[str, asyncio.Task] = {}
_BATCH_GPU_QUEUE_LOCK = asyncio.Lock()
_BATCH_WAITING_ORDER: list[tuple[str, str]] = []
_BATCH_ACTIVE_JOB: tuple[str, str] | None = None


def _register_batch_waiter(run_id: str, job_id: str) -> None:
    item = (str(run_id), str(job_id))
    if item != _BATCH_ACTIVE_JOB and item not in _BATCH_WAITING_ORDER:
        _BATCH_WAITING_ORDER.append(item)


def _unregister_batch_waiter(run_id: str, job_id: str) -> None:
    item = (str(run_id), str(job_id))
    try:
        _BATCH_WAITING_ORDER.remove(item)
    except ValueError:
        pass


def _public_job_progress(job: dict | None) -> dict:
    job = job or {}
    return {
        "stage": str(job.get("stage") or "queued"),
        "stage_index": int(job.get("stage_index") or 0),
        "stage_total": int(job.get("stage_total") or 0),
        "current_page_index": job.get("current_page_index"),
    }


def _batch_queue_metadata(run_id: str, job_id: str) -> dict:
    item = (str(run_id), str(job_id))
    active = _BATCH_ACTIVE_JOB
    if item == active:
        state = "running"
        waiting_position = 0
        jobs_ahead = 0
    else:
        try:
            waiting_index = _BATCH_WAITING_ORDER.index(item)
        except ValueError:
            waiting_index = -1
        state = "queued" if waiting_index >= 0 else "finished"
        waiting_position = waiting_index + 1 if waiting_index >= 0 else 0
        jobs_ahead = waiting_index + (1 if active else 0) if waiting_index >= 0 else 0

    active_progress = None
    if active:
        try:
            active_progress = _public_job_progress(
                get_video_run_store().load_job(run_id=active[0], job_id=active[1])
            )
        except Exception:
            active_progress = {"stage": "running", "stage_index": 0, "stage_total": 0, "current_page_index": None}
    return {
        "queue_state": state,
        "queue_position": waiting_position,
        "jobs_ahead": max(0, jobs_ahead),
        "waiting_jobs": len(_BATCH_WAITING_ORDER),
        "active": active_progress,
    }


def _job_cancel_requested(run_id: str, job_id: str) -> bool:
    try:
        return bool(get_video_run_store().load_job(run_id=run_id, job_id=job_id).get("cancel_requested"))
    except Exception:
        return True


async def _run_persistent_batch_job(run_id: str, job_id: str) -> None:
    global _BATCH_ACTIVE_JOB
    store = get_video_run_store()
    try:
        if _job_cancel_requested(run_id, job_id):
            raise asyncio.CancelledError
        async with _BATCH_GPU_QUEUE_LOCK:
            _unregister_batch_waiter(run_id, job_id)
            _BATCH_ACTIVE_JOB = (str(run_id), str(job_id))
            job = store.load_job(run_id=run_id, job_id=job_id)
            payload = job.get("payload") or {}
            snapshot = job.get("input_snapshot") or {}
            if "scripts" not in snapshot:
                raise ValueError("舊工作缺少輸入快照，請使用目前設定建立新的渲染工作。")
            pages = [{"script": text} for text in snapshot["scripts"]]
            reference_path = Path(str(snapshot.get("reference_audio") or ""))
            reference_bytes = await asyncio.to_thread(reference_path.read_bytes) if reference_path.is_file() else None
            requested = payload.get("page_indexes") or []
            page_indexes = []
            for raw_index in requested:
                try:
                    index = int(raw_index)
                except (TypeError, ValueError):
                    continue
                if 0 <= index < len(pages) and str(pages[index].get("script") or "").strip() and index not in page_indexes:
                    page_indexes.append(index)
            if not page_indexes:
                raise ValueError("沒有具備講稿的可渲染頁面")

            store.update_job(
                run_id=run_id, job_id=job_id,
                updates={
                    "status": "running", "stage": "tts", "stage_index": 0,
                    "stage_total": len(page_indexes), "current_page_index": None, "error": "",
                },
            )

            # Stage 1: all TTS. Completed page artifacts are reused on resume.
            for order, page_index in enumerate(page_indexes, start=1):
                if _job_cancel_requested(run_id, job_id):
                    raise asyncio.CancelledError
                state = (store.load_job(run_id=run_id, job_id=job_id).get("pages") or {}).get(str(page_index), {})
                variant_id = str(state.get("variant_id") or "")
                try:
                    if variant_id:
                        store.get_variant_audio_path(run_id=run_id, page_index=page_index, variant_id=variant_id)
                    else:
                        raise FileNotFoundError
                except (FileNotFoundError, IndexError):
                    if not reference_bytes:
                        raise ValueError("上次工作的參考音檔不存在，請重新建立渲染工作。")
                    state = {}  # Missing audio invalidates every downstream stage.
                    response = await video_run_page_tts_endpoint(
                        run_id=run_id,
                        page_index=page_index,
                        text=str(pages[page_index].get("script") or ""),
                        voice=str(payload.get("tts_voice") or ""),
                        speed=float(payload.get("tts_speed") or 1.0),
                        reference_audio=UploadFile(file=io.BytesIO(reference_bytes), filename=reference_path.name),
                        reference_text=str(payload.get("reference_text") or ""),
                        selected_voice_key=str(payload.get("selected_voice_key") or ""),
                        response_mode="json",
                    )
                    data = json.loads(bytes(response.body).decode("utf-8"))
                    variant_id = str(data.get("variant_id") or "")
                store.update_job(
                    run_id=run_id, job_id=job_id,
                    updates={
                        "stage_index": order,
                        "stage_total": len(page_indexes),
                        "current_page_index": page_index,
                        "pages": {str(page_index): {
                            **state, "variant_id": variant_id,
                            "status": state.get("status") if state.get("status") in {"align_ready", "rendered"} else "tts_ready",
                            "stage_index": order, "stage_total": len(page_indexes),
                        }},
                    },
                )

            try:
                from backend.app.services.voxtts import release_voxtts_worker
                release_voxtts_worker()
            except Exception:
                pass

            subtitle_mode = str(payload.get("subtitle_mode") or "burn").lower()
            if subtitle_mode != "none":
                store.update_job(
                    run_id=run_id,
                    job_id=job_id,
                    updates={"stage": "alignment", "stage_index": 0, "stage_total": len(page_indexes), "current_page_index": None},
                )
                for order, page_index in enumerate(page_indexes, start=1):
                    if _job_cancel_requested(run_id, job_id):
                        raise asyncio.CancelledError
                    state = (store.load_job(run_id=run_id, job_id=job_id).get("pages") or {}).get(str(page_index), {})
                    variant_id = str(state.get("variant_id") or "")
                    variant = store.get_page_variant(run_id=run_id, page_index=page_index, variant_id=variant_id)
                    segments_path = Path(str((variant.get("paths") or {}).get("segments") or ""))
                    if segments_path.is_file() and state.get("status") in {"align_ready", "rendered"}:
                        segments = (json.loads(segments_path.read_text(encoding="utf-8")).get("segments") or [])
                        align_backend = str(((variant.get("alignment") or {}).get("metadata") or {}).get("backend") or "")
                        warning = str(((variant.get("alignment") or {}).get("metadata") or {}).get("warning") or "")
                    else:
                        state = {**state, "status": "tts_ready"}
                        response = await video_run_page_align_endpoint(
                            run_id=run_id,
                            page_index=page_index,
                            text=str(pages[page_index].get("script") or ""),
                            language="auto",
                            alignment_mode="auto",
                            split_min_chars=int(payload.get("split_min_chars") or 10),
                            split_max_chars=int(payload.get("split_max_chars") or 32),
                            enable_pause_split=False,
                            pause_threshold_ms=320,
                            tts_id=variant_id,
                            variant_id=variant_id,
                        )
                        data = json.loads(bytes(response.body).decode("utf-8"))
                        segments = data.get("segments") or []
                        align_backend = str(data.get("backend") or "")
                        warning = str(data.get("warning") or "")
                    store.update_job(
                        run_id=run_id, job_id=job_id,
                        updates={
                            "stage_index": order,
                            "stage_total": len(page_indexes),
                            "current_page_index": page_index,
                            "pages": {str(page_index): {
                                **state, "variant_id": variant_id,
                                "status": "rendered" if state.get("status") == "rendered" else "align_ready",
                                "align_backend": align_backend, "warning": warning,
                                "stage_index": order, "stage_total": len(page_indexes),
                            }},
                        },
                    )
            try:
                from backend.app.services.subtitle_alignment import release_alignment_worker
                release_alignment_worker()
            except Exception:
                pass

            store.update_job(
                run_id=run_id,
                job_id=job_id,
                updates={"stage": "render", "stage_index": 0, "stage_total": len(page_indexes), "current_page_index": None},
            )
            style = payload.get("subtitle_settings") or {}
            for order, page_index in enumerate(page_indexes, start=1):
                if _job_cancel_requested(run_id, job_id):
                    raise asyncio.CancelledError
                state = (store.load_job(run_id=run_id, job_id=job_id).get("pages") or {}).get(str(page_index), {})
                variant_id = str(state.get("variant_id") or "")
                try:
                    if state.get("status") == "rendered":
                        store.get_variant_video_path(run_id=run_id, page_index=page_index, variant_id=variant_id)
                        store.update_job(
                            run_id=run_id,
                            job_id=job_id,
                            updates={
                                "stage_index": order,
                                "stage_total": len(page_indexes),
                                "current_page_index": page_index,
                            },
                        )
                        continue
                except (FileNotFoundError, IndexError):
                    pass
                variant = store.get_page_variant(run_id=run_id, page_index=page_index, variant_id=variant_id)
                segments = []
                if subtitle_mode != "none":
                    segments_path = Path(str((variant.get("paths") or {}).get("segments") or ""))
                    if segments_path.is_file():
                        segments = json.loads(segments_path.read_text(encoding="utf-8")).get("segments") or []
                await render_subtitle_ass_video(
                    segments_json=json.dumps(segments, ensure_ascii=False),
                    subtitle_style="bg-dark",
                    subtitle_mode=subtitle_mode,
                    enable_highlight=bool(style.get("enable_highlight", False)),
                    font_size=int(style.get("font_size") or 52),
                    enable_background=bool(style.get("enable_background", True)),
                    bg_color=str(style.get("bg_color") or "#000000"),
                    bg_opacity=int(style.get("bg_opacity") or 55),
                    margin_v=int(style.get("margin_v") or 90),
                    align_backend=str(state.get("align_backend") or ""),
                    run_id=run_id,
                    page_index=page_index,
                    variant_label=f"batch-page-{page_index + 1}",
                    tts_id=variant_id,
                    align_id=variant_id if subtitle_mode != "none" else "",
                    variant_id=variant_id,
                    tts_voice=str(payload.get("tts_voice") or ""),
                    tts_speed=str(payload.get("tts_speed") or 1.0),
                    selected_voice_key=str(payload.get("selected_voice_key") or ""),
                    reference_text=str(payload.get("reference_text") or ""),
                )
                store.update_job(
                    run_id=run_id, job_id=job_id,
                    updates={
                        "stage_index": order,
                        "stage_total": len(page_indexes),
                        "current_page_index": page_index,
                        "pages": {str(page_index): {
                            **state, "variant_id": variant_id, "status": "rendered",
                            "stage_index": order, "stage_total": len(page_indexes),
                        }},
                    },
                )

            if _job_cancel_requested(run_id, job_id):
                raise asyncio.CancelledError
            result: dict = {}
            if bool(payload.get("auto_merge")):
                store.update_job(
                    run_id=run_id,
                    job_id=job_id,
                    updates={
                        "stage": "merge", "stage_index": 0, "stage_total": 1,
                        "current_page_index": None,
                    },
                )
                latest_job = store.load_job(run_id=run_id, job_id=job_id)
                variant_ids = {
                    str(page_index): str(
                        ((latest_job.get("pages") or {}).get(str(page_index), {}) or {}).get("variant_id") or ""
                    )
                    for page_index in page_indexes
                }
                from backend.app.api.video_runs import merge_selected_video_run_variants

                merge_response = await merge_selected_video_run_variants(
                    run_id=run_id,
                    page_indexes_json=json.dumps(page_indexes),
                    variant_ids_json=json.dumps(variant_ids),
                    response_mode="json",
                    transitions_enabled=bool(payload.get("transitions_enabled")),
                )
                merge_data = json.loads(bytes(merge_response.body).decode("utf-8"))
                export_id = str(merge_data.get("export_variant_id") or "")
                if not export_id:
                    raise RuntimeError("自動合併完成但沒有 export_variant_id")
                result = {
                    "export_variant_id": export_id,
                    "video_url": f"/api/video-runs/{run_id}/exports/{export_id}/video",
                    "srt_url": (
                        f"/api/video-runs/{run_id}/exports/{export_id}/subtitles.srt"
                        if subtitle_mode != "none" else ""
                    ),
                    "bundle_url": f"/api/video-runs/{run_id}/exports/{export_id}/download.zip",
                }

            store.update_job(
                run_id=run_id, job_id=job_id,
                updates={
                    "status": "completed", "stage": "completed", "stage_index": len(page_indexes),
                    "stage_total": len(page_indexes), "current_page_index": None,
                    "cancel_requested": False, "result": result,
                },
            )
    except asyncio.CancelledError:
        cancelled = _job_cancel_requested(run_id, job_id)
        status = "cancelled" if cancelled else "interrupted"
        store.update_job(
            run_id=run_id, job_id=job_id,
            updates={"status": status, "stage": status, "cancel_requested": cancelled},
        )
    except Exception as exc:
        logger.error("[BatchJob] run=%s job=%s failed: %s", run_id, job_id, exc, exc_info=True)
        store.update_job(
            run_id=run_id, job_id=job_id,
            updates={"status": "failed", "stage": "failed", "error": str(exc)[:1000]},
        )
    finally:
        _unregister_batch_waiter(run_id, job_id)
        if _BATCH_ACTIVE_JOB == (str(run_id), str(job_id)):
            _BATCH_ACTIVE_JOB = None
        _BATCH_JOB_TASKS.pop(job_id, None)


def _start_batch_job_task(run_id: str, job_id: str) -> None:
    existing = _BATCH_JOB_TASKS.get(job_id)
    if existing and not existing.done():
        return
    _register_batch_waiter(run_id, job_id)
    _BATCH_JOB_TASKS[job_id] = asyncio.create_task(_run_persistent_batch_job(run_id, job_id))


def recover_persistent_batch_jobs() -> int:
    """Mark orphaned work for an explicit one-time recovery decision in the UI."""
    store = get_video_run_store()
    jobs = store.list_all_jobs(statuses={"queued", "running"})
    for job in jobs:
        recoverable = "scripts" in (job.get("input_snapshot") or {})
        cancelled = bool(job.get("cancel_requested"))
        status = "cancelled" if cancelled else ("interrupted" if recoverable else "failed")
        store.update_job(
            run_id=job["run_id"], job_id=job["job_id"],
            updates={"status": status, "stage": status,
                     "error": "" if recoverable or cancelled else "舊工作缺少輸入快照，請重新建立渲染工作。"},
        )
    return len(jobs)


@router.post("/api/video-runs/{run_id}/jobs/render")
async def create_batch_render_job(run_id: str, request: BatchRenderJobRequest):
    store = get_video_run_store()
    try:
        for existing in store.list_jobs(run_id=run_id):
            if existing.get("status") in {"queued", "running"}:
                _start_batch_job_task(run_id, str(existing["job_id"]))
                return JSONResponse(existing, status_code=202)
        # Starting a new request supersedes unaccepted recovery prompts.
        for existing in store.list_jobs(run_id=run_id):
            if existing.get("status") == "interrupted":
                store.update_job(run_id=run_id, job_id=existing["job_id"], updates={
                    "status": "cancelled", "stage": "cancelled", "cancel_requested": True,
                })
        job = store.create_job(run_id=run_id, payload=request.model_dump())
        _start_batch_job_task(run_id, str(job["job_id"]))
        return JSONResponse(job, status_code=202)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")


@router.get("/api/video-runs/{run_id}/jobs/{job_id}")
async def get_batch_render_job(run_id: str, job_id: str):
    try:
        job = get_video_run_store().load_job(run_id=run_id, job_id=job_id)
        return JSONResponse({**job, "queue": _batch_queue_metadata(run_id, job_id)})
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")


@router.get("/api/agent/video-jobs/{run_id}/{job_id}")
async def get_agent_video_job(run_id: str, job_id: str):
    """Agent-oriented job status with stable artifact URLs on completion."""
    try:
        job = get_video_run_store().load_job(run_id=run_id, job_id=job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Agent video job not found")
    result = dict(job.get("result") or {})
    return JSONResponse({
        "run_id": run_id,
        "job_id": job_id,
        "status": job.get("status"),
        "stage": job.get("stage"),
        "stage_index": job.get("stage_index", 0),
        "stage_total": job.get("stage_total", 0),
        "current_page_index": job.get("current_page_index"),
        "queue": _batch_queue_metadata(run_id, job_id),
        "error": job.get("error", ""),
        "result": result,
    })


@router.get("/api/video-runs/{run_id}/jobs-current")
async def get_current_batch_render_job(run_id: str):
    """Return this run's active/queued job so a refreshed UI can reattach."""
    try:
        jobs = get_video_run_store().list_jobs(run_id=run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")
    for job in jobs:
        if job.get("status") in {"queued", "running", "interrupted"}:
            job_id = str(job.get("job_id") or "")
            return JSONResponse({**job, "queue": _batch_queue_metadata(run_id, job_id)})
    return Response(status_code=204)


@router.post("/api/video-runs/{run_id}/jobs/{job_id}/cancel")
async def cancel_batch_render_job(run_id: str, job_id: str):
    try:
        item = (str(run_id), str(job_id))
        task = _BATCH_JOB_TASKS.get(job_id)
        if item != _BATCH_ACTIVE_JOB:
            _unregister_batch_waiter(run_id, job_id)
            job = get_video_run_store().update_job(
                run_id=run_id,
                job_id=job_id,
                updates={"status": "cancelled", "stage": "cancelled", "cancel_requested": True},
            )
            if task and not task.done():
                task.cancel()
            _BATCH_JOB_TASKS.pop(job_id, None)
        else:
            # Active work stops safely at the next page boundary, avoiding a
            # half-written TTS/alignment/video artifact.
            job = get_video_run_store().update_job(
                run_id=run_id, job_id=job_id, updates={"cancel_requested": True},
            )
        return JSONResponse(job, status_code=202)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")


@router.post("/api/video-runs/{run_id}/jobs/{job_id}/resume")
async def resume_batch_render_job(run_id: str, job_id: str):
    store = get_video_run_store()
    try:
        # No await before task registration: decisions serialize on this event loop.
        job = store.load_job(run_id=run_id, job_id=job_id)
        if job.get("status") in {"queued", "running"}:
            return JSONResponse(job, status_code=202)
        if job.get("status") != "interrupted":
            raise HTTPException(status_code=409, detail="只有意外中斷的工作可以恢復；請建立新的渲染工作。")
        if "scripts" not in (job.get("input_snapshot") or {}):
            raise HTTPException(status_code=409, detail="舊工作缺少輸入快照，請重新建立渲染工作。")
        job = store.update_job(
            run_id=run_id, job_id=job_id,
            updates={"status": "queued", "stage": "queued", "cancel_requested": False, "error": ""},
        )
        _start_batch_job_task(run_id, job_id)
        return JSONResponse(job, status_code=202)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found")
