"""Exercise the real ASS renderer and FFmpeg, without loading TTS models."""
import asyncio
import io
import json
import shutil
import subprocess
import wave
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image
from pypdf import PdfWriter

from backend.app.api import video, video_runs
from backend.app.services.artifact_store import VideoRunStore
from backend.app.services.page_images import IMAGE_VERSION


@pytest.mark.skipif(not shutil.which('ffmpeg') or not shutil.which('ffprobe'), reason='FFmpeg integration requires ffmpeg/ffprobe')
@pytest.mark.parametrize('mode', ['burn', 'sidecar', 'none'])
def test_render_produces_video_and_persists_variant(tmp_path, monkeypatch, mode):
    source = tmp_path / 'slide.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=160, height=90)
    with source.open('wb') as handle:
        writer.write(handle)
    store = VideoRunStore(tmp_path / 'runs')
    run_id = store.create_run(pdf_path=source, scripts=['字幕測試'])['run_id']
    image = io.BytesIO()
    Image.new('RGB', (1920, 1080), 'white').save(image, format='JPEG')
    store.record_page_images(run_id=run_id, page_index=0, large=image.getvalue(), small=image.getvalue(), version=IMAGE_VERSION)
    audio = tmp_path / 'audio.wav'
    with wave.open(str(audio), 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b'\x00\x00' * 8000)
    variant = store.record_page_variant_tts(run_id=run_id, page_index=0, audio_source_path=audio)
    variant_id = variant['variant_id']
    segments = [{'start': 0, 'end': 0.5, 'text': '字幕測試'}]
    store.record_page_variant_alignment(run_id=run_id, page_index=0, variant_id=variant_id, segments=segments)
    monkeypatch.setattr(video, 'get_video_run_store', lambda: store)
    monkeypatch.setattr(video_runs, 'get_video_run_store', lambda: store)

    async def render():
        app = FastAPI()
        app.include_router(video.router)
        app.include_router(video_runs.router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/api/video-abstract/render-subtitle-ass-video', data={
                'run_id': run_id, 'page_index': '0', 'variant_id': variant_id,
                'subtitle_mode': mode, 'segments_json': json.dumps(segments),
            })
            assert response.status_code == 200, response.text[:1000]
            assert response.headers['x-variant-id'] == variant_id
            assert response.headers['content-type'] == 'video/mp4'
            result = await client.get(f'/api/video-runs/{run_id}')
            return result.json(), response.content

    manifest, content = asyncio.run(render())
    page = manifest['pages'][0]
    assert page['selected_variant_id'] == variant_id
    saved = next(item for item in page['variants'] if item['variant_id'] == variant_id)
    path = Path(saved['paths']['video'])
    assert path.read_bytes() == content
    assert path.stat().st_size > 0
    if mode == 'burn':
        assert '字幕測試' in Path(saved['paths']['ass']).read_text()
    probe = json.loads(subprocess.check_output([
        'ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(path),
    ], timeout=30))
    assert {'audio', 'video'} <= {stream['codec_type'] for stream in probe['streams']}
    stream = next(s for s in probe['streams'] if s['codec_type'] == 'video')
    assert (stream['width'], stream['height']) == (1920, 1080)
