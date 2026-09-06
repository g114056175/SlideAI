"""One persistent 1080p image and one thumbnail per slide, shared by all consumers."""
import io
import os
import threading
from pathlib import Path

from PIL import Image
from pdf2image import convert_from_path

IMAGE_VERSION = 1
_PAGE_LOCKS = [threading.Lock() for _ in range(64)]
_RENDER_SLOTS = threading.BoundedSemaphore(2)


def ensure_page_images(store, run_id, page_index):
    # Bounded lock storage; unrelated collisions only serialize rendering.
    with _PAGE_LOCKS[hash((str(store.root), run_id, page_index)) % len(_PAGE_LOCKS)]:
        manifest = store.load_manifest(run_id)
        pages = manifest.get('pages') or []
        if not 0 <= page_index < len(pages):
            raise IndexError('Page not found')
        page = pages[page_index]
        paths = page.get('paths') or {}
        if page.get('image_version') == IMAGE_VERSION and all(
            paths.get(key) and Path(paths[key]).is_file() for key in ('slide', 'thumbnail')
        ):
            return {key: Path(paths[key]) for key in ('slide', 'thumbnail')}
        pdf = (manifest.get('paths') or {}).get('pdf')
        if not pdf or not Path(pdf).is_file():
            raise FileNotFoundError('Run PDF not found; existing images were preserved')
        with _RENDER_SLOTS:
            images = convert_from_path(pdf, first_page=page_index + 1, last_page=page_index + 1,
                                       size=1920, thread_count=1, timeout=120,
                                       poppler_path=os.getenv('POPPLER_PATH') or None)
            if len(images) != 1:
                raise ValueError('PDF page image count mismatch')
            try:
                large = images[0].convert('RGB')
                large.thumbnail((1920, 1080), Image.Resampling.LANCZOS)
                big = io.BytesIO()
                large.save(big, format='JPEG', quality=90)
                small = large.copy()
                small.thumbnail((320, 180), Image.Resampling.LANCZOS)
                thumb = io.BytesIO()
                small.save(thumb, format='JPEG', quality=80)
                large.close()
                small.close()
            finally:
                for image in images:
                    image.close()
        page = store.record_page_images(run_id=run_id, page_index=page_index,
                                        large=big.getvalue(), small=thumb.getvalue(), version=IMAGE_VERSION)
        return {key: Path(page['paths'][key]) for key in ('slide', 'thumbnail')}


def ensure_run_images(store, run_id):
    for index in range(len(store.load_manifest(run_id).get('pages') or [])):
        ensure_page_images(store, run_id, index)
