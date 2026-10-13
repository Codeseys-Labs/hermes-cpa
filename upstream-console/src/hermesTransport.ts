// Per-document ephemeral transport; no browser storage or management credentials.
type Op = 'status.read' | 'usage.read' | 'config.read' | 'config.apply'
  | 'providers.read' | 'auth.read' | 'models.read' | 'quota.read' | 'logs.read' | 'plugins.read';
type Phase = 'preview' | 'consent' | 'commit';

export type HostFailureKind = 'unavailable' | 'consent-expired' | 'outcome-unknown';

/** Every rejection from the host bridge; `kind` is the only part callers should branch on. */
export class HostRequestError extends Error {
  readonly kind: HostFailureKind;
  constructor(kind: HostFailureKind, message: string) {
    super(message);
    this.name = 'HostRequestError';
    this.kind = kind;
  }
}

type Pending = { resolve: (value: unknown) => void; reject: (error: HostRequestError) => void;
  timer: ReturnType<typeof setTimeout>; op: Op; phase: Phase };

const OUTCOME_UNKNOWN = 'Outcome unknown; reconcile without resubmitting';
const READ_MS = 10000;
// Host phase budgets (desktop/plugin.js) plus one second of delivery slack, so the trusted host
// always closes first. Only a host PHASE message moves a request to its next phase; nothing the
// guest supplies, and no repeated or backward phase, renews a deadline.
const SLACK_MS = 1000;
const BUDGET_MS: Record<Phase, number> = {
  preview: 20000 + SLACK_MS, consent: 120000 + SLACK_MS, commit: 30000 + SLACK_MS,
};
const PHASE_ORDER: Record<Phase, Phase | undefined> = {
  preview: 'consent', consent: 'commit', commit: undefined,
};
let port: MessagePort | undefined;
let generation: string | undefined;
let nonce: string | undefined;
let established = false;
let revoked = false;
let sequence = 0;
const pending = new Map<string, Pending>();
let initialize: (() => void) | undefined;
const ready = new Promise<void>(resolve => { initialize = resolve; });

function revoke() {
  revoked = true;
  port?.close();
  port = undefined;
  for (const item of pending.values()) {
    clearTimeout(item.timer);
    item.reject(item.op === 'config.apply' && item.phase === 'commit'
      ? new HostRequestError('outcome-unknown', OUTCOME_UNKNOWN)
      : new HostRequestError('unavailable', 'Transport revoked'));
  }
  pending.clear();
}

if (typeof window !== 'undefined') window.addEventListener('message', event => {
  if (event.source !== parent || port || revoked || event.ports.length !== 1) return;
  const data = event.data;
  if (data?.type !== 'cpa-bridge-init' || data.v !== 2 ||
      typeof data.nonce !== 'string' || data.nonce.length < 16 || data.nonce.length > 128 ||
      typeof data.generation !== 'string' || data.generation.length < 16 || data.generation.length > 128) return;
  generation = data.generation;
  nonce = data.nonce;
  port = event.ports[0];
  port.onmessage = ({ data: response }) => {
    if (!established) {
      if (response?.v !== 2 || response.type !== 'READY' || response.nonce !== nonce ||
          response.generation !== generation) { revoke(); return; }
      established = true;
      port!.postMessage({ v: 2, type: 'ACK', nonce, generation });
      initialize?.();
      return;
    }
    if (!response || response.v !== 2 || response.generation !== generation ||
        typeof response.id !== 'string') return;
    let size: number;
    try { size = new TextEncoder().encode(JSON.stringify(response)).length; } catch { revoke(); return; }
    if (size > 1048576) { revoke(); return; }
    const item = pending.get(response.id);
    if (!item) return;
    if (response.type === 'PHASE') {
      const next = response.phase === PHASE_ORDER[item.phase] ? PHASE_ORDER[item.phase] : undefined;
      if (item.op !== 'config.apply' || !next ||
          Object.keys(response).some(key => !['v', 'type', 'id', 'generation', 'phase'].includes(key))) {
        revoke(); return;
      }
      clearTimeout(item.timer);
      item.phase = next;
      item.timer = setTimeout(() => {
        pending.delete(response.id);
        item.reject(next === 'commit'
          ? new HostRequestError('outcome-unknown', OUTCOME_UNKNOWN)
          : new HostRequestError('consent-expired', 'Consent expired'));
        revoke();
      }, BUDGET_MS[next]);
      return;
    }
    if (response.type !== undefined) { revoke(); return; }
    clearTimeout(item.timer);
    pending.delete(response.id);
    if (response.ok === true) item.resolve(response.data);
    else item.reject(response.error?.code === 'RECONCILE_REQUIRED'
      ? new HostRequestError('outcome-unknown', OUTCOME_UNKNOWN)
      : new HostRequestError('unavailable', 'Host request failed'));
  };
  port.start();
  port.onmessageerror = revoke;
  port.postMessage({ v: 2, type: 'HELLO', nonce, generation });
});
if (typeof window !== 'undefined') window.addEventListener('pagehide', revoke);

export async function request(op: Op, args: unknown = {}): Promise<unknown> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([ready, new Promise<never>((_, reject) => {
      timer = setTimeout(() => { revoke(); reject(new HostRequestError('unavailable', 'Host unavailable')); }, READ_MS);
    })]);
  } finally { clearTimeout(timer); }
  const channel = port;
  if (!channel || pending.size >= 32) throw new HostRequestError('unavailable', 'Transport unavailable or busy');
  const id = String(++sequence);
  const message = { v: 2, id, generation, op, args };
  if (new TextEncoder().encode(JSON.stringify(message)).length > 1048576) {
    throw new HostRequestError('unavailable', 'Oversized request');
  }
  return new Promise((resolve, reject) => {
    // A mutation's first budget covers only the host preview; human review starts at PHASE.
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new HostRequestError('unavailable', op === 'config.apply'
        ? 'Preview unavailable; no admission requested' : 'Host timeout'));
      revoke();
    }, op === 'config.apply' ? BUDGET_MS.preview : READ_MS);
    pending.set(id, { resolve, reject, timer, op, phase: 'preview' });
    channel.postMessage(message);
  });
}
