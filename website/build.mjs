/* Dependency-free, deterministic build from the committed public archive. */
import {createHash} from 'node:crypto';
import {existsSync, mkdirSync, readFileSync, writeFileSync, readdirSync, lstatSync, cpSync, rmSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import {dirname, resolve, join} from 'node:path';
import {gunzipSync} from 'node:zlib';

const root = dirname(fileURLToPath(import.meta.url));
const option = process.argv.indexOf('--archive');
const archive = resolve(option < 0 ? join(root, '../examples/pokemon/sample/archive') : process.argv[option + 1]);
const digest = data => createHash('sha256').update(data).digest('hex');
const manifest = JSON.parse(readFileSync(join(archive, 'routes.json'), 'utf8'));
if (manifest.schema !== 'jevharness.pokemon.site-archive.v1') throw new Error('Unsupported archive schema');
const expected = new Set();
for (const [sha, blob] of Object.entries(manifest.blobs)) {
  if (!/^[a-f0-9]{64}$/.test(sha) || blob.path !== `blobs/${sha}.gz`) throw new Error('Invalid blob path');
  const path = join(archive, blob.path);
  if (!lstatSync(path).isFile() || lstatSync(path).isSymbolicLink()) throw new Error('Blob must be a regular file');
  const encoded = readFileSync(path), decoded = gunzipSync(encoded);
  if (encoded.length !== blob.gzip_bytes || digest(encoded) !== blob.gzip_sha256 ||
      decoded.length !== blob.bytes || digest(decoded) !== sha) throw new Error('Archive integrity check failed');
  expected.add(`${sha}.gz`);
}
if (readdirSync(join(archive, 'blobs')).some(name => !expected.has(name))) throw new Error('Unregistered archive payload');
for (const [route, entry] of Object.entries(manifest.routes)) {
  if (!route.startsWith('/') || !manifest.blobs[entry.blob] || /[?&]split=(test|holdout)(?:&|$)/.test(route)) throw new Error('Invalid public route');
}
const dist = join(root, 'dist');
rmSync(dist, {recursive: true, force: true});
mkdirSync(join(dist, 'server'), {recursive: true});
mkdirSync(join(dist, 'client/archive'), {recursive: true});
for (const name of ['index.js', 'router.mjs']) cpSync(join(root, 'worker', name), join(dist, 'server', name));
writeFileSync(join(dist, 'server/routes.js'), 'export default ' + JSON.stringify(manifest) + ';\n');
writeFileSync(join(dist, 'server/wrangler.json'), JSON.stringify({
  main: 'index.js', compatibility_date: '2026-09-21',
  assets: {directory: '../client', binding: 'ASSETS', run_worker_first: true},
}, null, 2) + '\n');
cpSync(join(archive, 'blobs'), join(dist, 'client/archive/blobs'), {recursive: true});
if (existsSync(join(root, '.openai/hosting.json'))) {
  mkdirSync(join(dist, '.openai'), {recursive: true});
  cpSync(join(root, '.openai/hosting.json'), join(dist, '.openai/hosting.json'));
}
console.log(JSON.stringify({routes: Object.keys(manifest.routes).length, payloads: expected.size,
  gzip_bytes: Object.values(manifest.blobs).reduce((sum, blob) => sum + blob.gzip_bytes, 0),
  worker_manifest_bytes: Buffer.byteLength(JSON.stringify(manifest)), output: dist}));
