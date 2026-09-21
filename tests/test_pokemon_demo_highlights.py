"""Read-only highlight routes keep archive binding and asset boundaries."""
import copy
import json
import os

import pytest
from fastapi.testclient import TestClient

from auto_jev.storage import RunStore, TraceRevisionError, atomic_write_json
from examples.pokemon import demo, highlights as h


@pytest.fixture
def service(tmp_path, monkeypatch):
    package = tmp_path / 'package'
    static = package / 'static'
    static.mkdir(parents=True)
    monkeypatch.setattr(demo, '__file__', str(package / 'demo.py'))
    assets = {'highlight-demo.js': b'/* local controller */',
              'highlight-replay.js': b'/* local renderer adapter */',
              'highlight-demo.css': b'/* local styles */'}
    for name, data in assets.items():
        (static / name).write_bytes(data)
    store = RunStore(tmp_path / 'runs')
    directory = store.root / h.REVIEWED_RUN
    directory.mkdir()
    atomic_write_json(directory / 'run.json', {'run_id': h.REVIEWED_RUN, 'config': {}, 'status': 'completed'})
    state = tmp_path / 'state.json'
    atomic_write_json(state, {'run_id': h.REVIEWED_RUN, 'test_released': False})
    log = '|turn|2\n|message|</script><script>UNTRUSTED_LOG()</script>\n|win|AutoJev'
    for candidate, split, episode in ((h.REVIEWED_CANDIDATE, 'validation', h.REVIEWED_EPISODE),
                                      ('a' * 64, 'validation', h.REVIEWED_EPISODE),
                                      (h.REVIEWED_CANDIDATE, 'train', h.REVIEWED_EPISODE),
                                      (h.REVIEWED_CANDIDATE, 'validation', 'another-game')):
        store.save_trace(h.REVIEWED_RUN, split, candidate, episode,
                         {'episode_id': episode, 'replay_log': log, 'status': 'completed',
                          'winner': 'player', 'score': 1, 'turns': 15, 'traces': []})
    payload = {
        'schema': 'auto_jev.pokemon.highlights.v1',
        'binding': {'run_id': h.REVIEWED_RUN, 'split': 'validation',
                    'candidate_hash': h.REVIEWED_CANDIDATE, 'episode_id': h.REVIEWED_EPISODE,
                    'trace_revision': h.REVIEWED_REVISION, 'replay_sha256': h.REVIEWED_REPLAY_SHA256,
                    'replay_line_count': 185},
        'winner': 'player', 'score': 1, 'turns': 15,
        'chapters': [{'id': 'turn-2', 'decision_index': 1, 'request_id': 2,
                      'decision': {'observation': {'complete': [1, {'original': True}]},
                                   'trace': [{'id': 'pick', 'response': {
                                       'answers': {'action': {'choice': 'move:3', 'probabilities': {'move:3': .95}}}}}]}}],
    }
    calls = []
    original_builder = h.build_highlights

    def build(store_argument, run_id, split, candidate, episode, revision=None):
        calls.append((store_argument.root, run_id, split, candidate, episode, revision))
        if (run_id, split, candidate, episode) != (h.REVIEWED_RUN, 'validation', h.REVIEWED_CANDIDATE, h.REVIEWED_EPISODE):
            # Exercise the production allowlist before it would read a trace.
            return original_builder(store_argument, run_id, split, candidate, episode, revision)
        if revision is not None and revision != h.REVIEWED_REVISION:
            raise TraceRevisionError('The requested highlight revision has not been reviewed')
        return copy.deepcopy(payload)

    monkeypatch.setattr(h, 'build_highlights', build)
    client = TestClient(demo.create_app(store.root, state))
    return {'client': client, 'store': store, 'state': state, 'static': static,
            'assets': assets, 'payload': payload, 'calls': calls}


def url(endpoint, *, run=h.REVIEWED_RUN, candidate=h.REVIEWED_CANDIDATE, episode=h.REVIEWED_EPISODE):
    return f'/api/run/{run}/game/{candidate}/{episode}/{endpoint}'


def test_highlight_route_preserves_complete_payload_and_passes_revision(service):
    response = service['client'].get(url('highlights'), params={'split': 'validation', 'revision': h.REVIEWED_REVISION})
    assert response.status_code == 200
    assert response.json() == service['payload']
    assert service['calls'] == [(service['store'].root, h.REVIEWED_RUN, 'validation', h.REVIEWED_CANDIDATE,
                                 h.REVIEWED_EPISODE, h.REVIEWED_REVISION)]
    assert 'application/json' in response.headers['content-type']
    for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        assert service['client'].request(method, url('highlights')).status_code == 405
    assert len(service['calls']) == 1


def test_stale_highlight_revision_is_conflict_not_another_trace(service):
    response = service['client'].get(url('highlights'), params={'revision': '0' * 64})
    assert response.status_code == 409
    assert 'not been reviewed' in response.json()['detail']


@pytest.mark.parametrize('query,changes,status', [
    ({'split': 'train'}, {}, 404), ({'split': 'eval'}, {}, 400),
    ({'split': 'test'}, {}, 403), ({'split': 'holdout'}, {}, 403),
    ({}, {'candidate': 'a' * 64}, 404), ({}, {'episode': 'another-game'}, 404),
    ({}, {'run': 'outside-run'}, 404),
])
def test_unreviewed_highlight_requests_do_not_leak_a_different_game(service, query, changes, status):
    response = service['client'].get(url('highlights', **changes), params=query)
    assert response.status_code == status
    assert 'chapters' not in response.json() and 'UNTRUSTED_LOG' not in response.text


def test_legacy_replay_stays_available_and_log_is_encoded_as_data(service):
    client = service['client']
    response = client.get(url('replay'), params={'split': 'validation'})
    assert response.status_code == 200
    assert '/assets/highlight-replay.js' not in response.text
    assert 'auto-jev-seek' in response.text and 'Replays.battle.seekTurn' in response.text
    assert 'config/config.js' in response.text and 'js/replay-embed.js' in response.text
    assert '<script>UNTRUSTED_LOG()</script>' not in response.text
    assert '\\u003c/script\\u003e' in response.text
    assert "connect-src 'none'" in response.headers['content-security-policy']
    assert response.headers['referrer-policy'] == 'no-referrer'
    assert response.headers['cache-control'] == 'no-store'
    assert service['calls'] == []
    # Other old Train/Eval archives remain valid full replays.
    assert client.get(url('replay', candidate='a' * 64)).status_code == 200
    assert client.get(url('replay'), params={'split': 'train'}).status_code == 200


def test_highlight_replay_loads_only_the_local_adapter_in_addition_to_legacy_renderer(service):
    response = service['client'].get(url('replay'), params={'split': 'validation', 'highlights': 'true'})
    assert response.status_code == 200
    assert response.text.count('<script src="/assets/highlight-replay.js"></script>') == 1
    assert 'js/replay-embed.js' in response.text
    assert "script-src 'self'" in response.headers['content-security-policy']
    assert "connect-src 'none'" in response.headers['content-security-policy']


@pytest.mark.parametrize('query,changes', [
    ({'split': 'train'}, {}), ({}, {'candidate': 'a' * 64}), ({}, {'episode': 'another-game'}),
])
def test_highlight_mode_refuses_unreviewed_game_without_affecting_full_replay(service, query, changes):
    response = service['client'].get(url('replay', **changes), params={**query, 'highlights': 'true'})
    assert response.status_code == 404
    assert '/assets/highlight-replay.js' not in response.text


def test_exact_three_assets_have_correct_mime_and_are_read_only(service):
    client = service['client']
    for name, data in service['assets'].items():
        response = client.get('/assets/' + name)
        assert response.status_code == 200 and response.content == data
        assert response.headers['content-type'].startswith('text/css' if name.endswith('.css') else 'text/javascript')
        assert response.headers['x-content-type-options'] == 'nosniff'
        assert response.headers['cache-control'] == 'no-cache'
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            assert client.request(method, '/assets/' + name, content=b'replaced').status_code == 405
        assert (service['static'] / name).read_bytes() == data


@pytest.mark.parametrize('name', ['index.html', 'manifest.json', 'arbitrary.js', '.env',
                                 'highlight-demo.js.map', 'highlight-demo.JS', 'highlight-demo.js.bak'])
def test_other_existing_files_are_not_public_assets(service, name):
    (service['static'] / name).write_bytes(b'PRIVATE_CONTENT')
    response = service['client'].get('/assets/' + name)
    assert response.status_code == 404 and b'PRIVATE_CONTENT' not in response.content


@pytest.mark.parametrize('encoded', ['..%2fhighlight-demo.js', '%2e%2e%2fhighlight-demo.js',
                                    '%2fhighlight-demo.js', 'nested%2fhighlight-demo.js',
                                    '..%5chighlight-demo.js'])
def test_asset_path_escape_is_rejected(service, encoded):
    (service['static'].parent / 'highlight-demo.js').write_bytes(b'PRIVATE_CONTENT')
    response = service['client'].get('/assets/' + encoded)
    assert response.status_code == 404 and b'PRIVATE_CONTENT' not in response.content


@pytest.mark.parametrize('kind', ['missing', 'directory', 'fifo', 'symlink_inside', 'symlink_outside'])
def test_allowlisted_name_still_requires_a_regular_non_symlink_file(service, kind):
    path = service['static'] / 'highlight-demo.js'
    path.unlink()
    if kind == 'directory':
        path.mkdir()
    elif kind == 'fifo':
        os.mkfifo(path)
    elif kind.startswith('symlink'):
        target = service['static'] / 'highlight-replay.js' if kind == 'symlink_inside' else service['static'].parent / 'private.js'
        if kind == 'symlink_outside':
            target.write_bytes(b'PRIVATE_CONTENT')
        path.symlink_to(target)
    response = service['client'].get('/assets/highlight-demo.js')
    assert response.status_code == 404 and b'PRIVATE_CONTENT' not in response.content


def test_symlinked_static_ancestor_is_not_followed(service):
    static = service['static']
    target = static.parent / 'private-assets'
    target.mkdir()
    (target / 'highlight-demo.js').write_bytes(b'PRIVATE_CONTENT')
    static.rename(static.with_name('old-static'))
    static.symlink_to(target, target_is_directory=True)
    response = service['client'].get('/assets/highlight-demo.js')
    assert response.status_code == 404 and b'PRIVATE_CONTENT' not in response.content
