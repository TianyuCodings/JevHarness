"""Presentation regression tests: no battles, models or private game partitions."""
import ast
import builtins
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from examples.pokemon import flow_annotations as annotations


ARCHIVE = (Path(__file__).resolve().parents[1] / 'runs' /
           '20260921-012437-pokemon-mixed-expanded-405d85' / 'candidates.json')
PARENTS = {annotations.SEED: None, annotations.R1: annotations.SEED,
           annotations.R2: annotations.R1, annotations.R3: annotations.R1,
           annotations.R4: annotations.R3, annotations.R5: annotations.R1}


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def spec_hash(value):
    return digest(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False))


@pytest.fixture(scope='module')
def specs():
    if not ARCHIVE.is_file():
        pytest.skip('The optional mixed source archive is not present in this checkout')
    # Only candidate source definitions, never game inputs, results or summaries.
    return {ident: row['spec'] for ident, row in json.loads(ARCHIVE.read_text()).items()}


def render(specs, ident):
    parent = PARENTS[ident]
    return annotations.annotate_candidate(ident, specs[ident], parent_hash=parent,
                                          parent_spec=specs.get(parent))


def assert_anchor(node, anchor):
    if 'field' in anchor:
        value = node
        for part in anchor['field'].split('.'):
            value = value[part]
        assert anchor['value_sha256'] == spec_hash(value)
        assert anchor['text'] == (value if isinstance(value, str) else
                                  json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False))
    else:
        function = next(f for f in ast.parse(node['source']).body
                        if isinstance(f, ast.FunctionDef) and f.name == anchor['function'])
        source = ast.get_source_segment(node['source'], function)
        assert anchor['function_sha256'] == digest(source)
        assert anchor['text'] in source
        assert anchor['function_start_line'] == function.lineno
        assert anchor['function_end_line'] == function.end_lineno
        assert function.lineno <= anchor['line'] <= function.end_lineno


@pytest.mark.parametrize('ident', annotations.REVIEWED_HASHES)
def test_each_reviewed_feature_is_bound_to_real_source_and_true_parent(specs, ident):
    result = render(specs, ident)
    assert result['review_status'] == 'reviewed', result
    assert 'human' not in result['review_method']
    nodes = {n['id']: n for n in specs[ident]['nodes']}
    parent = PARENTS[ident]
    old = {n['id']: n for n in specs[parent]['nodes']} if parent else {}
    assert set(nodes) == set(result['nodes'])
    for node_id, note in result['nodes'].items():
        assert note['features']
        for feature in note['features']:
            assert feature['status'] in {'added', 'changed', 'retained'}
            evidence = feature['evidence']
            assert evidence['candidate_hash'] == spec_hash(specs[ident]) == ident
            assert evidence['node_sha256'] == spec_hash(nodes[node_id])
            if 'source' in nodes[node_id]:
                assert evidence['source_sha256'] == digest(nodes[node_id]['source'])
            assert evidence['anchors']
            for anchor in evidence['anchors']:
                assert_anchor(nodes[node_id], anchor)
            if note['parent_node']:
                pnode = old[note['parent_node']]
                assert evidence['parent_candidate_hash'] == parent
                assert evidence['parent_node_sha256'] == spec_hash(pnode)
                for anchor in evidence['parent_anchors']:
                    assert_anchor(pnode, anchor)
                if feature['status'] in {'changed', 'retained'}:
                    assert evidence['parent_anchors'], (ident, node_id, feature['label'])


@pytest.mark.parametrize('ident', annotations.REVIEWED_HASHES)
def test_modified_source_cannot_keep_reviewed_labels_under_an_old_hash(specs, ident):
    modified = deepcopy(specs[ident])
    node = next(n for n in modified['nodes'] if n.get('source'))
    node['source'] += '\n# Changed after the source review.\n'
    parent = PARENTS[ident]
    result = annotations.annotate_candidate(ident, modified, parent_hash=parent, parent_spec=specs.get(parent))
    assert result['review_status'] == 'unreviewed'
    assert result['changes'] == []
    assert all(n['description'].startswith('Unreviewed') for n in result['nodes'].values())


@pytest.mark.parametrize('child,incorrect_parent', [(annotations.R3, annotations.R2), (annotations.R5, annotations.R4)])
def test_round_number_does_not_substitute_for_true_lineage(specs, child, incorrect_parent):
    wrong = annotations.annotate_candidate(child, specs[child], parent_hash=incorrect_parent,
                                          parent_spec=specs[incorrect_parent])
    assert wrong['review_status'] == 'unreviewed'
    assert render(specs, child)['review_status'] == 'reviewed'


def test_missing_or_modified_parent_prevents_semantic_claims(specs):
    assert annotations.annotate_candidate(annotations.R3, specs[annotations.R3],
        parent_hash=annotations.R1)['review_status'] == 'unreviewed'
    modified = deepcopy(specs[annotations.R1])
    modified['nodes'][0]['source'] += '\n# A different parent.\n'
    assert annotations.annotate_candidate(annotations.R3, specs[annotations.R3],
        parent_hash=annotations.R1, parent_spec=modified)['review_status'] == 'unreviewed'


def test_selected_child_distinguishes_new_features_from_improved_parent_features(specs):
    result = render(specs, annotations.R3)
    features = {f['label']: f['status'] for f in result['nodes']['features']['features']}
    assert features['Turns-to-KO races'] == features['Hypothetical hidden coverage'] == 'added'
    for label in ('Coverage-aware switch-entry risk', 'Ability + status damage adjustments',
                  'Status-adjusted speed', 'Race-aware action criteria'):
        assert features[label] == 'changed'
    assert result['nodes']['plan']['parent_node'] == 'tactics'
    assert result['nodes']['pick']['parent_node'] == 'action_choice'
    assert result['nodes']['plan']['change_type'] == result['nodes']['pick']['change_type'] == 'changed'
    assert 'scout' not in result['nodes']
    first = render(specs, annotations.R1)
    assert next(f for f in first['nodes']['features']['features'] if f['label'] == 'Scored shortlist criteria')['status'] == 'changed'


def test_new_scout_and_renamed_analysis_are_not_conflated(specs):
    scout = render(specs, annotations.R2)['nodes']['scout']
    assert scout['parent_node'] is None and scout['change_type'] == 'added'
    assert all(f['status'] == 'added' for f in scout['features'])
    renamed = render(specs, annotations.R5)['nodes']['analysis']
    assert renamed['parent_node'] == 'features' and renamed['change_type'] == 'changed'
    baseline = render(specs, annotations.SEED)
    assert all(n['parent_node'] is None and n['change_type'] == 'retained' for n in baseline['nodes'].values())


def test_annotations_do_not_open_files_execute_code_or_mutate_inputs(specs, monkeypatch):
    before = deepcopy(specs)

    def forbidden(*args, **kwargs):
        raise AssertionError('Annotation must be a pure source-reading operation')

    monkeypatch.setattr(builtins, 'open', forbidden)
    monkeypatch.setattr(Path, 'open', forbidden)
    for ident in annotations.REVIEWED_HASHES:
        first = render(specs, ident)
        first['nodes'][next(iter(first['nodes']))]['features'][0]['label'] = 'external mutation'
        second = render(specs, ident)
        assert 'external mutation' not in json.dumps(second)
    assert specs == before


def test_unknown_code_gets_only_declared_dependencies_without_semantic_guessing():
    old = {'nodes': [{'id': 'old', 'kind': 'python', 'depends_on': [], 'source': 'invalid source'}]}
    child = {'nodes': [{'id': 'new', 'kind': 'python', 'depends_on': ['old'],
                        'source': 'raise RuntimeError("must not execute")\n# damage speed hidden coverage'}]}
    result = annotations.annotate_candidate(spec_hash(child), child, parent_hash=spec_hash(old), parent_spec=old)
    assert result['review_status'] == 'unreviewed' and result['changes'] == []
    note = result['nodes']['new']
    assert note['parent_node'] is None
    assert [f['label'] for f in note['features']] == ['Dependencies: old']
    assert 'hidden coverage' not in json.dumps(result)


def test_stale_source_anchor_fails_closed_to_unreviewed(specs, monkeypatch):
    broken = deepcopy(annotations._REVIEWS)
    node = broken[annotations.R3]['nodes']['features']
    feature = list(node['features'][0])
    feature[2] = ('code', 'a_function_that_does_not_exist', '')
    node['features'] = (tuple(feature), *node['features'][1:])
    monkeypatch.setattr(annotations, '_REVIEWS', broken)
    result = render(specs, annotations.R3)
    assert result['review_status'] == 'unreviewed' and result['changes'] == []


def test_overview_annotations_use_event_parent_without_extra_archive_reads(specs, monkeypatch):
    from examples.pokemon import presentation

    records = [{'hash': h, 'spec': specs[h], 'parents': [] if h == annotations.SEED else [annotations.R2]}
               for h in (annotations.SEED, annotations.R1, annotations.R2, annotations.R3)]

    class Store:
        candidate_reads = 0

        def get_run(self, run):
            assert run == 'mixed'
            return {'config': {'seed_pipeline_hash': annotations.SEED}}

        def list_candidates(self, run):
            self.candidate_reads += 1
            return records

        def list_evaluations(self, run):
            return []

        def list_events(self, run):
            return [{'kind': 'proposal', 'payload': {'candidate': annotations.R3, 'parent': annotations.R1}}]

    reads = []

    def read(path, default):
        reads.append(Path(path).name)
        assert reads[-1] in {'state.json', 'manifest.json', 'rules.json', 'train.json', 'validation.json'}
        return {'run_id': 'mixed'} if reads[-1] == 'state.json' else default

    monkeypatch.setattr(presentation, 'read_json', read)
    store = Store()
    data = presentation.overview(store, Path('public/state.json'))
    selected = next(c for c in data['candidates'] if c['hash'] == annotations.R3)
    assert selected['parent_hash'] == annotations.R1
    assert selected['presentation']['review_status'] == 'reviewed'
    assert store.candidate_reads == 1
