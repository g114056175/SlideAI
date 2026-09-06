from __future__ import annotations

import json
import os
import shutil
import uuid
import hashlib
import fcntl
import inspect
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _safe_name(value: str, fallback: str = "untitled") -> str:
    raw = str(value or fallback).strip() or fallback
    keep = []
    for ch in raw:
        if ch.isalnum() or ch in {"-", "_", "."}:
            keep.append(ch)
        elif ch.isspace():
            keep.append("_")
    name = "".join(keep).strip("._")
    return (name or fallback)[:96]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def get_video_runs_root() -> Path:
    configured = os.getenv("SLIDEAI_VIDEO_RUNS_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (_repo_root() / "data" / "video_runs").resolve()


def _locked_run_mutation(method):
    """Serialize one complete read/modify/write operation for a video run."""
    signature = inspect.signature(method)

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        bound = signature.bind(self, *args, **kwargs)
        run_id = str(bound.arguments.get("run_id") or "").strip()
        if not run_id:
            return method(self, *args, **kwargs)
        with self.run_lock(run_id):
            return method(self, *args, **kwargs)

    return wrapped


class VideoRunStore:
    """Filesystem-backed artifact store for PDF-to-video runs.

    This is intentionally simple: one directory per run, JSON manifest files,
    and page/variant subfolders. It gives us persistent inspection without
    introducing database coupling before the workflow stabilizes.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or get_video_runs_root()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks_root = self.root / ".locks"
        self._locks_root.mkdir(parents=True, exist_ok=True)
        self._thread_locks_guard = threading.Lock()
        self._thread_locks: Dict[str, threading.RLock] = {}

    @contextmanager
    def run_lock(self, run_id: str):
        """Lock a run across both threads and backend processes on Linux."""
        safe_run_id = _safe_name(run_id, "run")
        with self._thread_locks_guard:
            thread_lock = self._thread_locks.setdefault(safe_run_id, threading.RLock())
        with thread_lock:
            lock_path = self._locks_root / f"{safe_run_id}.lock"
            with lock_path.open("a+b") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def new_run_id(self) -> str:
        return f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"

    def safe_filename(self, value: str, fallback: str = "source.pdf") -> str:
        return _safe_name(value, fallback)

    def run_dir(self, run_id: str) -> Path:
        return self.root / _safe_name(run_id, "run")

    def manifest_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "manifest.json"

    def create_run(
        self,
        *,
        pdf_path: str | Path,
        original_filename: str = "source.pdf",
        scripts: Optional[Iterable[str]] = None,
        settings: Optional[Dict[str, Any]] = None,
        pdf_id: Optional[str] = None,
        project_id: Optional[int] = None,
        source: str = "web",
        run_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        src = Path(pdf_path).expanduser().resolve()
        if not src.is_file():
            raise FileNotFoundError(f"PDF not found: {src}")
        run_id = run_id or self.new_run_id()
        rdir = self.run_dir(run_id)
        if (rdir / "manifest.json").exists():
            raise FileExistsError(f"run already exists: {run_id}")
        input_dir = rdir / "input"
        pages_dir = rdir / "pages"
        input_dir.mkdir(parents=True, exist_ok=True)
        pages_dir.mkdir(parents=True, exist_ok=True)

        pdf_name = _safe_name(original_filename or src.name, "source.pdf")
        if not pdf_name.lower().endswith(".pdf"):
            pdf_name += ".pdf"
        stored_pdf = input_dir / pdf_name
        if src != stored_pdf.resolve():
            shutil.copy2(src, stored_pdf)

        script_list = list(scripts or [])
        manifest: Dict[str, Any] = {
            "run_id": run_id,
            "status": "created",
            "source": source,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "pdf_id": pdf_id,
            "project_id": project_id,
            "original_filename": original_filename or src.name,
            "display_name": original_filename or src.name,
            "paths": {
                "root": str(rdir),
                "pdf": str(stored_pdf),
                "pages": str(pages_dir),
            },
            "settings": settings or {},
            "pages": [
                {
                    "page_index": i,
                    "page_number": i + 1,
                    "script": text,
                    "variants": [],
                    "selected_variant_id": None,
                }
                for i, text in enumerate(script_list)
            ],
        }
        self._write_json(self.manifest_path(run_id), manifest)
        self._write_json(rdir / "scripts.json", {"scripts": script_list})
        return manifest

    def load_manifest(self, run_id: str) -> Dict[str, Any]:
        path = self.manifest_path(run_id)
        if not path.is_file():
            raise FileNotFoundError(f"run not found: {run_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def save_manifest(self, manifest: Dict[str, Any]) -> None:
        manifest["updated_at"] = _utc_now()
        self._write_json(self.manifest_path(str(manifest["run_id"])), manifest)

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for path in sorted(self.root.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                out.append(self._summary(data))
            except Exception:
                continue
            if len(out) >= limit:
                break
        return out


    @_locked_run_mutation
    def delete_run(self, run_id: str) -> None:
        rdir = self.run_dir(run_id)
        if not rdir.is_dir() or not (rdir / "manifest.json").is_file():
            raise FileNotFoundError(f"run not found: {run_id}")
        shutil.rmtree(rdir)

    @_locked_run_mutation
    def rename_run(self, run_id: str, display_name: str) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        name = str(display_name or "").strip()
        if not name:
            raise ValueError("display_name is required")
        manifest["display_name"] = name[:120]
        self.save_manifest(manifest)
        return manifest

    @_locked_run_mutation
    def update_page_scripts(self, run_id: str, scripts: Iterable[str]) -> Dict[str, Any]:
        """Persist the editable page scripts in the run manifest.

        The web editor treats manifest.pages[].script as the source of truth
        when reopening a project, so edits must be written here instead of only
        living in Vue state.
        """
        manifest = self.load_manifest(run_id)
        script_list = [str(s or "") for s in (scripts or [])]
        pages = manifest.setdefault("pages", [])
        while len(pages) < len(script_list):
            i = len(pages)
            pages.append({
                "page_index": i,
                "page_number": i + 1,
                "script": "",
                "variants": [],
                "selected_variant_id": None,
            })
        for i, text in enumerate(script_list):
            pages[i]["script"] = text
        self.save_manifest(manifest)
        self._write_json(self.run_dir(run_id) / "scripts.json", {"scripts": [p.get("script", "") for p in pages]})
        return manifest

    @_locked_run_mutation
    def update_settings(
        self,
        run_id: str,
        settings: Dict[str, Any],
        reference_audio: bytes | None = None,
        reference_audio_name: str = "reference.wav",
    ) -> Dict[str, Any]:
        """Persist current editable UI settings for reopening/API reuse."""
        manifest = self.load_manifest(run_id)
        merged = dict(manifest.get("settings") or {})
        current = dict(merged.get("current") or {})
        current.update(settings or {})

        if reference_audio is not None:
            input_dir = self.run_dir(run_id) / "input"
            input_dir.mkdir(parents=True, exist_ok=True)
            safe_name = _safe_name(reference_audio_name or "reference.wav", "reference.wav")
            if "." not in safe_name:
                safe_name += ".wav"
            ref_path = input_dir / f"reference_audio{Path(safe_name).suffix or '.wav'}"
            ref_path.write_bytes(reference_audio)
            current["reference_audio"] = {
                "path": str(ref_path),
                "filename": safe_name,
            }
        elif current.get("selected_voice_key") == "custom" and current.get("has_reference_audio") is False:
            old_ref = current.pop("reference_audio", {}) or {}
            old_path = Path(str(old_ref.get("path") or ""))
            try:
                if old_path.is_file() and self.run_dir(run_id).resolve() in old_path.resolve().parents:
                    old_path.unlink()
            except Exception:
                pass

        merged["current"] = current
        manifest["settings"] = merged
        self.save_manifest(manifest)
        return manifest

    def page_dir(self, run_id: str, page_index: int) -> Path:
        return self.run_dir(run_id) / "pages" / f"page_{page_index + 1:03d}"

    @_locked_run_mutation
    def record_page_images(self, *, run_id, page_index, large, small, version):
        manifest = self.load_manifest(run_id)
        page = manifest["pages"][page_index]
        pdir = self.page_dir(run_id, page_index)
        pdir.mkdir(parents=True, exist_ok=True)
        for key, name, content in (
            ("slide", f"page_{page_index + 1:03d}.jpg", large),
            ("thumbnail", "thumbnail.jpg", small),
        ):
            target = pdir / name
            with tempfile.NamedTemporaryFile(dir=pdir, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(content)
            try:
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            page.setdefault("paths", {})[key] = str(target)
        page["image_version"] = version
        self.save_manifest(manifest)
        return page

    @_locked_run_mutation
    def record_page_asset(
        self,
        *,
        run_id: str,
        page_index: int,
        slide_bytes: bytes | None = None,
        suffix: str = ".jpg",
    ) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        pdir = self.page_dir(run_id, page_index)
        pdir.mkdir(parents=True, exist_ok=True)
        page = self._ensure_page(manifest, page_index)
        if slide_bytes is not None:
            ext = suffix if suffix.startswith(".") else f".{suffix}"
            if ext.lower() not in {".jpg", ".jpeg", ".png"}:
                ext = ".jpg"
            slide_path = pdir / f"page_{page_index + 1:03d}{ext}"
            slide_path.write_bytes(slide_bytes)
            page.setdefault("paths", {})["slide"] = str(slide_path)
        else:
            jpg_path = pdir / f"page_{page_index + 1:03d}.jpg"
            png_path = pdir / f"page_{page_index + 1:03d}.png"
            if jpg_path.is_file():
                page.setdefault("paths", {})["slide"] = str(jpg_path)
            elif png_path.is_file():
                page.setdefault("paths", {})["slide"] = str(png_path)
        self.save_manifest(manifest)
        return page

    @_locked_run_mutation
    def record_page_tts(
        self,
        *,
        run_id: str,
        page_index: int,
        audio_bytes: bytes,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        page = self._ensure_page(manifest, page_index)
        digest = hashlib.sha1(audio_bytes + json.dumps(metadata or {}, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:10]
        tts_id = f"tts-{len(page.get('tts', [])) + 1:03d}-{digest}"
        tdir = self.page_dir(run_id, page_index) / "tts" / tts_id
        tdir.mkdir(parents=True, exist_ok=True)
        audio_path = tdir / "audio.wav"
        audio_path.write_bytes(audio_bytes)
        item = {
            "tts_id": tts_id,
            "created_at": _utc_now(),
            "status": "ready",
            "paths": {"audio": str(audio_path)},
            "metadata": metadata or {},
        }
        self._write_json(tdir / "tts.json", item)
        page.setdefault("tts", []).append(item)
        page["selected_tts_id"] = tts_id
        manifest["status"] = "rendering"
        self.save_manifest(manifest)
        return item

    @_locked_run_mutation
    def record_page_alignment(
        self,
        *,
        run_id: str,
        page_index: int,
        segments: List[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        page = self._ensure_page(manifest, page_index)
        digest = hashlib.sha1(json.dumps(segments or [], sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:10]
        align_id = f"align-{len(page.get('alignments', [])) + 1:03d}-{digest}"
        adir = self.page_dir(run_id, page_index) / "align" / align_id
        adir.mkdir(parents=True, exist_ok=True)
        seg_path = adir / "segments.json"
        self._write_json(seg_path, {"segments": segments or []})
        item = {
            "align_id": align_id,
            "created_at": _utc_now(),
            "status": "ready",
            "paths": {"segments": str(seg_path)},
            "metadata": metadata or {},
        }
        self._write_json(adir / "alignment.json", item)
        page.setdefault("alignments", []).append(item)
        page["selected_align_id"] = align_id
        manifest["status"] = "rendering"
        self.save_manifest(manifest)
        return item

    def _ensure_page(self, manifest: Dict[str, Any], page_index: int) -> Dict[str, Any]:
        while len(manifest.get("pages", [])) <= page_index:
            i = len(manifest["pages"])
            manifest["pages"].append({
                "page_index": i,
                "page_number": i + 1,
                "script": "",
                "variants": [],
                "selected_variant_id": None,
            })
        return manifest["pages"][page_index]

    def _variant_dir(self, run_id: str, page_index: int, variant_id: str) -> Path:
        return self.page_dir(run_id, page_index) / "variants" / _safe_name(variant_id, "variant")

    def _find_variant(self, page: Dict[str, Any], variant_id: str) -> Optional[Dict[str, Any]]:
        for variant in page.get("variants") or []:
            if variant.get("variant_id") == variant_id:
                return variant
        return None

    def _create_page_variant_record(
        self,
        page: Dict[str, Any],
        *,
        label: str = "",
        settings: Optional[Dict[str, Any]] = None,
        status: str = "created",
    ) -> Dict[str, Any]:
        variant_id = f"v{len(page.get('variants', [])) + 1:03d}-{uuid.uuid4().hex[:6]}"
        variant = {
            "variant_id": variant_id,
            "label": label,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "status": status,
            "paths": {},
            "settings": settings or {},
        }
        page.setdefault("variants", []).append(variant)
        page["selected_variant_id"] = variant_id
        return variant

    @_locked_run_mutation
    def record_page_variant_tts(
        self,
        *,
        run_id: str,
        page_index: int,
        audio_bytes: bytes | None = None,
        audio_source_path: str | Path | None = None,
        metadata: Optional[Dict[str, Any]] = None,
        label: str = "",
    ) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        page = self._ensure_page(manifest, page_index)
        variant = self._create_page_variant_record(
            page,
            label=label or f"web-page-{page_index + 1}",
            settings={"tts": metadata or {}},
            status="tts_ready",
        )
        vdir = self._variant_dir(run_id, page_index, variant["variant_id"])
        vdir.mkdir(parents=True, exist_ok=True)
        audio_path = vdir / "audio.wav"
        if audio_source_path is not None:
            source_path = Path(audio_source_path).resolve()
            if not source_path.is_file():
                raise FileNotFoundError(f"generated audio not found: {source_path}")
            if source_path != audio_path.resolve():
                shutil.copy2(source_path, audio_path)
        elif audio_bytes is not None:
            audio_path.write_bytes(audio_bytes)
        else:
            raise ValueError("audio_bytes or audio_source_path is required")
        chunks_payload: Dict[str, Any] = {}
        if audio_source_path is not None:
            source_chunks_dir = Path(audio_source_path).with_suffix(".chunks")
            source_chunks_json = source_chunks_dir / "chunks.json"
            if source_chunks_json.is_file():
                chunks_dir = vdir / "chunks"
                shutil.copytree(source_chunks_dir, chunks_dir, dirs_exist_ok=True)
                try:
                    chunks_payload = json.loads((chunks_dir / "chunks.json").read_text(encoding="utf-8"))
                except Exception:
                    chunks_payload = {}
                for chunk in chunks_payload.get("chunks") or []:
                    filename = _safe_name(str(chunk.get("filename") or ""), "chunk.wav")
                    chunk["path"] = str(chunks_dir / filename)
        tts_json = {
            "variant_id": variant["variant_id"],
            "created_at": _utc_now(),
            "status": "ready",
            "metadata": metadata or {},
            "paths": {"audio": str(audio_path)},
            "chunks": chunks_payload.get("chunks") or [],
            "chunk_silence_ms": float(chunks_payload.get("silence_ms") or 0.0),
        }
        self._write_json(vdir / "tts.json", tts_json)
        variant.setdefault("paths", {})["audio"] = str(audio_path)
        if chunks_payload:
            variant.setdefault("paths", {})["chunks"] = str(vdir / "chunks")
        variant["tts"] = tts_json
        variant["updated_at"] = _utc_now()
        manifest["status"] = "rendering"
        self.save_manifest(manifest)
        self._write_json(vdir / "variant.json", variant)
        return variant

    @_locked_run_mutation
    def record_page_variant_alignment(
        self,
        *,
        run_id: str,
        page_index: int,
        variant_id: str,
        segments: List[Dict[str, Any]],
        srt: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        page = self._ensure_page(manifest, page_index)
        variant = self._find_variant(page, variant_id)
        if variant is None:
            raise FileNotFoundError(f"variant not found: {variant_id}")
        vdir = self._variant_dir(run_id, page_index, variant_id)
        vdir.mkdir(parents=True, exist_ok=True)
        seg_path = vdir / "segments.json"
        self._write_json(seg_path, {"segments": segments or []})
        srt_path = vdir / "subtitles.srt"
        srt_path.write_text(srt or "", encoding="utf-8")
        align_json = {
            "variant_id": variant_id,
            "created_at": _utc_now(),
            "status": "ready",
            "metadata": metadata or {},
            "paths": {"segments": str(seg_path), "srt": str(srt_path)},
        }
        self._write_json(vdir / "alignment.json", align_json)
        variant.setdefault("paths", {})["segments"] = str(seg_path)
        variant.setdefault("paths", {})["srt"] = str(srt_path)
        variant["alignment"] = align_json
        variant["status"] = "align_ready"
        variant["updated_at"] = _utc_now()
        manifest["status"] = "rendering"
        self.save_manifest(manifest)
        self._write_json(vdir / "variant.json", variant)
        return variant

    @_locked_run_mutation
    def record_page_variant(
        self,
        *,
        run_id: str,
        page_index: int,
        video_bytes: bytes | None = None,
        video_source_path: str | Path | None = None,
        audio_bytes: bytes | None = None,
        slide_bytes: bytes | None = None,
        segments: Optional[List[Dict[str, Any]]] = None,
        ass_content: str | None = None,
        settings: Optional[Dict[str, Any]] = None,
        label: str = "",
        variant_id: str = "",
    ) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        page = self._ensure_page(manifest, page_index)

        variant = self._find_variant(page, variant_id) if variant_id else None
        if variant is None:
            variant = self._create_page_variant_record(
                page,
                label=label,
                settings=settings or {},
                status="rendering",
            )
            variant_id = variant["variant_id"]
        vdir = self._variant_dir(run_id, page_index, variant_id)
        vdir.mkdir(parents=True, exist_ok=True)

        paths: Dict[str, str] = dict(variant.get("paths") or {})
        video_path = vdir / "video.mp4"
        if video_source_path is not None:
            source_path = Path(video_source_path).resolve()
            if not source_path.is_file():
                raise FileNotFoundError(f"rendered video not found: {source_path}")
            if source_path != video_path.resolve():
                shutil.copy2(source_path, video_path)
        elif video_bytes is not None:
            video_path.write_bytes(video_bytes)
        else:
            raise ValueError("video_bytes or video_source_path is required")
        paths["video"] = str(video_path)
        if audio_bytes is not None:
            audio_path = vdir / "audio.wav"
            audio_path.write_bytes(audio_bytes)
            paths["audio"] = str(audio_path)
        if slide_bytes is not None:
            page_paths = page.setdefault("paths", {})
            existing_slide = str(page_paths.get("slide") or "")
            if existing_slide and Path(existing_slide).is_file():
                slide_path = Path(existing_slide)
            else:
                # Keep one canonical page background image.  The run thumbnail
                # cache writes JPEG, so render artifacts should not create a
                # duplicate PNG beside it.
                slide_path = self.page_dir(run_id, page_index) / f"page_{page_index + 1:03d}.jpg"
                slide_path.write_bytes(slide_bytes)
            page.setdefault("paths", {})["slide"] = str(slide_path)
            paths["slide"] = str(slide_path)
        if ass_content is not None:
            ass_path = vdir / "subtitles.ass"
            ass_path.write_text(ass_content, encoding="utf-8")
            paths["ass"] = str(ass_path)
        if segments is not None:
            seg_path = vdir / "segments.json"
            self._write_json(seg_path, {"segments": segments})
            paths["segments"] = str(seg_path)

        variant["label"] = label or variant.get("label", "")
        variant["status"] = "rendered"
        variant["paths"] = paths
        variant["settings"] = settings or variant.get("settings") or {}
        variant["updated_at"] = _utc_now()
        page["selected_variant_id"] = variant_id
        manifest.setdefault("settings", {})["last_render"] = settings or {}
        manifest["status"] = "rendering" if any(not p.get("variants") for p in manifest["pages"]) else "rendered"
        self.save_manifest(manifest)
        self._write_json(vdir / "variant.json", variant)
        return variant

    @_locked_run_mutation
    def select_page_variant(self, *, run_id: str, page_index: int, variant_id: str) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        pages = manifest.get("pages") or []
        if page_index < 0 or page_index >= len(pages):
            raise IndexError("page index out of range")
        variants = pages[page_index].get("variants") or []
        if not any(v.get("variant_id") == variant_id for v in variants):
            raise FileNotFoundError(f"variant not found: {variant_id}")
        pages[page_index]["selected_variant_id"] = variant_id
        self.save_manifest(manifest)
        return pages[page_index]

    @_locked_run_mutation
    def delete_page_variant(self, *, run_id: str, page_index: int, variant_id: str) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        pages = manifest.get("pages") or []
        if page_index < 0 or page_index >= len(pages):
            raise IndexError("page index out of range")

        page = pages[page_index]
        variants = page.get("variants") or []
        kept = [v for v in variants if v.get("variant_id") != variant_id]
        if len(kept) == len(variants):
            raise FileNotFoundError(f"variant not found: {variant_id}")

        new_vdir = self.page_dir(run_id, page_index) / "variants" / _safe_name(variant_id, "variant")
        old_vdir = self.page_dir(run_id, page_index) / _safe_name(variant_id, "variant")
        shutil.rmtree(new_vdir, ignore_errors=True)
        shutil.rmtree(old_vdir, ignore_errors=True)
        page["variants"] = kept
        if page.get("selected_variant_id") == variant_id:
            page["selected_variant_id"] = kept[-1].get("variant_id") if kept else None
        manifest["status"] = "rendering" if any(not p.get("variants") for p in pages) else "rendered"
        self.save_manifest(manifest)
        return page

    def get_variant_video_path(self, *, run_id: str, page_index: int, variant_id: str) -> Path:
        manifest = self.load_manifest(run_id)
        pages = manifest.get("pages") or []
        if page_index < 0 or page_index >= len(pages):
            raise IndexError("page index out of range")
        for variant in pages[page_index].get("variants") or []:
            if variant.get("variant_id") == variant_id:
                video = ((variant.get("paths") or {}).get("video") or "").strip()
                if not video:
                    break
                path = Path(video)
                if path.is_file():
                    return path
                break
        raise FileNotFoundError(f"variant video not found: {variant_id}")

    def get_variant_audio_path(self, *, run_id: str, page_index: int, variant_id: str) -> Path:
        manifest = self.load_manifest(run_id)
        pages = manifest.get("pages") or []
        if page_index < 0 or page_index >= len(pages):
            raise IndexError("page index out of range")
        for variant in pages[page_index].get("variants") or []:
            if variant.get("variant_id") == variant_id:
                path = Path(str((variant.get("paths") or {}).get("audio") or ""))
                if path.is_file():
                    return path
                break
        raise FileNotFoundError(f"variant audio not found: {variant_id}")

    def get_page_variant(self, *, run_id: str, page_index: int, variant_id: str) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        pages = manifest.get("pages") or []
        if page_index < 0 or page_index >= len(pages):
            raise IndexError("page index out of range")
        variant = self._find_variant(pages[page_index], variant_id)
        if variant is None:
            raise FileNotFoundError(f"variant not found: {variant_id}")
        return variant

    def get_page_slide_path(self, *, run_id: str, page_index: int) -> Path:
        manifest = self.load_manifest(run_id)
        pages = manifest.get("pages") or []
        if page_index < 0 or page_index >= len(pages):
            raise IndexError("page index out of range")
        path = Path(str((pages[page_index].get("paths") or {}).get("slide") or ""))
        if path.is_file():
            return path
        raise FileNotFoundError(f"page slide not found: {page_index}")

    def get_variant_srt_path(self, *, run_id: str, page_index: int, variant_id: str) -> Path:
        manifest = self.load_manifest(run_id)
        pages = manifest.get("pages") or []
        if page_index < 0 or page_index >= len(pages):
            raise IndexError("page index out of range")
        for variant in pages[page_index].get("variants") or []:
            if variant.get("variant_id") == variant_id:
                path = Path(str((variant.get("paths") or {}).get("srt") or ""))
                if path.is_file():
                    return path
                break
        raise FileNotFoundError(f"variant SRT not found: {variant_id}")

    @_locked_run_mutation
    def record_export_variant(
        self,
        *,
        run_id: str,
        video_bytes: bytes | None = None,
        video_source_path: str | Path | None = None,
        source_pages: Optional[List[int]] = None,
        settings: Optional[Dict[str, Any]] = None,
        label: str = "",
        srt_content: str | None = None,
    ) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        exports = manifest.setdefault("exports", {})
        variants = exports.setdefault("variants", [])
        variant_id = f"export-v{len(variants) + 1:03d}-{uuid.uuid4().hex[:6]}"
        vdir = self.run_dir(run_id) / "exports" / variant_id
        vdir.mkdir(parents=True, exist_ok=True)

        video_path = vdir / "video.mp4"
        if video_source_path is not None:
            source_path = Path(video_source_path).resolve()
            if not source_path.is_file():
                raise FileNotFoundError(f"merged video not found: {source_path}")
            if source_path != video_path.resolve():
                shutil.copy2(source_path, video_path)
        elif video_bytes is not None:
            video_path.write_bytes(video_bytes)
        else:
            raise ValueError("video_bytes or video_source_path is required")
        paths = {"video": str(video_path)}
        if srt_content:
            srt_path = vdir / "subtitles.srt"
            srt_path.write_text(srt_content, encoding="utf-8")
            paths["srt"] = str(srt_path)
        variant = {
            "variant_id": variant_id,
            "label": label,
            "created_at": _utc_now(),
            "status": "rendered",
            "source_pages": source_pages or [],
            "paths": paths,
            "settings": settings or {},
        }
        variants.append(variant)
        exports["selected_variant_id"] = variant_id
        self.save_manifest(manifest)
        self._write_json(vdir / "variant.json", variant)
        return variant

    @_locked_run_mutation
    def select_export_variant(self, *, run_id: str, variant_id: str) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        exports = manifest.setdefault("exports", {})
        variants = exports.get("variants") or []
        if not any(v.get("variant_id") == variant_id for v in variants):
            raise FileNotFoundError(f"export variant not found: {variant_id}")
        exports["selected_variant_id"] = variant_id
        self.save_manifest(manifest)
        return exports

    @_locked_run_mutation
    def delete_export_variant(self, *, run_id: str, variant_id: str) -> Dict[str, Any]:
        manifest = self.load_manifest(run_id)
        exports = manifest.setdefault("exports", {})
        variants = exports.get("variants") or []
        kept = [v for v in variants if v.get("variant_id") != variant_id]
        if len(kept) == len(variants):
            raise FileNotFoundError(f"export variant not found: {variant_id}")
        vdir = self.run_dir(run_id) / "exports" / _safe_name(variant_id, "variant")
        shutil.rmtree(vdir, ignore_errors=True)
        exports["variants"] = kept
        if exports.get("selected_variant_id") == variant_id:
            exports["selected_variant_id"] = kept[-1].get("variant_id") if kept else None
        self.save_manifest(manifest)
        return exports

    def get_export_video_path(self, *, run_id: str, variant_id: str) -> Path:
        manifest = self.load_manifest(run_id)
        exports = manifest.get("exports") or {}
        for variant in exports.get("variants") or []:
            if variant.get("variant_id") == variant_id:
                video = ((variant.get("paths") or {}).get("video") or "").strip()
                if video:
                    path = Path(video)
                    if path.is_file():
                        return path
                break
        raise FileNotFoundError(f"export video not found: {variant_id}")

    def get_export_srt_path(self, *, run_id: str, variant_id: str) -> Path:
        manifest = self.load_manifest(run_id)
        exports = manifest.get("exports") or {}
        for variant in exports.get("variants") or []:
            if variant.get("variant_id") == variant_id:
                path = Path(str((variant.get("paths") or {}).get("srt") or ""))
                if path.is_file():
                    return path
                break
        raise FileNotFoundError(f"export SRT not found: {variant_id}")

    @_locked_run_mutation
    def create_job(self, *, run_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.manifest_path(run_id).is_file():
            raise FileNotFoundError(f"run not found: {run_id}")
        job_id = f"job-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        manifest = self.load_manifest(run_id)
        current = (manifest.get("settings") or {}).get("current") or {}
        payload = dict(payload)
        payload["selected_voice_key"] = payload.get("selected_voice_key") or current.get("selected_voice_key") or ""
        payload["reference_text"] = payload.get("reference_text") or current.get("reference_text") or ""
        # Job-owned inputs survive edits to the project while a job is queued
        # or interrupted. Never recover using a mixture of old audio/new text.
        snapshot = {"scripts": [str(page.get("script") or "") for page in manifest.get("pages") or []]}
        reference = current.get("reference_audio") or {}
        source = Path(str(reference.get("path") or ""))
        if not source.is_file():
            voices_dir = Path(__file__).resolve().parents[1] / "static" / "ref_voices"
            voices = json.loads((voices_dir / "manifest.json").read_text(encoding="utf-8"))
            voice = voices.get(payload["selected_voice_key"]) or {}
            source = voices_dir / Path(str(voice.get("file") or "")).name
            payload["reference_text"] = payload["reference_text"] or voice.get("transcript") or ""
        if source.is_file():
            target = self.run_dir(run_id) / "jobs" / job_id / ("reference" + source.suffix)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            snapshot["reference_audio"] = str(target)
        job = {
            "input_snapshot": snapshot,
            "job_id": job_id,
            "run_id": run_id,
            "status": "queued",
            "stage": "queued",
            "cancel_requested": False,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "payload": payload,
            "pages": {},
            "error": "",
        }
        self._write_json(self.run_dir(run_id) / "jobs" / f"{job_id}.json", job)
        return job

    def load_job(self, *, run_id: str, job_id: str) -> Dict[str, Any]:
        path = self.run_dir(run_id) / "jobs" / f"{_safe_name(job_id, 'job')}.json"
        if not path.is_file():
            raise FileNotFoundError(f"job not found: {job_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    @_locked_run_mutation
    def update_job(self, *, run_id: str, job_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        job = self.load_job(run_id=run_id, job_id=job_id)
        for key, value in (updates or {}).items():
            if key == "pages" and isinstance(value, dict):
                pages = dict(job.get("pages") or {})
                pages.update(value)
                job["pages"] = pages
            else:
                job[key] = value
        job["updated_at"] = _utc_now()
        self._write_json(self.run_dir(run_id) / "jobs" / f"{_safe_name(job_id, 'job')}.json", job)
        return job

    def list_jobs(self, *, run_id: str, limit: int = 20) -> List[Dict[str, Any]]:
        jobs_dir = self.run_dir(run_id) / "jobs"
        jobs = []
        for path in sorted(jobs_dir.glob("job-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                jobs.append(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if len(jobs) >= limit:
                break
        return jobs

    def list_all_jobs(
        self,
        *,
        statuses: Optional[Iterable[str]] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        """List jobs across runs in durable FIFO order.


        The backend is intentionally a single-process local service.  Job JSON
        files provide restart recovery without introducing a database solely
        for the GPU queue.
        """
        wanted = {str(value) for value in (statuses or []) if str(value)}
        rows: List[tuple[str, int, Dict[str, Any]]] = []
        for path in self.root.glob("*/jobs/job-*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if wanted and str(payload.get("status") or "") not in wanted:
                    continue
                rows.append((
                    str(payload.get("created_at") or ""),
                    path.stat().st_mtime_ns,
                    payload,
                ))
            except Exception:
                continue
        rows.sort(key=lambda row: (row[0], row[1], str(row[2].get("job_id") or "")))
        return [row[2] for row in rows[:max(1, int(limit))]]

    def _summary(self, manifest: Dict[str, Any]) -> Dict[str, Any]:
        pages = manifest.get("pages") or []
        rendered = sum(1 for p in pages if p.get("variants"))
        return {
            "run_id": manifest.get("run_id"),
            "status": manifest.get("status"),
            "source": manifest.get("source"),
            "created_at": manifest.get("created_at"),
            "updated_at": manifest.get("updated_at"),
            "original_filename": manifest.get("original_filename"),
            "display_name": manifest.get("display_name") or manifest.get("original_filename"),
            "pdf_id": manifest.get("pdf_id"),
            "project_id": manifest.get("project_id"),
            "page_count": len(pages),
            "rendered_pages": rendered,
            "root": (manifest.get("paths") or {}).get("root"),
        }

    @staticmethod
    def _write_json(path: Path, data: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as tmp:
                json.dump(data, tmp, ensure_ascii=False, indent=2)
                tmp.flush()
                os.fsync(tmp.fileno())
                tmp_path = Path(tmp.name)
            os.replace(tmp_path, path)
        finally:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)


_STORE: VideoRunStore | None = None


def get_video_run_store() -> VideoRunStore:
    global _STORE
    if _STORE is None:
        _STORE = VideoRunStore()
    return _STORE
