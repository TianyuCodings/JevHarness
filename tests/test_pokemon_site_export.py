"""Site response snapshots retain bytes and prevent accidental data inclusion."""
import gzip
import hashlib
import json

import httpx
import pytest

from scripts.export_pokemon_site import ArchiveWriter, ExportError, route_key, validate_archive


def response(body, media='application/json', **headers):
    return httpx.Response(200, content=body, headers={'content-type': media, **headers})


def test_exact_reflection_bytes_gzip_hashes_and_deduplication(tmp_path):
    writer = ArchiveWriter(tmp_path)
    body = b'{"complete":"original\\ninput", "no":"rounding or JSON reserialization"}\n'
    digest = hashlib.sha256(body).hexdigest()
    headers = {'x-archive-sha256': digest, 'x-archive-bytes': str(len(body)),
               'content-disposition': 'inline; filename="original.json"'}
    writer.add('/api/run/r/reflection/' + digest + '/full', response(body, 'text/plain', **headers))
    writer.add('/api/run/r/reflection/' + digest + '/prompt', response(body, 'text/plain', **headers))
    manifest = writer.finish({'scope': 'fixture'})
    assert len(manifest['routes']) == 2 and len(manifest['blobs']) == 1
    blob = manifest['blobs'][digest]
    encoded = (tmp_path / blob['path']).read_bytes()
    assert gzip.decompress(encoded) == body
    assert hashlib.sha256(encoded).hexdigest() == blob['gzip_sha256']
    assert gzip.compress(body, compresslevel=9, mtime=0) == encoded
    assert validate_archive(tmp_path) == manifest


def test_stale_original_reflection_hash_cannot_be_relabelled(tmp_path):
    writer = ArchiveWriter(tmp_path)
    with pytest.raises(ExportError, match='declared original bytes'):
        writer.add('/reflection', response(b'changed', 'text/plain', **{'x-archive-sha256': '0' * 64, 'x-archive-bytes': '7'}))


@pytest.mark.parametrize('body,media', [
    (b'{"api_key":"a confidential value"}', 'application/json'),
    (b'{"authorization":"Bearer confidential"}', 'application/json'),
    (b'sk-' + b'x' * 40, 'text/plain'),
    (b'-----BEGIN OPENSSH PRIVATE KEY-----', 'text/plain'),
])
def test_unredacted_sensitive_values_are_rejected_without_echoing_them(tmp_path, body, media):
    with pytest.raises(ExportError) as error:
        ArchiveWriter(tmp_path).add('/fixture', response(body, media))
    assert 'confidential' not in str(error.value) and 'x' * 40 not in str(error.value)


def test_query_identity_and_conflicting_response_fail_closed(tmp_path):
    writer = ArchiveWriter(tmp_path)
    path = '/api/run/r/game/c/e/decision/1'
    writer.add(path + '?revision=source-hash&split=validation', response(b'{"x":1}'), revision='source-hash')
    writer.add(path + '?split=validation', response(b'{"x":1}'), revision='source-hash')
    assert len(writer.routes) == 1
    with pytest.raises(ExportError, match='different responses'):
        writer.add(path + '?split=validation', response(b'{"x":2}'), revision='source-hash')
    assert route_key('/api/latency') == '/api/latency?split=eval'
    assert route_key('/api/run/r/game/c/e/replay?highlight_session=abc&highlights=1') == '/api/run/r/game/c/e/replay?highlights=true&split=validation'


@pytest.mark.parametrize('fault', ['gzip', 'unregistered', 'symlink', 'sealed_route'])
def test_modified_or_extra_archive_content_is_rejected(tmp_path, fault):
    writer = ArchiveWriter(tmp_path)
    writer.add('/api/overview', response(b'{"ok":true}'))
    manifest = writer.finish({'scope': 'fixture'})
    blob = next(iter(manifest['blobs'].values()))
    path = tmp_path / blob['path']
    if fault == 'gzip':
        path.write_bytes(path.read_bytes() + b'corrupted')
    elif fault == 'unregistered':
        (tmp_path / 'private.txt').write_text('not published')
    elif fault == 'symlink':
        target = tmp_path.parent / (tmp_path.name + '-external')
        target.write_bytes(path.read_bytes())
        path.unlink(); path.symlink_to(target)
    else:
        manifest['routes']['/api/game?split=holdout'] = manifest['routes']['/api/overview']
        (tmp_path / 'routes.json').write_text(json.dumps(manifest))
    with pytest.raises(ExportError):
        validate_archive(tmp_path)
