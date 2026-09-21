import copy
import hashlib
import json

import pytest

from auto_jev.reflection import ReflectionContextError, archive_prompt
from auto_jev.storage import RunStore
from auto_jev.trace_codec import pack_json, unpack_json


def test_complete_json_roundtrip_with_reserved_keys_and_independent_values():
    shared = {'$ref': 3, 'unicode': '完整证据' * 100, 'nested': [0, 0.0, -0.0, None, True, False]}
    value = {'pipeline': [{'trace': [shared, copy.deepcopy(shared)], 'output': shared}]}
    packed = pack_json(value)
    restored = unpack_json(json.loads(json.dumps(packed)))
    assert restored == value
    restored['pipeline'][0]['trace'][0]['nested'].append('mutation')
    assert restored['pipeline'][0]['trace'][1]['nested'] == shared['nested']
    assert len(json.dumps(packed, ensure_ascii=False)) < len(json.dumps(value, ensure_ascii=False))


def test_realistic_fifty_bar_trace_is_lossless_and_smaller():
    from auto_jev.crypto import evaluate_episode
    from auto_jev.data import demo_episodes
    from auto_jev.providers import JevClient
    from auto_jev.spec import seed_spec
    ep = demo_episodes()[0]
    bars = ep['bars']
    ep['bars'] = [{**bars[i % len(bars)], 'timestamp': bars[0]['timestamp'] + i * 3600} for i in range(50)]
    result = evaluate_episode(seed_spec(), ep, JevClient(mock=True))
    packed = pack_json(result)
    assert unpack_json(packed) == result
    assert len(result['traces']) == 49
    assert len(json.dumps(packed)) < len(json.dumps(result)) / 3


def test_archive_keeps_plain_original_and_exact_transmitted_lossless_prompt(tmp_path):
    store = RunStore(tmp_path)
    run_id = store.create_run('codec', {})
    result = {'traces': [{'trace': [{'id': str(i), 'output': 'FULL_DATA' * 100} for i in range(4)]} for _ in range(49)]}
    payload = {'instruction': 'return pipeline', 'training_feedback': {'pipeline': [{'episode_id': 't', 'result': result}]}}
    prompt, manifest = archive_prompt(store, run_id, 'parent', payload, 1_000_000, 'lossless_dag')
    decoded = json.loads(prompt)
    decoded['training_feedback'] = unpack_json(decoded['training_feedback'])
    assert decoded == payload
    assert json.loads((store.run_dir(run_id) / manifest['full_path']).read_text()) == payload
    assert (store.run_dir(run_id) / manifest['path']).read_text() == prompt
    assert manifest['sha256'] == hashlib.sha256(prompt.encode()).hexdigest()
    assert manifest['trace_count'] == 49 and manifest['node_count'] == 196
    assert manifest['prompt_bytes'] < manifest['full_bytes']
    with pytest.raises(ReflectionContextError):
        archive_prompt(store, run_id, 'parent', payload, 1, 'lossless_dag')
    failed = store.list_events(run_id, 'reflection_input')[-1]['payload']
    assert (store.run_dir(run_id) / failed['full_path']).is_file()
    assert not failed['context_fits']


def test_packing_rejects_non_json_and_unpacking_rejects_cycles():
    with pytest.raises(ValueError):
        pack_json({'bad': float('nan')})
    with pytest.raises(ValueError):
        pack_json({1: 'nonstring key'})
    packed = pack_json([1])
    packed['objects'][0] = ['list', [{'$ref': 0}]]
    with pytest.raises(ValueError, match='cyclic'):
        unpack_json(packed)
