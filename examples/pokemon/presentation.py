"""Read-only Train/Eval presentation, built from the mixed search archive."""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

from auto_jev.storage import StoreError, read_json, scrub_secrets
from examples.pokemon.flow_annotations import annotate_candidate


IDENTITY_FIELDS = ('player_team_id', 'opponent_team_id', 'opponent', 'seed', 'opponent_seed')
REFLECTION_FIELDS = ('complete', 'episode_ids', 'trace_count', 'node_count', 'parent',
                     'prompt_bytes', 'sha256', 'path', 'max_prompt_bytes', 'context_fits',
                     'encoding', 'full_path', 'full_bytes', 'full_sha256')
FRONTIER_FIELDS = ('candidate', 'gepa_idx', 'probabilities', 'candidate_hashes', 'frontier', 'raw_frontier')


def _identity(episode):
    nested = episode.get('provenance') or {}
    return {key: episode.get(key, nested.get(key)) for key in IDENTITY_FIELDS}


def _history(record):
    source = record.get('evaluation_history')
    if not isinstance(source, dict):
        return None
    return {key: source[key] for key in ('sha256', 'path', 'bytes') if key in source}


def _game(record, metadata):
    row = {key: record.get(key) for key in ('episode_id', 'score', 'status')}
    row['metadata'] = metadata.get(record['episode_id'], {})
    reference = _history(record)
    if reference:
        row['history'] = reference
    return row


def _metrics(rows, total):
    result = {'wins': 0, 'losses': 0, 'draws': 0, 'errors': 0,
              'completed': 0, 'observed': len(rows), 'total': total}
    for row in rows:
        score = row.get('score')
        if row.get('status') != 'ok' or type(score) not in (int, float) or score not in (0, 0.5, 1):
            result['errors'] += 1
            continue
        result['completed'] += 1
        result[{1: 'wins', 0: 'losses', 0.5: 'draws'}[score]] += 1
    return result


def _episode_catalog(directory, config):
    catalog = {'train': [], 'eval': []}
    for public, internal, filename in [('train', 'train', 'train.json'), ('eval', 'validation', 'validation.json')]:
        # Only the two public input partitions are opened. Runtime observations,
        # game traces, final summaries and other branches are not read here.
        definitions = read_json(directory / filename, [])
        definitions = {e['id']: e for e in definitions if isinstance(e, dict) and isinstance(e.get('id'), str)}
        summaries = [e for e in config.get('episodes', []) if e.get('split') == internal]
        if not summaries:
            summaries = list(definitions.values())
        for episode in summaries:
            definition = definitions.get(episode['id'], {})
            expected = (config.get('data_hashes') or {}).get(episode['id'])
            if definition and expected:
                actual = hashlib.sha256(json.dumps(definition, sort_keys=True, allow_nan=False).encode()).hexdigest()
                if actual != expected:
                    raise StoreError('Public episode definition differs from the evaluated input hash')
            source = {**episode, **definition}
            catalog[public].append({'episode_id': episode['id'], 'metadata': _identity(source)})
    return catalog


def _rounds(events, evaluations):
    """Bind proposal events to actual reflection rounds, not GEPA iterations."""
    active = {}
    proposals = {}
    bindings = {}
    reflections = []
    frontiers = []
    reflection_by_sha = {}
    for event in events:
        kind, payload = event.get('kind'), event.get('payload') or {}
        if kind == 'round_started':
            active = {'number': payload.get('number'), 'iteration': payload.get('iteration')}
        elif kind == 'parent_selected':
            frontiers.append({**{k: payload[k] for k in FRONTIER_FIELDS if k in payload},
                              'round': active.get('number'), 'iteration': active.get('iteration'),
                              'timestamp': event.get('timestamp')})
        elif kind == 'reflection_input':
            row = {**{k: payload[k] for k in REFLECTION_FIELDS if k in payload},
                   'round': active.get('number'), 'timestamp': event.get('timestamp')}
            reflections.append(row)
            reflection_by_sha[payload.get('sha256')] = row
        elif kind == 'reflection_dispatch' and payload.get('sha256') in reflection_by_sha:
            reflection_by_sha[payload['sha256']]['round'] = payload.get('round')
        elif kind == 'proposal':
            child = payload.get('candidate')
            if not isinstance(child, str):
                continue
            number = active.get('number')
            binding = {'round': number, 'parent_hash': payload.get('parent'), 'status': 'pending',
                       'proposed_at': event.get('timestamp'), 'minibatch': None}
            proposals[number] = (child, binding)
            bindings.setdefault(child, binding)
        elif kind == 'round_completed':
            proposed = proposals.get(payload.get('number'))
            if not proposed or proposed[1]['parent_hash'] != payload.get('parent'):
                continue
            child, binding = proposed
            episodes = payload.get('episode_ids') or []
            start, end = binding.get('proposed_at'), event.get('timestamp')
            rows = [e for e in evaluations if e.get('candidate') == child and e.get('split') == 'train'
                    and not (e.get('evaluation_history') or {}).get('capture_traces', False)
                    and (start is None or e.get('timestamp', 0) >= start)
                    and (end is None or e.get('timestamp', 0) <= end)]
            by_id = defaultdict(list)
            for row in rows:
                by_id[row['episode_id']].append(row.get('score'))
            for episode, count in Counter(episodes).items():
                by_id[episode] = by_id[episode][-count:]
            child_scores = [by_id[episode].pop(0) if by_id[episode] else None for episode in episodes]
            binding.update(status='accepted' if payload.get('accepted') else 'rejected',
                           minibatch={'episodes': episodes, 'parent_scores': payload.get('scores') or [],
                                      'child_scores': child_scores})
            if not payload.get('accepted'):
                binding['reason'] = ('Candidate failed format validation' if payload.get('valid') is False
                                     else 'No strict improvement over the parent on this training minibatch')
            # Preserve a prior accepted occurrence if an identical candidate is
            # proposed again and rejected on a later minibatch.
            if bindings.get(child, {}).get('status') != 'accepted' or binding['status'] == 'accepted':
                bindings[child] = binding
    return bindings, reflections, frontiers


def overview(store, state_path):
    directory = Path(state_path).parent
    raw = read_json(Path(state_path), {})
    manifest = read_json(directory / 'manifest.json', {})
    rules = read_json(directory / 'rules.json', {})
    # run_id is the mixed branch in the experiment state. Do not enumerate or
    # open other run directories merely because they are listed in that state.
    run_id = raw.get('run_id') or raw.get('main_run_id') or raw.get('evolution_run_id')
    if not run_id:
        run_id = next((r for r in raw.get('run_ids', []) if isinstance(r, str) and r != raw.get('code_run_id')), None)
    if run_id == raw.get('code_run_id'):
        run_id = None
    run = store.get_run(run_id) if run_id else {}
    config, progress, result = run.get('config') or {}, run.get('progress') or {}, run.get('result') or {}
    catalog = _episode_catalog(directory, config)
    counts = manifest.get('split_counts') or {}
    totals = {public: len(catalog[public]) or counts.get(internal, 0)
              for public, internal in [('train', 'train'), ('eval', 'validation')]}
    seed_hash = config.get('seed_pipeline_hash')
    selected_hash = run.get('frozen_hash') or result.get('best_hash')
    metadata = {e['episode_id']: e['metadata'] for rows in catalog.values() for e in rows}
    allowed = {public: {e['episode_id'] for e in catalog[public]} for public in catalog}
    evaluations = []
    if run_id:
        evaluations = [e for e in store.list_evaluations(run_id)
                       if e.get('split') in ('train', 'validation') and
                       e.get('episode_id') in allowed['train' if e['split'] == 'train' else 'eval']]
    events = store.list_events(run_id) if run_id else []
    bindings, reflections, frontiers = _rounds(events, evaluations)
    reflections = [r for r in reflections if set(r.get('episode_ids') or []).issubset(allowed['train'])]
    latest = {}
    for row in evaluations:
        latest[(row['candidate'], row['split'], row['episode_id'])] = row
    candidates, edges = [], []
    records = store.list_candidates(run_id) if run_id else []
    specs = {record['hash']: record['spec'] for record in records}
    for record in records:
        ident = record['hash']
        bound = bindings.get(ident) or {}
        parents = record.get('parents') or []
        parent = bound.get('parent_hash') or (parents[0] if parents else None)
        status = 'seed' if ident == seed_hash else bound.get('status', 'accepted' if record.get('gepa_idx') is not None else 'pending')
        games = {public: [_game(latest[(ident, internal, e['episode_id'])], metadata)
                          for e in catalog[public] if (ident, internal, e['episode_id']) in latest]
                 for public, internal in [('train', 'train'), ('eval', 'validation')]}
        entry = {'hash': ident, 'name': record['spec'].get('name', ident[:12]), 'parent_hash': parent,
                 'round': 0 if ident == seed_hash else bound.get('round'), 'status': status,
                 'selected': ident == selected_hash, 'spec': record['spec'],
                 'metrics': {split: _metrics(rows, totals[split]) for split, rows in games.items()},
                 'minibatch': bound.get('minibatch'), 'games': games,
                 'presentation': annotate_candidate(ident, record['spec'], parent_hash=parent,
                                                    parent_spec=specs.get(parent))}
        if bound.get('reason'):
            entry['reason'] = bound['reason']
        candidates.append(entry)
        if parent and parent != ident:
            edges.append({'source': parent, 'target': ident, 'round': entry['round']})
    candidates.sort(key=lambda c: (c['round'] is None, c['round'] or 0, c['hash']))
    paired = []
    if seed_hash and selected_hash and seed_hash != selected_hash:
        for episode in catalog['eval']:
            ident = episode['episode_id']
            before = latest.get((seed_hash, 'validation', ident))
            after = latest.get((selected_hash, 'validation', ident))
            if not before or not after:
                continue
            side = lambda row, candidate: {**_game(row, metadata), 'hash': candidate}
            improved = before.get('status') == after.get('status') == 'ok' and before.get('score') == 0 and after.get('score') == 1
            paired.append({'episode_id': ident, 'metadata': episode['metadata'], 'improved': improved,
                           'before': side(before, seed_hash), 'after': side(after, selected_hash)})
    paired.sort(key=lambda p: (not p['improved'], p['episode_id']))
    model = run.get('proposer_metadata') or {}
    for event in events:
        if event.get('kind') == 'reflection_complete' and (event.get('payload') or {}).get('proposer'):
            model = event['payload']['proposer']
    state_status = run.get('status', 'waiting')
    return scrub_secrets({
        'schema': 'auto_jev.pokemon.overview.v1',
        'state': {'status': state_status, 'phase': 'completed' if state_status == 'completed' else 'evolution' if run_id else 'setup',
                  'profile': manifest.get('profile'), 'updated_at': run.get('updated_at')},
        'run': {'run_id': run_id, 'status': state_status, 'seed_hash': seed_hash, 'selected_hash': selected_hash,
                'rounds_target': config.get('evolution_rounds', raw.get('rounds_target')),
                'rounds_completed': result.get('rounds_completed', progress.get('rounds_completed', 0)),
                'rounds_accepted': result.get('rounds_accepted', progress.get('rounds_accepted', 0)),
                'skipped_batches': result.get('skipped_batches', progress.get('skipped_batches', 0)),
                'converged': result.get('converged', progress.get('converged', False)),
                'stop_reason': result.get('stop_reason', progress.get('stop_reason')),
                'reflection_batch_games': (config.get('reflection') or {}).get('batch_size', rules.get('reflection_batch_games')),
                'reflection_model': model.get('model'), 'reflection_effort': model.get('effort'),
                'reflection_inputs': reflections, 'frontier_selections': frontiers},
        'splits': {'train': {'label': 'Train', 'total': totals['train'], 'coverage_note': 'Observed training subsets only. Coverage differs by candidate; these are not comparable full-training-set scores.'},
                   'eval': {'label': 'Eval', 'total': totals['eval'], 'coverage_note': 'Completed evaluation games only. An unevaluated candidate has no win rate, rather than a zero win rate.'}},
        'episodes': catalog, 'candidates': candidates, 'edges': edges, 'paired_examples': paired,
    })
