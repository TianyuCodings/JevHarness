"""Public preparation metadata and honest reflection progress on the dashboard."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from auto_jev.storage import RunStore, atomic_write_json
from examples.pokemon.demo import create_app


def test_prepared_counts_are_visible_without_opening_episode_definitions(tmp_path, monkeypatch):
    from examples.pokemon import demo

    atomic_write_json(tmp_path / 'manifest.json', {
        'profile': 'expanded', 'split_counts': {'train': 18, 'validation': 12, 'test': 12},
        'private_extra': 'do not expose',
    })
    atomic_write_json(tmp_path / 'rules.json', {
        'reflection_batch_games': 2, 'skip_perfect_score': True,
    })
    original = demo.read_json
    reads = []

    def public_metadata_only(path, *args, **kwargs):
        reads.append(Path(path).name)
        assert Path(path).name in {'manifest.json', 'rules.json', 'state.json'}
        return original(path, *args, **kwargs)

    monkeypatch.setattr(demo, 'read_json', public_metadata_only)
    client = TestClient(create_app(tmp_path / 'runs', tmp_path / 'state.json'))
    state = client.get('/api/state').json()
    assert state['split_counts'] == {'train': 18, 'validation': 12, 'test': 12}
    assert state['reflection_batch_games'] == 2 and state['skip_perfect_score'] is True
    assert state['phase'] == 'setup' and state['test_released'] is False
    assert 'private_extra' not in state
    assert client.get('/api/test-summary').status_code == 403
    assert client.post('/api/state').status_code == 405
    assert set(reads) == {'manifest.json', 'rules.json', 'state.json'}


@pytest.mark.parametrize('metadata_location,converged,completed', [
    ('progress', False, 2), ('progress', True, 2), ('result', True, 0),
])
def test_rendered_progress_keeps_skips_separate_from_actual_reflections(tmp_path, metadata_location, converged, completed):
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is needed for the dashboard JavaScript regression')
    page = Path(__file__).resolve().parents[1] / 'examples/pokemon/static/index.html'
    store = RunStore(tmp_path / 'runs')
    run_id = store.create_run('progress', {
        'evolution_rounds': 5, 'reflection': {'batch_size': 2}, 'skip_perfect_score': True})
    accepted = 1 if completed else 0
    stop_reason = 'training_perfect' if converged else None
    store.update_run(run_id, status='completed' if converged else 'running', **{
        metadata_location: {'rounds_completed': completed, 'skipped_batches': 9,
                            'converged': converged, 'stop_reason': stop_reason,
                            'rounds_accepted': accepted}})
    state_path = tmp_path / 'state.json'
    atomic_write_json(state_path, {'run_id': run_id})
    atomic_write_json(tmp_path / 'manifest.json', {
        'split_counts': {'train': 18, 'validation': 12, 'test': 12}})
    response = TestClient(create_app(store.root, state_path)).get('/api/overview')
    assert response.status_code == 200
    fixture = response.json()
    assert fixture['run']['converged'] is converged
    assert fixture['run']['stop_reason'] == stop_reason
    # Execute the real page's renderers with a minimal DOM; no browser, HTTP,
    # model calls, episode evaluations, or live result files are involved.
    script = r"""
const fs=require('fs'),vm=require('vm');
const code=fs.readFileSync(process.argv[1],'utf8').split('<script>')[1].split('</script>')[0].replace(/\nrefresh\(\);\s*$/,'');
const fields=new Map(),document={querySelector(selector){if(!fields.has(selector))fields.set(selector,{textContent:'',checked:false,classList:{remove(){},add(){}}});return fields.get(selector);}};
const fixture=JSON.parse(process.argv[2]);
const sandbox={document,window:{addEventListener(){}},setInterval(){},fixture};
vm.createContext(sandbox);vm.runInContext(code,sandbox);
vm.runInContext('S.overview=fixture;renderPerformance();',sandbox);
process.stdout.write(JSON.stringify(Object.fromEntries([...fields].map(([key,value])=>[key,value.textContent]))));
"""
    process = subprocess.run([node, '-e', script, str(page), json.dumps(fixture)],
                             text=True, capture_output=True, check=True)
    fields = json.loads(process.stdout)
    assert fields['#rounds'] == f'{completed} / 5'
    assert f'{accepted} proposals accepted · 9 perfect batches skipped' in fields['#round-note']
    assert fields['#experiment-scope'] == (
        '18 Train games · 12 Eval games · 2 games per reflection')
    assert fields['#tree-count'] == f'0 CANDIDATES · {completed} REFLECTIONS'
    assert fields['#run-status'] == ('Completed' if converged else 'Running')
    assert fields['#initial-score'] == fields['#eval-score'] == 'Not evaluated'
    if converged:
        # Completion by convergence must not silently fill the requested target.
        assert completed < fixture['run']['rounds_target']
        assert fields['#rounds'] != '5 / 5'
