import copy
import json, time, math
import uuid
from datetime import datetime, timezone
from pathlib import Path
import httpx
from .crypto import EpisodeExecutionError, _Book, _error_evidence, _parse_target, _run_evidence
from .file_lock import flock
from .data import fetch_coinbase, fetch_rss
from .runtime import PipelineRuntime
from .spec import spec_hash, validate_spec
from .storage import atomic_write_json
from .observations import build_observation
from .frozen import validate_artifact

HOUR, LOOKBACK_BARS, NEWS_KEEP, NEWS_CHARS = 3600, 72, 20, 1500
TICKER_URL = 'https://api.exchange.coinbase.com/products/{asset}/ticker'


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _ts(v):
    return float(v) if isinstance(v, (int, float)) else datetime.fromisoformat(str(v).replace('Z', '+00:00')).timestamp()


def _closed_bars(episode, cutoff):
    raw = episode.get('bars', episode.get('history', [])) if isinstance(episode, dict) else getattr(episode, 'bars', episode)
    out = []
    for b in raw:
        ts = _ts(b.get('timestamp', b.get('time')))
        if ts + HOUR <= cutoff:
            out.append({'timestamp': ts, 'open': float(b['open']), 'high': float(b['high']), 'low': float(b['low']),
                        'close': float(b['close']), 'volume': float(b.get('volume', 0.0))})
    out.sort(key=lambda b: b['timestamp'])
    return out[-LOOKBACK_BARS:]


def _merge_news(old, fetched, seen_at):
    by_id = {n['id']: n for n in old}
    for item in fetched:
        nid = str(item.get('id') or item.get('link') or item.get('title'))
        full = str(item.get('content') or item.get('summary') or '')
        prev, body = by_id.get(nid), full[:NEWS_CHARS]
        headline = str(item.get('headline', item.get('title', '')))[:300]
        changed = not prev or prev['content'] != body or prev.get('headline') != headline
        by_id[nid] = {'id': nid, 'headline': headline, 'content': body, 'input_truncated': bool(item.get('content_truncated') or len(full) > NEWS_CHARS),
                      'content_chars': len(full), 'published_at': item.get('published_at'), 'source': item.get('source'), 'timing_quality': 'received', 'last_seen': seen_at,
                      'available_at': seen_at if changed else prev['available_at'],
                      'version': prev['version'] + 1 if prev and changed else (prev['version'] if prev else 1)}
    return sorted(by_id.values(), key=lambda n: n['available_at'])[-NEWS_KEEP:]


def _ticker(asset):
    r = httpx.get(TICKER_URL.format(asset=asset), timeout=10.0, headers={'User-Agent': 'auto_jev-paper'})
    r.raise_for_status()
    received, d = time.time(), r.json()
    bid, ask, last = (float(d[k]) if d.get(k) not in (None, '') else None for k in ('bid', 'ask', 'price'))
    mid = (bid + ask) / 2 if bid and ask else last
    if not mid or not math.isfinite(mid) or mid <= 0:
        raise RuntimeError('ticker returned no usable quote')
    return {'source': 'coinbase public ticker (anonymous GET)', 'bid': bid, 'ask': ask, 'last': last, 'mid': mid,
            'quote_time': d.get('time'), 'received_at': received,
            'fill_model': 'simulated: mid quote, frozen slippage/fees applied by _Book; no real order placed'}


def _load(path, h, asset, costs):
    if path.exists():
        st = json.loads(path.read_text())
        if st.get('artifact_hash') != h or st.get('asset') != asset or st.get('costs') != costs:
            raise ValueError('state file belongs to another artifact/asset; refusing to mix')
        return st
    return {'schema': 'auto_jev.paper.v1', 'mode': 'paper_continuous', 'simulated': True, 'artifact_hash': h, 'asset': asset,
            'costs': costs, 'created_at': time.time(), 'cash': costs['initial_cash'], 'quantity': 0.0, 'fees_paid': 0.0,
            'memory': {}, 'bar': None, 'last_decision': None, 'news': [], 'trades': [], 'equity': [], 'traces': [],
            'metadata': {'execution': 'simulated fills at public mid plus frozen slippage/fees; no real orders placed',
                         'liquidation': 'none; continuous account marked at latest mid (historical eval windows liquidate at window end)',
                         'llm': 'none in frozen pipeline', 'credentials': 'Jev authentication is used; no credentials stored'}}


class PaperExecutionError(EpisodeExecutionError):
    """A failed paper attempt; its account state was not committed."""


def _account_snapshot(state):
    return copy.deepcopy({key: state.get(key) for key in
                          ('cash', 'quantity', 'fees_paid', 'memory', 'bar', 'last_audit_path')})


def _step(spec, jev, h, costs, asset, path, rss_url):
    state = _load(path, h, asset, costs)
    clock_bar = (int(time.time()) // HOUR - 1) * HOUR
    if state['bar'] is not None and state['bar'] >= clock_bar:
        return {'skipped': True, 'reason': 'latest closed bar already decided', 'bar': state['bar'], 'state': state}
    cutoff = clock_bar + HOUR
    attempt_id = uuid.uuid4().hex
    audit_directory = path.parent / (path.stem + '.traces')
    audit_path = audit_directory / (str(clock_bar) + '-' + attempt_id + '.json')
    failure_path = path.with_suffix('.failure.json')
    phase, bar, obs, run, target, quote, book = 'market', None, None, None, None, None, None
    memory_before = copy.deepcopy(state['memory'])
    obs_ts, decision_ts = None, None
    try:
        bars = _closed_bars(fetch_coinbase(asset, _iso(cutoff - LOOKBACK_BARS * HOUR), _iso(cutoff), HOUR), cutoff)
        if not bars:
            raise RuntimeError('no closed hourly bars returned')
        bar = bars[-1]
        if state['bar'] is not None and bar['timestamp'] <= state['bar']:
            return {'skipped': True, 'reason': 'no new closed bar published yet', 'bar': state['bar'], 'state': state}
        phase = 'news'
        news = _merge_news(state['news'], fetch_rss(rss_url), time.time()) if rss_url else state['news']
        market_cutoff, obs_ts = bar['timestamp'] + HOUR, time.time()
        input_cutoff = obs_ts
        phase = 'observation'
        obs = build_observation(asset, bars, news,
            {"cash": state['cash'], "quantity": state['quantity'], "equity": state['cash']+state['quantity']*bar['close']}, obs_ts)
        phase = 'runtime'
        run = PipelineRuntime(spec, jev).run(copy.deepcopy(obs), copy.deepcopy(memory_before))
        phase = 'output'
        target = _parse_target(run['output'])
        decision_ts = time.time()
        phase = 'quote'
        quote = _ticker(asset)
        phase = 'execution'
        book = _Book(state['cash'], costs['fee_bps'] / 1e4, costs['slippage_bps'] / 1e4)
        book.qty, book.fees_paid = state['quantity'], state['fees_paid']
        book.rebalance(target, quote['mid'], quote['received_at'])
        fills = [dict(t, simulated=True) if isinstance(t, dict) else {'raw': t, 'simulated': True} for t in book.trades]
        equity = book.equity(quote['mid'])
        decision = {'bar': bar['timestamp'], 'input_cutoff': input_cutoff, 'obs_timestamp': obs_ts, 'decision_timestamp': decision_ts,
                    'target': target, 'quote': quote, 'fills': fills, 'fee': book.fees_paid - state['fees_paid'],
                    'exec_price': fills[-1].get('price', quote['mid']) if fills else None, 'equity': equity, 'simulated': True,
                    'audit_path': str(audit_path),
                    'latency_ms': {'pipeline': run.get('elapsed_ms'), 'bar_close_to_decision': (decision_ts - market_cutoff) * 1000,
                                   'decision_to_quote': (quote['received_at'] - decision_ts) * 1000}}
        trace = {**copy.deepcopy(decision), **_run_evidence(run, memory_before),
                 'timestamp': obs_ts, 'status': 'ok', 'obs': copy.deepcopy(obs),
                 'runtime_trace': copy.deepcopy(run.get('trace', []))}
        next_state = copy.deepcopy(state)
        next_state.update(cash=book.cash, quantity=book.qty, fees_paid=book.fees_paid, memory=copy.deepcopy(run.get('memory')),
                          bar=bar['timestamp'], last_decision=decision, news=news, updated_at=decision_ts,
                          audit_directory=str(audit_directory), last_audit_path=str(audit_path))
        next_state['trades'] = (state['trades'] + fills)[-2000:]
        next_state['equity'] = (state['equity'] + [{'timestamp': quote['received_at'], 'equity': equity, 'mid': quote['mid'], 'cash': book.cash,
                                                  'quantity': book.qty, 'fees_paid': book.fees_paid}])[-2000:]
        next_state['traces'] = (state['traces'] + [trace])[-200:]
        audit = {'schema': 'auto_jev.paper.step.v1', 'attempt_id': attempt_id, 'artifact_hash': h, 'asset': asset,
                 'costs': copy.deepcopy(costs), 'status': 'ok', 'simulated': True, 'trace': trace,
                 'account_before': _account_snapshot(state), 'account_after': _account_snapshot(next_state),
                 'parent_audit_path': state.get('last_audit_path'), 'state_path': str(path)}
        if not state.get('audit_directory'):
            # Preserve all evidence still present in an older account before its
            # rolling display windows begin dropping decisions under this format.
            audit['initial_state'] = copy.deepcopy(state)
        phase = 'audit'
        atomic_write_json(audit_path, audit)
        phase = 'account_commit'
        # Commit last. Any earlier exception leaves the previous account file
        # unchanged; a failed commit turns the prepared audit into a failure.
        atomic_write_json(path, next_state)
        return {'skipped': False, 'bar': bar['timestamp'], 'target': target, 'decision': decision,
                'state': next_state, 'audit_path': str(audit_path)}
    except Exception as error:
        runtime_partial = getattr(error, 'partial_result', None)
        runtime_partial = runtime_partial if isinstance(runtime_partial, dict) else run
        failed = {**_run_evidence(runtime_partial, memory_before), 'memory': copy.deepcopy(memory_before),
                  'bar': bar['timestamp'] if bar else None, 'timestamp': obs_ts, 'obs_timestamp': obs_ts,
                  'decision_timestamp': decision_ts, 'obs': copy.deepcopy(obs), 'target': target,
                  'quote': copy.deepcopy(quote), 'status': 'error', 'phase': phase, 'simulated': True,
                  'memory_committed': False, 'account_committed': False, 'audit_path': str(audit_path),
                  'error': _error_evidence(error)}
        failed['runtime_trace'] = copy.deepcopy(failed['trace'])
        partial = {'schema': 'auto_jev.paper.failure.v1', 'attempt_id': attempt_id, 'artifact_hash': h,
                   'asset': asset, 'costs': copy.deepcopy(costs), 'status': 'error', 'simulated': True,
                   'account_committed': False, 'state_path': str(path), 'state': copy.deepcopy(state),
                   'memory': copy.deepcopy(memory_before), 'trades': copy.deepcopy(state['trades']),
                   'equity_curve': copy.deepcopy(state['equity']), 'traces': copy.deepcopy(state['traces']) + [failed],
                   'failed_decision': failed, 'runtime_partial_result': copy.deepcopy(runtime_partial),
                   'attempted_trades': copy.deepcopy(book.trades) if book is not None else [],
                   'audit_directory': str(audit_directory), 'parent_audit_path': state.get('last_audit_path'),
                   'failure_path': str(failure_path), 'failure': {'phase': phase, **_error_evidence(error)}}
        wrapped = PaperExecutionError(f'Paper execution failed during {phase}: {error}', cause=error, partial_result=partial)
        # Every failed attempt has its own durable file; the sibling failure
        # file is only a convenient pointer to the most recent failure.
        for destination in (audit_path, failure_path):
            try:
                atomic_write_json(destination, wrapped.partial_result)
            except Exception as audit_error:
                wrapped.partial_result.setdefault('audit_write_errors', []).append(
                    {'path': str(destination), 'type': type(audit_error).__name__})
        raise wrapped from error


def paper_step(artifact, jev, *, asset='BTC-USD', state_path='artifacts/paper.json', rss_url=None):
    validate_artifact(artifact, jev)
    spec = artifact['spec']
    validate_spec(spec)
    h = spec_hash(spec)
    if artifact.get('spec_hash') != h:
        raise ValueError('artifact spec_hash does not match spec')
    if artifact.get('task_id') != 'crypto_spot':
        raise ValueError('paper trading requires task crypto_spot')
    raw_costs = artifact.get('costs') or spec.get('costs') or {}
    costs = {k: float(raw_costs[k]) for k in ('initial_cash', 'fee_bps', 'slippage_bps')}
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.parent / (path.name + '.lock'), 'w') as lock:
        flock(lock)
        return _step(spec, jev, artifact['artifact_hash'], costs, asset, path, rss_url)
