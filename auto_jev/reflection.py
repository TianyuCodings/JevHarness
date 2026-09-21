"""Lossless reflection inputs and an auditable, explicit context boundary."""
import copy
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .providers import ProposerError
from .trace_codec import pack_json

DEFAULT_MAX_PROMPT_BYTES = 1_000_000


class ReflectionContextError(ProposerError):
    pass


def full_feedback(eval_batch):
    if eval_batch.trajectories is None or not (len(eval_batch.outputs) == len(eval_batch.scores) == len(eval_batch.trajectories)):
        raise ValueError('Full reflection requires one complete trajectory per evaluated episode')
    records = []
    for trajectory, output, score in zip(eval_batch.trajectories, eval_batch.outputs, eval_batch.scores):
        record = copy.deepcopy(trajectory)
        # Keep arbitrary task trajectory fields; no sampling, summarization or slicing.
        record['result'] = copy.deepcopy(output)
        record['score'] = score
        records.append(record)
    return {'pipeline': records}


def coverage(feedback):
    records = feedback['pipeline']
    return {'complete': True, 'episode_ids': [r['episode_id'] for r in records],
            'trace_count': sum(len(r['result'].get('traces', [])) for r in records),
            'node_count': sum(len(t.get('trace', [])) for r in records for t in r['result'].get('traces', []))}


def prompt_limit(proposer):
    limit = getattr(proposer, 'config', {}).get('max_prompt_bytes', DEFAULT_MAX_PROMPT_BYTES)
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError('max_prompt_bytes must be a positive integer')
    return limit


def atomic_text(path, text):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.reflection-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def archive_prompt(store, run_id, parent, payload, limit, encoding='plain'):
    if encoding not in ('plain', 'lossless_dag'):
        raise ValueError('reflection_encoding must be plain or lossless_dag')
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))
    transmitted = payload
    if encoding == 'lossless_dag':
        transmitted = {**payload, 'training_feedback': pack_json(payload['training_feedback'])}
    prompt = json.dumps(transmitted, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':'))
    encoded = prompt.encode('utf-8'); sha = hashlib.sha256(encoded).hexdigest()
    number = len(store.list_events(run_id, 'reflection_input')) + 1
    relative = f'reflections/{number:04d}-{sha}.prompt.json'
    raw_relative = relative.replace('.prompt.json', '.full.json') if encoding != 'plain' else relative
    if encoding != 'plain':
        atomic_text(store.run_dir(run_id) / raw_relative, raw)
    atomic_text(store.run_dir(run_id) / relative, prompt)
    record = {**coverage(payload['training_feedback']), 'parent': parent, 'prompt_bytes': len(encoded),
              'sha256': sha, 'path': relative, 'max_prompt_bytes': limit,
              'dispatched': False, 'context_fits': len(encoded) <= limit,
              'encoding': encoding, 'full_path': raw_relative,
              'full_bytes': len(raw.encode('utf-8')),
              'full_sha256': hashlib.sha256(raw.encode('utf-8')).hexdigest()}
    store.append_event(run_id, 'reflection_input', record)
    if len(encoded) > limit:
        raise ReflectionContextError(f'Full reflection input is {len(encoded)} bytes, exceeding max_prompt_bytes={limit}; '
                                     f'no text was truncated and no model was called. Complete input: {relative}')
    return prompt, record
