"""Public showcase files never grant access to arbitrary archive paths."""
import base64
import json

import pytest
from fastapi.testclient import TestClient

from examples.pokemon import demo


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j3ioAAAAASUVORK5CYII=')


@pytest.fixture
def showcase(tmp_path, monkeypatch):
    package = tmp_path / 'package'
    directory = package / 'static' / 'showcase'
    directory.mkdir(parents=True)
    monkeypatch.setattr(demo, '__file__', str(package / 'demo.py'))
    (directory / 'turn-2.png').write_bytes(PNG)
    manifest = {'images': ['turn-2.png'], 'source': 'archived public Eval replay'}
    (directory / 'manifest.json').write_text(json.dumps(manifest))
    client = TestClient(demo.create_app(tmp_path / 'runs', tmp_path / 'state.json'))
    return directory, client, manifest


def test_public_png_and_manifest_are_read_only_and_nosniff(showcase):
    directory, client, manifest = showcase
    image = client.get('/showcase/turn-2.png')
    assert image.status_code == 200 and image.content == PNG
    assert image.headers['content-type'] == 'image/png'
    assert image.headers['x-content-type-options'] == 'nosniff'
    metadata = client.get('/showcase/manifest.json')
    assert metadata.status_code == 200 and metadata.json() == manifest
    assert metadata.headers['content-type'] == 'application/json'
    assert metadata.headers['x-content-type-options'] == 'nosniff'
    for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        assert client.request(method, '/showcase/turn-2.png', content=b'changed').status_code == 405
    assert (directory / 'turn-2.png').read_bytes() == PNG


@pytest.mark.parametrize('filename', ['private.json', 'state.json', 'image.svg', 'image.jpg',
                                      'image.PNG', 'Image.png', '.hidden.png', 'manifest.json.png.js',
                                      'space image.png'])
def test_other_existing_files_are_not_public_showcase_resources(showcase, filename):
    directory, client, _ = showcase
    (directory / filename).write_bytes(b'PRIVATE_FILE_CONTENT')
    response = client.get('/showcase/' + filename)
    assert response.status_code == 404 and b'PRIVATE_FILE_CONTENT' not in response.content


def test_missing_file_and_directory_named_png_are_unavailable(showcase):
    directory, client, _ = showcase
    (directory / 'directory.png').mkdir()
    assert client.get('/showcase/missing.png').status_code == 404
    assert client.get('/showcase/directory.png').status_code == 404


@pytest.mark.parametrize('path', ['%2e%2e%2fsecret.png', '..%2fsecret.png',
                                  '%2e%2e%2f%2e%2e%2fsecret.png', '%2fsecret.png',
                                  'nested%2fsecret.png', '%2e%2e%5csecret.png'])
def test_encoded_path_escape_cannot_read_files_outside_showcase(showcase, path):
    directory, client, _ = showcase
    (directory.parent / 'secret.png').write_bytes(b'OUTSIDE_PRIVATE_CONTENT')
    (directory / 'nested').mkdir(exist_ok=True)
    (directory / 'nested' / 'secret.png').write_bytes(b'NESTED_PRIVATE_CONTENT')
    response = client.get('/showcase/' + path)
    assert response.status_code == 404
    assert b'PRIVATE_CONTENT' not in response.content


@pytest.mark.parametrize('filename', ['linked.png', 'manifest.json'])
@pytest.mark.parametrize('inside', [True, False])
def test_leaf_symlinks_are_rejected_even_when_target_is_public(showcase, filename, inside):
    directory, client, _ = showcase
    target = directory / 'turn-2.png' if inside else directory.parent / 'private.png'
    if not inside:
        target.write_bytes(b'OUTSIDE_PRIVATE_CONTENT')
    link = directory / filename
    link.unlink(missing_ok=True)
    link.symlink_to(target)
    response = client.get('/showcase/' + filename)
    assert response.status_code == 404 and b'OUTSIDE_PRIVATE_CONTENT' not in response.content


def test_symlinked_showcase_directory_cannot_expose_a_different_directory(showcase):
    directory, client, _ = showcase
    outside = directory.parent.parent / 'private-assets'
    outside.mkdir()
    (outside / 'private.png').write_bytes(b'OUTSIDE_PRIVATE_CONTENT')
    directory.rename(directory.with_name('original-showcase'))
    directory.symlink_to(outside, target_is_directory=True)
    response = client.get('/showcase/private.png')
    assert response.status_code == 404 and b'OUTSIDE_PRIVATE_CONTENT' not in response.content


def test_symlinked_static_directory_is_also_rejected(showcase):
    directory, client, _ = showcase
    static = directory.parent
    outside = static.parent / 'private-static'
    (outside / 'showcase').mkdir(parents=True)
    (outside / 'showcase' / 'private.png').write_bytes(b'OUTSIDE_PRIVATE_CONTENT')
    static.rename(static.with_name('original-static'))
    static.symlink_to(outside, target_is_directory=True)
    response = client.get('/showcase/private.png')
    assert response.status_code == 404 and b'OUTSIDE_PRIVATE_CONTENT' not in response.content


def test_deferred_file_response_cannot_follow_a_replaced_symlink(showcase, monkeypatch):
    directory, client, _ = showcase
    target = directory.parent / 'private.png'
    target.write_bytes(b'OUTSIDE_PRIVATE_CONTENT')
    original = demo.FileResponse

    def replace_before_deferred_read(path, *args, **kwargs):
        # Reproduce the interval between route validation and FileResponse's
        # later ASGI file open. A descriptor-based response does not enter it.
        if str(path) == str(directory / 'turn-2.png'):
            path.unlink()
            path.symlink_to(target)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(demo, 'FileResponse', replace_before_deferred_read)
    response = client.get('/showcase/turn-2.png')
    assert response.status_code in (200, 404, 409)
    assert b'OUTSIDE_PRIVATE_CONTENT' not in response.content
    if response.status_code == 200:
        assert response.content == PNG
