/* Exact archived responses. No policy execution, model client, or outbound API. */
function problem(status, detail) {
  return new Response(JSON.stringify({detail}), {status, headers: {
    'content-type': 'application/json', 'cache-control': 'no-store',
    'x-content-type-options': 'nosniff',
  }});
}

function boolean(value) {
  if (value === null || value === 'false' || value === '0') return false;
  if (value === 'true' || value === '1') return true;
  throw new Error('Invalid boolean query parameter');
}

export function resolveRoute(rawUrl, manifest) {
  const url = new URL(rawUrl), path = url.pathname, input = url.searchParams;
  const query = new URLSearchParams();
  let permitted = [], download = false;
  if (path === '/') {
    // Autoplay and Sites navigation parameters do not change archived content.
    permitted = [...input.keys()];
  } else if (path === '/api/latency') {
    permitted = ['split'];
    const split = input.get('split') ?? 'eval';
    if (!['train', 'eval'].includes(split)) return {error: problem(400, 'Latency is available for Train or Eval only')};
    query.set('split', split);
  } else if (/^\/api\/run\/[^/]+\/game\/[^/]+\/[^/]+(?:\/decision\/\d+|\/replay|\/highlights)?$/.test(path)) {
    permitted = ['split', 'revision'];
    const split = input.get('split') ?? 'validation';
    if (!['train', 'validation'].includes(split)) return {error: problem(404, 'This split is not included in the public archive')};
    query.set('split', split);
    if (path.endsWith('/replay')) {
      permitted.push('highlights', 'highlight_session');
      if (boolean(input.get('highlights'))) query.set('highlights', 'true');
    }
  } else if (/^\/api\/run\/[^/]+\/reflection\/[a-f0-9]{64}\/(prompt|full)$/.test(path)) {
    permitted = ['download'];
    download = boolean(input.get('download'));
  }
  for (const key of input.keys()) {
    if (!permitted.includes(key) || input.getAll(key).length !== 1) {
      return {error: problem(400, 'Unsupported or repeated query parameter')};
    }
  }
  query.sort();
  const key = path + (query.size ? '?' + query.toString() : '');
  const entry = manifest.routes[key];
  if (!entry) return {error: problem(404, 'This resource is not included in the public archive')};
  if (input.has('revision') && input.get('revision') !== entry.trace_revision) {
    return {error: problem(409, 'The requested trace revision does not match the published archive')};
  }
  return {entry, key, download};
}

export function createArchiveWorker(manifest) {
  return {
    async fetch(request, env) {
      if (!['GET', 'HEAD'].includes(request.method)) {
        const response = problem(405, 'The published archive is read-only');
        response.headers.set('allow', 'GET, HEAD');
        return response;
      }
      let result;
      try { result = resolveRoute(request.url, manifest); }
      catch { return problem(400, 'Invalid archive request'); }
      if (result.error) return result.error;
      const {entry, download} = result;
      const blob = manifest.blobs[entry.blob];
      if (!blob || !/^[a-f0-9]{64}$/.test(entry.blob) || blob.path !== `blobs/${entry.blob}.gz`) {
        return problem(503, 'The published archive manifest is invalid');
      }
      const headers = new Headers(entry.headers);
      headers.set('content-encoding', 'gzip');
      headers.set('content-length', String(blob.gzip_bytes));
      headers.set('x-content-type-options', 'nosniff');
      if (!headers.has('cache-control')) headers.set('cache-control', 'no-store');
      if (download && headers.has('content-disposition')) {
        headers.set('content-disposition', headers.get('content-disposition').replace(/^inline(?=;|$)/, 'attachment'));
      }
      if (request.method === 'HEAD') return new Response(null, {status: entry.status, headers});
      if (!env?.ASSETS?.fetch) return problem(503, 'The archive asset binding is unavailable');
      const assetUrl = new URL('/archive/' + blob.path, request.url);
      // Only the platform's local static-asset binding is used. This is not a
      // fetch to the public origin, a model endpoint, or a third-party service.
      const asset = await env.ASSETS.fetch(new Request(assetUrl));
      if (!asset.ok) return problem(503, 'A published archive payload is unavailable');
      // The payload is already gzip-compressed; Workers must not encode it again.
      return new Response(asset.body, {status: entry.status, headers, encodeBody: 'manual'});
    },
  };
}
