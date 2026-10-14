const { test } = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { MessageChannel } = require('node:worker_threads');
const fs = require('node:fs');
const path = require('node:path');

if (!globalThis.crypto) globalThis.crypto = crypto.webcrypto;
if (!globalThis.MessageChannel) globalThis.MessageChannel = MessageChannel;

const source = fs.readFileSync(path.join(__dirname, '../../console/launcher-bridge.js'), 'utf8');
const loadBridge = () => import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function until(predicate, detail = 'condition') {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (predicate()) return;
    await pause(5);
  }
  throw new Error(`Timed out waiting for ${detail}`);
}

test('captured profile replaces any guest-supplied profile query', async () => {
  const { appendProfile } = await loadBridge();
  const url = appendProfile('/console/result?profile=retarget&other=value#fragment', 'original-home');
  const parsed = new URL(url, 'https://launcher.test');
  assert.equal(parsed.searchParams.get('profile'), 'original-home');
  assert.equal(parsed.searchParams.get('other'), 'value');
  assert.equal(parsed.hash, '#fragment');
});

test('desktop bridge keeps lease private, captures profile, serializes host calls, and retains accepted receipts', async t => {
  const { createTrustedLauncherSession } = await loadBridge();
  const calls = [];
  const receipts = [];
  const reviews = [];
  const presented = [];
  let active = 0;
  let peak = 0;
  const documentId = '00000000-0000-4000-8000-000000000001';
  const expiresAt = Math.floor(Date.now() / 1000) + 60;
  const backend = {
    get: async () => ({ data: {} }),
    post: async (url, body) => {
      calls.push({ url, body: structuredClone(body) });
      active += 1;
      peak = Math.max(peak, active);
      await pause(1);
      active -= 1;
      if (url.startsWith('/console/unlock')) return { data: { ok: true, lease_id: 'lease_private_1234567890', generation: 7, document_id: body.document_id } };
      if (url.startsWith('/console/prepare')) return {
        data: { review_id: `review-${body.request_id}`, summary: 'Read native config', effects: [], requires_confirmation: false },
      };
      if (url.startsWith('/console/execute')) return { data: { operation_id: `operation-${body.request_id}` } };
      if (url.startsWith('/console/result')) return {
        data: {
          operation_id: body.operation_id,
          state: 'succeeded',
          result: { status: 200, headers: {}, body: { kind: 'json', value: { ok: true } } },
        },
      };
      if (url.startsWith('/console/plugin')) {
        if (!body.review_id) return {
          data: { review_id: 'plugin-review', summary: 'Native plugin handler', effects: ['plugin_code'], requires_confirmation: false },
        };
        return {
          data: {
            ok: true, kind: 'native-resource', grant_id: 'resource-grant-1',
            entry_path: '/v0/resource/plugins/demo/index.html', plugin_id: 'demo',
            expires_at: expiresAt,
          },
        };
      }
      if (url.startsWith('/console/resource/close')) return { data: { ok: true } };
      if (url.startsWith('/console/lock')) return { data: { ok: true, locked: true } };
      throw new Error(`unexpected route ${url}`);
    },
  };
  let childPort;
  const session = createTrustedLauncherSession({
    backend,
    profile: 'original-home',
    documentId,
    review: async details => { reviews.push(details); return true; },
    capabilities: {
      presentNativeResource: async request => {
        presented.push(request);
        return { url: 'hermes-plugin-resource://p-grant/v0/resource/plugins/demo/index.html', close: async () => { presented.push('closed'); } };
      },
    },
    onReceipt: receipt => receipts.push(receipt),
  });
  t.after(async () => { await session.close(); childPort?.close(); });
  await session.unlock(7);

  const responses = [];
  const frame = {
    postMessage(init, target, ports) {
      assert.equal(target, '*');
      assert.equal(init.type, 'hermes:cpa:init');
      assert.equal(init.protocol, 3);
      assert.equal(init.unlocked, true);
      assert.equal(JSON.stringify(init).includes('lease_private_1234567890'), false);
      childPort = ports[0];
      childPort.onmessage = ({ data }) => {
        if (data.type === 'hermes:cpa:ready') {
          childPort.postMessage({ type: 'hermes:cpa:ack', protocol: 3, session_id: init.session_id, client_nonce: init.client_nonce });
          childPort.postMessage({
            type: 'hermes:cpa:request', protocol: 3, session_id: init.session_id, id: 'one', operation: 'management.request',
            payload: { method: 'GET', path: '/v8/management/config', body: { kind: 'empty' }, response_kind: 'json' },
          });
          childPort.postMessage({
            type: 'hermes:cpa:request', protocol: 3, session_id: init.session_id, id: 'two', operation: 'management.request',
            payload: { method: 'GET', path: '/v8/management/plugins', body: { kind: 'empty' }, response_kind: 'json' },
          });
          childPort.postMessage({
            type: 'hermes:cpa:request', protocol: 3, session_id: init.session_id, id: 'plugin', operation: 'plugin.open',
            payload: { plugin_id: 'demo', menu_index: 0 },
          });
        } else if (data.type === 'hermes:cpa:response') {
          responses.push(data);
        }
      };
      childPort.start();
      childPort.postMessage({ type: 'hermes:cpa:hello', protocol: 3, session_id: init.session_id, client_nonce: init.client_nonce });
    },
  };
  assert.equal(session.acceptConnect({ source: {}, data: { type: 'hermes:cpa:connect', protocol: 3, client_nonce: 'client_nonce_123' } }, frame), false);
  assert.equal(session.acceptConnect({ source: frame, data: { type: 'hermes:cpa:connect', protocol: 3, client_nonce: 'client_nonce_123' } }, frame), true);
  await until(() => responses.length === 3, 'both management replies and the approved plugin presentation');

  assert.deepEqual(responses.map(response => response.ok), [true, true, true]);
  const pluginReply = responses.find(response => response.id === 'plugin');
  assert.deepEqual(pluginReply.result, { opened: true });
  assert.equal(JSON.stringify(pluginReply).includes('resource-grant-1'), false, 'the isolated resource grant never crosses the console port');
  assert.equal(peak, 1, 'the declared-large host route is never overlapped');
  assert.ok(receipts.every(receipt => receipt.profile === 'original-home'));
  const execute = calls.filter(call => call.url.startsWith('/console/execute'));
  assert.deepEqual([...new Map(receipts.map(receipt => [receipt.operation_id, receipt.state]))],
    execute.map(call => [`operation-${call.body.request_id}`, 'succeeded']));
  assert.equal(reviews.length, 1, 'native plugin resources always require outer confirmation');
  assert.match(reviews[0].risk, /read or change credentials/i);
  assert.deepEqual(presented[0], {
    documentId,
    grantId: 'resource-grant-1',
    entryPath: '/v0/resource/plugins/demo/index.html',
    expiresAt,
  });
  assert.ok(calls.every(call => call.url.endsWith('?profile=original-home')));
  const [unlock] = calls;
  assert.equal('lease_id' in unlock.body, false);
  assert.equal(unlock.body.document_id, documentId);
  for (const call of calls.filter(call => !call.url.startsWith('/console/unlock') && !call.url.startsWith('/console/resource/close'))) {
    assert.equal(call.body.document_id, documentId);
    assert.equal(call.body.lease_id, 'lease_private_1234567890');
  }
  assert.equal(execute.length, 2);
  assert.equal(new Set(execute.map(call => call.body.request_id)).size, 2);
  for (const call of execute) assert.match(call.body.request_id, /^[A-Za-z0-9_.:-]{1,128}$/);
  assert.deepEqual(calls.filter(call => call.url.startsWith('/console/prepare')).map(call => call.body.request_id),
    execute.map(call => call.body.request_id));
  assert.deepEqual(responses.filter(response => response.id !== 'plugin').map(response => response.id).sort(), ['one', 'two']);
  assert.deepEqual(receipts.filter(receipt => receipt.state === 'accepted').map(receipt => receipt.request_id),
    execute.map(call => call.body.request_id));

  await session.close();
  await until(() => calls.some(call => call.url.startsWith('/console/resource/close')), 'native resource revocation');
  await until(() => calls.some(call => call.url.startsWith('/console/lock')), 'captured-home cleanup');
  const resourceClose = calls.find(call => call.url.startsWith('/console/resource/close'));
  assert.deepEqual(resourceClose.body, { document_id: documentId, grant_id: 'resource-grant-1' });
  assert.equal(presented.includes('closed'), true);
  const lock = calls.find(call => call.url.startsWith('/console/lock'));
  assert.equal(lock.url, '/console/lock?profile=original-home');
  assert.ok(calls.indexOf(resourceClose) < calls.indexOf(lock), 'the resource grant is revoked before the console lease');
});

test('v3 bridge rejects stale handshakes before any backend mutation', async () => {
  const { createTrustedLauncherSession } = await loadBridge();
  let calls = 0;
  const session = createTrustedLauncherSession({
    backend: { get: async () => ({}), post: async (_url, body) => {
      calls += 1;
      return { data: { ok: true, lease_id: 'lease_private_1234567890', generation: 1, document_id: body.document_id } };
    } },
    profile: 'home', documentId: '00000000-0000-4000-8000-000000000002', review: async () => true, capabilities: {},
  });
  await session.unlock(1);
  const frame = { postMessage() { assert.fail('invalid connect must not transfer a port'); } };
  assert.equal(session.acceptConnect({ source: frame, data: { type: 'hermes:cpa:connect', protocol: 2, client_nonce: 'nonce' } }, frame), false);
  assert.equal(calls, 1);
  session.close({ notifyGuest: false });
});

// ---- scriptable owner/host fixture shared by the regressions below ----
const DOCUMENT_ID = '00000000-0000-4000-8000-0000000000aa';
const LEASE_ID = 'lease_private_1234567890';
const fastTiming = { pollIntervalMs: 1, pollMaxIntervalMs: 2, revokeRetryMs: 1 };
const readRequest = { method: 'GET', path: '/v8/management/config', body: { kind: 'empty' }, response_kind: 'json' };
const writeRequest = {
  method: 'POST', path: '/v8/management/credentials/refresh', body: { kind: 'json', value: { name: 'fixture.json' } }, response_kind: 'json',
};
const prepared = body => ({ data: { review_id: `review-${body.request_id}`, summary: 'summary', effects: [], requires_confirmation: false } });
const accepted = body => ({ data: { operation_id: `operation-${body.request_id}` } });
const succeeded = body => ({
  data: { operation_id: body.operation_id, state: 'succeeded', result: { status: 200, headers: {}, body: { kind: 'json', value: { ok: true } } } },
});
// What the Dashboard's ApiError and the Desktop renderer Error look like for the host's explicit pre-dispatch refusal.
const dashboardBusy = () => Object.assign(new Error('Too Many Requests'), {
  name: 'ApiError', status: 429, body: JSON.stringify({ error: 'PLUGIN_NOT_FORWARDED_BUSY', forwarded: false }),
});
const desktopBusy = () => Object.assign(new Error('busy'), { code: 'PLUGIN_NOT_FORWARDED_BUSY', forwarded: false });

function fixture(handlers) {
  const calls = [];
  const backend = {
    get: async () => ({ data: {} }),
    post: async (url, body, options) => {
      const name = url.split('?')[0];
      calls.push({ url, name, body, options });
      if (name === '/console/unlock') return { data: { ok: true, lease_id: LEASE_ID, generation: 1, document_id: body.document_id } };
      const handler = handlers[name];
      if (!handler) throw new Error(`unexpected route ${url}`);
      return handler(body);
    },
  };
  return { backend, calls, count: name => calls.filter(call => call.name === name).length };
}

function connectGuest(session) {
  const waiting = new Map();
  const state = { locked: false };
  let port;
  let sessionId;
  const ready = new Promise(resolve => {
    const frame = {
      postMessage(init, _target, ports) {
        sessionId = init.session_id;
        port = ports[0];
        port.onmessage = ({ data }) => {
          if (data.type === 'hermes:cpa:ready') {
            port.postMessage({ type: 'hermes:cpa:ack', protocol: 3, session_id: init.session_id, client_nonce: init.client_nonce });
            resolve();
          } else if (data.type === 'hermes:cpa:locked') state.locked = true;
          else if (data.type === 'hermes:cpa:response') waiting.get(data.id)?.(data);
        };
        port.start();
        port.postMessage({ type: 'hermes:cpa:hello', protocol: 3, session_id: init.session_id, client_nonce: init.client_nonce });
      },
    };
    assert.equal(session.acceptConnect({ source: frame, data: { type: 'hermes:cpa:connect', protocol: 3, client_nonce: 'client_nonce_123' } }, frame), true);
  });
  return {
    state,
    ready,
    request: (id, operation, payload) => new Promise(resolve => {
      waiting.set(id, resolve);
      port.postMessage({ type: 'hermes:cpa:request', protocol: 3, session_id: sessionId, id, operation, payload });
    }),
    close: () => port?.close(),
  };
}

async function openSession(t, handlers, extra = {}) {
  const bridge = await loadBridge();
  const host = fixture(handlers);
  const receipts = [];
  const states = [];
  const session = bridge.createTrustedLauncherSession({
    backend: host.backend, profile: 'original-home', documentId: DOCUMENT_ID, review: async () => true, capabilities: {},
    timing: fastTiming, onReceipt: receipt => receipts.push(receipt), onState: state => states.push(state), ...extra,
  });
  await session.unlock(1);
  const guest = connectGuest(session);
  await guest.ready;
  t.after(async () => { await session.close(); guest.close(); });
  return { session, guest, host, receipts, states, bridge };
}

test('owner IDs isolate reopened documents and deduplicate settled requests within the same document', async t => {
  const reviews = new Map();
  const retained = new Map();
  const reviewed = [];
  let nativeExecutions = 0;
  const handlers = {
    '/console/prepare': body => {
      const prior = [...reviews].find(([, review]) =>
        review.document_id === body.document_id && review.request_id === body.request_id);
      if (prior) assert.deepEqual(body, prior[1], 'a duplicate ID must retain its original request');
      const reviewId = prior?.[0] ?? crypto.randomUUID();
      if (!prior) reviews.set(reviewId, structuredClone(body));
      return { data: { review_id: reviewId, summary: 'Write native config', effects: ['config_write'], requires_confirmation: true } };
    },
    '/console/execute': body => {
      const review = reviews.get(body.review_id);
      assert.ok(review);
      assert.equal(body.request_id, review.request_id, 'prepare and execute keep one owner request ID');
      assert.equal(body.document_id, review.document_id);
      assert.deepEqual(body.approved_effects, ['config_write']);
      const accepted = retained.get(body.request_id);
      if (accepted && accepted.documentId !== body.document_id) throw new Error('REQUEST_CONFLICT');
      if (!accepted) {
        nativeExecutions += 1;
        retained.set(body.request_id, { documentId: body.document_id });
      }
      return { data: { operation_id: body.request_id } };
    },
    '/console/result': body => {
      assert.equal(retained.get(body.operation_id)?.documentId, body.document_id,
        'poll exactly the accepted operation in its originating document');
      return succeeded(body);
    },
    '/console/lock': () => ({ data: { ok: true, accepted_operations_retained: true } }),
  };
  const review = async details => { reviewed.push(details); return true; };
  const request = { method: 'PUT', path: '/v8/management/api-keys',
    body: { kind: 'json', value: ['generated-fixture-key'] }, response_kind: 'json' };
  const original = await openSession(t, handlers, { review });
  const firstReply = await original.guest.request('1', 'management.request', request);
  assert.equal(firstReply.ok, true);
  assert.equal(firstReply.id, '1');
  const firstOperation = original.receipts.find(receipt => receipt.state === 'accepted').operation_id;
  const duplicateReply = await original.guest.request('1', 'management.request', request);
  assert.equal(duplicateReply.ok, true);
  assert.equal(duplicateReply.id, '1');
  assert.equal(nativeExecutions, 1, 'a repeated settled guest ID must reconcile its receipt, not execute the native effect again');
  assert.equal(retained.size, 1);
  assert.deepEqual(original.receipts.filter(receipt => receipt.state === 'accepted').map(receipt => receipt.operation_id),
    [firstOperation, firstOperation], 'same-document repeats retain the original owner operation ID');
  await original.session.close();
  assert.equal(original.host.count('/console/lock'), 1);
  assert.equal(retained.has(firstOperation), true, 'closing a document must not erase its accepted receipt');

  const reopened = await openSession(t, handlers, { documentId: crypto.randomUUID(), review });
  const secondReply = await reopened.guest.request('1', 'management.request', request);
  assert.equal(secondReply.ok, true, 'the first execute after reopening must not collide with the old guest ID');
  assert.equal(secondReply.id, '1', 'guest response correlation does not become an owner operation ID');
  const longGuestId = 'g'.repeat(128);
  const transactionReply = await reopened.guest.request(longGuestId, 'management.transaction', {
    requests: [request], expected_revision: 'a'.repeat(64),
  });
  assert.equal(transactionReply.ok, true);
  assert.equal(transactionReply.id, longGuestId, 'the full supported guest ID remains valid');
  assert.equal(retained.size, 3);
  assert.equal(nativeExecutions, 3, 'only the first request, reopened request and distinct transaction execute native effects');
  assert.equal(original.host.count('/console/execute'), 2, 'only the two explicit guest attempts reach execute');
  assert.equal(reopened.host.count('/console/execute'), 2, 'no execute is replayed to repair an ID collision');
  assert.equal(original.host.count('/console/result'), 2);
  assert.equal(reopened.host.count('/console/result'), 2);
  assert.equal(reviewed.length, 4, 'each effectful guest attempt still requires its own trusted review');
  for (const details of reviewed) assert.deepEqual(details.effects, ['config_write']);
  for (const host of [original, reopened]) {
    assert.equal(host.states.includes('outcome-unknown'), false);
    const acceptedIds = host.receipts.filter(receipt => receipt.state === 'accepted').map(receipt => receipt.operation_id);
    assert.deepEqual(host.receipts.filter(receipt => receipt.state === 'succeeded').map(receipt => receipt.operation_id), acceptedIds);
    assert.deepEqual(host.host.calls.filter(call => call.name === '/console/result').map(call => call.body.operation_id), acceptedIds);
    for (const id of acceptedIds) {
      assert.match(id, /^[A-Za-z0-9_.:-]{1,128}$/, 'owner IDs stay within the existing identifier contract');
      assert.equal(id.includes(host.session.documentId), false, 'receipt IDs do not disclose the trusted document ID');
    }
  }
});

// ---- review 1 and 5: fractional owner expiry and a skewed owner clock ----
test('resource lifetime accepts fractional owner expiry and never trusts a skewed owner clock', async () => {
  const { resourceLifetimeMs, RESOURCE_MAX_LIFETIME_MS } = await loadBridge();
  const now = 1_700_000_000_000;
  const started = now - 250;
  assert.equal(resourceLifetimeMs(now / 1000 + 60.25, started, now), 60_250, 'a fractional owner expiry is honored while it is plausible');
  assert.equal(resourceLifetimeMs(now / 1000 + 3600.5, started, now), RESOURCE_MAX_LIFETIME_MS - 250, 'an owner clock an hour ahead cannot extend the grant');
  assert.equal(resourceLifetimeMs(now / 1000 - 120.5, started, now), RESOURCE_MAX_LIFETIME_MS - 250, 'an owner clock two minutes behind must not close a fresh grant');
  assert.equal(resourceLifetimeMs(now / 1000 + 10, now - RESOURCE_MAX_LIFETIME_MS - 1, now), 0, 'a response delayed past the maximum grant is closed');
});

test('a native resource with a fractional owner expiry opens, keeps the owner value, and arms a clamped local timer', async t => {
  const delays = [];
  const realSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (callback, ms, ...args) => {
    if (ms >= 60_000 && ms < 600_000) delays.push(ms);
    return realSetTimeout(callback, ms, ...args);
  };
  t.after(() => { globalThis.setTimeout = realSetTimeout; });
  const presented = [];
  const expiries = [Date.now() / 1000 + 3600.5, Date.now() / 1000 - 120.5];
  let opened = 0;
  const { guest, host } = await openSession(t, {
    '/console/plugin': body => {
      if (!body.review_id) return { data: { review_id: 'plugin-review', summary: 'Native plugin handler', effects: ['plugin_code'], requires_confirmation: true } };
      return { data: {
        ok: true, kind: 'native-resource', grant_id: `resource-grant-${opened + 1}`, plugin_id: 'demo',
        entry_path: '/v0/resource/plugins/demo/index.html', expires_at: expiries[opened++],
      } };
    },
    '/console/resource/close': () => ({ data: { ok: true } }),
  }, {
    capabilities: {
      presentNativeResource: async request => {
        presented.push(request);
        return { url: 'hermes-plugin-resource://p-grant/v0/resource/plugins/demo/index.html', close: async () => {} };
      },
    },
  });
  for (const [index, expiresAt] of expiries.entries()) {
    const reply = await guest.request(`open-${index}`, 'plugin.open', { plugin_id: 'demo', menu_index: 0 });
    assert.deepEqual(reply.result, { opened: true }, 'neither a fractional nor a skewed owner expiry is UNAVAILABLE');
    assert.equal(presented[index].expiresAt, expiresAt, 'the host receives the owner value untouched');
  }
  assert.equal(delays.length, 2);
  for (const delay of delays) assert.ok(delay > 299_000 && delay <= 300_000, `local timer ${delay} stays within the five minute maximum`);
  assert.equal(host.count('/console/resource/close'), 1, 'opening the second resource revokes the first grant');
});

// ---- review 3: accepted operations and result polls ----
test('an accepted operation survives failed, busy and malformed result polls without resubmitting', async t => {
  let polls = 0;
  const { guest, host, receipts, states } = await openSession(t, {
    '/console/prepare': prepared,
    '/console/execute': accepted,
    '/console/result': body => {
      polls += 1;
      if (polls === 1) throw new Error('socket hang up');
      if (polls === 2) throw desktopBusy();
      if (polls === 3) return { data: { operation_id: 'another-operation', state: 'succeeded' } };
      if (polls === 4) return { data: { state: 'mystery' } };
      if (polls === 5) return { data: { operation_id: body.operation_id, state: 'running' } };
      return succeeded(body);
    },
  });
  const reply = await guest.request('write-1', 'management.request', writeRequest);
  assert.equal(reply.ok, true);
  assert.equal(reply.result.status, 200);
  assert.equal(polls, 6);
  assert.equal(host.count('/console/execute'), 1, 'an accepted write is never submitted twice');
  assert.deepEqual([...new Set(receipts.map(receipt => receipt.state))], ['accepted', 'running', 'succeeded']);
  assert.equal(states.includes('outcome-unknown'), false);
  assert.equal(host.count('/console/lock'), 0, 'the session stays unlocked');
});

test('an accepted operation whose polls keep failing ends OUTCOME_UNKNOWN, never UNAVAILABLE, and locks without resubmitting', async t => {
  const realNow = Date.now;
  let offset = 0;
  Date.now = () => realNow() + offset;
  t.after(() => { Date.now = realNow; });
  let polls = 0;
  const { guest, host, receipts, states } = await openSession(t, {
    '/console/prepare': prepared,
    '/console/execute': accepted,
    '/console/result': () => {
      polls += 1;
      if (polls === 3) offset = 11 * 60 * 1000;
      throw new Error('network down');
    },
    '/console/lock': () => ({ data: { ok: true } }),
  });
  const reply = await guest.request('write-1', 'management.request', writeRequest);
  assert.equal(reply.ok, false);
  assert.deepEqual(reply.error, { code: 'OUTCOME_UNKNOWN' });
  assert.ok(states.includes('outcome-unknown'));
  assert.equal(host.count('/console/execute'), 1, 'the unknown operation is never submitted again');
  assert.equal(receipts.at(-1).state, 'unknown');
  await until(() => host.count('/console/lock') === 1, 'lock after the unknown outcome');
});

test('a poll that proves the pinned owner is gone stays OUTCOME_UNKNOWN immediately', async t => {
  let polls = 0;
  const { guest, host } = await openSession(t, {
    '/console/prepare': prepared,
    '/console/execute': accepted,
    '/console/result': () => { polls += 1; throw new Error('STALE_SCOPE'); },
    '/console/lock': () => ({ data: { ok: true } }),
  });
  const reply = await guest.request('write-1', 'management.request', writeRequest);
  assert.deepEqual(reply.error, { code: 'OUTCOME_UNKNOWN' });
  assert.equal(polls, 1);
  assert.equal(host.count('/console/execute'), 1);
});

test('an accepted operation with an unusable receipt or an oversize result is OUTCOME_UNKNOWN', async t => {
  const { guest: garbled } = await openSession(t, {
    '/console/prepare': prepared,
    '/console/execute': () => ({ data: { operation_id: 'not a safe id!' } }),
    '/console/lock': () => ({ data: { ok: true } }),
  });
  assert.deepEqual((await garbled.request('write-1', 'management.request', writeRequest)).error, { code: 'OUTCOME_UNKNOWN' });
  const { guest: oversize } = await openSession(t, {
    '/console/prepare': prepared,
    '/console/execute': accepted,
    '/console/result': body => ({ data: {
      operation_id: body.operation_id, state: 'succeeded',
      result: { status: 200, headers: {}, body: { kind: 'bytes', base64: 'A'.repeat(44_739_252 + 4), content_type: 'application/octet-stream' } },
    } }),
    '/console/lock': () => ({ data: { ok: true } }),
  });
  assert.deepEqual((await oversize.request('write-2', 'management.request', writeRequest)).error, { code: 'OUTCOME_UNKNOWN' });
});

// ---- busy before forwarding ----
test('only the host\'s exact pre-forwarding refusal proves a request was not forwarded', async () => {
  const { provablyNotForwarded } = await loadBridge();
  const body = JSON.stringify({ error: 'PLUGIN_NOT_FORWARDED_BUSY', forwarded: false });
  assert.equal(provablyNotForwarded(desktopBusy()), true);
  assert.equal(provablyNotForwarded(dashboardBusy()), true);
  assert.equal(provablyNotForwarded({ status: 429, body: JSON.parse(body) }), true);
  for (const ambiguous of [
    null, 'busy', new Error('socket hang up'),
    Object.assign(new Error('x'), { code: 'PLUGIN_NOT_FORWARDED_BUSY', forwarded: true }),
    Object.assign(new Error('x'), { code: 'PLUGIN_NOT_FORWARDED_BUSY' }),
    Object.assign(new Error('x'), { code: 'BUSY', forwarded: false }),
    Object.assign(new Error('x'), { status: 503, body }),
    Object.assign(new Error('x'), { status: 429, body: '{"error":"PLUGIN_NOT_FORWARDED_BUSY"}' }),
    Object.assign(new Error('x'), { status: 429, body: '{"error":"PLUGIN_NOT_FORWARDED_BUSY","forwarded":true}' }),
    Object.assign(new Error('x'), { status: 429, body: '{"error":"RATE_LIMITED","forwarded":false}' }),
    Object.assign(new Error('x'), { status: 429, body: 'not json' }),
    Object.assign(new Error('x'), { status: 429 }),
  ]) assert.equal(provablyNotForwarded(ambiguous), false);
});

test('a host refusal before forwarding is a definitive HOST_BUSY on both surfaces and leaves the session usable', async t => {
  let prepares = 0;
  let executes = 0;
  const { guest, host, states } = await openSession(t, {
    '/console/prepare': body => { if (++prepares === 1) throw dashboardBusy(); return prepared(body); },
    '/console/execute': body => { if (++executes === 1) throw desktopBusy(); return accepted(body); },
    '/console/result': succeeded,
    '/console/lock': () => ({ data: { ok: true } }),
  });
  const beforePrepare = await guest.request('prepare-busy', 'management.request', writeRequest);
  assert.deepEqual(beforePrepare.error, { code: 'HOST_BUSY' });
  assert.equal(host.count('/console/execute'), 0);
  const beforeExecute = await guest.request('execute-busy', 'management.request', writeRequest);
  assert.deepEqual(beforeExecute.error, { code: 'HOST_BUSY' }, 'a refused execute provably did not run');
  assert.equal(states.includes('outcome-unknown'), false);
  assert.equal(host.count('/console/lock'), 0);
  assert.equal(guest.state.locked, false);
  const later = await guest.request('later', 'management.request', readRequest);
  assert.equal(later.ok, true, 'the same unlocked session still works');
});

test('every other failure after approval stays OUTCOME_UNKNOWN and locks', async t => {
  const failures = [
    () => new Error('socket hang up'),
    () => Object.assign(new Error('x'), { code: 'PLUGIN_NOT_FORWARDED_BUSY', forwarded: true }),
    () => Object.assign(new Error('x'), { status: 429, body: '{"error":"RATE_LIMITED"}' }),
    () => Object.assign(new Error('x'), { status: 503, body: '{"error":"PLUGIN_NOT_FORWARDED_BUSY","forwarded":false}' }),
  ];
  for (const failure of failures) {
    const { guest, host, states } = await openSession(t, {
      '/console/prepare': prepared,
      '/console/execute': () => { throw failure(); },
      '/console/lock': () => ({ data: { ok: true } }),
    });
    const reply = await guest.request('write-1', 'management.request', writeRequest);
    assert.deepEqual(reply.error, { code: 'OUTCOME_UNKNOWN' });
    assert.ok(states.includes('outcome-unknown'));
    await until(() => host.count('/console/lock') === 1, 'lock after an ambiguous execute');
  }
});

test('revocation retries a refusal that never reached the owner, ignores other failures, and never queues behind a hung call', async t => {
  let lockCalls = 0;
  const { session, host } = await openSession(t, {
    '/console/lock': () => { lockCalls += 1; if (lockCalls < 3) throw desktopBusy(); return { data: { ok: true } }; },
  });
  await session.close();
  assert.equal(host.count('/console/lock'), 3, 'two provable refusals are retried until the owner accepts the revoke');

  const quiet = await openSession(t, { '/console/lock': () => { throw new Error('connection reset'); } });
  await quiet.session.close();
  assert.equal(quiet.host.count('/console/lock'), 1, 'an ambiguous revoke failure stays best effort');

  const hung = await openSession(t, {
    '/console/prepare': prepared,
    '/console/execute': accepted,
    '/console/result': () => new Promise(() => {}),
    '/console/lock': () => ({ data: { ok: true } }),
  });
  void hung.guest.request('hung', 'management.request', writeRequest);
  await until(() => hung.host.count('/console/result') === 1, 'a result poll that never returns');
  const cleanup = hung.session.close();
  await Promise.race([cleanup, pause(1000).then(() => { throw new Error('revocation starved behind the hung poll'); })]);
  assert.equal(hung.host.count('/console/lock'), 1);
});

// ---- review 11: export bound is separate from the 10 MiB upload bound ----
test('base64 validation and decoding are exact at the export bound', async () => {
  const { base64ByteLength, decodeBase64, MAX_EXPORT_BYTES, MAX_RESPONSE_BYTES } = await loadBridge();
  assert.equal(MAX_EXPORT_BYTES, 32 * 1024 * 1024);
  assert.equal(MAX_EXPORT_BYTES, MAX_RESPONSE_BYTES);
  for (const length of [0, 1, 2, 3, 4, 5, 255, 256, 257, 1000]) {
    const original = Buffer.alloc(length, 0).map((_, index) => (index * 37 + length) & 255);
    const encoded = original.toString('base64');
    assert.equal(base64ByteLength(encoded, 2000), length);
    assert.equal(Buffer.compare(Buffer.from(decodeBase64(encoded, 2000)), original), 0);
  }
  const atLimit = 'A'.repeat(44_739_240) + 'AQI=';
  assert.equal(base64ByteLength(atLimit, MAX_EXPORT_BYTES), MAX_EXPORT_BYTES);
  assert.throws(() => base64ByteLength('A'.repeat(44_739_244), MAX_EXPORT_BYTES), { code: 'PAYLOAD_TOO_LARGE' });
  const decoded = decodeBase64(atLimit, MAX_EXPORT_BYTES);
  assert.equal(decoded.byteLength, MAX_EXPORT_BYTES);
  assert.equal(decoded.buffer.byteLength, MAX_EXPORT_BYTES, 'one exactly sized buffer, no copy needed');
  assert.deepEqual([...decoded.subarray(MAX_EXPORT_BYTES - 3)], [0, 1, 2]);
  for (const malformed of ['AAA', 'AA=A', 'A===', '====', 'AA A', 'AAA\n', 'AÃA=', '=AAA']) {
    assert.throws(() => base64ByteLength(malformed, 100), { code: 'PAYLOAD_TOO_LARGE' }, malformed);
  }
});

test('file.save exports exactly 32 MiB end to end, far above the 10 MiB upload limit', async t => {
  const saved = [];
  const encoded = 'A'.repeat(44_739_240) + 'AQI=';
  const { guest, host } = await openSession(t, {
    '/console/file': body => body.review_id
      ? { data: { ok: true, filename: 'export.bin', content_type: 'application/octet-stream', base64: encoded } }
      : { data: { review_id: 'file-review', summary: 'Save export', effects: ['save_file'], requires_confirmation: true } },
  }, {
    capabilities: { saveFile: async (request, contentType) => { saved.push({ request, contentType }); return { status: 'saved' }; } },
  });
  const reply = await guest.request('export', 'file.save', { filename: 'export.bin', content_type: 'application/octet-stream', base64: encoded });
  assert.deepEqual(reply.result, { status: 'saved' });
  assert.equal(host.count('/console/file'), 2);
  assert.equal(saved[0].contentType, 'application/octet-stream');
  assert.ok(saved[0].request.bytes instanceof ArrayBuffer);
  assert.equal(saved[0].request.bytes.byteLength, 32 * 1024 * 1024);
  assert.deepEqual([...new Uint8Array(saved[0].request.bytes, 32 * 1024 * 1024 - 2)], [1, 2]);
});

test('file.save rejects one byte over the export bound before any host call', async t => {
  const { guest, host } = await openSession(t, {
    '/console/file': () => assert.fail('an oversize export must never reach the owner'),
  }, { capabilities: { saveFile: async () => assert.fail('nothing may be saved') } });
  const reply = await guest.request('export', 'file.save', {
    filename: 'export.bin', content_type: 'application/octet-stream', base64: 'A'.repeat(44_739_244),
  });
  assert.deepEqual(reply.error, { code: 'PAYLOAD_TOO_LARGE' });
  assert.equal(host.count('/console/file'), 0);
});

// ---- review 12: trusted outer open action ----
test('external open settles only on link activation; cancel, timeout and disposal report not opened', async () => {
  const { createExternalOpenGate } = await loadBridge();
  const shown = [];
  const gate = createExternalOpenGate(value => shown.push(value), 25);
  const url = 'https://example.test/oauth?state=abc&code_challenge=xyz';

  const opened = gate.request(url);
  await pause(1);
  assert.deepEqual(shown, [{ url }], 'the exact reviewed URL is what the outer UI shows');
  gate.activate();
  await opened;
  assert.deepEqual(shown, [{ url }, null]);

  const declined = gate.request(url);
  await pause(1);
  gate.cancel();
  await assert.rejects(declined, { code: 'CONSENT_DECLINED' });

  await assert.rejects(gate.request(url), { code: 'TIMEOUT' });

  gate.activate();
  gate.cancel();
  await assert.rejects(gate.request('javascript:alert(1)'), { code: 'INVALID_INPUT' });
  await assert.rejects(gate.request('data:text/html,<script>1</script>'), { code: 'INVALID_INPUT' });

  const first = gate.request('https://example.test/first');
  const second = gate.request('https://example.test/second');
  await pause(1);
  assert.equal(shown.at(-1).url, 'https://example.test/first', 'requests are presented one at a time');
  gate.activate();
  await first;
  await pause(1);
  assert.equal(shown.at(-1).url, 'https://example.test/second');
  gate.dispose();
  await assert.rejects(second, { code: 'UNAVAILABLE' });
  await assert.rejects(gate.request(url), { code: 'UNAVAILABLE' });
  assert.equal(shown.at(-1), null);
});
