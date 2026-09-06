from pathlib import Path
import asyncio
import base64
import json

import pytest
import requests
from fastapi import HTTPException

from backend.app.api import video_runs
from backend.app.services.artifact_store import VideoRunStore
from backend.app.services.utility import api as llm_api


@pytest.mark.parametrize('provider', ['google', 'anthropic', 'openai', 'openrouter', 'xai', 'groq', 'custom'])
def test_each_provider_sends_a_single_image(provider, tmp_path, monkeypatch):
    image = tmp_path / 'page.jpg'
    image.write_bytes(b'image-content')
    monkeypatch.setattr(llm_api, 'get_configured_llm_provider', lambda *_: provider)
    monkeypatch.setattr(llm_api, 'get_llm_model_name', lambda *_: 'vision-model')
    monkeypatch.setenv('CUSTOM_LLM_ENDPOINT', 'http://localhost/v1/chat/completions')
    captured = []

    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {'candidates': [{'content': {'parts': [{'text': '圖片講稿。'}]}}],
                    'content': [{'type': 'text', 'text': '圖片講稿。'}],
                    'choices': [{'message': {'content': '圖片講稿。'}}]}

    def post(endpoint, **kwargs):
        captured.append(kwargs['json'])
        return Response()

    monkeypatch.setattr(llm_api.requests, 'post', post)
    assert asyncio.run(llm_api.generate_presentation_scripts_from_images([image], api_key='test-key')) == ['圖片講稿。']
    assert len(captured) == 1
    payload = captured[0]
    encoded = base64.b64encode(b'image-content').decode()
    if provider == 'google':
        parts = payload['contents'][0]['parts']
        assert parts[1]['inline_data'] == {'mime_type': 'image/jpeg', 'data': encoded}
    elif provider == 'anthropic':
        parts = payload['messages'][0]['content']
        assert parts[0]['source']['data'] == encoded
        assert parts[0]['source']['media_type'] == 'image/jpeg'
    else:
        parts = payload['messages'][0]['content']
        assert parts[1]['image_url']['url'] == 'data:image/jpeg;base64,' + encoded


def test_unsupported_vision_error_is_not_silently_empty(tmp_path, monkeypatch):
    image = tmp_path / 'page.jpg'
    image.write_bytes(b'image')
    monkeypatch.setattr(llm_api, 'get_configured_llm_provider', lambda *_: 'custom')
    monkeypatch.setattr(llm_api, 'get_llm_model_name', lambda *_: 'text-only')
    response = requests.Response()
    response.status_code = 400
    monkeypatch.setattr(llm_api.requests, 'post', lambda *a, **kw: response)
    with pytest.raises(RuntimeError, match='支援圖片'):
        asyncio.run(llm_api.generate_presentation_scripts_from_images([image], api_key='test-key'))


@pytest.mark.parametrize('fail', [False, True])
def test_single_page_generation_preserves_other_scripts(tmp_path, monkeypatch, fail):
    source = tmp_path / 'source.pdf'
    source.write_bytes(b'%PDF-1.4')
    store = VideoRunStore(tmp_path / 'runs')
    run = store.create_run(pdf_path=source, scripts=['第一頁舊講稿', '第二頁舊講稿'])
    monkeypatch.setattr(video_runs, 'get_video_run_store', lambda: store)
    monkeypatch.setattr(llm_api, 'get_llm_api_key', lambda: 'test-key')
    monkeypatch.setattr(llm_api, 'llm_is_configured', lambda: True)
    seen = []
    from backend.app.services import page_images
    def ensure(store, run_id, index):
        seen.append(index)
        return {'slide': Path('selected-page.jpg')}
    monkeypatch.setattr(page_images, 'ensure_page_images', ensure)
    async def generate(**kwargs):
        assert kwargs['image_paths'] == ['selected-page.jpg']
        if fail: raise RuntimeError('vision failed')
        return ['新的第二頁講稿。']
    monkeypatch.setattr(llm_api, 'generate_presentation_scripts_from_images', generate)
    request = video_runs.VideoRunGenerateScriptsRequest(scope='current', pages=[1])
    if fail:
        with pytest.raises(HTTPException):
            asyncio.run(video_runs.generate_video_run_scripts(run['run_id'], request))
    else:
        response = asyncio.run(video_runs.generate_video_run_scripts(run['run_id'], request))
        assert json.loads(response.body)['source'] == 'slide-image'
    assert seen == [1]
    scripts = [page['script'] for page in store.load_manifest(run['run_id'])['pages']]
    assert scripts == ['第一頁舊講稿', '第二頁舊講稿' if fail else '新的第二頁講稿。']
