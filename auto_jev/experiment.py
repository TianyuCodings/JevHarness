"""Durable research workflow: evolution, freeze, then sealed test evaluation."""
import hashlib
import importlib.metadata
import json
import os
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path

from .file_lock import flock
from .storage import RunStore, StoreError, atomic_write_json, now_iso


def _read(path):
    return json.loads(Path(path).read_text())


def _sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _research_source_hash():
    root = Path(__file__).parent
    files = ('experiment.py', 'evolution.py', 'reflection.py', 'trace_codec.py', 'cli.py')
    contract = {name: _sha(root / name) for name in files}
    contract['gepa_version'] = importlib.metadata.version('gepa')
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()


def _validate_windows(episodes, *, assets, bars_per_asset, window_bars, interval):
    grouped = defaultdict(list)
    ids = set()
    for ep in episodes:
        if ep['id'] in ids:
            raise ValueError('Duplicate episode ID')
        ids.add(ep['id'])
        if ep['interval_seconds'] != interval or len(ep['bars']) != window_bars:
            raise ValueError('Episode size or interval differs from experiment contract')
        grouped[ep['asset']].extend(b['timestamp'] for b in ep['bars'])
    if set(grouped) != set(assets):
        raise ValueError('Episode assets differ from experiment contract')
    ranges = {}
    for asset, timestamps in grouped.items():
        timestamps.sort()
        if len(timestamps) != bars_per_asset or any(b - a != interval for a, b in zip(timestamps, timestamps[1:])):
            raise ValueError('Each asset must have exactly the requested contiguous, unique candles')
        ranges[asset] = (timestamps[0], timestamps[-1] + interval)
    return ranges


def run_experiment(config_path, *, resume=False):
    """Paths are relative to the project working directory, as in the normal CLI."""
    from .crypto import evaluate_baselines
    from .evolution import freeze_run, run_evolution
    from .frozen import evaluate_frozen, source_hash
    from .providers import JevClient, make_proposer

    config_path = Path(config_path).resolve()
    cfg = _read(config_path)
    output = Path(cfg['output_dir']).resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'experiment.lock').open('a') as lock:
        try:
            flock(lock, non_blocking=True)
        except BlockingIOError:
            raise ValueError('This experiment already has an active worker') from None
        path = output / 'state.json'
        hashes = {key: _sha(cfg[key]) for key in ('train', 'validation', 'test', 'proposer_config', 'seed_pipeline')}
        hashes['config'] = _sha(config_path)
        state = _read(path) if path.exists() else None
        if state is not None:
            if (state['input_hashes'] != hashes or state['source_hash'] != source_hash()
                    or state.get('research_source_hash') != _research_source_hash()):
                raise ValueError('Experiment inputs or runtime code changed; use a new output directory')
            if state['status'] == 'completed':
                return state
            if not resume:
                raise ValueError('Experiment already exists; use --resume to continue its checkpoint')
        else:
            if resume:
                raise ValueError('No existing experiment to resume')
            state = {'schema': 'auto_jev.experiment.v1', 'created_at': now_iso(),
                     'status': 'created', 'phase': 'preflight', 'run_id': None,
                     'input_hashes': hashes, 'source_hash': source_hash(),
                     'research_source_hash': _research_source_hash(),
                     'cache_namespace': uuid.uuid4().hex, 'test_completed_ids': []}

        state_lock = threading.RLock()

        def update(**fields):
            with state_lock:
                state.update(fields)
                state['updated_at'] = now_iso()
                atomic_write_json(path, state)

        class TrackingStore(RunStore):
            def create_run(self, *args, **kwargs):
                ident = super().create_run(*args, **kwargs)
                update(run_id=ident)
                return ident

        store = TrackingStore(cfg.get('runs_dir', 'runs'))
        update(status='running', worker_pid=os.getpid(), error=None)
        heartbeat_stop = threading.Event()
        heartbeat_thread = None
        try:
            train, validation = _read(cfg['train']), _read(cfg['validation'])
            assets = cfg['assets']
            bars = cfg['bars_per_split']
            interval = cfg['interval_seconds']
            train_range = _validate_windows(train, assets=assets, bars_per_asset=bars,
                                            window_bars=cfg['train_batch_bars'], interval=interval)
            val_range = _validate_windows(validation, assets=assets, bars_per_asset=bars,
                                          window_bars=bars, interval=interval)
            if any(train_range[a][1] > val_range[a][0] for a in assets):
                raise ValueError('Training overlaps validation')
            jev = JevClient(cache_dir='.cache/jev', cache_namespace=state['cache_namespace'],
                            transport=cfg.get('jev_transport', 'vercel'))
            proposer = make_proposer(_read(cfg['proposer_config']))

            def heartbeat():
                while not heartbeat_stop.wait(10):
                    update(heartbeat_at=now_iso(), jev=dict(jev.stats))

            heartbeat_thread = threading.Thread(target=heartbeat, name='experiment-heartbeat', daemon=True)
            heartbeat_thread.start()
            current = store.get_run(state['run_id']) if state['run_id'] else None
            if not current or current['status'] != 'completed':
                update(phase='evolution')
                result = run_evolution(train, validation, jev=jev, proposer=proposer, store=store,
                    run_name=cfg['name'], seed=cfg.get('seed', 0), costs=cfg['costs'],
                    seed_pipeline=_read(cfg['seed_pipeline']), reflection_batch_size=1,
                    evolution_rounds=cfg['evolution_rounds'], max_metric_calls=None,
                    resume_run_id=state['run_id'] if current else None)
                current = store.get_run(result['run_id'])
                if current['status'] != 'completed':
                    update(status='paused', phase='evolution')
                    return state
            update(phase='freeze')
            artifact = freeze_run(store, state['run_id'])
            artifact_path = output / 'frozen.json'
            # Reuse the original freeze timestamp/hash on test-stage resume.
            if artifact_path.exists():
                existing = _read(artifact_path)
                if existing['spec_hash'] != artifact['spec_hash'] or existing['source_hash'] != artifact['source_hash']:
                    raise ValueError('Selected artifact changed during experiment resume')
                artifact = existing
                store.save_frozen(state['run_id'], artifact)
            else:
                atomic_write_json(artifact_path, artifact)
            update(artifact_path=str(artifact_path), artifact_hash=artifact['artifact_hash'], phase='test')

            # Test observations are first parsed here, after candidate selection and freeze.
            if _sha(cfg['test']) != hashes['test']:
                raise ValueError('Sealed test data changed during evolution')
            test = _read(cfg['test'])
            test_range = _validate_windows(test, assets=assets, bars_per_asset=bars,
                                          window_bars=bars, interval=interval)
            if any(val_range[a][1] > test_range[a][0] for a in assets):
                raise ValueError('Validation overlaps test')
            if {ep['id'] for ep in test} & {ep['id'] for ep in train + validation}:
                raise ValueError('Test IDs overlap search data')
            summaries_path = output / 'test_results.json'
            summaries = _read(summaries_path) if summaries_path.exists() else []
            completed = set()
            by_episode = {row['episode_id']: row for row in summaries}
            archived = {row['episode_id']: row for row in store.list_holdout(state['run_id'])}
            for episode in test:
                update(current_test_episode=episode['id'])
                metrics = by_episode.get(episode['id'])
                if metrics is None:
                    trace_path = store._trace_path(state['run_id'], 'holdout', artifact['spec_hash'], episode['id'])
                    recovered = trace_path.exists()
                    if recovered:
                        # A complete trace is already the durable evaluation commit.
                        # Recover metrics without rerunning Jev after a later failure.
                        try:
                            result = store.load_trace_summary(state['run_id'], 'holdout', artifact['spec_hash'], episode['id'])
                        except StoreError:
                            # A crash can occur between publishing complete JSON
                            # and publishing its byte index. Re-index locally;
                            # never repeat a completed remote evaluation for this.
                            result = store.load_trace(state['run_id'], 'holdout', artifact['spec_hash'], episode['id'])
                            if result.get('episode_id') != episode['id'] or result.get('score') is None or result.get('status') == 'error':
                                raise ValueError('Unindexed test trace is not a completed evaluation')
                            store.save_trace(state['run_id'], 'holdout', artifact['spec_hash'], episode['id'], result)
                        elapsed = None
                        if result.get('score') is None or result.get('status') == 'error':
                            raise ValueError('Archived test trace is not a completed evaluation')
                    else:
                        started = time.monotonic()
                        try:
                            result = evaluate_frozen(artifact, [episode], jev)[0]
                        except Exception as exc:
                            partial = getattr(exc, 'partial_result', None)
                            if isinstance(partial, dict):
                                failed_id = f"{episode['id']}-failed-{uuid.uuid4().hex[:8]}"
                                store.save_trace(state['run_id'], 'holdout', artifact['spec_hash'], failed_id, partial)
                                update(test_failure_trace=str(store._trace_path(
                                    state['run_id'], 'holdout', artifact['spec_hash'], failed_id)))
                            raise
                        elapsed = time.monotonic() - started
                        store.save_trace(state['run_id'], 'holdout', artifact['spec_hash'], episode['id'], result)
                    metrics = {key: result.get(key) for key in ('episode_id', 'asset', 'score', 'net_profit',
                               'return_pct', 'max_drawdown_pct', 'initial_cash', 'final_equity', 'fees_paid', 'decisions')}
                    metrics.update(elapsed_seconds=elapsed, recovered_from_trace=recovered,
                                   artifact_hash=artifact['artifact_hash'])
                    metrics['baselines'] = {
                        name: {key: value.get(key) for key in ('score', 'net_profit', 'return_pct', 'max_drawdown_pct')}
                        for name, value in evaluate_baselines(episode, **cfg['costs']).items()}
                    summaries.append(metrics)
                    atomic_write_json(summaries_path, summaries)
                if metrics['artifact_hash'] != artifact['artifact_hash']:
                    raise ValueError('Test summary belongs to a different frozen artifact')
                previous = archived.get(episode['id'])
                if previous is None:
                    store.append_holdout(state['run_id'], metrics)
                elif previous != metrics:
                    raise ValueError('Test summary differs from archived holdout metrics')
                completed.add(episode['id'])
                update(test_completed_ids=sorted(completed), jev=dict(jev.stats))
            update(status='completed', phase='completed', current_test_episode=None,
                   test_results_path=str(summaries_path), completed_at=now_iso())
            return state
        except BaseException as exc:
            update(status='failed', error=f'{type(exc).__name__}: {str(exc)[:500]}')
            raise
        finally:
            heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=2)
