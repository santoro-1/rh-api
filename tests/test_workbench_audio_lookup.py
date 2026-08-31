from __future__ import annotations

import hashlib

import pytest

from app.database import SessionLocal
from app.models import AudioGenerationTask, GenerationBatch, GenerationTask
from tests.conftest import create_user
from tests.test_workbench_audio_validation import audio_request


def test_lookup_recovers_exact_audio_receipt_without_creating_work(client, audio_request):
    created = client.post('/api/workbench/audio-batches', json=audio_request)
    assert created.status_code == 201
    request = {key: audio_request[key] for key in ('access_token', 'request_key')}
    for _ in range(2):
        response = client.post('/api/workbench/audio-batches/lookup', json=request)
        assert response.status_code == 200
        result = response.json()
        assert result['schema'] == 'runninghub.workbench-audio-lookup.v1'
        assert result['found'] is True
        assert result['request_key'] == request['request_key']
        assert result['batch'] == created.json()
        binding = result['input_bindings'][created.json()['items'][0]['item_id']]
        assert binding['script_sha256'] == hashlib.sha256('只生成声音。'.encode()).hexdigest()
        assert binding['voice_asset_id'] == 'audio-only-voice'
        assert binding['speech_settings']['speed'] == 1.04
        assert 'access_token' not in str(result)
    with SessionLocal() as db:
        assert db.query(GenerationBatch).count() == 1
        assert db.query(AudioGenerationTask).count() == 1
        assert db.query(GenerationTask).count() == 0


def test_missing_or_foreign_lookup_never_creates_audio(client, audio_request):
    created = client.post('/api/workbench/audio-batches', json=audio_request)
    assert created.status_code == 201
    other = create_user('audio-lookup-other', with_config=False)
    login = client.post('/api/auth/center/login', json={'username': other.username, 'password': 'password123'})
    for token, key in (
        (audio_request['access_token'], 'absent'),
        (login.json()['access_token'], audio_request['request_key']),
    ):
        result = client.post('/api/workbench/audio-batches/lookup', json={
            'access_token': token, 'request_key': key,
        })
        assert result.status_code == 200
        assert result.json() == {'schema': 'runninghub.workbench-audio-lookup.v1', 'found': False}
    with SessionLocal() as db:
        assert db.query(GenerationBatch).count() == 1
        assert db.query(AudioGenerationTask).count() == 1


@pytest.mark.parametrize('key', [None, '', ' ', 'x' * 65, 42, {}])
def test_lookup_rejects_invalid_keys(client, audio_request, key):
    response = client.post('/api/workbench/audio-batches/lookup', json={
        'access_token': audio_request['access_token'], 'request_key': key,
    })
    assert response.status_code == 422
    with SessionLocal() as db:
        assert db.query(GenerationBatch).count() == 0


def test_lookup_requires_login(client):
    response = client.post('/api/workbench/audio-batches/lookup', json={'request_key': 'key'})
    assert response.status_code == 401


def test_real_lookup_contract_recovers_jyd_interrupted_submission(client, audio_request, tmp_path, monkeypatch):
    """Exercise the actual cloud serializer and local recovery, not two mocks."""
    from pathlib import Path
    jyd_source = Path(__file__).resolve().parents[2].parent / '公寓' / 'jyd_plain_json_probe' / 'src'
    if not jyd_source.is_dir():
        pytest.skip('Sibling JYD checkout is required for cross-project contract test')
    monkeypatch.syspath_prepend(str(jyd_source))
    from jyd_probe.project_audio import ProjectAudioCoordinator
    from jyd_probe.project_store import ProjectStore

    class Bridge:
        submissions = 0

        def list_workbench_voices(self, token):
            return {'voices': [{'voice_asset_id': 'audio-only-voice'}]}

        def create_workbench_audio_batch(self, token, payload):
            self.submissions += 1
            response = client.post('/api/workbench/audio-batches', json={'access_token': token, **payload})
            assert response.status_code == 201, response.text
            raise SystemExit('exit after cloud commit, before receipt is saved locally')

        def lookup_workbench_audio_batch(self, token, request_key):
            response = client.post('/api/workbench/audio-batches/lookup', json={'access_token': token, 'request_key': request_key})
            assert response.status_code == 200
            return response.json()

        def get_workbench_audio_batch(self, token, batch_id):
            return client.post(f'/api/workbench/audio-batches/{batch_id}', json={'access_token': token}).json()

    store = ProjectStore(tmp_path / 'local.db')
    project = store.create_project(owner_user_id='local-user', owner_username='local-user', name='contract',
                                   items=[{'row_key': '1', 'script_text': '只生成声音。'}])
    bridge = Bridge()
    coordinator = ProjectAudioCoordinator(store, bridge, storage_root=tmp_path / 'storage', max_audio_bytes=1024)
    with pytest.raises(SystemExit):
        coordinator.start('local-user', project['project_id'], audio_request['access_token'],
                          default_voice_asset_id='audio-only-voice', voice_assignments=None,
                          settings={'speed': 1.04}, idempotency_key='cross-project')
    restarted = ProjectAudioCoordinator(ProjectStore(store.path), bridge, storage_root=tmp_path / 'storage', max_audio_bytes=1024)
    after = restarted.sync('local-user', project['project_id'], audio_request['access_token'])
    assert after['operations'][-1]['status'] == 'RUNNING'
    assert after['items'][0]['audio_submission'] is None
    assert bridge.submissions == 1
    with SessionLocal() as db:
        assert db.query(AudioGenerationTask).count() == 1
        assert after['operations'][-1]['result']['batch_id'] == db.query(GenerationBatch).one().id
