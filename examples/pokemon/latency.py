"""Latency from archived mixed-policy decisions, without executing a policy."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import math
import os
import threading

from auto_jev.storage import StoreError, TraceRevisionError, scrub_secrets
from .presentation import overview


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _stats(values):
    values = sorted(value for value in values if _number(value))
    if not values:
        return {'count': 0, 'median_ms': None, 'p95_ms': None, 'mean_ms': None}

    def percentile(fraction):
        position = (len(values) - 1) * fraction
        lower, upper = math.floor(position), math.ceil(position)
        return values[lower] + (values[upper] - values[lower]) * (position - lower)

    return {'count': len(values), 'median_ms': percentile(.5),
            'p95_ms': percentile(.95), 'mean_ms': math.fsum(values) / len(values)}


def _binding(stat):
    return {key: getattr(stat, key) for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns')}


def _compact(decision):
    """Keep timing fields only, never a response body, code, or observation."""
    if not isinstance(decision, dict):
        raise StoreError('Archived decision is not an object')
    trace = decision.get('trace')
    nodes = []
    if isinstance(trace, list):
        for node in trace:
            if not isinstance(node, dict):
                raise StoreError('Archived node is not an object')
            response = node.get('response')
            response = response if isinstance(response, dict) else {}
            nodes.append({key: node.get(key) for key in ('id', 'kind', 'status', 'elapsed_ms')} | {
                'cache_hit': response.get('cache_hit'), 'response_ms': response.get('elapsed_ms')})
    return {'status': decision.get('status'), 'decision_ms': decision.get('decision_ms'),
            'has_trace': isinstance(trace, list), 'nodes': nodes}


class LatencyArchive:
    """Cache compact timing samples against the actual open trace revision.

    The first request reads one indexed decision at a time. Later requests read
    only the small metadata/index files while the trace binding is unchanged.
    This cache is per application, bounded, and never writes research artifacts.
    """
    def __init__(self, store, state_path, *, max_cached_games=256):
        self.store, self.state_path = store, state_path
        self.max_cached_games = max_cached_games
        self._cache = OrderedDict()
        self._lock = threading.RLock()

    def _game(self, run_id, split, candidate, episode):
        path = self.store._trace_path(run_id, split, candidate, episode)
        if not path.exists():
            return None
        stream, index, legacy = self.store._paged_trace(path)
        try:
            revision = index['revision']
            binding = index['binding']
            # Include the entire cheap index: a changed offset list must not
            # retrieve samples cached against a previous index of the same file.
            index_digest = hashlib.sha256(json.dumps(index, sort_keys=True).encode()).hexdigest()
            key = (str(path.resolve()), revision, index_digest)
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                samples = cached
            elif legacy is not None:
                records = legacy.get('traces', [])
                if not isinstance(records, list):
                    raise StoreError('Archived trace list is invalid')
                samples = [_compact(record) for record in records]
            else:
                samples, previous_end = [], 0
                offsets = index.get('decisions')
                if not isinstance(offsets, list):
                    raise StoreError('Invalid trace byte index')
                for entry in offsets:
                    if (not isinstance(entry, list) or len(entry) != 2
                            or any(type(value) is not int for value in entry)):
                        raise StoreError('Invalid trace byte index')
                    offset, length = entry
                    if offset < previous_end or length < 1 or offset + length > binding['st_size']:
                        raise StoreError('Invalid trace byte index')
                    stream.seek(offset)
                    raw = stream.read(length)
                    if len(raw) != length:
                        raise TraceRevisionError('Trace changed while collecting latency')
                    try:
                        samples.append(_compact(json.loads(raw)))
                    except (ValueError, UnicodeError) as exc:
                        raise StoreError('Archived decision is not valid JSON') from exc
                    previous_end = offset + length
            # fstat catches in-place edits; stat catches replacement of the path
            # while this descriptor still points at the previous inode.
            try:
                stable = _binding(os.fstat(stream.fileno())) == binding == _binding(path.stat())
            except OSError as exc:
                raise TraceRevisionError('Trace changed while collecting latency') from exc
            if not stable:
                raise TraceRevisionError('Trace changed while collecting latency')
            if cached is None:
                # Drop old revisions of the same game instead of retaining them.
                for old in [old for old in self._cache if old[0] == key[0]]:
                    del self._cache[old]
                self._cache[key] = samples
                while len(self._cache) > self.max_cached_games:
                    self._cache.popitem(last=False)
            return {'revision': revision, 'samples': samples}
        finally:
            stream.close()

    def report(self, split='eval'):
        if split not in ('train', 'eval'):
            raise StoreError('Latency is available for Train or Eval only')
        # One cold scan per app even if browsers request both panels together.
        with self._lock:
            public = overview(self.store, self.state_path)
            run = public['run']
            run_id = run['run_id']
            candidates = []
            internal = 'validation' if split == 'eval' else 'train'
            for candidate in public['candidates']:
                samples, revisions, missing = [], [], []
                for game in candidate['games'][split]:
                    episode = game['episode_id']
                    archived = self._game(run_id, internal, candidate['hash'], episode)
                    if archived is None:
                        missing.append(episode)
                    else:
                        samples.extend(archived['samples'])
                        revisions.append({'episode_id': episode, 'revision': archived['revision']})
                metrics = _candidate_metrics(samples, candidate['spec'].get('nodes', []))
                candidates.append({
                    'hash': candidate['hash'], 'name': candidate['name'],
                    'selected': candidate['hash'] == run['selected_hash'],
                    'initial': candidate['hash'] == run['seed_hash'],
                    'coverage': {'games': len(revisions), 'total': public['splits'][split]['total'],
                                 'missing_games': len(missing), 'missing_episode_ids': missing},
                    **metrics, 'revisions': revisions})
            return scrub_secrets({
                'schema': 'auto_jev.pokemon.latency.v1', 'run_id': run_id, 'split': split,
                'selected_hash': run['selected_hash'], 'initial_hash': run['seed_hash'],
                'methodology': {
                    'decision': 'Measured decision_ms for successful decisions with every Jev cache flag explicitly false. Decisions containing cache hits, unknown flags, failures, missing timings, or no Jev calls are excluded and counted separately.',
                    'jev': 'Client call elapsed_ms from uncached successful Jev responses, including client and network overhead. Historical request_elapsed_ms is never substituted. Calls counts all observed Jev node attempts, including cache hits and failures.',
                    'python': 'Successful Python node elapsed_ms, including process startup, isolation and data copying. Node statistics use all observed decisions; Jev node timings exclude cached or unknown responses.',
                    'parallel': 'Node timings may overlap. Their sum is not decision latency. Decision latency excludes battle-engine turns and reflection-model time.',
                    'cache': 'Uncached means no local Jev response-cache hit was recorded. It does not establish whether the upstream service used a cache.',
                    'percentile': 'Median and P95 use linear interpolation between ordered samples. All values are milliseconds.',
                    'source': 'Latest archived result per candidate and episode, restricted to this mixed run and the requested Train/Eval split. Coverage and cache exclusions differ; these are observed timings, not a controlled speed benchmark.',
                }, 'candidates': candidates})


def _candidate_metrics(samples, spec_nodes):
    expected = [(node.get('id'), node.get('kind')) for node in spec_nodes]
    by_node = OrderedDict((key, {'values': [], 'cache_hits': 0, 'unknown_cache': 0, 'errors': 0})
                          for key in expected)
    decision_values, python_values, jev_values = [], [], []
    decision_counts = dict.fromkeys(('observed', 'excluded_cached', 'excluded_unknown',
                                   'excluded_failed', 'excluded_missing_timing', 'without_jev'), 0)
    jev_counts = dict.fromkeys(('calls', 'uncached_calls', 'cache_hits', 'unknown_cache', 'errors'), 0)
    for sample in samples:
        decision_counts['observed'] += 1
        nodes = sample['nodes']
        jev = [node for node in nodes if node['kind'] == 'jev']
        shape_known = (sample['has_trace'] and len(nodes) == len(expected)
                       and set((node['id'], node['kind']) for node in nodes) == set(expected))
        if sample['status'] != 'ok' or any(node['status'] != 'ok' for node in nodes):
            decision_counts['excluded_failed'] += 1
        elif not shape_known or any(type(node['cache_hit']) is not bool for node in jev):
            decision_counts['excluded_unknown'] += 1
        elif not jev:
            decision_counts['without_jev'] += 1
        elif any(node['cache_hit'] for node in jev):
            decision_counts['excluded_cached'] += 1
        elif not _number(sample['decision_ms']):
            decision_counts['excluded_missing_timing'] += 1
        else:
            decision_values.append(sample['decision_ms'])
        for node in nodes:
            key = (node['id'], node['kind'])
            if key not in by_node:
                continue
            bucket = by_node[key]
            if node['kind'] == 'jev':
                # Blocked/cancelled nodes did not make a provider call.
                if node['status'] in ('blocked', 'cancelled'):
                    continue
                jev_counts['calls'] += 1
            if node['status'] != 'ok':
                bucket['errors'] += 1
                if node['kind'] == 'jev':
                    jev_counts['errors'] += 1
                continue
            if node['kind'] == 'jev':
                if node['cache_hit'] is True:
                    jev_counts['cache_hits'] += 1
                    bucket['cache_hits'] += 1
                    continue
                if node['cache_hit'] is not False:
                    jev_counts['unknown_cache'] += 1
                    bucket['unknown_cache'] += 1
                    continue
                jev_counts['uncached_calls'] += 1
                if _number(node['response_ms']):
                    jev_values.append(node['response_ms'])
            if _number(node['elapsed_ms']):
                bucket['values'].append(node['elapsed_ms'])
                if node['kind'] == 'python':
                    python_values.append(node['elapsed_ms'])
    return {'decision': {**_stats(decision_values), **decision_counts},
            'jev': {**_stats(jev_values), **jev_counts}, 'python': _stats(python_values),
            'nodes': [{'id': key[0], 'kind': key[1], **_stats(value['values']),
                       **{k: v for k, v in value.items() if k != 'values'}}
                      for key, value in by_node.items()]}
