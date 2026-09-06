from types import SimpleNamespace
from unittest.mock import Mock

from backend.scripts import video_run_cli as cli


def test_cli_render_reuses_persisted_audio_and_image(tmp_path, monkeypatch):
    reference = tmp_path / 'ref.wav'
    reference.write_bytes(b'audio')
    manifest = {'run_id': 'run', 'pages': [{'script': '講稿', 'page_number': 1}]}
    monkeypatch.setattr(cli, 'request_json', lambda *a, **kw: manifest)
    responses = [
        SimpleNamespace(ok=True, json=lambda: {'variant_id': 'v1'}),
        SimpleNamespace(ok=True, json=lambda: {'segments': [{'start': 0, 'end': 1, 'text': '講稿'}], 'backend': 'qwen'}),
        SimpleNamespace(ok=True, headers={'X-Variant-Id': 'v1'}, content=b'video'),
    ]
    session = Mock()
    session.post.side_effect = responses
    monkeypatch.setattr(cli.requests, 'Session', lambda: session)
    args = SimpleNamespace(api_base='http://test', run_id='run', reference_audio=str(reference), pages='all', variants=1,
                           voice='', speed=1, reference_text='ref', timeout=5, split_min=10, split_max=32,
                           enable_highlight=False, font_size=52, bg_color='#000000', bg_opacity=55, margin_v=90, pause=0)
    cli.cmd_render_run(args)
    calls = session.post.call_args_list
    assert calls[0].args[0].endswith('/api/video-runs/run/pages/0/tts')
    assert calls[1].args[0].endswith('/api/video-runs/run/pages/0/align')
    for call in calls[1:]:
        assert call.kwargs['data']['variant_id'] == 'v1'
        assert 'files' not in call.kwargs
    session.get.assert_not_called()


def test_cli_merge_streams_canonical_export_without_downloading_parts(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, 'request_json', lambda *a: {'pages': [{'selected_variant_id': 'v1'}]})
    response = Mock(ok=True)
    response.iter_content.return_value = [b'one', b'two']
    session = Mock()
    session.post.return_value = response
    context = Mock()
    context.__enter__ = Mock(return_value=session)
    context.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(cli.requests, 'Session', lambda: context)
    output = tmp_path / 'out.mp4'
    cli.cmd_merge_run(SimpleNamespace(api_base='http://test', run_id='run', timeout=5, output=str(output)))
    assert output.read_bytes() == b'onetwo'
    assert session.post.call_args.args[0].endswith('/api/video-runs/run/exports/merge-selected')
    session.get.assert_not_called()
