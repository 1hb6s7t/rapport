import json

from rapport.config import ConnectionMode
from rapport.learning.style_rag import SYSTEM


def choose(state, tmp_path, digest='a' * 64):
    path = tmp_path / 'manifest.json'
    record = {'scope': 'local_reply_drafts', 'training_complete': True,
              'id': 'r7-epoch-8-' + digest[:12], 'artifact_hash': digest,
              'selection_basis': 'explicit_user_choice_2026-10-02'}
    path.write_text(json.dumps(record), 'utf-8')
    state.settings.reply_adapter_manifest = str(path)
    return path, record


def context(client):
    result = client.post('/api/imports', json={'text': 'A: 你那个杯子修好了吗？\nB: 已经修好了，试过不漏水了。'})
    assert result.status_code == 200
    ident = result.json()['id']
    assert client.post(f'/api/imports/{ident}/confirm', json={}).status_code == 200
    return {'import_id': ident}


def test_selected_route_only_changes_local_replies(app_client, tmp_path):
    state = app_client.app_state
    _, record = choose(state, tmp_path)
    cfg = state.task_provider_config('reply')
    assert cfg['preset'] == 'selected-local' and cfg['model'] == record['id']
    assert state.task_provider_config('intent')['preset'] != 'selected-local'
    assert state.learning.active_model('reply') is None, 'Does not forge research model approval'
    state.set_connection_mode(ConnectionMode.cloud)
    state.save_provider_config('deepseek', None, None, None, None)
    assert state.task_provider_config('reply')['preset'] == 'deepseek'


def test_selected_reply_runs_through_existing_preview_and_compiler(app_client, tmp_path):
    choose(app_client.app_state, tmp_path)
    body = context(app_client)
    assert app_client.post('/api/replies', json=body).status_code == 409
    preview = app_client.post('/api/replies/preview', json=body).json()
    assert preview['destination']['preset'] == 'selected-local'
    assert '第 8 轮' in preview['destination']['label']
    result = app_client.post('/api/replies', json={**body, 'ticket_id': preview['ticket_id'],
                            'payload_hash': preview['payload_hash'], 'accept_cost': True})
    assert result.status_code == 200, result.text
    assert app_client.app_state.provider_override.calls[-1][0] == SYSTEM


def test_weight_change_invalidates_previously_confirmable_preview(app_client, tmp_path):
    choose(app_client.app_state, tmp_path)
    body = context(app_client)
    preview = app_client.post('/api/replies/preview', json=body).json()
    choose(app_client.app_state, tmp_path, 'b' * 64)
    result = app_client.post('/api/replies', json={**body, 'ticket_id': preview['ticket_id'],
                            'payload_hash': preview['payload_hash'], 'accept_cost': True})
    assert result.status_code == 409
    assert result.json()['error']['code'] == 'hash_mismatch'
    assert not app_client.app_state.provider_override.calls
