"""Read-only local Pokémon research dashboard and client-side replay renderer."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import stat
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from auto_jev.storage import RunStore, StoreError, TraceRevisionError, read_json, scrub_secrets
from .presentation import overview
from .latency import LatencyArchive

STATE_FIELDS = {
    'schema', 'phase', 'stage', 'status', 'run_id', 'main_run_id', 'pilot_run_id',
    'evolution_run_id', 'code_run_id', 'run_ids', 'rounds_target', 'rounds_completed', 'error',
    'updated_at', 'active_branch', 'test_released', 'frozen_path', 'synthetic', 'label',
    'profile', 'split_counts', 'reflection_batch_games', 'skip_perfect_score',
}


def create_app(root: str | Path = 'runs', state_path: str | Path = 'artifacts/pokemon_expanded_v2/state.json'):
    store = RunStore(root)
    state_path = Path(state_path)
    app = FastAPI(title='Auto_Jev Pokémon research', docs_url=None, redoc_url=None)
    latency = LatencyArchive(store, state_path)

    def state():
        # Counts and rules are public preparation metadata; never open game
        # definitions or final results merely to render the experiment scope.
        manifest = read_json(state_path.parent / 'manifest.json', {})
        rules = read_json(state_path.parent / 'rules.json', {})
        prepared = {k: manifest[k] for k in ('profile', 'split_counts') if k in manifest}
        prepared.update({k: rules[k] for k in ('reflection_batch_games', 'skip_perfect_score') if k in rules})
        raw = read_json(state_path, {})
        return scrub_secrets({**prepared, **{k: v for k, v in raw.items() if k in STATE_FIELDS}})

    def allowed_run(run_id):
        current = state()
        ids = {current.get(k) for k in ('run_id', 'main_run_id', 'pilot_run_id', 'evolution_run_id', 'code_run_id')}
        ids.update(x for x in current.get('run_ids', []) if isinstance(x, str))
        if run_id not in ids:
            raise HTTPException(404, 'Run is not part of this Pokémon experiment')
        return store.get_run(run_id)

    def released(run_id, run=None):
        current = state()
        if current.get('test_released') is not True:
            return False
        run = run or allowed_run(run_id)
        frozen = store.get_frozen(run_id)
        return bool(frozen and frozen.get('spec_hash') and frozen.get('spec_hash') == run.get('frozen_hash'))

    def split_for(run_id, split, candidate=None):
        run = allowed_run(run_id)
        if split in ('train', 'validation'):
            return split
        if split in ('test', 'holdout'):
            if not released(run_id, run):
                raise HTTPException(403, 'Test games remain sealed until freeze and test release')
            if candidate and candidate != run.get('frozen_hash'):
                raise HTTPException(403, 'Released test games belong to the frozen candidate only')
            return 'holdout'
        raise HTTPException(400, 'Unknown split')

    def game_metadata(run_id, split, candidate, episode):
        actual = split_for(run_id, split, candidate)
        path = store._trace_path(run_id, actual, candidate, episode)
        stream, index, legacy = store._paged_trace(path)
        try:
            if legacy is not None:
                result = {k: v for k, v in legacy.items() if k != 'traces'}
                decisions = legacy.get('traces', [])
            else:
                offsets = index.get('decisions', [])
                if offsets:
                    first = offsets[0][0]
                    end = offsets[-1][0] + offsets[-1][1]
                    size = index['binding']['st_size']
                    if not (0 <= first <= end <= size):
                        raise StoreError('Invalid trace byte index')
                    stream.seek(0)
                    prefix = stream.read(first)
                    stream.seek(end)
                    result = json.loads(prefix + stream.read())
                else:
                    result = json.load(stream)
                result.pop('traces', None)
                decisions = index.get('summary', {}).get('traces', [])
            if {k: getattr(os.fstat(stream.fileno()), k) for k in index['binding']} != index['binding']:
                raise TraceRevisionError('Game trace changed; refresh before selecting a turn')
            history = result.get('action_history')
            if not isinstance(history, list):
                history = [{k: row[k] for k in ('turn', 'phase', 'action', 'elapsed_ms', 'decision_ms', 'status') if k in row} | {'decision_index': i} for i, row in enumerate(decisions)]
            result.update(action_history=history, trace_count=len(decisions), trace_revision=index['revision'])
            return scrub_secrets(result)
        finally:
            stream.close()

    @app.exception_handler(StoreError)
    async def bad_artifact(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=404)

    @app.exception_handler(TraceRevisionError)
    async def stale_artifact(request, exc):
        return JSONResponse({'detail': str(exc)}, status_code=409)

    @app.get('/')
    def home():
        return FileResponse(Path(__file__).parent / 'static' / 'index.html')

    @app.get('/showcase/{filename}')
    def showcase_asset(filename: str):
        # Only captured public replay images and their provenance manifest.
        if filename != 'manifest.json' and not re.fullmatch(r'[a-z0-9][a-z0-9._-]*\.png', filename):
            raise HTTPException(404, 'Unknown showcase asset')
        return public_asset(('static', 'showcase'), filename,
                            'application/json' if filename == 'manifest.json' else 'image/png')

    @app.get('/assets/{filename}')
    def player_asset(filename: str):
        media = {'highlight-demo.js': 'text/javascript', 'highlight-replay.js': 'text/javascript',
                 'highlight-demo.css': 'text/css'}
        if filename not in media:
            raise HTTPException(404, 'Unknown player asset')
        return public_asset(('static',), filename, media[filename])

    def public_asset(parts, filename, media_type):
        descriptors = []
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory_fd = os.open(Path(__file__).parent, flags)
            descriptors.append(directory_fd)
            for part in parts:
                directory_fd = os.open(part, flags, dir_fd=directory_fd)
                descriptors.append(directory_fd)
            file_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
            descriptors.append(file_fd)
            if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                raise HTTPException(404, 'Showcase asset is unavailable')
            with os.fdopen(file_fd, 'rb', closefd=False) as stream:
                data = stream.read()
        except OSError as exc:
            raise HTTPException(404, 'Showcase asset is unavailable') from exc
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        return Response(data, media_type=media_type,
                        headers={'X-Content-Type-Options': 'nosniff', 'Cache-Control': 'no-cache'})

    @app.get('/api/state')
    def get_state():
        current = state()
        current.setdefault('phase', 'setup')
        current.setdefault('status', 'waiting')
        current['test_released'] = bool(current.get('run_id') and released(current['run_id']))
        return current

    @app.get('/api/overview')
    def get_overview():
        return overview(store, state_path)

    @app.get('/api/latency')
    def get_latency(split: str = 'eval'):
        if split not in ('eval', 'train'):
            raise HTTPException(400, 'Latency is available for Train or Eval only')
        return latency.report(split)

    @app.get('/api/test-summary')
    def test_summary():
        current = state()
        run_id = current.get('run_id')
        if not run_id or not released(run_id):
            raise HTTPException(403, 'Final comparisons remain sealed until freeze and test release')
        summary = read_json(state_path.parent / 'summary.json', None)
        if not isinstance(summary, dict):
            raise HTTPException(404, 'The final comparison summary has not been archived')
        return scrub_secrets(summary)

    @app.get('/api/run/{run_id}')
    def get_run(run_id: str, split: str = 'validation'):
        actual = split_for(run_id, split)
        run = allowed_run(run_id)
        # Never expose sealed episode definitions through the dashboard metadata.
        config = dict(run.get('config') or {})
        config['episodes'] = [e for e in config.get('episodes', []) if e.get('split') in ('train', 'validation')]
        run = {**run, 'config': config}
        if actual == 'holdout':
            evaluations = []
            for record in store.list_holdout(run_id):
                nested = record.get('episodes') or record.get('results')
                if isinstance(nested, list):
                    evaluations.extend({**r, 'candidate': r.get('candidate', run.get('frozen_hash')), 'split': 'test'} for r in nested)
                elif record.get('episode_id'):
                    evaluations.append({**record, 'candidate': record.get('candidate', run.get('frozen_hash')), 'split': 'test'})
            evaluations = [e for e in evaluations if e.get('candidate') == run.get('frozen_hash')]
        else:
            evaluations = store.list_evaluations(run_id, split=actual)
        events = [e for e in store.list_events(run_id) if (e.get('payload') or {}).get('split') not in ('test', 'holdout')]
        return scrub_secrets({'run': run, 'candidates': store.list_candidates(run_id), 'evaluations': evaluations,
                             'events': events, 'test_released': released(run_id, run)})

    @app.get('/api/run/{run_id}/game/{candidate}/{episode}')
    def game(run_id: str, candidate: str, episode: str, split: str = 'validation'):
        return game_metadata(run_id, split, candidate, episode)

    @app.get('/api/run/{run_id}/reflection/{sha256}/{variant}')
    def reflection_archive(run_id: str, sha256: str, variant: str, download: bool = False):
        allowed_run(run_id)
        if not re.fullmatch(r'[a-f0-9]{64}', sha256) or variant not in ('prompt', 'full'):
            raise HTTPException(400, 'Invalid reflection archive identifier')
        registered = [event.get('payload') or {} for event in store.list_events(run_id)
                      if event.get('kind') == 'reflection_input'
                      and (event.get('payload') or {}).get('sha256') == sha256]
        if not registered:
            raise HTTPException(404, 'Reflection input was not registered for this run')
        entry = registered[-1]
        if variant == 'full' and entry.get('encoding', 'plain') != 'plain':
            relative, expected_hash, expected_bytes = (entry.get('full_path'), entry.get('full_sha256'), entry.get('full_bytes'))
        elif variant == 'full':
            relative = entry.get('full_path', entry.get('path'))
            expected_hash = entry.get('full_sha256', entry.get('sha256'))
            expected_bytes = entry.get('full_bytes', entry.get('prompt_bytes'))
        else:
            relative, expected_hash, expected_bytes = entry.get('path'), entry.get('sha256'), entry.get('prompt_bytes')
        pattern = r'reflections/[0-9]{4,}-' + re.escape(sha256) + r'\.(?:prompt|full)\.json'
        if (not isinstance(relative, str) or not re.fullmatch(pattern, relative)
                or not isinstance(expected_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_hash)
                or type(expected_bytes) is not int or expected_bytes < 0):
            raise HTTPException(409, 'Registered reflection archive metadata is invalid or incomplete')
        # Walk only the registered run/reflections/file, refusing symlinks at
        # every opened component. No request parameter is interpreted as a path.
        directory_fd = reflection_fd = file_fd = None
        try:
            directory_fd = os.open(store.run_dir(run_id), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            reflection_fd = os.open('reflections', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            file_fd = os.open(relative.split('/')[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=reflection_fd)
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_size != expected_bytes:
                raise HTTPException(409, 'Reflection archive size or file type does not match its registration')
            with os.fdopen(file_fd, 'rb') as stream:
                file_fd = None
                data = stream.read()
                after = os.fstat(stream.fileno())
            binding = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
            if (binding(before) != binding(after) or len(data) != expected_bytes
                    or hashlib.sha256(data).hexdigest() != expected_hash):
                raise HTTPException(409, 'Reflection archive changed or failed its SHA-256 verification')
        except OSError as exc:
            raise HTTPException(404, 'Registered reflection archive cannot be safely opened') from exc
        finally:
            for descriptor in (file_fd, reflection_fd, directory_fd):
                if descriptor is not None:
                    os.close(descriptor)
        disposition = 'attachment' if download else 'inline'
        return Response(data, media_type='text/plain; charset=utf-8', headers={
            'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
            'X-Archive-SHA256': expected_hash, 'X-Archive-Bytes': str(expected_bytes),
            'Content-Disposition': f'{disposition}; filename="{relative.split("/")[1]}"',
        })

    @app.get('/api/run/{run_id}/game/{candidate}/{episode}/decision/{decision_index}')
    def decision(run_id: str, candidate: str, episode: str, decision_index: int,
                 split: str = 'validation', revision: str | None = Query(default=None)):
        actual = split_for(run_id, split, candidate)
        return scrub_secrets(store.load_trace_decision(run_id, actual, candidate, episode, decision_index, revision))

    @app.get('/api/run/{run_id}/game/{candidate}/{episode}/replay')
    def replay(run_id: str, candidate: str, episode: str, split: str = 'validation', highlights: bool = False):
        if highlights:
            from .highlights import build_highlights
            actual = split_for(run_id, split, candidate)
            build_highlights(store, run_id, actual, candidate, episode)
        game = game_metadata(run_id, split, candidate, episode)
        log = game.get('replay_log', '')
        if isinstance(log, list):
            log = '\n'.join(str(line) for line in log)
        if not isinstance(log, str) or not log.strip():
            raise HTTPException(404, 'This game has no archived engine replay')
        encoded = json.dumps(log).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
        title = html.escape(episode)
        page = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>''' + title + '''</title><style>body{margin:0;background:#f6f7f4;font:12px monospace}.notice{padding:10px;background:#eef2e9;color:#394536}#load-status{padding:10px}.wrapper{max-width:640px!important;margin:auto}.battle-log{display:none!important}.replay-controls,.replay-controls-2{padding:8px!important;box-sizing:border-box}button{cursor:pointer;border:1px solid #ced8c7;border-radius:4px;background:#fbfcf7;color:#263d2b;font:11px sans-serif;padding:5px 7px}.notice{font-size:10px;line-height:1.4}#load-status{font-size:10px;line-height:1.4}</style></head><body><div class="notice">LOCAL REPLAY · Official Pokémon Showdown renderer. Internet is needed for renderer assets. The replay is not uploaded.</div><div id="load-status">Loading official replay renderer…</div><script>
const logData=document.createElement('script');logData.type='text/plain';logData.className='battle-log-data';logData.textContent=''' + encoded + ''';document.body.append(logData);
let pendingTurn=null;
window.addEventListener('message',event=>{if(event.source===parent&&event.data?.type==='auto-jev-seek'&&Number.isFinite(event.data.turn)){pendingTurn=event.data.turn;if(window.Replays?.battle)Replays.battle.seekTurn(pendingTurn);}});
const watchdog=setInterval(()=>{if(window.Replays?.battle){clearInterval(watchdog);document.getElementById('load-status').textContent='Engine log loaded. Use Play or choose an action in the dashboard.';Replays.battle.setMute(true);if(pendingTurn!==null)Replays.battle.seekTurn(pendingTurn);parent.postMessage({type:'auto-jev-replay-ready'},'*');}},250);
setTimeout(()=>{if(!window.Replays?.battle){clearInterval(watchdog);document.getElementById('load-status').textContent='Renderer unavailable. Check the connection to play.pokemonshowdown.com. Open Inspect decisions in the dashboard for the complete local engine log.';parent.postMessage({type:'auto-jev-replay-error'},'*');}},20000);
</script>
<!-- The official embed appends its dependencies asynchronously. battledata
     evaluates Config at load time, so load its configuration first to avoid
     a network-order race when multiple replays are opened together. -->
<script src="https://play.pokemonshowdown.com/config/config.js?a7"></script>
<script src="https://play.pokemonshowdown.com/js/replay-embed.js"></script><script>
// The official embed derives protocol-relative asset URLs from localhost HTTP.
// Normalize its public resource roots before creating the battle, and repair
// FX URLs if its asynchronously loaded animation bundle initialized before Dex.
const officialInit=Replays.init;
Replays.init=function(){
  Dex.resourcePrefix='https://play.pokemonshowdown.com/';
  Dex.fxPrefix='https://play.pokemonshowdown.com/fx/';
  if(window.BattleEffects)for(const effect of Object.values(BattleEffects))if(effect.url)effect.url=new URL(effect.url,Dex.fxPrefix).href.replace(/^http:/,'https:');
  return officialInit.call(this);
};
</script></body></html>'''
        if highlights:
            page = page.replace('</body>', '<script src="/assets/highlight-replay.js"></script></body>')
        csp = "default-src 'none'; script-src 'self' 'unsafe-inline' https://play.pokemonshowdown.com; style-src 'unsafe-inline' https://play.pokemonshowdown.com; img-src data: https://play.pokemonshowdown.com; font-src https://play.pokemonshowdown.com; media-src https://play.pokemonshowdown.com; connect-src 'none'; frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
        return HTMLResponse(page, headers={'Content-Security-Policy': csp, 'Referrer-Policy': 'no-referrer', 'Cache-Control': 'no-store'})

    @app.get('/api/run/{run_id}/game/{candidate}/{episode}/highlights')
    def highlights(run_id: str, candidate: str, episode: str, split: str = 'validation',
                   revision: str | None = Query(default=None)):
        from .highlights import build_highlights
        actual = split_for(run_id, split, candidate)
        return scrub_secrets(build_highlights(store, run_id, actual, candidate, episode, revision=revision))

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='runs')
    parser.add_argument('--state', default='artifacts/pokemon_expanded_v2/state.json')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8768)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.root, args.state), host=args.host, port=args.port)


if __name__ == '__main__':
    main()
