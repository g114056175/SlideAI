"""Recovery contracts without loading speech models or invoking FFmpeg."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from backend.app.api import video
from backend.app.services.artifact_store import VideoRunStore


@pytest.fixture
def store(tmp_path, monkeypatch):
    pdf = tmp_path / 'input.pdf'
    pdf.write_bytes(b'%PDF-1.4\n%%EOF')
    store = VideoRunStore(tmp_path / 'runs')
    manifest = store.create_run(pdf_path=pdf, scripts=['原本講稿。'])
    store.test_run_id = manifest['run_id']
    monkeypatch.setattr(video, 'get_video_run_store', lambda: store)
    return store


def test_job_snapshots_scripts_and_reference_audio(store):
    run_id = store.test_run_id
    store.update_settings(run_id, {'selected_voice_key': 'custom'}, reference_audio=b'original-audio')
    job = store.create_job(run_id=run_id, payload={'page_indexes': [0]})
    store.update_page_scripts(run_id, ['後來修改的講稿。'])
    store.update_settings(run_id, {}, reference_audio=b'replacement-audio')
    saved = store.load_job(run_id=run_id, job_id=job['job_id'])
    assert saved['input_snapshot']['scripts'] == ['原本講稿。']
    assert Path(saved['input_snapshot']['reference_audio']).read_bytes() == b'original-audio'


def test_decline_is_persistent_and_cannot_resume(store, monkeypatch):
    job = store.create_job(run_id=store.test_run_id, payload={'page_indexes': [0]})
    start = AsyncMock()
    monkeypatch.setattr(video, '_start_batch_job_task', start)
    video.recover_persistent_batch_jobs()
    response = asyncio.run(video.get_current_batch_render_job(store.test_run_id))
    assert json.loads(response.body)['status'] == 'interrupted'
    asyncio.run(video.cancel_batch_render_job(store.test_run_id, job['job_id']))
    assert asyncio.run(video.get_current_batch_render_job(store.test_run_id)).status_code == 204
    with pytest.raises(HTTPException) as error:
        asyncio.run(video.resume_batch_render_job(store.test_run_id, job['job_id']))
    assert error.value.status_code == 409
    start.assert_not_called()
    assert video.recover_persistent_batch_jobs() == 0


@pytest.mark.parametrize('mode', ['none', 'srt', 'burn'])
def test_completed_stages_are_not_repeated_on_resume(store, monkeypatch, mode, tmp_path):
    run_id = store.test_run_id
    audio = tmp_path / 'audio.wav'
    audio.write_bytes(b'completed-audio')
    variant = store.record_page_variant_tts(run_id=run_id, page_index=0, audio_source_path=audio)
    variant_id = variant['variant_id']
    store.record_page_variant_alignment(run_id=run_id, page_index=0, variant_id=variant_id,
                                       segments=[{'start': 0, 'end': 1, 'text': '原本講稿。'}])
    store.record_page_variant(run_id=run_id, page_index=0, variant_id=variant_id, video_bytes=b'completed-video')
    job = store.create_job(run_id=run_id, payload={'page_indexes': [0], 'subtitle_mode': mode})
    store.update_job(run_id=run_id, job_id=job['job_id'], updates={
        'pages': {'0': {'variant_id': variant_id, 'status': 'rendered'}}})
    tts, align, render = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(video, 'video_run_page_tts_endpoint', tts)
    monkeypatch.setattr(video, 'video_run_page_align_endpoint', align)
    monkeypatch.setattr(video, 'render_subtitle_ass_video', render)
    monkeypatch.setattr(video, '_BATCH_GPU_QUEUE_LOCK', asyncio.Lock())
    asyncio.run(video._run_persistent_batch_job(run_id, job['job_id']))
    assert store.load_job(run_id=run_id, job_id=job['job_id'])['status'] == 'completed'
    tts.assert_not_awaited()
    align.assert_not_awaited()
    render.assert_not_awaited()


def test_legacy_job_without_snapshot_does_not_silently_use_new_inputs(store):
    job = store.create_job(run_id=store.test_run_id, payload={'page_indexes': [0]})
    store.update_job(run_id=store.test_run_id, job_id=job['job_id'], updates={'input_snapshot': {}})
    video.recover_persistent_batch_jobs()
    saved = store.load_job(run_id=store.test_run_id, job_id=job['job_id'])
    assert saved['status'] == 'failed'
    assert '快照' in saved['error']


@pytest.mark.parametrize('user_cancel', [False, True])
def test_shutdown_cancellation_remains_recoverable(store, monkeypatch, user_cancel):
    run_id = store.test_run_id
    job = store.create_job(run_id=run_id, payload={'page_indexes': [0]})

    async def scenario():
        lock = asyncio.Lock()
        await lock.acquire()
        monkeypatch.setattr(video, '_BATCH_GPU_QUEUE_LOCK', lock)
        task = asyncio.create_task(video._run_persistent_batch_job(run_id, job['job_id']))
        await asyncio.sleep(0)
        if user_cancel:
            store.update_job(run_id=run_id, job_id=job['job_id'], updates={'cancel_requested': True})
        task.cancel()
        await task
        lock.release()

    asyncio.run(scenario())
    saved = store.load_job(run_id=run_id, job_id=job['job_id'])
    assert saved['status'] == ('cancelled' if user_cancel else 'interrupted')
    assert saved['cancel_requested'] is user_cancel
