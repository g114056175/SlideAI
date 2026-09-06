import asyncio
import io
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, HTTPException, UploadFile
from pypdf import PdfWriter

from backend.app.api import video, video_runs
from backend.app.services.artifact_store import VideoRunStore
from backend.app.services.upload_limits import read_upload_limited


def test_removed_legacy_endpoints_and_missing_run_are_rejected():
    async def check():
        app = FastAPI()
        app.include_router(video.router)
        app.include_router(video_runs.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            for method, path in [('GET', '/api/video-abstract/thumbnail'),
                                 ('POST', '/api/video-abstract/merge-rendered-videos'),
                                 ('POST', '/api/video-abstract/render-subtitle-video'),
                                 ('POST', '/api/video-abstract/subtitle-align')]:
                assert (await client.request(method, path)).status_code == 404
            response = await client.post('/api/video-abstract/render-subtitle-ass-video', data={'segments_json': '[]'})
            assert response.status_code == 422
    asyncio.run(check())


@pytest.mark.parametrize('basic', [False, True])
def test_upload_always_creates_run_without_calling_llm(tmp_path, monkeypatch, basic):
    store = VideoRunStore(tmp_path / 'runs')
    monkeypatch.setattr(video, 'get_video_run_store', lambda: store)
    monkeypatch.setattr(video, '_pregenerate_run_thumbnails_safe', lambda *args: None)
    monkeypatch.setattr(video, '_is_mock_mode', lambda: basic)
    monkeypatch.setattr(video, '_is_local_only_mode', lambda: False)
    from backend.app.services.utility import api
    llm = Mock(side_effect=AssertionError('upload must not invoke LLM'))
    monkeypatch.setattr(api, 'generate_presentation_scripts_from_images', llm)
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    data = io.BytesIO()
    writer.write(data)

    async def check():
        app = FastAPI()
        app.include_router(video.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/video-abstract', files={'file': ('slide.pdf', data.getvalue(), 'application/pdf')}, data={'subtitle_source': 'zh', 'skip_llm': 'false'})
            assert response.status_code == 200, response.text
            body = response.json()
            assert body['run_id']
            assert body['model_services_skipped'] is basic
            assert store.load_manifest(body['run_id'])['pages'][0]['script'] == ''
    asyncio.run(check())
    llm.assert_not_called()


def test_oversized_upload_rejected_before_reading_into_memory():
    upload = UploadFile(filename='audio.wav', file=io.BytesIO(b'12345'))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(read_upload_limited(upload, limit=4))
    assert exc.value.status_code == 413
    assert upload.file.tell() == 0
