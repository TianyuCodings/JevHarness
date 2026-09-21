/* Local equivalent of the Worker+ASSETS contract, with no outside requests. */
import {createServer} from 'node:http';
import {readFile} from 'node:fs/promises';
import {dirname, join} from 'node:path';
import {fileURLToPath} from 'node:url';
import worker from './dist/server/index.js';

const root = dirname(fileURLToPath(import.meta.url));
const option = process.argv.indexOf('--port');
const port = Number(option < 0 ? 8768 : process.argv[option + 1]);
if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('Invalid port');
const env = {ASSETS: {async fetch(request) {
  const path = new URL(request.url).pathname;
  if (!/^\/archive\/blobs\/[a-f0-9]{64}\.gz$/.test(path)) return new Response(null, {status: 404});
  try { return new Response(await readFile(join(root, 'dist/client', path))); }
  catch { return new Response(null, {status: 404}); }
}}};
createServer(async (incoming, outgoing) => {
  try {
    const request = new Request(`http://127.0.0.1:${port}${incoming.url}`, {method: incoming.method});
    const response = await worker.fetch(request, env);
    outgoing.writeHead(response.status, Object.fromEntries(response.headers));
    outgoing.end(Buffer.from(await response.arrayBuffer()));
  } catch {
    outgoing.writeHead(500, {'content-type': 'application/json'});
    outgoing.end(JSON.stringify({detail: 'Archive preview failed'}));
  }
}).listen(port, '127.0.0.1', () => console.log(`JevHarness archive: http://127.0.0.1:${port}`));
