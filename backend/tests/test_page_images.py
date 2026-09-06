import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from PIL import Image

from backend.app.services.artifact_store import VideoRunStore
from backend.app.services import page_images
from backend.app.api import video_runs


@pytest.fixture
def sample(tmp_path, monkeypatch):
    pdf = tmp_path / 'input.pdf'
    pdf.write_bytes(b'%PDF-1.4')
    store = VideoRunStore(tmp_path / 'runs')
    run_id = store.create_run(pdf_path=pdf, scripts=['keep script'])['run_id']
    raster = Mock(side_effect=lambda *a, **kw: [Image.new('RGB', (1920, 1080), 'white')])
    monkeypatch.setattr(page_images, 'convert_from_path', raster)
    return store, run_id, raster


def test_upgrade_and_reuse_pair(sample):
    store, run_id, raster = sample
    store.record_page_asset(run_id=run_id, page_index=0, slide_bytes=b'legacy')
    before = store.load_manifest(run_id)
    pair = page_images.ensure_page_images(store, run_id, 0)
    for key, dimensions in [('slide', (1920, 1080)), ('thumbnail', (320, 180))]:
        with Image.open(pair[key]) as image:
            assert image.size == dimensions
    stamps = {k: p.stat().st_mtime_ns for k, p in pair.items()}
    assert page_images.ensure_page_images(store, run_id, 0) == pair
    assert stamps == {k: p.stat().st_mtime_ns for k, p in pair.items()}
    raster.assert_called_once()
    assert store.load_manifest(run_id)['pages'][0]['script'] == before['pages'][0]['script']
    assert len(list(store.page_dir(run_id, 0).iterdir())) == 2


def test_concurrent_requests_render_once(sample):
    store, run_id, raster = sample
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: page_images.ensure_page_images(store, run_id, 0), range(4)))
    assert all(result == results[0] for result in results)
    raster.assert_called_once()


def test_missing_pdf_preserves_existing_image(sample):
    store, run_id, raster = sample
    page = store.record_page_asset(run_id=run_id, page_index=0, slide_bytes=b'legacy')
    Path(store.load_manifest(run_id)['paths']['pdf']).unlink()
    with pytest.raises(FileNotFoundError):
        page_images.ensure_page_images(store, run_id, 0)
    assert Path(page['paths']['slide']).read_bytes() == b'legacy'
    raster.assert_not_called()


def test_endpoints_serve_distinct_sizes(sample, monkeypatch):
    store, run_id, _ = sample
    monkeypatch.setattr(video_runs, 'get_video_run_store', lambda: store)
    small = asyncio.run(video_runs.video_run_thumbnail(run_id, 1))
    big = asyncio.run(video_runs.get_video_run_page_image(run_id, 0))
    assert small.path != big.path
    assert small.headers['cache-control'] == 'no-cache'
    with Image.open(small.path) as image:
        assert image.size == (320, 180)


def test_portrait_is_not_stretched(sample):
    store, run_id, raster = sample
    raster.side_effect = lambda *a, **kw: [Image.new('RGB', (960, 1920))]
    pair = page_images.ensure_page_images(store, run_id, 0)
    with Image.open(pair['slide']) as image:
        assert image.size == (540, 1080)


def test_browser_revalidation_avoids_resending_unchanged_image(sample, monkeypatch):
    from types import SimpleNamespace
    store, run_id, _ = sample
    monkeypatch.setattr(video_runs, 'get_video_run_store', lambda: store)
    first = asyncio.run(video_runs.get_video_run_page_image(run_id, 0))
    request = SimpleNamespace(headers={'if-none-match': first.headers['etag']})
    cached = asyncio.run(video_runs.get_video_run_page_image(run_id, 0, request))
    assert cached.status_code == 304
    assert cached.body == b''
