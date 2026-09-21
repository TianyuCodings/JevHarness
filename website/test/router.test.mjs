import test from 'node:test';
import assert from 'node:assert/strict';
import {gzipSync, gunzipSync} from 'node:zlib';
import {createHash} from 'node:crypto';
import {createArchiveWorker} from '../worker/router.mjs';

const run = 'reviewed', hash = 'a'.repeat(64), revision = 'b'.repeat(64);
const game = `/api/run/${run}/game/${hash}/game-05`;
function fixture() {
  const manifest = {routes: {}, blobs: {}};
  const encoded = new Map(), reads = [];
  function add(path, text, headers = {}, trace = undefined) {
    const body = Buffer.from(text), bytes = gzipSync(body), sha = createHash('sha256').update(body).digest('hex');
    manifest.blobs[sha] = {path: `blobs/${sha}.gz`, bytes: body.length, gzip_bytes: bytes.length};
    manifest.routes[path] = {blob: sha, status: 200, headers: {'content-type': 'application/json', ...headers}, trace_revision: trace};
    encoded.set(`/archive/blobs/${sha}.gz`, bytes);
  }
  add('/', '<html>site</html>', {'content-type': 'text/html'});
  add('/api/overview', '{"public":true}');
  add('/api/latency?split=eval', '{"split":"eval","median":568.088}');
  add('/api/latency?split=train', '{"split":"train","median":99}');
  add(game + '?split=validation', '{"id":"eval game"}', {}, revision);
  add(game + '?split=train', '{"id":"train game"}', {}, 'c'.repeat(64));
  add(game + '/decision/1?split=validation', '{"full":[1,2,3]}', {}, revision);
  add(game + '/highlights?split=validation', '{"chapters":[2,3,12,15]}', {}, revision);
  add(game + '/replay?split=validation', '<html>full replay</html>', {'content-type': 'text/html', 'content-security-policy': "connect-src 'none'; frame-ancestors 'self'"}, revision);
  add(game + '/replay?highlights=true&split=validation', '<html>highlights adapter</html>', {'content-type': 'text/html'}, revision);
  add(`/api/run/${run}/reflection/${hash}/full`, 'RAW\nCOMPLETE\nINPUT', {
    'content-type': 'text/plain', 'content-disposition': 'inline; filename="full.json"',
    'x-archive-sha256': 'original-sha', 'x-archive-bytes': '18'});
  const env = {ASSETS: {async fetch(request) {
    const path = new URL(request.url).pathname; reads.push(path);
    return encoded.has(path) ? new Response(encoded.get(path)) : new Response(null, {status: 404});
  }}};
  const worker = createArchiveWorker(manifest);
  async function call(path, method = 'GET', runtime = env) {
    return worker.fetch(new Request('https://demo.example' + path, {method}), runtime);
  }
  const text = async response => gunzipSync(Buffer.from(await response.arrayBuffer())).toString();
  return {manifest, env, reads, call, text};
}

test('query defaults, order, split and exact revision resolve independently', async () => {
  const f = fixture();
  assert.match(await f.text(await f.call('/?autoplay=1')), /site/);
  assert.match(await f.text(await f.call('/api/latency')), /eval/);
  assert.match(await f.text(await f.call('/api/latency?split=train')), /train/);
  assert.match(await f.text(await f.call(game + '/decision/1?revision=' + revision + '&split=validation')), /full/);
  assert.match(await f.text(await f.call(game + '?split=train')), /train game/);
  assert.equal((await f.call(game + '/decision/1?revision=' + 'd'.repeat(64))).status, 409);
});

test('highlights and ordinary replay remain separate with session query ignored', async () => {
  const f = fixture();
  const normal = await f.call(game + '/replay?highlights=false&split=validation');
  assert.equal(normal.headers.get('content-security-policy'), "connect-src 'none'; frame-ancestors 'self'");
  assert.match(await f.text(normal), /full replay/);
  assert.match(await f.text(await f.call(game + '/replay?highlight_session=one&split=validation&highlights=1')), /highlights adapter/);
  assert.match(await f.text(await f.call(game + '/replay?highlight_session=two&highlights=true')), /highlights adapter/);
});

test('reflection download preserves bytes and provenance headers', async () => {
  const f = fixture(), path = `/api/run/${run}/reflection/${hash}/full`;
  const response = await f.call(path + '?download=true');
  assert.equal(response.headers.get('content-disposition'), 'attachment; filename="full.json"');
  assert.equal(response.headers.get('x-archive-sha256'), 'original-sha');
  assert.equal(response.headers.get('x-archive-bytes'), '18');
  assert.equal(await f.text(response), 'RAW\nCOMPLETE\nINPUT');
  const inline = await f.call(path);
  assert.equal(inline.headers.get('content-disposition'), 'inline; filename="full.json"');
});

test('read-only and unexported scope fail without touching the asset store', async () => {
  const f = fixture();
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) assert.equal((await f.call('/api/overview', method)).status, 405);
  for (const path of ['/api/state', '/api/test-summary', '/.env', '/archive/blobs/' + 'a'.repeat(64) + '.gz',
    game + '?split=test', game + '?split=holdout', game + '/replay?split=train&highlights=true']) {
    assert.equal((await f.call(path)).status, 404);
  }
  for (const path of ['/api/latency?split=eval&split=train', game + '?split=',
    game + '/replay?highlights=banana', '/api/overview?source=private']) {
    assert.ok([400, 404].includes((await f.call(path)).status));
  }
  assert.equal(f.reads.length, 0);
});

test('HEAD sends headers only; missing ASSETS fails clearly', async () => {
  const f = fixture();
  const head = await f.call('/api/overview', 'HEAD');
  assert.equal(head.status, 200); assert.equal(await head.text(), '');
  assert.equal(f.reads.length, 0);
  assert.equal((await f.call('/api/overview', 'GET', {})).status, 503);
});

test('all successful runtime reads are local content-addressed asset requests', async () => {
  const f = fixture();
  for (const path of ['/api/overview', game + '/highlights', '/api/latency?split=train']) {
    const response = await f.call(path);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('content-encoding'), 'gzip');
    assert.equal(response.headers.get('x-content-type-options'), 'nosniff');
    await f.text(response);
  }
  assert.equal(f.reads.length, 3);
  assert.ok(f.reads.every(path => /^\/archive\/blobs\/[a-f0-9]{64}\.gz$/.test(path)));
});
