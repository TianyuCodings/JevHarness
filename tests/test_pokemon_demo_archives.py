"""Read-only reflection downloads are exact, registered, hash-verified bytes."""
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from auto_jev.storage import RunStore, atomic_write_json
from examples.pokemon.demo import create_app


@pytest.fixture
def archive(tmp_path):
    store = RunStore(tmp_path / 'runs')
    run_id = store.create_run('reflection viewer fixture', {})
    state_path = tmp_path / 'state.json'
    atomic_write_json(state_path, {'run_id': run_id, 'test_released': False})
    prompt = json.dumps({'instruction': 'Choose an action', 'training_feedback': {'encoding': 'fixture'}}, ensure_ascii=False).encode()
    full = json.dumps({'instruction': 'Choose an action', 'training_feedback': {'complete': '原始输入\n' * 100}}, ensure_ascii=False).encode()
    sha = hashlib.sha256(prompt).hexdigest()
    folder = store.run_dir(run_id) / 'reflections'
    folder.mkdir()
    prompt_path = folder / f'0001-{sha}.prompt.json'
    full_path = folder / f'0001-{sha}.full.json'
    prompt_path.write_bytes(prompt)
    full_path.write_bytes(full)
    registration = {'sha256': sha, 'path': str(prompt_path.relative_to(store.run_dir(run_id))),
                    'prompt_bytes': len(prompt), 'encoding': 'lossless_dag',
                    'full_path': str(full_path.relative_to(store.run_dir(run_id))),
                    'full_bytes': len(full), 'full_sha256': hashlib.sha256(full).hexdigest(),
                    'complete': True, 'episode_ids': ['training-1']}
    store.append_event(run_id, 'reflection_input', registration)
    return {'store': store, 'run_id': run_id, 'sha': sha, 'registration': registration,
            'client': TestClient(create_app(store.root, state_path)), 'prompt': prompt, 'full': full,
            'prompt_path': prompt_path, 'full_path': full_path, 'folder': folder,
            'url': f'/api/run/{run_id}/reflection/{sha}'}


@pytest.mark.parametrize('variant', ['prompt', 'full'])
def test_reflection_returns_exact_verified_bytes_and_download(archive, variant):
    client, raw = archive['client'], archive[variant]
    response = client.get(archive['url'] + '/' + variant)
    assert response.status_code == 200
    assert response.content == raw
    assert response.headers['x-archive-sha256'] == hashlib.sha256(raw).hexdigest()
    assert int(response.headers['x-archive-bytes']) == len(raw)
    assert response.headers['x-content-type-options'] == 'nosniff'
    downloaded = client.get(archive['url'] + '/' + variant + '?download=true')
    assert downloaded.content == raw
    assert downloaded.headers['content-disposition'].startswith('attachment;')
    assert client.post(archive['url'] + '/' + variant).status_code == 405


def test_unknown_archive_and_invalid_selectors_cannot_select_a_file(archive):
    client = archive['client']
    assert client.get(archive['url'].replace(archive['sha'], '0' * 64) + '/prompt').status_code == 404
    assert client.get(archive['url'] + '/../../state.json').status_code == 404
    assert client.get(archive['url'] + '/invalid').status_code == 400
    assert client.get(archive['url'].replace(archive['run_id'], 'unlisted-run') + '/prompt').status_code == 404


@pytest.mark.parametrize('path', ['../state.json', '/etc/passwd', 'reflections/../../state.json', 'reflections/other.json'])
def test_even_registered_traversal_or_unexpected_paths_are_rejected(archive, path):
    archive['store'].append_event(archive['run_id'], 'reflection_input', {**archive['registration'], 'path': path})
    assert archive['client'].get(archive['url'] + '/prompt').status_code == 409


def test_tampering_same_size_is_rejected_by_sha(archive):
    data = archive['full']
    archive['full_path'].write_bytes(b'X' + data[1:])
    assert archive['client'].get(archive['url'] + '/full').status_code == 409


def test_truncated_file_is_not_served_as_complete(archive):
    archive['prompt_path'].write_bytes(archive['prompt'][:-1])
    assert archive['client'].get(archive['url'] + '/prompt').status_code == 409


def test_file_symlink_is_rejected_even_if_target_hash_matches(archive, tmp_path):
    outside = tmp_path / 'elsewhere.json'
    outside.write_bytes(archive['full'])
    archive['full_path'].unlink()
    archive['full_path'].symlink_to(outside)
    assert archive['client'].get(archive['url'] + '/full').status_code == 404


def test_reflections_directory_symlink_is_rejected(archive, tmp_path):
    actual = tmp_path / 'elsewhere'
    archive['folder'].rename(actual)
    archive['folder'].symlink_to(actual, target_is_directory=True)
    assert archive['client'].get(archive['url'] + '/prompt').status_code == 404


def test_missing_original_file_is_not_reconstructed_or_substituted(archive):
    archive['full_path'].unlink()
    assert archive['client'].get(archive['url'] + '/full').status_code == 404
    assert archive['client'].get(archive['url'] + '/prompt').content == archive['prompt']


def test_legacy_plain_archive_can_serve_full_input_from_same_registered_file(archive):
    old = {k: v for k, v in archive['registration'].items() if not k.startswith('full_') and k != 'encoding'}
    archive['store'].append_event(archive['run_id'], 'reflection_input', old)
    assert archive['client'].get(archive['url'] + '/full').content == archive['prompt']


def test_dashboard_metadata_does_not_eagerly_open_reflection_archives(archive, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Metadata refresh must not read reflection files')

    monkeypatch.setattr('examples.pokemon.demo.os.open', forbidden)
    response = archive['client'].get('/api/run/' + archive['run_id'])
    assert response.status_code == 200
    assert response.json()['events'][0]['kind'] == 'reflection_input'
