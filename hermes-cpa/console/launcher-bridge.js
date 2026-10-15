// Shared trusted launcher protocol. Bundled into desktop/plugin.js and dashboard/index.js.
// Guest documents never receive lease IDs, document IDs, or backend credentials.

export const CPA_PROTOCOL = 3;
export const MAX_WIRE_BYTES = 48 * 1024 * 1024;
export const MAX_RESPONSE_BYTES = 32 * 1024 * 1024;
// file.save exports share the native response bound; uploads keep their own, smaller per-file limit in the guest and owner.
export const MAX_EXPORT_BYTES = MAX_RESPONSE_BYTES;
export const MAX_INFLIGHT = 32;
export const MAX_INFLIGHT_BYTES = 64 * 1024 * 1024;
export const REQUEST_TIMEOUT_MS = 10 * 60 * 1000;
export const HANDSHAKE_TIMEOUT_MS = 10 * 1000;
// Longest lifetime the owner ever grants a native resource; the owner's clock stays the expiry authority.
export const RESOURCE_MAX_LIFETIME_MS = 5 * 60 * 1000;
export const RESULT_POLL_INTERVAL_MS = 1000;
export const RESULT_POLL_MAX_INTERVAL_MS = 5000;
export const RESULT_POLL_TIMEOUT_MS = 2 * 60 * 1000;
export const REVOKE_ATTEMPTS = 3;
export const REVOKE_RETRY_MS = 250;
export const EXTERNAL_OPEN_PROMPT_MS = 5 * 60 * 1000;
export const NOT_FORWARDED_BUSY = 'PLUGIN_NOT_FORWARDED_BUSY';

const textEncoder = new TextEncoder();
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const record = (value) => value !== null && typeof value === 'object' && !Array.isArray(value);
const sizeOf = (value) => textEncoder.encode(JSON.stringify(value)).byteLength;
const safeId = (value) => typeof value === 'string' && /^[A-Za-z0-9_.:-]{1,128}$/.test(value);
const uuid = () => {
  if (!globalThis.crypto?.randomUUID) throw new Error('Secure UUID generation is unavailable');
  return globalThis.crypto.randomUUID();
};
const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));

export class LauncherError extends Error {
  /** `notForwarded` is true only when the request provably never reached the plugin. */
  constructor(code, message = code, { notForwarded = false } = {}) {
    super(message);
    this.code = code;
    this.notForwarded = notForwarded;
  }
}

export function appendProfile(path, profile) {
  if (typeof path !== 'string' || !path.startsWith('/') || path.startsWith('//')) {
    throw new LauncherError('INVALID_INPUT');
  }
  const captured = String(profile ?? '').trim();
  const hashAt = path.indexOf('#');
  const withoutHash = hashAt < 0 ? path : path.slice(0, hashAt);
  const hash = hashAt < 0 ? '' : path.slice(hashAt);
  const query = withoutHash.indexOf('?');
  const base = query < 0 ? withoutHash : withoutHash.slice(0, query);
  const params = new URLSearchParams(query < 0 ? '' : withoutHash.slice(query + 1));
  if (captured) params.set('profile', captured);
  else params.delete('profile');
  const serialized = params.toString();
  return `${base}${serialized ? `?${serialized}` : ''}${hash}`;
}

const BASE64_ALPHABET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/';
const BASE64_VALUES = new Int8Array(128).fill(-1);
for (let index = 0; index < BASE64_ALPHABET.length; index += 1) BASE64_VALUES[BASE64_ALPHABET.charCodeAt(index)] = index;
const sextet = (value, index) => {
  const code = value.charCodeAt(index);
  return code === 61 ? 0 : BASE64_VALUES[code];
};

/** Validates canonical padded base64 and returns the decoded length without allocating the bytes. */
export function base64ByteLength(value, limit = MAX_RESPONSE_BYTES) {
  if (typeof value !== 'string' || value.length > Math.ceil(limit / 3) * 4 + 8 || value.length % 4 !== 0) {
    throw new LauncherError('PAYLOAD_TOO_LARGE');
  }
  let padding = 0;
  if (value.charCodeAt(value.length - 1) === 61) padding = value.charCodeAt(value.length - 2) === 61 ? 2 : 1;
  for (let index = 0; index < value.length - padding; index += 1) {
    const code = value.charCodeAt(index);
    if (code > 127 || BASE64_VALUES[code] < 0) throw new LauncherError('PAYLOAD_TOO_LARGE');
  }
  const bytes = (value.length / 4) * 3 - padding;
  if (bytes > limit) throw new LauncherError('PAYLOAD_TOO_LARGE');
  return bytes;
}

/** Decodes straight into one exactly sized buffer; no intermediate binary string for a 32 MiB export. */
export function decodeBase64(value, limit = MAX_RESPONSE_BYTES) {
  const length = base64ByteLength(value, limit);
  const output = new Uint8Array(length);
  let offset = 0;
  for (let index = 0; index < value.length; index += 4) {
    const group = (sextet(value, index) << 18) | (sextet(value, index + 1) << 12) |
      (sextet(value, index + 2) << 6) | sextet(value, index + 3);
    if (offset < length) output[offset++] = group >> 16;
    if (offset < length) output[offset++] = (group >> 8) & 255;
    if (offset < length) output[offset++] = group & 255;
  }
  return output;
}

export function verifiedDocumentId() {
  return uuid();
}

function responseData(value) {
  if (!record(value) || !record(value.data)) throw new LauncherError('UNAVAILABLE');
  return value.data;
}

function errorCode(error) {
  if (error instanceof LauncherError) return error.code;
  return 'UNAVAILABLE';
}

/**
 * The host's explicit refusal before forwarding: HTTP 429 `{error: 'PLUGIN_NOT_FORWARDED_BUSY', forwarded: false}`.
 * Desktop preserves `code`/`forwarded` on its Error; the Dashboard's ApiError carries `status` and the raw
 * `body`. Nothing else proves a request never reached the plugin.
 */
export function provablyNotForwarded(error) {
  if (error === null || typeof error !== 'object') return false;
  if (error.code === NOT_FORWARDED_BUSY && error.forwarded === false) return true;
  if (error.status !== 429) return false;
  let body = error.body;
  if (typeof body === 'string') {
    try { body = JSON.parse(body); } catch { return false; }
  }
  return record(body) && body.error === NOT_FORWARDED_BUSY && body.forwarded === false;
}

/**
 * Local lifetime of a presented native resource. The owner's wall clock may sit minutes ahead of or behind
 * this machine, so it can only shorten the lifetime and only while it still lies in the future; the lifetime
 * never exceeds what the owner could have granted since the execute call began. The owner, not this timer,
 * decides when the grant actually expires.
 */
export function resourceLifetimeMs(expiresAt, executeStartedAt, now) {
  const ceiling = Math.max(0, RESOURCE_MAX_LIFETIME_MS - (now - executeStartedAt));
  const remote = expiresAt * 1000 - now;
  return remote > 0 ? Math.min(remote, ceiling) : ceiling;
}

function assertWire(value, limit = MAX_WIRE_BYTES) {
  let bytes;
  try { bytes = sizeOf(value); } catch { throw new LauncherError('INVALID_INPUT'); }
  if (bytes > limit) throw new LauncherError('PAYLOAD_TOO_LARGE');
  return bytes;
}

function assertReview(value) {
  if (!record(value) || !safeId(value.review_id) || typeof value.summary !== 'string' ||
      !Array.isArray(value.effects) || value.effects.length > 256 ||
      !value.effects.every(effect => typeof effect === 'string' && effect.length > 0 && effect.length <= 512) ||
      typeof value.requires_confirmation !== 'boolean') {
    throw new LauncherError('UNAVAILABLE');
  }
  return value;
}

function validateManagementRequest(value) {
  if (!record(value) || !['GET', 'POST', 'PUT', 'PATCH', 'DELETE'].includes(value.method) ||
      typeof value.path !== 'string' || !value.path.startsWith('/') || value.path.startsWith('//') ||
      value.path.includes('\\') || typeof value.response_kind !== 'string' ||
      !['json', 'text', 'bytes'].includes(value.response_kind) || !record(value.body)) {
    throw new LauncherError('INVALID_INPUT');
  }
  assertWire(value);
}

function validateTransaction(value) {
  if (!record(value) || !Array.isArray(value.requests) || value.requests.length === 0 || value.requests.length > 256 ||
      typeof value.expected_revision !== 'string' || !/^[a-f0-9]{64}$/.test(value.expected_revision)) {
    throw new LauncherError('INVALID_INPUT');
  }
  value.requests.forEach(validateManagementRequest);
  assertWire(value);
}

function assertResponsePayload(value) {
  assertWire(value);
  const body = record(value) && record(value.body) ? value.body : null;
  if (body?.kind === 'bytes') base64ByteLength(body.base64, MAX_RESPONSE_BYTES);
  return value;
}

function isExternalUrl(value) {
  if (typeof value !== 'string' || value.length > 16 * 1024) return false;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' || url.protocol === 'http:';
  } catch { return false; }
}

function isNativePluginEntryPath(value) {
  return typeof value === 'string' && value.length <= 2 * 1024 * 1024 &&
    /^\/v0\/resource\/plugins\/[A-Za-z0-9._~!$&'()*+,;=:@%/-]+(?:\?[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$/.test(value);
}

function isExternalPluginResource(value) {
  if (isExternalUrl(value)) return true;
  return typeof value === 'string' && value.startsWith('data:') && value.length <= 2 * 1024 * 1024;
}

function isEffectful(operation, payload) {
  if (operation === 'management.request') {
    return !record(payload) || payload.method !== 'GET' || /\/oauth\/auth-url(?:$|\?)/.test(payload.path);
  }
  return operation !== 'session.lock';
}

// May share the management lifecycle lane. The owner treats any GET reaching plugin code (declared /v0/management
// routes, per-plugin quota, OAuth auth URLs) as mutating, so only core /v8/management reads and two plain reads qualify.
function isReadOnlyManagement(operation, payload) {
  if (operation !== 'management.request' || payload.method !== 'GET') return false;
  const path = payload.path.split('?')[0];
  if (path.includes('%')) return false;
  if (path === '/v1/models' || path === '/v0/management/config') return true;
  return path.startsWith('/v8/management/') && !/^\/v8\/management\/(?:oauth\/auth-url|plugins\/[^/]+\/quota)$/.test(path);
}

/**
 * A new browser tab needs fresh trusted user activation, which the asynchronous review round trip has already
 * spent, and `window.open(..., 'noopener')` cannot report a blocked popup. The outer UI therefore renders the
 * exact reviewed URL as a real link: only a click on that link settles the request as opened. Cancel, the
 * prompt deadline and disposal settle it as not opened. Requests are presented one at a time.
 */
export function createExternalOpenGate(onChange = () => {}, promptMs = EXTERNAL_OPEN_PROMPT_MS) {
  let pending;
  let queue = Promise.resolve();
  let disposed = false;
  const settle = (error) => {
    const current = pending;
    if (!current) return;
    pending = undefined;
    clearTimeout(current.timer);
    onChange(null);
    if (error) current.reject(error);
    else current.resolve();
  };
  const present = (url) => new Promise((resolve, reject) => {
    if (disposed) { reject(new LauncherError('UNAVAILABLE')); return; }
    pending = { url, resolve, reject, timer: setTimeout(() => settle(new LauncherError('TIMEOUT')), promptMs) };
    onChange({ url });
  });
  return {
    request(url) {
      if (disposed) return Promise.reject(new LauncherError('UNAVAILABLE'));
      if (!isExternalUrl(url)) return Promise.reject(new LauncherError('INVALID_INPUT'));
      const next = queue.then(() => present(url));
      queue = next.catch(() => undefined);
      return next;
    },
    activate() { settle(); },
    cancel() { settle(new LauncherError('CONSENT_DECLINED')); },
    dispose() {
      disposed = true;
      settle(new LauncherError('UNAVAILABLE'));
    },
  };
}

/**
 * The sole privileged side of protocol v3. `backend` owns profile routing and
 * authenticated plugin REST; this module adds the captured profile to every
 * path before invoking it. `review` is trusted outer UI, never guest DOM.
 */
export function createTrustedLauncherSession({
  backend, profile, documentId, review, capabilities, onState = () => {},
  onReceipt = () => {}, onPluginResource = () => {}, onClose = () => {}, timing = {},
}) {
  if (!backend || typeof backend.post !== 'function' || typeof backend.get !== 'function' ||
      typeof profile !== 'string' || !documentId || typeof review !== 'function') {
    throw new Error('Trusted launcher session requires captured host capabilities');
  }
  const pollIntervalMs = timing.pollIntervalMs ?? RESULT_POLL_INTERVAL_MS;
  const pollMaxIntervalMs = timing.pollMaxIntervalMs ?? RESULT_POLL_MAX_INTERVAL_MS;
  const revokeRetryMs = timing.revokeRetryMs ?? REVOKE_RETRY_MS;

  let closed = false;
  let unlocked = false;
  let leaseId = '';
  let sessionId = '';
  let clientNonce = '';
  let port;
  let handshakeTimer;
  let requestTokens = 120;
  let tokenTime = Date.now();
  let inflightBytes = 0;
  const inflight = new Map();
  let backendTail = Promise.resolve();
  let revokeTail = Promise.resolve();
  let resourceGrantId = '';
  let resourceClose;
  let resourceExpiryTimer;

  const serialBackend = task => {
    const next = backendTail.then(task, task);
    backendTail = next.catch(() => undefined);
    return next;
  };

  // The owner pins each review to resource versions and answers a stale execute with RESOURCE_CONFLICT, which
  // this bridge must treat as OUTCOME_UNKNOWN. Mutating requests therefore run prepare..receipt alone, FIFO;
  // provably read-only ones share the lane.
  const lifecycleQueue = [];
  let lifecycleReaders = 0;
  let lifecycleWriter = false;
  const pumpLifecycle = () => {
    while (lifecycleQueue.length) {
      const next = lifecycleQueue[0];
      if (lifecycleWriter || (next.exclusive && lifecycleReaders > 0)) return;
      lifecycleQueue.shift();
      if (next.exclusive) lifecycleWriter = true; else lifecycleReaders += 1;
      next.grant();
    }
  };
  const lifecycle = async (exclusive, task) => {
    await new Promise(grant => { lifecycleQueue.push({ exclusive, grant }); pumpLifecycle(); });
    try {
      return await task();
    } finally {
      if (exclusive) lifecycleWriter = false; else lifecycleReaders -= 1;
      pumpLifecycle();
    }
  };

  const send = async (path, body, options) => {
    try {
      return responseData(await backend.post(appendProfile(path, profile), body, options));
    } catch (error) {
      if (!provablyNotForwarded(error)) throw error;
      throw new LauncherError('HOST_BUSY', 'The host was busy and did not forward this request.', { notForwarded: true });
    }
  };

  const call = async (path, body, options = {}) => {
    if (closed) throw new LauncherError('UNAVAILABLE', 'UNAVAILABLE', { notForwarded: true });
    assertWire(body);
    return serialBackend(async () => {
      if (closed) throw new LauncherError('UNAVAILABLE', 'UNAVAILABLE', { notForwarded: true });
      return send(path, body, options);
    });
  };

  // Revocations are tiny and idempotent, so they never queue behind a hung large call. A refusal that provably
  // did not reach the owner is retried; any other failure stays best effort (the owner expires leases itself).
  const revoke = (path, body) => {
    const next = revokeTail.then(async () => {
      for (let attempt = 1; attempt <= REVOKE_ATTEMPTS; attempt += 1) {
        try {
          await backend.post(appendProfile(path, profile), body, { timeoutMs: 9000 });
          return;
        } catch (error) {
          if (attempt === REVOKE_ATTEMPTS || !provablyNotForwarded(error)) return;
          await wait(revokeRetryMs * attempt);
        }
      }
    });
    revokeTail = next;
    return next;
  };

  const lock = () => (unlocked && leaseId
    ? revoke('/console/lock', { lease_id: leaseId, document_id: documentId })
    : Promise.resolve());

  const closeResource = async () => {
    clearTimeout(resourceExpiryTimer);
    resourceExpiryTimer = undefined;
    const closePresentation = resourceClose;
    const grantId = resourceGrantId;
    resourceClose = undefined;
    resourceGrantId = '';
    onPluginResource(null);
    const revocation = grantId
      ? revoke('/console/resource/close', { document_id: documentId, grant_id: grantId })
      : Promise.resolve();
    try { await closePresentation?.(); } catch { /* the host independently expires presentation handles */ }
    await revocation;
  };

  const close = ({ notifyGuest = true, revokeLease = true } = {}) => {
    if (closed) return;
    closed = true;
    clearTimeout(handshakeTimer);
    if (notifyGuest && port && sessionId) {
      try { port.postMessage({ type: 'hermes:cpa:locked', protocol: CPA_PROTOCOL, session_id: sessionId }); } catch { /* port is already gone */ }
    }
    try { port?.close(); } catch { /* nothing to release */ }
    port = undefined;
    for (const entry of inflight.values()) clearTimeout(entry.timer);
    inflight.clear();
    inflightBytes = 0;
    const cleanup = Promise.allSettled([
      closeResource(),
      revokeLease ? lock() : Promise.resolve(),
    ]);
    onState('locked');
    onClose(cleanup);
    return cleanup;
  };

  const failUnknown = () => {
    onState('outcome-unknown');
    close();
  };

  const reviewThenExecute = async (path, first, reviewBody, beforeExecute = () => {}) => {
    const prepared = assertReview(await call(path, first));
    const approved = !(prepared.requires_confirmation || reviewBody.forceConfirmation) || await review({
      summary: prepared.summary,
      effects: [...prepared.effects],
      operation: reviewBody.operation,
      risk: reviewBody.risk,
    });
    if (!approved) throw new LauncherError('CONSENT_DECLINED');
    beforeExecute(prepared);
    return call(path === '/console/prepare' ? '/console/execute' : path, {
      lease_id: leaseId,
      document_id: documentId,
      ...(reviewBody.requestId ? { request_id: reviewBody.requestId } : {}),
      review_id: prepared.review_id,
      approved_effects: [...prepared.effects],
    });
  };

  const RESULT_STATES = new Set(['pending', 'running', 'succeeded', 'failed', 'unknown']);
  const staleScope = error => error?.code === 'STALE_SCOPE' || error?.message === 'STALE_SCOPE';

  // The operation was accepted before this runs. Reading its receipt is side-effect free, so a failed or
  // malformed poll is retried until the deadline; it can end only in a terminal receipt or OUTCOME_UNKNOWN.
  const resultFor = async (operationId) => {
    const receipt = fields => onReceipt({ profile, document_id: documentId, operation_id: operationId, ...fields });
    const deadline = Date.now() + REQUEST_TIMEOUT_MS;
    let delay = pollIntervalMs;
    while (!closed && Date.now() < deadline) {
      let result;
      try {
        result = await call('/console/result', {
          lease_id: leaseId,
          document_id: documentId,
          operation_id: operationId,
        }, { timeoutMs: RESULT_POLL_TIMEOUT_MS });
      } catch (error) {
        // A retired selection can never read the receipt again; stay unknown instead of polling a dead owner.
        if (staleScope(error)) break;
      }
      const state = record(result) && result.operation_id === operationId && RESULT_STATES.has(result.state)
        ? result.state : undefined;
      if (state === undefined) {
        await wait(delay);
        delay = Math.min(delay * 2, pollMaxIntervalMs);
        continue;
      }
      delay = pollIntervalMs;
      const status = Number.isInteger(result.result?.status) ? result.result.status : undefined;
      const withheld = result.result_withheld === true;
      if (state === 'succeeded' && !withheld) {
        let response;
        try { response = assertResponsePayload(result.result); } catch { break; }
        receipt({ state, status, result_withheld: false });
        return { response, requiresUnlock: result.requires_unlock === true };
      }
      receipt({ state, status, result_withheld: withheld });
      if (state === 'succeeded') return { withheld: true, requiresUnlock: result.requires_unlock === true };
      if (state === 'failed') throw new LauncherError(typeof result.code === 'string' ? result.code : 'FAILED');
      if (state === 'unknown') throw new LauncherError('OUTCOME_UNKNOWN');
      await wait(pollIntervalMs);
    }
    receipt({ state: 'unknown' });
    throw new LauncherError('OUTCOME_UNKNOWN');
  };

  const management = async (operation, payload, requestId) => {
    if (operation === 'management.request') validateManagementRequest(payload);
    else validateTransaction(payload);
    // Queued synchronously in guest order; a request abandoned while queued never reaches the owner.
    return lifecycle(!isReadOnlyManagement(operation, payload), async () => {
      if (closed || !inflight.has(requestId)) throw new LauncherError('UNAVAILABLE', 'UNAVAILABLE', { notForwarded: true });
      // Guest counters restart on remount; keep same-document IDs stable without exposing document authority.
      const digest = await crypto.subtle.digest('SHA-256', textEncoder.encode(JSON.stringify([documentId, requestId])));
      const ownerRequestId = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
      const first = {
        lease_id: leaseId,
        document_id: documentId,
        request_id: ownerRequestId,
        ...(operation === 'management.request'
          ? { request: payload }
          : { requests: payload.requests, expected_revision: payload.expected_revision }),
      };
      let dispatched = false;
      let receipt;
      try {
        receipt = await reviewThenExecute('/console/prepare', first, { operation, requestId: ownerRequestId }, (prepared) => {
          dispatched = true;
          const entry = inflight.get(requestId);
          if (entry && prepared.effects.length > 0) entry.effectful = true;
        });
      } catch (error) {
        // After approval only an explicit refusal before forwarding proves that nothing ran.
        if (dispatched && error?.notForwarded !== true) throw new LauncherError('OUTCOME_UNKNOWN');
        throw error;
      }
      // The execute call returned, so the operation was accepted: from here only a terminal receipt is definitive.
      if (!record(receipt) || !safeId(receipt.operation_id)) throw new LauncherError('OUTCOME_UNKNOWN');
      onReceipt({ profile, document_id: documentId, request_id: ownerRequestId, operation_id: receipt.operation_id, state: 'accepted' });
      const terminal = await resultFor(receipt.operation_id);
      if (terminal.requiresUnlock) setTimeout(() => close(), 0);
      if (terminal.withheld) throw new LauncherError('RESULT_WITHHELD');
      return terminal.response;
    });
  };

  const browser = async (action, payload) => {
    const value = payload?.[action === 'open' ? 'url' : 'text'];
    if (typeof value !== 'string' || value.length === 0 || value.length > 1024 * 1024 ||
        (action === 'open' && !isExternalUrl(value))) throw new LauncherError('INVALID_INPUT');
    const response = await reviewThenExecute('/console/browser', {
      lease_id: leaseId,
      document_id: documentId,
      action,
      value,
    }, { operation: `browser.${action}` });
    if (!record(response) || response.ok !== true || response.action !== action || typeof response.value !== 'string') {
      throw new LauncherError('UNAVAILABLE');
    }
    if (action === 'open') {
      if (!isExternalUrl(response.value) || typeof capabilities.openExternal !== 'function') throw new LauncherError('UNAVAILABLE');
      await capabilities.openExternal(response.value);
    } else {
      if (typeof capabilities.copy !== 'function') throw new LauncherError('UNAVAILABLE');
      await capabilities.copy(response.value);
    }
    return { completed: true };
  };

  const file = async (payload) => {
    if (!record(payload) || typeof payload.filename !== 'string' || !/^[^\\/\0]{1,255}$/.test(payload.filename) ||
        typeof payload.content_type !== 'string' || payload.content_type.length > 160 || typeof payload.base64 !== 'string') {
      throw new LauncherError('INVALID_INPUT');
    }
    if (typeof capabilities.saveFile !== 'function') throw new LauncherError('HOST_CAPABILITY_UNAVAILABLE');
    base64ByteLength(payload.base64, MAX_EXPORT_BYTES);
    const response = await reviewThenExecute('/console/file', {
      lease_id: leaseId,
      document_id: documentId,
      filename: payload.filename,
      content_type: payload.content_type,
      base64: payload.base64,
    }, { operation: 'file.save', forceConfirmation: true });
    if (!record(response) || response.ok !== true || typeof response.filename !== 'string' ||
        typeof response.content_type !== 'string' || typeof response.base64 !== 'string') {
      throw new LauncherError('UNAVAILABLE');
    }
    const bytes = decodeBase64(response.base64, MAX_EXPORT_BYTES);
    return capabilities.saveFile({ suggestedFilename: response.filename, bytes: bytes.buffer }, response.content_type);
  };

  const plugin = async (payload) => {
    if (!record(payload) || typeof payload.plugin_id !== 'string' || !/^[A-Za-z0-9._-]{1,128}$/.test(payload.plugin_id) ||
        !Number.isInteger(payload.menu_index) || payload.menu_index < 0 || payload.menu_index > 1024) {
      throw new LauncherError('INVALID_INPUT');
    }
    let executeStartedAt = Date.now();
    const response = await reviewThenExecute('/console/plugin', {
      lease_id: leaseId,
      document_id: documentId,
      plugin_id: payload.plugin_id,
      menu_index: payload.menu_index,
    }, {
      operation: 'plugin.open',
      forceConfirmation: true,
      risk: 'This opens installed plugin code. It may read or change credentials and perform native network or other effectful operations.',
    }, () => { executeStartedAt = Date.now(); });
    if (!record(response) || response.ok !== true || response.plugin_id !== payload.plugin_id) {
      throw new LauncherError('UNAVAILABLE');
    }
    await closeResource();
    if (response.kind === 'native-resource') {
      if (!safeId(response.grant_id) || !isNativePluginEntryPath(response.entry_path) ||
          !Number.isFinite(response.expires_at) || response.expires_at <= 0) {
        throw new LauncherError('UNAVAILABLE');
      }
      resourceGrantId = response.grant_id;
      if (typeof capabilities.presentNativeResource !== 'function') {
        await closeResource();
        throw new LauncherError('HOST_CAPABILITY_UNAVAILABLE');
      }
      let presentation;
      try {
        presentation = await capabilities.presentNativeResource({
          documentId,
          grantId: response.grant_id,
          entryPath: response.entry_path,
          expiresAt: response.expires_at,
        });
      } catch {
        await closeResource();
        throw new LauncherError('UNAVAILABLE');
      }
      if (!record(presentation) || typeof presentation.url !== 'string' || presentation.url.length === 0 ||
          presentation.url.length > 16 * 1024 || typeof presentation.close !== 'function' ||
          closed || resourceGrantId !== response.grant_id) {
        try { await presentation?.close?.(); } catch { /* close the newly created isolated presentation */ }
        await closeResource();
        throw new LauncherError('UNAVAILABLE');
      }
      resourceClose = presentation.close;
      const lifetimeMs = resourceLifetimeMs(response.expires_at, executeStartedAt, Date.now());
      if (lifetimeMs <= 0) {
        await closeResource();
        throw new LauncherError('UNAVAILABLE');
      }
      resourceExpiryTimer = setTimeout(() => { void closeResource(); }, lifetimeMs);
      onPluginResource({
        kind: 'native-resource',
        plugin_id: payload.plugin_id,
        menu_index: payload.menu_index,
        url: presentation.url,
      });
      return { opened: true };
    }
    if (response.kind === 'external-resource' && isExternalPluginResource(response.path)) {
      onPluginResource({
        kind: 'external-resource',
        plugin_id: payload.plugin_id,
        menu_index: payload.menu_index,
        path: response.path,
      });
      return { opened: true };
    }
    throw new LauncherError('UNAVAILABLE');
  };

  const dispatch = async (operation, payload, requestId) => {
    if (!unlocked) throw new LauncherError('UNAVAILABLE');
    if (operation === 'management.request' || operation === 'management.transaction') return management(operation, payload, requestId);
    if (operation === 'browser.open') return browser('open', payload);
    if (operation === 'browser.copy') return browser('copy', payload);
    if (operation === 'file.save') return file(payload);
    if (operation === 'plugin.open') return plugin(payload);
    if (operation === 'session.lock') {
      if (!record(payload) || Object.keys(payload).length !== 0) throw new LauncherError('INVALID_INPUT');
      return { locked: true };
    }
    throw new LauncherError('INVALID_INPUT');
  };

  const reply = (id, ok, result) => {
    if (closed || !port) return;
    const message = ok
      ? { type: 'hermes:cpa:response', protocol: CPA_PROTOCOL, session_id: sessionId, id, ok: true, result }
      : { type: 'hermes:cpa:response', protocol: CPA_PROTOCOL, session_id: sessionId, id, ok: false, error: { code: result } };
    try {
      assertWire(message);
      port.postMessage(message);
    } catch { close(); }
  };

  const consumeRateToken = () => {
    const now = Date.now();
    requestTokens = Math.min(120, requestTokens + (now - tokenTime) / 500);
    tokenTime = now;
    if (requestTokens < 1) return false;
    requestTokens -= 1;
    return true;
  };

  const receiveRequest = (message) => {
    if (!record(message) || message.type !== 'hermes:cpa:request' || message.protocol !== CPA_PROTOCOL ||
        message.session_id !== sessionId || !safeId(message.id) || typeof message.operation !== 'string' ||
        !own(message, 'payload')) {
      close();
      return;
    }
    let bytes;
    try { bytes = assertWire(message); } catch { close(); return; }
    if (!consumeRateToken() || inflight.has(message.id) || inflight.size >= MAX_INFLIGHT ||
        inflightBytes + bytes > MAX_INFLIGHT_BYTES) {
      reply(message.id, false, 'RATE_LIMITED');
      return;
    }
    const effectful = isEffectful(message.operation, message.payload);
    const entry = { bytes, settled: false, timer: undefined };
    inflight.set(message.id, entry);
    inflightBytes += bytes;
    entry.timer = setTimeout(() => {
      if (entry.settled) return;
      entry.settled = true;
      inflight.delete(message.id);
      inflightBytes -= bytes;
      reply(message.id, false, effectful ? 'OUTCOME_UNKNOWN' : 'TIMEOUT');
      if (effectful) failUnknown();
    }, REQUEST_TIMEOUT_MS);
    void dispatch(message.operation, message.payload, message.id).then(
      result => {
        if (entry.settled) return;
        entry.settled = true;
        clearTimeout(entry.timer);
        inflight.delete(message.id);
        inflightBytes -= bytes;
        reply(message.id, true, result);
        if (message.operation === 'session.lock') close();
      },
      error => {
        if (entry.settled) return;
        entry.settled = true;
        clearTimeout(entry.timer);
        inflight.delete(message.id);
        inflightBytes -= bytes;
        const code = errorCode(error);
        reply(message.id, false, code);
        if (code === 'OUTCOME_UNKNOWN') failUnknown();
      },
    );
  };

  const acceptConnect = (event, activeFrame) => {
    if (closed || !unlocked || port || event?.source !== activeFrame || !record(event.data) ||
        event.data.type !== 'hermes:cpa:connect' || event.data.protocol !== CPA_PROTOCOL ||
        !safeId(event.data.client_nonce)) return false;
    clientNonce = event.data.client_nonce;
    sessionId = uuid();
    const channel = new MessageChannel();
    port = channel.port1;
    let phase = 'hello';
    handshakeTimer = setTimeout(close, HANDSHAKE_TIMEOUT_MS);
    port.onmessageerror = close;
    port.onmessage = ({ data }) => {
      if (closed || !record(data) || data.protocol !== CPA_PROTOCOL || data.session_id !== sessionId ||
          (phase !== 'ready' && data.client_nonce !== clientNonce)) { close(); return; }
      if (phase === 'hello') {
        if (data.type !== 'hermes:cpa:hello') { close(); return; }
        phase = 'ack';
        port.postMessage({ type: 'hermes:cpa:ready', protocol: CPA_PROTOCOL, session_id: sessionId, client_nonce: clientNonce, unlocked: true });
        return;
      }
      if (phase === 'ack') {
        if (data.type !== 'hermes:cpa:ack') { close(); return; }
        phase = 'ready';
        clearTimeout(handshakeTimer);
        onState('ready');
        return;
      }
      if (phase === 'ready') receiveRequest(data);
    };
    port.start();
    try {
      activeFrame.postMessage({ type: 'hermes:cpa:init', protocol: CPA_PROTOCOL, client_nonce: clientNonce, session_id: sessionId, unlocked: true }, '*', [channel.port2]);
      return true;
    } catch { close(); return false; }
  };

  return {
    get closed() { return closed; },
    get unlocked() { return unlocked; },
    get documentId() { return documentId; },
    async unlock(expectedGeneration) {
      if (closed || unlocked || !Number.isInteger(expectedGeneration) || expectedGeneration < 1) throw new LauncherError('UNAVAILABLE');
      const result = await call('/console/unlock', {
        request_id: uuid(), expected_generation: expectedGeneration, document_id: documentId, consent: true,
      });
      if (!record(result) || result.ok !== true || result.document_id !== documentId ||
          !Number.isInteger(result.generation) || result.generation < 1 ||
          typeof result.lease_id !== 'string' || !safeId(result.lease_id)) throw new LauncherError('UNAVAILABLE');
      leaseId = result.lease_id;
      unlocked = true;
      onState('unlocked');
      return result;
    },
    acceptConnect,
    closeResource,
    close,
  };
}
