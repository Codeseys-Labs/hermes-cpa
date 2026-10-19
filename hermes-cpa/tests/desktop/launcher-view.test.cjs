const { test } = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const { MessageChannel } = require('node:worker_threads');

if (!globalThis.crypto) globalThis.crypto = crypto.webcrypto;
if (!globalThis.MessageChannel) globalThis.MessageChannel = MessageChannel;

// The shared outer view runs unmodified against a tiny hooks runtime, so lifecycle ordering (effects, cleanup,
// the StrictMode double mount) is what the real component code does, not a model of it.
const read = name => fs.readFileSync(path.join(__dirname, '../../console', name), 'utf8');
const dataUrl = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`;
const viewSource = read('launcher-view.js').replace("from './launcher-bridge.js'", `from '${dataUrl(read('launcher-bridge.js'))}'`);
assert.ok(viewSource.includes('data:text/javascript;base64,'), 'the view must import the fixture-loaded bridge');
const loadView = () => import(dataUrl(viewSource));

const html = '<!doctype html><title>cpa</title>';
const sha256 = crypto.createHash('sha256').update(html).digest('hex');
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function replyWithin(promise) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('dialog required')), 2000); }),
    ]);
  } finally {
    clearTimeout(timer);
  }
}
async function until(predicate, detail) {
  for (let attempt = 0; attempt < 400; attempt += 1) {
    const value = predicate();
    if (value) return value;
    await pause(5);
  }
  throw new Error(`Timed out waiting for ${detail}`);
}

const windowListeners = new Set();
globalThis.window = {
  addEventListener: (_type, listener) => windowListeners.add(listener),
  removeEventListener: (_type, listener) => windowListeners.delete(listener),
  location: { reload() {} },
};

function createRuntime() {
  const slots = [];
  let cursor = 0;
  let effects = [];
  let unmounted = false;
  let queued = false;
  let component;
  let props;
  const state = { tree: undefined };
  const sameDeps = (a, b) => a.length === b.length && a.every((value, index) => Object.is(value, b[index]));
  const commit = () => {
    for (const effect of effects) {
      const slot = slots[effect.index];
      if (slot.deps && effect.deps && sameDeps(slot.deps, effect.deps)) continue;
      slot.cleanup?.();
      slot.deps = effect.deps;
      slot.cleanup = effect.run() ?? undefined;
    }
  };
  const render = () => {
    cursor = 0;
    effects = [];
    state.tree = component(props);
    commit();
  };
  const schedule = () => {
    if (queued) return;
    queued = true;
    queueMicrotask(() => { queued = false; if (!unmounted) render(); });
  };
  const React = {
    createElement: (type, elementProps, ...children) => ({ type, props: elementProps ?? {}, children: children.flat() }),
    useState(initial) {
      const index = cursor++;
      const slot = (slots[index] ??= { value: typeof initial === 'function' ? initial() : initial });
      slot.set ??= update => {
        const next = typeof update === 'function' ? update(slot.value) : update;
        if (Object.is(next, slot.value)) return;
        slot.value = next;
        schedule();
      };
      return [slot.value, slot.set];
    },
    useRef(initial) {
      const index = cursor++;
      return (slots[index] ??= { current: initial });
    },
    useCallback(callback, deps) {
      const index = cursor++;
      const slot = slots[index];
      if (slot && sameDeps(slot.deps, deps)) return slot.callback;
      slots[index] = { callback, deps };
      return callback;
    },
    useEffect(run, deps) {
      const index = cursor++;
      slots[index] ??= { effect: true };
      effects.push({ index, run, deps });
    },
  };
  return {
    React,
    mount(nextComponent, nextProps) {
      component = nextComponent;
      props = nextProps;
      render();
      return {
        get tree() { return state.tree; },
        // React StrictMode in development: every effect is cleaned up and run again on the same render.
        strictRemount() {
          for (const slot of slots) {
            if (!slot?.effect) continue;
            slot.cleanup?.();
            slot.cleanup = undefined;
            slot.deps = undefined;
          }
          commit();
        },
        unmount() {
          unmounted = true;
          for (const slot of slots) if (slot?.effect) slot.cleanup?.();
        },
      };
    },
  };
}

function find(node, predicate) {
  if (node === null || typeof node !== 'object') return undefined;
  if (Array.isArray(node)) {
    for (const item of node) {
      const found = find(item, predicate);
      if (found) return found;
    }
    return undefined;
  }
  return predicate(node) ? node : find(node.children, predicate);
}
const textOf = node => node.children.filter(child => typeof child === 'string').join('');
const phaseOf = app => find(app.tree, node => 'data-cpa-phase' in node.props)?.props['data-cpa-phase'];

function createHost(name, log, capabilities) {
  const retireCallbacks = [];
  let browserValue = '';
  return {
    name,
    retireCallbacks,
    capabilities,
    backend: {
      get: async requestPath => {
        log.push(`${name} get ${requestPath.split('?')[0]}`);
        return { generation: 1, data: { html, manifest: { sha256 } } };
      },
      post: async (requestPath, body) => {
        const route = requestPath.split('?')[0];
        log.push(`${name} post ${route}`);
        if (route === '/console/unlock') {
          return { data: { ok: true, lease_id: 'lease_private_1234567890', generation: 1, document_id: body.document_id } };
        }
        if (route === '/console/browser') {
          if (!body.review_id) {
            browserValue = body.value;
            return { data: { review_id: 'browser-review', summary: 'Open page', effects: ['open_url'], requires_confirmation: false } };
          }
          return { data: { ok: true, action: 'open', value: browserValue } };
        }
        return { data: { ok: true } };
      },
    },
    onRetire(callback) {
      retireCallbacks.push(callback);
      return () => {
        const at = retireCallbacks.indexOf(callback);
        if (at >= 0) retireCallbacks.splice(at, 1);
        log.push(`${name} unsubscribed`);
      };
    },
    close: async () => { log.push(`${name} close`); },
  };
}

async function openView(t, { dashboard, strict = false, capabilities = {}, configureHost, initialPhase = 'locked' }) {
  const { createCpaLauncher } = await loadView();
  const runtime = createRuntime();
  const log = [];
  const hosts = [];
  const ConfirmDialog = function ConfirmDialog() {};
  const SandboxedFrame = Object.assign(function SandboxedFrame() {}, { supportsVerifiedDocument: true });
  const Launcher = createCpaLauncher({
    React: runtime.React,
    SandboxedFrame: dashboard ? undefined : SandboxedFrame,
    ConfirmDialog,
    dashboard,
    sessionFor: () => {
      const host = createHost(`host${hosts.length + 1}`, log, capabilities);
      hosts.push(host);
      configureHost?.(host, hosts.length);
      return host;
    },
  });
  const app = runtime.mount(Launcher, { profile: 'work' });
  if (strict) app.strictRemount();
  const guests = [];
  t.after(() => { app.unmount(); for (const close of guests) close(); });
  await until(() => phaseOf(app) === initialPhase, `the ${initialPhase} console`);
  return {
    app,
    hosts,
    log,
    find: predicate => find(app.tree, predicate),
    async unlock() {
      const button = await until(() => find(app.tree, node => node.type === 'button' && textOf(node) === 'Unlock CPA Console'), 'the unlock button');
      button.props.onClick();
      const dialog = await until(() => find(app.tree, node => node.type === ConfirmDialog), 'the unlock confirmation');
      dialog.props.onConfirm();
      await until(() => phaseOf(app) === 'connecting', 'the unlocked console');
    },
    async connectGuest() {
      const frame = await until(() => find(app.tree, node => node.props.frameRef || (node.type === SandboxedFrame && node.props.ref)), 'the console frame');
      const ref = frame.props.frameRef ?? frame.props.ref;
      const waiting = new Map();
      let port;
      let sessionId;
      let markReady;
      const ready = new Promise(resolve => { markReady = resolve; });
      const guestWindow = {
        postMessage(init, _target, ports) {
          sessionId = init.session_id;
          port = ports[0];
          port.onmessage = ({ data }) => {
            if (data.type === 'hermes:cpa:ready') {
              port.postMessage({ type: 'hermes:cpa:ack', protocol: 3, session_id: init.session_id, client_nonce: init.client_nonce });
              markReady();
            } else if (data.type === 'hermes:cpa:response') waiting.get(data.id)?.(data);
          };
          port.start();
          port.postMessage({ type: 'hermes:cpa:hello', protocol: 3, session_id: init.session_id, client_nonce: init.client_nonce });
        },
      };
      ref.current = { contentWindow: guestWindow };
      for (const listener of windowListeners) {
        listener({ source: guestWindow, data: { type: 'hermes:cpa:connect', protocol: 3, client_nonce: 'client_nonce_123' } });
      }
      await ready;
      await until(() => phaseOf(app) === 'ready', 'the ready console');
      guests.push(() => port?.close());
      return {
        request: (id, operation, payload) => new Promise(resolve => {
          waiting.set(id, resolve);
          port.postMessage({ type: 'hermes:cpa:request', protocol: 3, session_id: sessionId, id, operation, payload });
        }),
      };
    },
  };
}

for (const dashboard of [false, true]) {
  test(`${dashboard ? 'dashboard' : 'desktop'} initialization recovers from a transient document read without unlocking`, async t => {
    let reads = 0;
    const view = await openView(t, {
      dashboard,
      configureHost(host) {
        const get = host.backend.get;
        host.backend.get = async (...args) => {
          reads += 1;
          if (reads === 1) throw Object.assign(new Error('transport unavailable'), { code: 'PLUGIN_REQUEST_FAILED' });
          return get(...args);
        };
      },
    });
    assert.equal(reads, 2);
    assert.equal(view.hosts.length, 1);
    assert.equal(view.log.some(entry => entry.includes(' post ')), false);
    assert.ok(view.find(node => node.type === 'button' && textOf(node) === 'Unlock CPA Console'));
  });
}

test('initialization exhausts three reads, then local retry captures a fresh owner without a window reload', async t => {
  const reads = [];
  let reloads = 0;
  const reload = window.location.reload;
  window.location.reload = () => { reloads += 1; };
  t.after(() => { window.location.reload = reload; });
  const view = await openView(t, {
    dashboard: false,
    initialPhase: 'error',
    configureHost(host, index) {
      const get = host.backend.get;
      host.backend.get = async (...args) => {
        reads.push(index);
        if (index === 1) throw Object.assign(new Error('selection unavailable'), { code: 'PLUGIN_REQUEST_FAILED' });
        return get(...args);
      };
    },
  });
  assert.deepEqual(reads, [1, 1, 1]);
  const retry = view.find(node => node.type === 'button' && textOf(node) === 'Retry loading console');
  assert.ok(retry);
  retry.props.onClick();
  await until(() => phaseOf(view.app) === 'locked', 'local retry recovery');
  assert.deepEqual(reads, [1, 1, 1, 2]);
  assert.ok(view.log.includes('host1 close'));
  assert.equal(view.hosts[0].retireCallbacks.length, 0);
  assert.equal(view.hosts.length, 2);
  assert.equal(reloads, 0);
  assert.equal(view.log.some(entry => entry.includes(' post ')), false);
});

test('a message-only stale selection is not automatically retried', async t => {
  let reads = 0;
  const view = await openView(t, {
    dashboard: false,
    initialPhase: 'error',
    configureHost(host) {
      host.backend.get = async () => { reads += 1; throw new Error('STALE_SCOPE'); };
    },
  });
  assert.equal(reads, 1);
  assert.ok(view.find(node => node.type === 'button' && textOf(node) === 'Retry loading console'));
  assert.equal(view.log.some(entry => entry.includes(' post ')), false);
});

test('invalid console integrity is not automatically refetched or presented', async t => {
  let reads = 0;
  const view = await openView(t, {
    dashboard: false,
    initialPhase: 'error',
    configureHost(host) {
      host.backend.get = async () => {
        reads += 1;
        return { generation: 1, data: { html: html + 'tampered', manifest: { sha256 } } };
      };
    },
  });
  await pause(550);
  assert.equal(reads, 1);
  assert.equal(view.find(node => node.type === 'button' && textOf(node) === 'Unlock CPA Console'), undefined);
  assert.equal(view.log.some(entry => entry.includes(' post ')), false);
});

for (const retire of [false, true]) {
  test(`${retire ? 'retirement' : 'unmount'} cancels initialization retry and rejects late asset completion`, async t => {
    let finish;
    let reads = 0;
    const view = await openView(t, {
      dashboard: false,
      initialPhase: 'loading',
      configureHost(host) {
        host.backend.get = () => {
          reads += 1;
          return new Promise(resolve => { finish = resolve; });
        };
      },
    });
    if (retire) await view.hosts[0].retireCallbacks[0]();
    else view.app.unmount();
    finish({ generation: 1, data: { html, manifest: { sha256 } } });
    await pause(550);
    assert.equal(reads, 1);
    assert.notEqual(phaseOf(view.app), 'locked');
    assert.equal(view.find(node => node.type === 'button' && textOf(node) === 'Unlock CPA Console'), undefined);
    assert.equal(view.log.some(entry => entry.includes(' post ')), false);
  });
}

test('retirement during retry backoff never reads through the retired owner', async t => {
  let reads = 0;
  const view = await openView(t, {
    dashboard: false,
    initialPhase: 'loading',
    configureHost(host) {
      host.backend.get = async () => { reads += 1; throw new Error('offline'); };
    },
  });
  await until(() => reads === 1, 'first document read');
  await view.hosts[0].retireCallbacks[0]();
  await pause(550);
  assert.equal(reads, 1);
  assert.equal(phaseOf(view.app), 'ended');
  assert.equal(view.hosts.length, 1);
});

test('an uncertain management operation retains receipt recovery and is never an initialization retry', async t => {
  const view = await openView(t, { dashboard: false });
  const host = view.hosts[0];
  const post = host.backend.post;
  let executions = 0;
  host.backend.post = async (requestPath, body) => {
    if (requestPath.split('?')[0] === '/console/prepare') {
      return { data: { review_id: 'review-uncertain', summary: 'Generated operation', effects: [], requires_confirmation: false } };
    }
    if (requestPath.split('?')[0] === '/console/execute') {
      executions += 1;
      throw new Error('reply lost');
    }
    return post(requestPath, body);
  };
  await view.unlock();
  const guest = await view.connectGuest();
  const response = await guest.request('uncertain', 'management.request', {
    method: 'PUT', path: '/v8/management/config', response_kind: 'json', body: { kind: 'json', value: {} },
  });
  assert.equal(response.error.code, 'OUTCOME_UNKNOWN');
  await until(() => phaseOf(view.app) === 'unknown', 'unknown operation outcome');
  assert.equal(executions, 1);
  assert.equal(view.hosts.length, 1);
  assert.equal(view.find(node => node.type === 'button' && textOf(node) === 'Retry loading console'), undefined);
  assert.ok(view.find(node => node.type === 'button' && textOf(node) === 'Reload'));
});

const openPrompt = node => node.props && 'data-cpa-external-open' in node.props;
const openLink = node => node.type === 'a' && node.props['data-cpa-action'] === 'open-external';
const declineButton = node => node.type === 'button' && node.props['data-cpa-action'] === 'cancel-external-open';

test('a StrictMode double mount closes the first captured session terminally; retirement revokes through the old owner before release', async t => {
  const view = await openView(t, { dashboard: false, strict: true });
  assert.equal(view.hosts.length, 2, 'the second mount captures a fresh session; a closed one is never reused');
  const [first, second] = view.hosts;
  assert.ok(view.log.includes('host1 close'));
  assert.ok(view.log.includes('host1 unsubscribed'));
  assert.equal(first.retireCallbacks.length, 0);
  assert.equal(second.retireCallbacks.length, 1);
  assert.equal(view.log.includes('host2 close'), false);

  await view.unlock();
  const before = view.log.length;
  await second.retireCallbacks[0]();
  const after = view.log.slice(before);
  assert.deepEqual(after.filter(entry => entry.startsWith('host2 post')), ['host2 post /console/lock'],
    'the old owner receives the lease revocation while it is still the captured owner');
  assert.ok(after.indexOf('host2 post /console/lock') < after.indexOf('host2 close'), 'revocation finishes before the session is released');
  assert.equal(view.log.some(entry => entry.startsWith('host1 post')), false, 'the closed first session never speaks again');
  await until(() => phaseOf(view.app) === 'ended', 'the ended console');
});

test('retiring a host before any unlock releases the captured session without a backend call', async t => {
  const view = await openView(t, { dashboard: false });
  const [host] = view.hosts;
  await host.retireCallbacks[0]();
  assert.ok(view.log.includes('host1 close'));
  assert.equal(view.log.some(entry => entry.startsWith('host1 post')), false);
  await until(() => phaseOf(view.app) === 'ended', 'the ended console');
  assert.equal(view.find(node => node.type === 'button' && textOf(node) === 'Unlock CPA Console'), undefined,
    'a retired captured session can never unlock; only a fresh mount captures a new one');
  assert.equal(view.hosts.length, 1, 'retirement under unchanged selection labels never silently recreates or reuses a session');
});

test('a backend-revision retirement of an unlocked console revokes through the old owner, ends the console, and cannot unlock again', async t => {
  const view = await openView(t, { dashboard: false });
  await view.unlock();
  const [host] = view.hosts;
  await host.retireCallbacks[0]();
  assert.ok(view.log.includes('host1 post /console/lock'));
  await until(() => phaseOf(view.app) === 'ended', 'the ended console');
  const releases = view.log.filter(entry => entry === 'host1 close').length;
  assert.equal(releases, 1, 'the host closes exactly once; the retire callback never re-enters session.close');
  assert.equal(host.retireCallbacks.length, 1, 'the retire callback stays registered until the host drops it; it is harmless once ended');
  await host.retireCallbacks[0]();
  assert.equal(view.log.filter(entry => entry === 'host1 post /console/lock').length, 1, 'a second retirement signal never revokes twice');
  assert.equal(view.find(node => node.type === 'button' && textOf(node) === 'Unlock CPA Console'), undefined);
  assert.equal(view.hosts.length, 1);
});

test('dashboard external open completes only when the user activates the trusted link, and cancel reports not opened', async t => {
  const view = await openView(t, { dashboard: true });
  await view.unlock();
  const guest = await view.connectGuest();
  const url = 'https://example.test/oauth?state=abc&code_challenge=xyz';
  let settled = false;
  const response = guest.request('open-1', 'browser.open', { url }).then(value => { settled = true; return value; });
  await until(() => view.find(openPrompt), 'the trusted open prompt');
  await pause(30);
  assert.equal(settled, false, 'the review round trip alone must never report the page as opened');
  const link = view.find(openLink);
  assert.equal(link.props.href, url, 'the link is exactly the reviewed URL');
  assert.equal(link.props.target, '_blank');
  assert.match(link.props.rel, /noopener/);
  assert.match(link.props.rel, /noreferrer/);
  assert.ok(view.find(node => node.type === 'code' && textOf(node) === url), 'the exact URL is shown to the user');
  link.props.onClick();
  assert.deepEqual((await response).result, { completed: true });
  await until(() => !view.find(openPrompt), 'the prompt to be dismissed');

  const declined = guest.request('open-2', 'browser.open', { url });
  await until(() => view.find(declineButton), 'the second prompt');
  view.find(declineButton).props.onClick();
  assert.deepEqual((await declined).error, { code: 'CONSENT_DECLINED' });
  await until(() => !view.find(openPrompt), 'the declined prompt to be dismissed');
  assert.equal(phaseOf(view.app), 'ready', 'declining one page does not end the console session');
});

test('desktop keeps the native external open and shows no trusted-link prompt', async t => {
  const opened = [];
  const view = await openView(t, { dashboard: false, capabilities: { openExternal: async url => { opened.push(url); } } });
  await view.unlock();
  const guest = await view.connectGuest();
  const url = 'https://example.test/oauth?state=abc';
  assert.deepEqual((await guest.request('open-1', 'browser.open', { url })).result, { completed: true });
  assert.deepEqual(opened, [url]);
  assert.equal(view.find(openPrompt), undefined);
});

test('late callbacks from an approved review cannot resolve the next effect review', async t => {
  const opened = [];
  const view = await openView(t, { dashboard: false, capabilities: { openExternal: async url => opened.push(url) } });
  const host = view.hosts[0];
  const post = host.backend.post;
  host.backend.post = async (requestPath, body) => {
    const response = await post(requestPath, body);
    if (requestPath.split('?')[0] === '/console/browser' && !body.review_id) {
      response.data.requires_confirmation = true;
    }
    return response;
  };
  await view.unlock();
  const guest = await view.connectGuest();
  const review = node => node.props.title === 'Review CPA effect';
  const url = 'https://example.test/oauth';
  const first = guest.request('review-1', 'browser.open', { url });
  const oldDialog = await until(() => view.find(review), 'the first effect review');
  oldDialog.props.onConfirm();
  assert.deepEqual((await first).result, { completed: true });
  const second = guest.request('review-2', 'browser.open', { url });
  const currentDialog = await until(() => {
    const dialog = view.find(review);
    return dialog && dialog !== oldDialog ? dialog : undefined;
  }, 'the next effect review');
  oldDialog.props.onClose();
  await Promise.resolve();
  assert.equal(view.find(review), currentDialog, 'a delayed close belongs only to its original review');
  oldDialog.props.onConfirm();
  await Promise.resolve();
  assert.equal(view.find(review), currentDialog, 'a stale confirmation cannot approve another effect');
  currentDialog.props.onClose();
  assert.deepEqual((await second).error, { code: 'CONSENT_DECLINED' });
  assert.deepEqual(opened, [url], 'only the independently approved first effect ran');
});

test('a grouped quota approval shows one trusted dialog per unlocked session and Lock ends it', async t => {
  const effects = ['network_destination', 'quota_reads_until_lock'];
  const summary = 'Read Codex usage windows for credential "codex-a.json"\nApproving this also permits the other fixed ' +
    'built-in Codex quota/profile reads until you Lock the console or this unlocked session expires.';
  const view = await openView(t, { dashboard: false });
  const host = view.hosts[0];
  const post = host.backend.post;
  const owner = { granted: false, reviews: new Set(), executed: [] };
  host.backend.post = async (requestPath, body) => {
    const route = requestPath.split('?')[0];
    if (route === '/console/prepare') {
      owner.reviews.add(`review-${body.request_id}`);
      return { data: { review_id: `review-${body.request_id}`, summary, effects, requires_confirmation: !owner.granted,
        approval_scope: { kind: 'quota_reads', provider: 'codex', granted: owner.granted } } };
    }
    if (route === '/console/execute') {
      assert.ok(owner.reviews.has(body.review_id));
      assert.deepEqual(body.approved_effects, effects);
      owner.executed.push(body.request_id);
      owner.granted = true;
      return { data: { operation_id: body.request_id } };
    }
    if (route === '/console/result') {
      return { data: { operation_id: body.operation_id, state: 'succeeded',
        result: { status: 200, headers: {}, body: { kind: 'json', value: { ok: true } } } } };
    }
    if (route === '/console/lock') owner.granted = false;
    return post(requestPath, body);
  };
  await view.unlock();
  const guest = await view.connectGuest();
  const read = url => ({ method: 'POST', path: '/v8/management/requests/api-call', response_kind: 'json', body: {
    kind: 'json', value: { authIndex: 'codex-index', method: 'GET', url, header: { Authorization: 'Bearer $TOKEN$' } } } });
  const review = node => node.props.title === 'Review CPA effect';
  const first = guest.request('usage', 'management.request', read('https://chatgpt.com/backend-api/wham/usage'));
  const second = guest.request('subscription', 'management.request',
    read('https://chatgpt.com/backend-api/subscriptions?account_id=generated'));
  const dialog = await until(() => view.find(review), 'the grouped quota review');
  assert.equal(dialog.props.description, summary, 'the host dialog states the session-wide scope from the owner');
  const list = find(dialog.props.children, node => node.type === 'ul');
  assert.deepEqual(list.children.map(item => item.children.join('')), effects);
  dialog.props.onConfirm();
  assert.equal((await first).ok, true);
  const covered = await replyWithin(second);
  assert.equal(covered.ok, true, 'the second parallel read ran without a second dialog');
  assert.equal(view.find(review), undefined);
  const followup = await replyWithin(
    guest.request('credits', 'management.request', read('https://chatgpt.com/backend-api/wham/rate-limit-reset-credits')),
  );
  assert.equal(followup.ok, true, 'the follow-up read ran without a dialog');
  assert.deepEqual(owner.executed.length, 3, 'every read keeps its own prepare and execute');
  await until(() => view.find(node => node.type === 'button' && textOf(node) === 'Lock CPA Console'), 'the lock button');
  view.find(node => node.type === 'button' && textOf(node) === 'Lock CPA Console').props.onClick();
  await until(() => phaseOf(view.app) === 'ended', 'the locked console');
  assert.ok(view.log.includes('host1 post /console/lock'));
  assert.equal(owner.granted, false, 'Lock ends the grouped approval');
  assert.equal(view.find(node => node.type === 'button' && textOf(node) === 'Unlock CPA Console'), undefined,
    'a locked session cannot continue; a new session starts without the approval');
});
