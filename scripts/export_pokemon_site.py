#!/usr/bin/env python3
"""Export the current read-only Pokémon UI as an allowlisted response archive.

Only UI-reachable mixed-policy Train/Eval resources are requested. The exporter
uses FastAPI in-process, never a live model, simulator, remote HTTP endpoint, or
environment credential. Frozen provenance hashes retain their original meaning.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
from urllib.parse import parse_qsl, urlencode, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

SCHEMA = 'jevharness.pokemon.site-archive.v1'
HEADERS = {'content-type', 'content-security-policy', 'referrer-policy', 'cache-control',
           'x-content-type-options', 'x-archive-bytes', 'x-archive-sha256', 'content-disposition'}
SECRET_PATTERN = re.compile(rb'(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{24,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')


class ExportError(ValueError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def route_key(url):
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.pop('revision', None)
    query.pop('highlight_session', None)
    query.pop('download', None)
    if '/game/' in parsed.path:
        query.setdefault('split', 'validation')
        if query.get('highlights') in ('false', '0'):
            query.pop('highlights')
        if query.get('highlights') in ('true', '1'):
            query['highlights'] = 'true'
    if parsed.path == '/api/latency':
        query.setdefault('split', 'eval')
    return parsed.path + ('?' + urlencode(sorted(query.items())) if query else '')


def check_public_body(body, content_type):
    if SECRET_PATTERN.search(body):
        raise ExportError('A response contains a credential-shaped value; export stopped without disclosing it')
    if 'json' in content_type:
        from auto_jev.storage import scrub_secrets
        payload = json.loads(body)
        if scrub_secrets(payload) != payload:
            raise ExportError('A JSON response still contains a non-redacted sensitive field')


class ArchiveWriter:
    def __init__(self, directory):
        self.directory = Path(directory)
        (self.directory / 'blobs').mkdir(parents=True, exist_ok=True)
        self.routes = {}
        self.blobs = {}

    def add(self, url, response, *, revision=None):
        if response.status_code not in (200, 404):
            raise ExportError(f'Unexpected export response {response.status_code}: {url}')
        body = response.content
        headers = {key.lower(): value for key, value in response.headers.items() if key.lower() in HEADERS}
        check_public_body(body, headers.get('content-type', ''))
        expected_hash = headers.get('x-archive-sha256')
        if expected_hash and (sha(body) != expected_hash or len(body) != int(headers['x-archive-bytes'])):
            raise ExportError('Reflection download does not match its declared original bytes')
        digest = sha(body)
        encoded = gzip.compress(body, compresslevel=9, mtime=0)
        blob = {'sha256': digest, 'bytes': len(body), 'gzip_sha256': sha(encoded), 'gzip_bytes': len(encoded),
                'path': f'blobs/{digest}.gz'}
        if digest not in self.blobs:
            (self.directory / blob['path']).write_bytes(encoded)
            self.blobs[digest] = blob
        entry = {'blob': digest, 'status': response.status_code, 'headers': headers}
        if revision:
            entry['trace_revision'] = revision
        key = route_key(url)
        if key in self.routes and self.routes[key] != entry:
            raise ExportError(f'Two different responses would occupy the same route: {key}')
        self.routes[key] = entry
        return json.loads(body) if 'json' in headers.get('content-type', '') and response.status_code == 200 else None

    def finish(self, provenance):
        manifest = {'schema': SCHEMA, 'provenance': provenance,
                    'hash_semantics': {
                        'blob_sha256': 'SHA-256 of the exact uncompressed HTTP response bytes. JSON transport bytes may differ from the source trace serialization.',
                        'gzip_sha256': 'SHA-256 of the deterministic gzip transport file (mtime=0). Decompression recovers the original HTTP response bytes exactly.',
                        'trace_revision': 'Original archived result identity, preserved from FastAPI. It is not a GET-response hash.',
                        'reflection': 'Original full/prompt bytes are unchanged; X-Archive-SHA256 and X-Archive-Bytes retain their source meanings.'},
                    'routes': dict(sorted(self.routes.items())), 'blobs': dict(sorted(self.blobs.items()))}
        (self.directory / 'routes.json').write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(',', ':')) + '\n')
        return manifest


def export_from_app(client, directory):
    """Materialize only resources reachable from the current overview."""
    from examples.pokemon.highlights import REVIEWED_RUN, REVIEWED_CANDIDATE, REVIEWED_EPISODE
    writer = ArchiveWriter(directory)

    def get(url, *, allow_missing=False, **kwargs):
        response = client.get(url)
        if response.status_code != 200 and not (allow_missing and response.status_code == 404):
            raise ExportError(f'Required website response is unavailable ({response.status_code}): {url}')
        return writer.add(url, response, **kwargs)

    overview = get('/api/overview')
    if not overview or overview['run']['run_id'] != REVIEWED_RUN or overview['run']['selected_hash'] != REVIEWED_CANDIDATE:
        raise ExportError('This exporter requires the reviewed mixed-policy experiment')
    run_id = overview['run']['run_id']
    get('/')
    for name in ('highlight-demo.js', 'highlight-replay.js', 'highlight-demo.css'):
        get('/assets/' + name)
    showcase = get('/showcase/manifest.json', allow_missing=True)
    if showcase:
        for image in showcase.get('images', []):
            name = image['file']
            if not re.fullmatch(r'[a-z0-9][a-z0-9._-]*\.png', name):
                raise ExportError('An image filename escapes the public asset allowlist')
            get('/showcase/' + name)
    for split in ('train', 'eval'):
        get('/api/latency?split=' + split)
    game_count = decision_count = 0
    provenance = []
    for candidate in overview['candidates']:
        ident = candidate['hash']
        for public, internal in (('train', 'train'), ('eval', 'validation')):
            for listed in candidate['games'][public]:
                episode = listed['episode_id']
                base = f'/api/run/{run_id}/game/{ident}/{episode}'
                game = get(base + '?split=' + internal, allow_missing=True)
                if game is None:
                    continue
                revision = game['trace_revision']
                # Attach the same revision to metadata routes as well, so an
                # optional revision query always fails closed when stale.
                writer.routes[route_key(base + '?split=' + internal)]['trace_revision'] = revision
                provenance.append({'candidate': ident, 'episode_id': episode, 'split': internal,
                                   'trace_revision': revision, 'decisions': game['trace_count']})
                game_count += 1
                if game.get('replay_log'):
                    get(base + '/replay?split=' + internal, revision=revision)
                for index in range(game['trace_count']):
                    get(base + '/decision/' + str(index) + '?' + urlencode({'split': internal, 'revision': revision}), revision=revision)
                    decision_count += 1
                if (ident, internal, episode) == (REVIEWED_CANDIDATE, 'validation', REVIEWED_EPISODE):
                    get(base + '/highlights?split=validation&revision=' + revision, revision=revision)
                    get(base + '/replay?split=validation&highlights=true', revision=revision)
    reflection_count = 0
    for entry in overview['run'].get('reflection_inputs', []):
        payload = entry.get('payload', entry)
        for variant in ('prompt', 'full'):
            get(f'/api/run/{run_id}/reflection/{payload["sha256"]}/{variant}')
            reflection_count += 1
    return writer.finish({'run_id': run_id, 'selected_hash': REVIEWED_CANDIDATE,
                          'scope': 'Current mixed-policy Train/Eval UI only. No test, code-only, credentials, GEPA checkpoints, or inference runtime.',
                          'games': provenance, 'game_count': game_count, 'decision_count': decision_count,
                          'reflection_file_count': reflection_count,
                          'source': 'In-process GET responses from examples.pokemon.demo.create_app; no network, model or simulator calls.'})


def validate_archive(directory):
    directory = Path(directory)
    manifest_path = directory / 'routes.json'
    if manifest_path.is_symlink():
        raise ExportError('Archive manifest cannot be a symlink')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('schema') != SCHEMA:
        raise ExportError('Unknown website response-archive schema')
    expected = {'routes.json'}
    for digest, blob in manifest['blobs'].items():
        if not re.fullmatch('[a-f0-9]{64}', digest) or blob['path'] != f'blobs/{digest}.gz':
            raise ExportError('Invalid content-addressed archive path')
        path = directory / blob['path']
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
            raise ExportError('An archive blob is missing or escapes the archive directory')
        encoded = path.read_bytes()
        if len(encoded) != blob['gzip_bytes'] or sha(encoded) != blob['gzip_sha256']:
            raise ExportError('Compressed archive content changed')
        body = gzip.decompress(encoded)
        if len(body) != blob['bytes'] or sha(body) != digest:
            raise ExportError('Uncompressed archive content changed')
        if SECRET_PATTERN.search(body):
            raise ExportError('An archive contains a credential-shaped value')
        expected.add(blob['path'])
    for key, entry in manifest['routes'].items():
        if entry['blob'] not in manifest['blobs'] or route_key(key) != key or not key.startswith('/'):
            raise ExportError('Invalid archived route binding')
        if '/holdout/' in key or '/test' in key or re.search(r'[?&]split=(?:test|holdout)', key):
            raise ExportError('A sealed route cannot be published')
    actual = {str(path.relative_to(directory)) for path in directory.rglob('*') if path.is_file()}
    if actual != expected:
        raise ExportError('Archive contains unregistered files')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('--state', type=Path, default=PROJECT / 'artifacts/pokemon_expanded_v2/state.json')
    parser.add_argument('--from-archive', type=Path)
    parser.add_argument('--output', type=Path, default=PROJECT / 'examples/pokemon/sample/archive')
    args = parser.parse_args()
    if bool(args.source_root) == bool(args.from_archive):
        parser.error('Choose exactly one of --source-root or --from-archive')
    destination = args.output.resolve()
    if args.from_archive and args.from_archive.resolve() == destination:
        manifest = validate_archive(destination)
    else:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.pokemon-export-', dir=destination.parent) as temporary:
            prepared = Path(temporary) / 'archive'
            if args.from_archive:
                validate_archive(args.from_archive)
                shutil.copytree(args.from_archive, prepared)
                manifest = validate_archive(prepared)
            else:
                from fastapi.testclient import TestClient
                from examples.pokemon.demo import create_app
                with TestClient(create_app(args.source_root, args.state)) as client:
                    manifest = export_from_app(client, prepared)
                validate_archive(prepared)
            if destination.exists():
                validate_archive(destination)
                shutil.rmtree(destination)
            shutil.move(str(prepared), destination)
    print(json.dumps({'archive': str(destination), 'routes': len(manifest['routes']),
                      'unique_payloads': len(manifest['blobs']),
                      'gzip_bytes': sum(blob['gzip_bytes'] for blob in manifest['blobs'].values()),
                      **{key: manifest['provenance'].get(key) for key in ('game_count', 'decision_count', 'reflection_file_count')}}, indent=2))


if __name__ == '__main__':
    main()
