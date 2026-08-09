from pathlib import Path


def test_voice_transport_sends_audio_bytes_not_user_windows_path():
    client = (Path(__file__).parents[1] / 'src/eirven_ai/voice_worker_client.py').read_text(encoding='utf-8')
    worker = (Path(__file__).parents[1] / 'src/eirven_ai/voice_worker.py').read_text(encoding='utf-8')
    assert 'command": "transcribe_bytes"' in client
    assert 'audio_b64' in client
    assert 'NamedTemporaryFile' in worker
    assert 'base64.b64decode' in worker
