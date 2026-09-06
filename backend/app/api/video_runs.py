from backend.app.services.upload_limits import read_upload_limited
import os
import logging
import json
import asyncio
import tempfile
import shutil
import zipfile
from datetime import datetime
from typing import Optional, List
import re
from urllib.parse import quote

import pypdf as PyPDF2
from fastapi import APIRouter, UploadFile, File, Form, Request, Query, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field

from backend.app.api.video_helpers import is_truthy_env
from backend.app.services.artifact_store import get_video_run_store
from backend.app.services.alignment.subtitle_builder import build_srt
from backend.app.services.video_merge import INSERTED_TRANSITION_STRATEGY, merge_video_files

logger = logging.getLogger("video_abstract")
router = APIRouter()


@router.get("/api/llm/status")
async def get_llm_status():
    """Return non-secret LLM readiness metadata for the script editor."""
    from backend.app.services.utility.api import get_llm_config_summary
    config = get_llm_config_summary()
    return JSONResponse({
        "configured": bool(config.get("configured")),
        "provider": str(config.get("provider") or "missing"),
        "model": str(config.get("model") or ""),
    })


def _safe_download_filename(value: str, fallback: str = "video", suffix: str = ".mp4") -> str:
    name = str(value or "").strip()
    name = re.sub(r"\.[Pp][Dd][Ff]$", "", name)
    name = re.sub(r'[\\/:*?"<>|]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip() or fallback
    if not name.lower().endswith(suffix):
        name += suffix
    return name


def _content_disposition_attachment(filename: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", filename).strip() or "video.mp4"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


def _download_bundle(entries: list[tuple[str, str]], filename: str) -> FileResponse:
    """Create a temporary ZIP without recompressing already-compressed videos."""
    temp_dir = tempfile.mkdtemp(prefix="slideai_download_")
    zip_path = os.path.join(temp_dir, "download.zip")
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED) as archive:
            for source_path, archive_name in entries:
                if source_path and os.path.isfile(source_path):
                    archive.write(source_path, arcname=archive_name)
        if not os.path.isfile(zip_path) or os.path.getsize(zip_path) == 0:
            raise RuntimeError("下載壓縮檔建立失敗")
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=filename,
            background=BackgroundTask(shutil.rmtree, temp_dir, ignore_errors=True),
        )
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


async def _media_duration_seconds(path: str) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
    try:
        return max(0.0, float((stdout or b"").decode().strip()))
    except (TypeError, ValueError):
        return 0.0


class LocalPdfRunRequest(BaseModel):
    pdf_path: str
    subtitle_source: str = "none"
    run_label: str = ""
    settings: dict = Field(default_factory=dict)
    scripts: list[str] = Field(default_factory=list)


class VideoRunUpdateRequest(BaseModel):
    display_name: Optional[str] = None


class VideoRunScriptsUpdateRequest(BaseModel):
    scripts: List[str]


class VideoRunGenerateScriptsRequest(BaseModel):
    pages: Optional[List[int]] = None
    scope: str = "all"  # all | current
    source: str = "pdf"  # reserved: keep explicit source in API contract
    language: str = "zh"
    overwrite: bool = True


def _normalize_script_text(raw: str) -> str:
    text = str(raw or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _strip_any_page_tags(text)
    lines = [ln.strip() for ln in text.split("\n")]
    compact = []
    blank = False
    for ln in lines:
        if not ln:
            if not blank:
                compact.append("")
            blank = True
            continue
        compact.append(ln)
        blank = False
    out = "\n".join(compact).strip()
    out = out.replace("\n\n\n", "\n\n")
    return out


def _trim_redundant_opening(page_idx: int, raw: str) -> str:
    """Keep greeting on page 1 only; remove repeated opening greetings on later pages."""
    text = str(raw or "").strip()
    if page_idx <= 0 or not text:
        return text
    patterns = [
        r"^大家好[，,。]?\s*",
        r"^各位好[，,。]?\s*",
        r"^今天(?:我(?:們)?|要)?(?:想)?(?:跟|和)?各位(?:分享|介紹)[^。！？!?\n]*[。！？!?\n]\s*",
        r"^今天(?:我(?:們)?|要)?(?:想)?(?:跟|和)?大家(?:分享|介紹)[^。！？!?\n]*[。！？!?\n]\s*",
    ]
    out = text
    for p in patterns:
        out = re.sub(p, "", out, flags=re.IGNORECASE)
    return out.strip() or text


def _is_outline_page(text: str) -> bool:
    src = str(text or "").lower()
    if not src:
        return False
    hints = [
        "目錄", "大綱", "章節", "contents", "table of contents", "agenda", "outline",
    ]
    return any(h in src for h in hints)


def _looks_incomplete(text: str) -> bool:
    s = str(text or "").strip()
    if not s:
        return True
    if s.endswith(("：", ":", "、", "，", ",")):
        return True
    if len(s) < 18:
        return True
    return False




def _strip_any_page_tags(raw: str) -> str:
    s = str(raw or "")
    s = re.sub(r"(?im)^\s*#?\s*PAGE[_\-\s]*\d+\s*#?\s*$", "", s)
    s = re.sub(r"(?im)^\s*#?\s*(?:END[_\-\s]*PAGE|ENDPAGE)[_\-\s]*\d+\s*#?\s*$", "", s)
    return s.strip()




@router.get("/api/video-runs")
async def list_video_runs(limit: int = Query(50, ge=1, le=200)):
    """List persistent PDF-to-video run records."""
    return JSONResponse({"runs": get_video_run_store().list_runs(limit=limit)})


@router.get("/api/video-runs/{run_id}/pdf")
async def video_run_pdf(run_id: str):
    """Download the original PDF for a persistent run."""
    try:
        manifest = get_video_run_store().load_manifest(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")

    pdf_path = ((manifest.get("paths") or {}).get("pdf") or "").strip()
    if not pdf_path or not os.path.isfile(pdf_path):
        raise HTTPException(status_code=404, detail="Run PDF not found")
    filename = str(manifest.get("original_filename") or os.path.basename(pdf_path) or "source.pdf")
    return FileResponse(pdf_path, media_type="application/pdf", filename=filename)


async def _page_image_response(run_id, page_index, kind, request=None):
    from backend.app.services.page_images import ensure_page_images
    try:
        paths = await asyncio.to_thread(ensure_page_images, get_video_run_store(), run_id, page_index)
        response = FileResponse(str(paths[kind]), media_type="image/jpeg",
                                stat_result=paths[kind].stat(), headers={"Cache-Control": "no-cache"})
        if request is not None:
            tags = request.headers.get("if-none-match", "").split(",")
            if any(tag.strip().removeprefix("W/") in {"*", response.headers["etag"]} for tag in tags):
                return Response(status_code=304, headers={
                    "ETag": response.headers["etag"], "Cache-Control": "no-cache",
                })
        return response
    except (FileNotFoundError, IndexError) as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/api/video-runs/{run_id}/thumbnail")
async def video_run_thumbnail(run_id: str, page: int = Query(1, ge=1), request: Request = None):
    return await _page_image_response(run_id, page - 1, "thumbnail", request)


@router.get("/api/video-runs/{run_id}/pages/{page_index}/image")
async def get_video_run_page_image(run_id: str, page_index: int, request: Request = None):
    return await _page_image_response(run_id, page_index, "slide", request)


@router.get("/api/video-runs/{run_id}")
async def get_video_run(run_id: str):
    """Return a persistent run manifest."""
    try:
        return JSONResponse(get_video_run_store().load_manifest(run_id))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")


@router.patch("/api/video-runs/{run_id}")
async def update_video_run(run_id: str, req: VideoRunUpdateRequest):
    """Update editable run metadata, currently the sidebar display name."""
    try:
        if req.display_name is None:
            raise ValueError("display_name is required")
        return JSONResponse(get_video_run_store().rename_run(run_id, req.display_name))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/api/video-runs/{run_id}/scripts")
async def update_video_run_scripts(run_id: str, req: VideoRunScriptsUpdateRequest):
    """Persist editable page scripts for a video run."""
    try:
        return JSONResponse(get_video_run_store().update_page_scripts(run_id, req.scripts))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")


@router.post("/api/video-runs/{run_id}/scripts/generate")
async def generate_video_run_scripts(run_id: str, req: VideoRunGenerateScriptsRequest):
    """Generate scripts from individual PDF page images, including scanned PDFs."""
    try:
        manifest = get_video_run_store().load_manifest(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")

    pdf_path = ((manifest.get("paths") or {}).get("pdf") or "").strip()
    if not pdf_path or not os.path.isfile(pdf_path):
        raise HTTPException(status_code=404, detail="Run PDF not found")

    from backend.app.services.utility.api import get_llm_api_key, llm_is_configured
    api_key = get_llm_api_key()
    if not llm_is_configured():
        raise HTTPException(status_code=400, detail="LLM provider, model or endpoint is not configured in backend/.env")

    pages = manifest.get("pages") or []
    page_count = len(pages)
    scope = str(req.scope or "all").strip().lower()
    if scope == "current":
        requested = req.pages if isinstance(req.pages, list) and req.pages else []
    else:
        requested = list(range(page_count))
    requested = sorted({int(i) for i in requested if 0 <= int(i) < page_count})
    if not requested:
        raise HTTPException(status_code=400, detail="No valid pages requested")

    try:
        from backend.app.services.utility.api import (
            generate_presentation_scripts_from_images, get_configured_llm_provider,
        )
        from backend.app.services.page_images import ensure_page_images
        store = get_video_run_store()
        image_paths = []
        for index in requested:
            paths = await asyncio.to_thread(ensure_page_images, store, run_id, index)
            image_paths.append(str(paths["slide"]))
        generated = await generate_presentation_scripts_from_images(
            image_paths=image_paths, api_key=api_key, language=req.language,
        )
        generated_map = {
            index: _trim_redundant_opening(index, _normalize_script_text(text))
            for index, text in zip(requested, generated)
        }
        if len(generated) != len(requested) or any(not text for text in generated_map.values()):
            raise ValueError("模型未回傳完整講稿；原講稿已保留。")
        store = get_video_run_store()
        # Preserve edits to other pages made while the model request was running.
        latest = store.load_manifest(run_id)
        scripts = [str(page.get("script") or "") for page in latest.get("pages") or []]
        for index in requested:
            if scripts[index] != str(pages[index].get("script") or ""):
                raise HTTPException(status_code=409, detail="生成期間講稿已被修改，請重新生成以避免覆蓋。")
            scripts[index] = generated_map[index]
        updated = store.update_page_scripts(run_id, scripts)
        return JSONResponse({
            "run": updated, "scripts": scripts, "updated_pages": requested,
            "skipped_empty_pages": [],
            "text_stats": {"requested_pages": len(requested), "non_empty_pages": len(requested)},
            "source": "slide-image", "provider": get_configured_llm_provider(api_key), "scope": scope,
        })
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"[VideoRun] script generation failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"LLM 講稿生成失敗: {exc}")


@router.patch("/api/video-runs/{run_id}/settings")
async def update_video_run_settings(
    run_id: str,
    settings_json: str = Form("{}"),
    reference_audio: Optional[UploadFile] = File(None),
):
    """Persist current editable voice/subtitle settings for a run."""
    try:
        import json
        try:
            settings = json.loads(settings_json or "{}")
        except Exception:
            raise ValueError("settings_json must be valid JSON")
        if not isinstance(settings, dict):
            raise ValueError("settings_json must be a JSON object")
        audio_bytes = await read_upload_limited(reference_audio) if reference_audio is not None else None
        audio_name = reference_audio.filename if reference_audio is not None else "reference.wav"
        return JSONResponse(get_video_run_store().update_settings(
            run_id,
            settings,
            reference_audio=audio_bytes,
            reference_audio_name=audio_name or "reference.wav",
        ))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/api/video-runs/{run_id}/reference-audio")
async def get_video_run_reference_audio(run_id: str):
    """Return the custom reference voice audio saved with this run."""
    try:
        manifest = get_video_run_store().load_manifest(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")
    ref = ((manifest.get("settings") or {}).get("current") or {}).get("reference_audio") or {}
    path = str(ref.get("path") or "")
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Reference audio not found")
    return FileResponse(path, media_type="audio/wav", filename=str(ref.get("filename") or "reference.wav"))


@router.delete("/api/video-runs/{run_id}")
async def delete_video_run(run_id: str):
    """Delete a persistent run and all artifacts under data/video_runs/<run_id>."""
    try:
        get_video_run_store().delete_run(run_id)
        return JSONResponse({"ok": True, "run_id": run_id})
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")


@router.get("/api/video-runs/{run_id}/pages/{page_index}/variants/{variant_id}/video")
async def get_video_run_variant_video(run_id: str, page_index: int, variant_id: str):
    """Return one persisted page-variant MP4."""
    try:
        path = get_video_run_store().get_variant_video_path(
            run_id=run_id,
            page_index=page_index,
            variant_id=variant_id,
        )
        return FileResponse(str(path), media_type="video/mp4", filename=f"{run_id}_page_{page_index + 1}_{variant_id}.mp4")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Variant video not found")
    except IndexError:
        raise HTTPException(status_code=404, detail="Page not found")


@router.get("/api/video-runs/{run_id}/pages/{page_index}/variants/{variant_id}/audio")
async def get_video_run_variant_audio(run_id: str, page_index: int, variant_id: str):
    """Stream one persisted TTS result without routing it through browser uploads."""
    try:
        path = get_video_run_store().get_variant_audio_path(
            run_id=run_id, page_index=page_index, variant_id=variant_id,
        )
        return FileResponse(
            str(path), media_type="audio/wav",
            filename=f"{run_id}_page_{page_index + 1}_{variant_id}.wav",
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Variant audio not found")
    except IndexError:
        raise HTTPException(status_code=404, detail="Page not found")


@router.get("/api/video-runs/{run_id}/pages/{page_index}/variants/{variant_id}/subtitles.srt")
async def get_video_run_variant_srt(run_id: str, page_index: int, variant_id: str):
    """Download the persisted sidecar subtitle timeline for a page variant."""
    try:
        path = get_video_run_store().get_variant_srt_path(
            run_id=run_id, page_index=page_index, variant_id=variant_id,
        )
        return FileResponse(
            str(path), media_type="application/x-subrip; charset=utf-8",
            filename=f"{run_id}_page_{page_index + 1}_{variant_id}.srt",
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Variant SRT not found")
    except IndexError:
        raise HTTPException(status_code=404, detail="Page not found")


@router.get("/api/video-runs/{run_id}/pages/{page_index}/variants/{variant_id}/download.zip")
async def get_video_run_variant_bundle(run_id: str, page_index: int, variant_id: str):
    """Download a page video and its optional SRT as one ZIP archive."""
    store = get_video_run_store()
    try:
        video_path = str(store.get_variant_video_path(
            run_id=run_id, page_index=page_index, variant_id=variant_id,
        ))
        entries = [(video_path, f"page_{page_index + 1}.mp4")]
        try:
            srt_path = str(store.get_variant_srt_path(
                run_id=run_id, page_index=page_index, variant_id=variant_id,
            ))
            entries.append((srt_path, f"page_{page_index + 1}.srt"))
        except FileNotFoundError:
            pass
        return await asyncio.to_thread(_download_bundle, entries, f"{run_id}_page_{page_index + 1}_{variant_id}.zip")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Variant video not found")
    except IndexError:
        raise HTTPException(status_code=404, detail="Page not found")


@router.post("/api/video-runs/{run_id}/pages/{page_index}/variants/{variant_id}/select")
async def select_video_run_variant(run_id: str, page_index: int, variant_id: str):
    """Select one persisted variant as the main variant for merge/export."""
    try:
        page = get_video_run_store().select_page_variant(
            run_id=run_id,
            page_index=page_index,
            variant_id=variant_id,
        )
        return JSONResponse(page)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Variant not found")
    except IndexError:
        raise HTTPException(status_code=404, detail="Page not found")


@router.delete("/api/video-runs/{run_id}/pages/{page_index}/variants/{variant_id}")
async def delete_video_run_variant(run_id: str, page_index: int, variant_id: str):
    """Delete one persisted page variant."""
    try:
        page = get_video_run_store().delete_page_variant(
            run_id=run_id,
            page_index=page_index,
            variant_id=variant_id,
        )
        return JSONResponse(page)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Variant not found")
    except IndexError:
        raise HTTPException(status_code=404, detail="Page not found")


@router.post("/api/video-runs/{run_id}/exports/merge-selected")
async def merge_selected_video_run_variants(
    run_id: str,
    page_indexes_json: str = Form("[]"),
    variant_ids_json: str = Form("{}"),
    response_mode: str = Form("json"),
    transitions_enabled: bool = Form(False),
):
    """Merge persisted page-variant MP4s and save the merged export variant.

    The frontend sends the currently rendered page indexes and a page-index to
    variant-id map.  If a page has no explicit entry, use the page's selected
    variant from the manifest.
    """
    store = get_video_run_store()
    try:
        manifest = store.load_manifest(run_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Run not found")
    download_filename = _safe_download_filename(
        manifest.get("display_name") or manifest.get("original_filename") or run_id,
        fallback=run_id,
    )

    try:
        page_indexes_raw = json.loads(page_indexes_json or "[]")
        variant_ids_raw = json.loads(variant_ids_json or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="合併參數格式錯誤")

    pages = manifest.get("pages") or []
    page_indexes: list[int] = []
    if isinstance(page_indexes_raw, list):
        for value in page_indexes_raw:
            try:
                idx = int(value)
            except Exception:
                continue
            if 0 <= idx < len(pages) and idx not in page_indexes:
                page_indexes.append(idx)
    if not page_indexes:
        page_indexes = [i for i, page in enumerate(pages) if page.get("selected_variant_id")]

    variant_ids = variant_ids_raw if isinstance(variant_ids_raw, dict) else {}
    wants_video_response = str(response_mode or "").strip().lower() in {"video", "blob", "download", "mp4"}
    input_paths: list[str] = []
    source_pages: list[int] = []
    source_variants: dict[str, str] = {}
    source_segment_paths: list[str] = []
    transition_images: list[str] = []
    missing: list[str] = []

    for idx in page_indexes:
        page = pages[idx] if 0 <= idx < len(pages) else {}
        explicit = variant_ids.get(str(idx), variant_ids.get(idx, ""))
        variant_id = str(explicit or page.get("selected_variant_id") or "").strip()
        if not variant_id:
            missing.append(f"第 {idx + 1} 頁未選擇影片變體")
            continue
        try:
            path = store.get_variant_video_path(run_id=run_id, page_index=idx, variant_id=variant_id)
        except FileNotFoundError:
            missing.append(f"第 {idx + 1} 頁找不到已渲染影片 ({variant_id})")
            continue
        input_paths.append(str(path))
        source_pages.append(idx)
        source_variants[str(idx)] = variant_id
        variant = next((v for v in (page.get("variants") or []) if v.get("variant_id") == variant_id), {})
        source_segment_paths.append(str((variant.get("paths") or {}).get("segments") or ""))
        transition_images.append(str((page.get("paths") or {}).get("slide") or ""))

    if not input_paths:
        detail = "沒有可合併的已渲染影片"
        if missing:
            detail += "：" + "；".join(missing[:5])
        raise HTTPException(status_code=400, detail=detail)

    can_use_inserted_transitions = (
        bool(transitions_enabled)
        and len(transition_images) == len(input_paths)
        and all(path and os.path.isfile(path) for path in transition_images)
    )
    expected_transition_strategy = (
        INSERTED_TRANSITION_STRATEGY
        if can_use_inserted_transitions
        else "full-reencode-v1"
    )
    exports = manifest.setdefault("exports", {})
    for old in exports.get("variants") or []:
        old_settings = old.get("settings") or {}
        old_video = (old.get("paths") or {}).get("video") or ""
        if (
            old.get("source_pages") == source_pages
            and old_settings.get("source_variants") == source_variants
            and bool((old_settings.get("transitions") or {}).get("enabled")) == bool(transitions_enabled)
            and (
                not transitions_enabled
                or (old_settings.get("transitions") or {}).get("strategy") == expected_transition_strategy
            )
            and old_video
            and os.path.isfile(old_video)
        ):
            store.select_export_variant(run_id=run_id, variant_id=old.get("variant_id"))
            if wants_video_response:
                return FileResponse(
                    old_video,
                    media_type="video/mp4",
                    headers={
                        "Content-Disposition": _content_disposition_attachment(download_filename),
                        "X-Export-Variant-Id": old.get("variant_id", ""),
                        "X-Export-Reused": "true",
                    },
                )
            return JSONResponse(
                {
                    "ok": True,
                    "reused": True,
                    "export_variant_id": old.get("variant_id"),
                    "variant": old,
                },
                headers={"X-Export-Variant-Id": old.get("variant_id", "")},
            )

    merge_temp_dir = tempfile.mkdtemp(prefix="slideai_run_merge_")
    try:
        merged_path, transition_metadata = await merge_video_files(
            input_paths,
            merge_temp_dir,
            transitions_enabled=transitions_enabled,
            transition_images=transition_images if can_use_inserted_transitions else None,
        )
    except (RuntimeError, ValueError) as exc:
        import shutil
        shutil.rmtree(merge_temp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc))
    merged_segments: list[dict] = []
    offset = 0.0
    inserted_duration = float(transition_metadata.get("duration_seconds") or 0.0)
    uses_inserted_transitions = transition_metadata.get("strategy") == INSERTED_TRANSITION_STRATEGY
    for position, (video_path, segment_path) in enumerate(zip(input_paths, source_segment_paths)):
        if segment_path and os.path.isfile(segment_path):
            try:
                payload = json.loads(open(segment_path, encoding="utf-8").read())
                for segment in payload.get("segments") or []:
                    item = dict(segment)
                    item["start"] = float(item.get("start") or 0) + offset
                    item["end"] = float(item.get("end") or 0) + offset
                    item["words"] = [
                        {**dict(word), "start": float(word.get("start") or 0) + offset, "end": float(word.get("end") or 0) + offset}
                        for word in (item.get("words") or [])
                    ]
                    merged_segments.append(item)
            except Exception as exc:
                logger.warning("Cannot include page SRT in merged export: %s", exc)
        durations = transition_metadata.get("input_durations") or []
        offset += float(durations[position]) if position < len(durations) else await _media_duration_seconds(video_path)
        if uses_inserted_transitions and position < len(input_paths) - 1:
            offset += inserted_duration
    merged_srt = build_srt(merged_segments) if merged_segments else ""
    try:
        variant = store.record_export_variant(
            run_id=run_id,
            video_source_path=merged_path,
            source_pages=source_pages,
            settings={
                "source_variants": source_variants,
                "requested_page_indexes": page_indexes,
                "missing": missing,
                "transitions": transition_metadata,
            },
            label=f"merged-{len(source_pages)}-pages" + ("-transitions" if transitions_enabled else ""),
            srt_content=merged_srt,
        )
    finally:
        import shutil
        shutil.rmtree(merge_temp_dir, ignore_errors=True)

    if wants_video_response:
        return FileResponse(
            str((variant.get("paths") or {}).get("video") or ""),
            media_type="video/mp4",
            headers={
                "Content-Disposition": _content_disposition_attachment(download_filename),
                "X-Export-Variant-Id": variant.get("variant_id", ""),
                "X-Export-Reused": "false",
            },
        )

    return JSONResponse(
        {
            "ok": True,
            "reused": False,
            "export_variant_id": variant.get("variant_id", ""),
            "variant": variant,
        },
        headers={"X-Export-Variant-Id": variant.get("variant_id", "")},
    )


@router.get("/api/video-runs/{run_id}/exports/{variant_id}/video")
async def get_video_run_export_video(run_id: str, variant_id: str):
    """Return one persisted merged/export MP4."""
    try:
        store = get_video_run_store()
        manifest = store.load_manifest(run_id)
        path = store.get_export_video_path(run_id=run_id, variant_id=variant_id)
        filename = _safe_download_filename(
            manifest.get("display_name") or manifest.get("original_filename") or run_id,
            fallback=run_id,
        )
        return FileResponse(str(path), media_type="video/mp4", filename=filename)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Export video not found")


@router.get("/api/video-runs/{run_id}/exports/{variant_id}/subtitles.srt")
async def get_video_run_export_srt(run_id: str, variant_id: str):
    try:
        path = get_video_run_store().get_export_srt_path(run_id=run_id, variant_id=variant_id)
        return FileResponse(str(path), media_type="application/x-subrip; charset=utf-8", filename=f"{run_id}_{variant_id}.srt")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Export SRT not found")


@router.get("/api/video-runs/{run_id}/exports/{variant_id}/download.zip")
async def get_video_run_export_bundle(run_id: str, variant_id: str):
    """Download a merged video and its optional SRT as one ZIP archive."""
    store = get_video_run_store()
    try:
        manifest = store.load_manifest(run_id)
        video_path = str(store.get_export_video_path(run_id=run_id, variant_id=variant_id))
        base_name = _safe_download_filename(
            manifest.get("display_name") or manifest.get("original_filename") or run_id,
            fallback=run_id,
        )
        base_name = re.sub(r"\.mp4$", "", base_name, flags=re.IGNORECASE)
        entries = [(video_path, f"{base_name}.mp4")]
        try:
            srt_path = str(store.get_export_srt_path(run_id=run_id, variant_id=variant_id))
            entries.append((srt_path, f"{base_name}.srt"))
        except FileNotFoundError:
            pass
        return await asyncio.to_thread(_download_bundle, entries, f"{base_name}.zip")
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Export video not found")


@router.post("/api/video-runs/{run_id}/exports/{variant_id}/select")
async def select_video_run_export(run_id: str, variant_id: str):
    """Select one persisted merged/export variant."""
    try:
        return JSONResponse(get_video_run_store().select_export_variant(run_id=run_id, variant_id=variant_id))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Export variant not found")


@router.delete("/api/video-runs/{run_id}/exports/{variant_id}")
async def delete_video_run_export(run_id: str, variant_id: str):
    """Delete one persisted merged/export variant."""
    try:
        return JSONResponse(get_video_run_store().delete_export_variant(run_id=run_id, variant_id=variant_id))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Export variant not found")


@router.post("/api/video-runs/local-pdf")
async def create_video_run_from_local_pdf(req: LocalPdfRunRequest, request: Request):
    """Create a persistent run record from a server-local PDF path.

    This endpoint is for backend-only batch preparation. It does not require
    opening the web UI and does not render pages yet.
    """
    client_host = (request.client.host if request.client else "") or ""
    allow_remote = is_truthy_env("SLIDEAI_ALLOW_LOCAL_PDF_API", "false")
    if not allow_remote and client_host not in {"127.0.0.1", "::1", "localhost"}:
        raise HTTPException(status_code=403, detail="local-pdf endpoint is restricted to localhost")

    pdf_path = os.path.abspath(os.path.expanduser(req.pdf_path))
    if not os.path.isfile(pdf_path):
        raise HTTPException(status_code=404, detail=f"PDF not found: {pdf_path}")

    try:
        with open(pdf_path, "rb") as pdf_file:
            page_count = len(PyPDF2.PdfReader(pdf_file).pages)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"PDF read failed: {exc}")

    scripts = list(req.scripts or [])
    if not scripts:
        scripts = ["" for _ in range(page_count)]
    elif len(scripts) < page_count:
        scripts.extend(["" for _ in range(page_count - len(scripts))])
    elif len(scripts) > page_count:
        scripts = scripts[:page_count]

    try:
        manifest = get_video_run_store().create_run(
            pdf_path=pdf_path,
            original_filename=os.path.basename(pdf_path),
            scripts=scripts,
            settings={
                **(req.settings or {}),
                "batch_request": {
                    "subtitle_source": req.subtitle_source,
                    "run_label": req.run_label,
                },
            },
            source="api-local-pdf",
        )
        return JSONResponse(manifest)
    except Exception as exc:
        logger.error(f"[VideoRun] create from local PDF failed: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Create run failed: {exc}")
