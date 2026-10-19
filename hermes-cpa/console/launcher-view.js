import {
  LauncherError,
  appendProfile,
  createExternalOpenGate,
  createTrustedLauncherSession,
  verifiedDocumentId,
} from './launcher-bridge.js';

const MAX_DOCUMENT_BYTES = 8 * 1024 * 1024;
const utf8 = new TextEncoder();
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const digestPattern = /^[a-f0-9]{64}$/;

function bytes(value) {
  return utf8.encode(value).byteLength;
}

function fixedHex(buffer) {
  return [...new Uint8Array(buffer)].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

async function verifiedAsset(envelope) {
  if (!record(envelope) || !Number.isInteger(envelope.generation) || envelope.generation < 1 || !record(envelope.data)) {
    throw new LauncherError('UNAVAILABLE');
  }
  const data = envelope.data;
  const html = data.html;
  const sha256 = data.manifest?.sha256 ?? data.sha256;
  if (typeof html !== 'string' || bytes(html) > MAX_DOCUMENT_BYTES || typeof sha256 !== 'string' || !digestPattern.test(sha256)) {
    throw new LauncherError('UNAVAILABLE');
  }
  const digest = fixedHex(await crypto.subtle.digest('SHA-256', utf8.encode(html)));
  if (digest !== sha256) throw new LauncherError('UNAVAILABLE');
  return { html, sha256, generation: envelope.generation };
}

function BrowserVerifiedFrame({ React, asset, frameRef, onLoad, onVerificationError }) {
  const [url, setUrl] = React.useState('');
  React.useEffect(() => {
    let active = true;
    let objectUrl = '';
    void (async () => {
      try {
        if (bytes(asset.html) > MAX_DOCUMENT_BYTES) throw new LauncherError('UNAVAILABLE');
        const digest = fixedHex(await crypto.subtle.digest('SHA-256', utf8.encode(asset.html)));
        if (!active) return;
        if (digest !== asset.sha256) throw new LauncherError('UNAVAILABLE');
        objectUrl = URL.createObjectURL(new Blob([asset.html], { type: 'text/html;charset=utf-8' }));
        if (active) setUrl(objectUrl);
      } catch (error) {
        if (active) onVerificationError(error instanceof Error ? error : new Error('Document verification failed'));
      }
    })();
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [asset.html, asset.sha256, onVerificationError]);
  return url ? React.createElement('iframe', {
    ref: frameRef,
    title: 'CPA Management Console',
    src: url,
    sandbox: 'allow-scripts',
    referrerPolicy: 'no-referrer',
    onLoad,
    style: { position: 'absolute', inset: 0, width: '100%', height: '100%', border: 0 },
  }) : null;
}

function errorMessage(code) {
  if (code === 'HOST_CAPABILITY_UNAVAILABLE') return 'This Hermes host cannot provide the requested trusted capability.';
  if (code === 'OUTCOME_UNKNOWN') return 'The result is unknown. Do not repeat the operation; reconcile the accepted receipt.';
  if (code === 'RESULT_WITHHELD') return 'The operation completed, but its protected result is no longer available. Unlock again before new work.';
  if (code === 'CONSENT_DECLINED') return 'The requested effect was not approved.';
  if (code === 'HOST_BUSY') return 'The Hermes host was busy and did not forward the request. Nothing changed; reload to try again.';
  return 'The CPA console is unavailable for this selection.';
}

/**
 * Shared outer launcher for both supported Hermes surfaces. The child receives
 * a verified opaque document and only an ephemeral MessagePort after unlock.
 */
export function createCpaLauncher({
  React, SandboxedFrame, ConfirmDialog, sessionFor, dashboard = false,
}) {
  if (!React || typeof sessionFor !== 'function') {
    throw new Error('CPA launcher requires trusted host adapters');
  }
  const h = React.createElement;
  return function CpaLauncher({ profile }) {
    const capturedProfile = React.useState(() => String(profile ?? '').trim())[0];
    const hostSession = React.useRef(null);
    const frame = React.useRef(null);
    const session = React.useRef(null);
    const active = React.useRef(true);
    const pendingReview = React.useRef(null);
    const reviewQueue = React.useRef(Promise.resolve());
    const dialogSequence = React.useRef(0);
    const externalGate = React.useRef(null);
    const externalLink = React.useRef(null);
    const [asset, setAsset] = React.useState(null);
    const [phase, setPhase] = React.useState('loading');
    const [message, setMessage] = React.useState('Loading verified CPA console…');
    const [dialog, setDialog] = React.useState(null);
    const [resource, setResource] = React.useState(null);
    const [resourceVisible, setResourceVisible] = React.useState(false);
    const [operationReceipts, setOperationReceipts] = React.useState([]);
    const [externalOpen, setExternalOpen] = React.useState(null);
    const [initialization, setInitialization] = React.useState(0);
    const [initializationFailed, setInitializationFailed] = React.useState(false);

    const releaseHost = React.useCallback((ownedHost) => {
      if (hostSession.current === ownedHost) hostSession.current = null;
      try { return Promise.resolve(ownedHost?.close?.()); } catch { return Promise.resolve(); }
    }, []);
    const resolveDialog = React.useCallback((approved, resolve) => {
      if (pendingReview.current !== resolve) return;
      pendingReview.current = null;
      setDialog(null);
      resolve(approved);
    }, []);

    const review = React.useCallback((details) => {
      const next = reviewQueue.current.then(() => {
        if (!active.current || !session.current || session.current.closed) return false;
        return new Promise(resolve => {
          pendingReview.current = resolve;
          setDialog({ kind: 'review', ...details, resolve, id: ++dialogSequence.current });
        });
      });
      reviewQueue.current = next.catch(() => false);
      return next;
    }, []);

    const closeSession = React.useCallback(() => {
      pendingReview.current?.(false);
      pendingReview.current = null;
      if (active.current) setDialog(null);
      const current = session.current;
      session.current = null;
      return current?.close();
    }, []);

    // The host retires a selection only after every retire callback settles, so revocation runs through the
    // still-captured old owner and can never cross over to its replacement.
    const retireHost = React.useCallback((ownedHost) => {
      if (hostSession.current !== ownedHost) return Promise.resolve();
      const cleanup = closeSession();
      if (cleanup) return Promise.resolve(cleanup).then(() => undefined);
      void releaseHost(ownedHost);
      if (active.current) {
        setPhase('ended');
        setMessage('CPA console session closed. Reload to unlock a new trusted session.');
      }
      return Promise.resolve();
    }, [closeSession, releaseHost]);

    React.useEffect(() => {
      active.current = true;
      setAsset(null);
      setInitializationFailed(false);
      setPhase('loading');
      setMessage('Loading verified CPA console…');
      if (!dashboard && SandboxedFrame?.supportsVerifiedDocument !== true) {
        setPhase('error');
        setMessage('This Hermes Desktop version cannot verify the CPA console document.');
        return () => { active.current = false; };
      }
      let ownedHost;
      try {
        ownedHost = sessionFor(capturedProfile);
        if (!record(ownedHost) || !record(ownedHost.backend) || typeof ownedHost.backend.get !== 'function' ||
            typeof ownedHost.backend.post !== 'function' || !record(ownedHost.capabilities)) {
          throw new Error('CPA launcher host session is unavailable');
        }
      } catch {
        void releaseHost(ownedHost);
        setPhase('error');
        setMessage('The CPA host session is unavailable.');
        setInitializationFailed(true);
        return () => { active.current = false; };
      }
      hostSession.current = ownedHost;
      let mounted = true;
      let retryTimer;
      const current = () => mounted && hostSession.current === ownedHost;
      const backend = ownedHost.backend;
      const unsubscribeRetire = typeof ownedHost.onRetire === 'function'
        ? ownedHost.onRetire(() => {
          clearTimeout(retryTimer);
          return retireHost(ownedHost);
        })
        : undefined;
      const loadAsset = async (attempt) => {
        if (!current()) return;
        let response;
        try {
          response = await backend.get(appendProfile('/console/asset', capturedProfile), { timeoutMs: 10000 });
        } catch (error) {
          if (!current()) return;
          if (attempt < 3 && error?.code !== 'STALE_SCOPE' && error?.message !== 'STALE_SCOPE') {
            setMessage(`The CPA console could not load. Retrying document read (${attempt + 1}/3)…`);
            retryTimer = setTimeout(() => { void loadAsset(attempt + 1); }, attempt * 500);
            return;
          }
          setPhase('error');
          setInitializationFailed(true);
          setMessage('The CPA console could not load. Check the backend connection and that CPA is enabled for this profile, then retry.');
          return;
        }
        if (!current()) return;
        try {
          const next = await verifiedAsset(response);
          if (!current()) return;
          setAsset(next);
          setPhase('locked');
          setMessage('Unlock CPA management to open the full console.');
        } catch {
          if (!current()) return;
          setPhase('error');
          setInitializationFailed(true);
          setMessage('The CPA console document could not be verified. Check the plugin installation before retrying.');
        }
      };
      void loadAsset(1);
      return () => {
        mounted = false;
        clearTimeout(retryTimer);
        active.current = false;
        if (typeof unsubscribeRetire === 'function') unsubscribeRetire();
        if (!closeSession()) void releaseHost(ownedHost);
      };
    }, [capturedProfile, closeSession, initialization, releaseHost, retireHost]);

    React.useEffect(() => {
      const listener = event => {
        const current = frame.current?.contentWindow;
        if (current) session.current?.acceptConnect(event, current);
      };
      window.addEventListener('message', listener);
      return () => window.removeEventListener('message', listener);
    }, []);

    React.useEffect(() => {
      if (externalOpen) externalLink.current?.focus();
    }, [externalOpen]);

    const unlock = React.useCallback(async () => {
      const ownedHost = hostSession.current;
      if (!asset || session.current || !ownedHost) return;
      setDialog(null);
      const documentId = verifiedDocumentId();
      // Only the Dashboard needs the trusted-link flow: it has no native external opener, and its asynchronous
      // review round trip has already spent the user activation a new tab requires.
      const gate = dashboard
        ? createExternalOpenGate(open => { if (active.current) setExternalOpen(open); })
        : null;
      externalGate.current = gate;
      const next = createTrustedLauncherSession({
        backend: ownedHost.backend,
        profile: capturedProfile,
        documentId,
        review,
        capabilities: gate ? { ...ownedHost.capabilities, openExternal: url => gate.request(url) } : ownedHost.capabilities,
        onState: state => {
          if (!active.current) return;
          if (state === 'ready') { setPhase('ready'); setMessage('CPA Management Console is ready.'); }
          if (state === 'outcome-unknown') { setPhase('unknown'); setMessage(errorMessage('OUTCOME_UNKNOWN')); }
          if (state === 'locked') {
            setPhase(current => current === 'unknown' ? current : 'locked');
            setMessage(current => current === errorMessage('OUTCOME_UNKNOWN') ? current : 'CPA console locked.');
          }
        },
        onReceipt: receipt => {
          if (!active.current) return;
          setOperationReceipts(current => {
            const index = current.findIndex(item => item.operation_id === receipt.operation_id);
            const next = index < 0 ? [...current, receipt] : current.map((item, i) => i === index ? { ...item, ...receipt } : item);
            let remainingToDrop = next.reduce((count, item) => count + Number(item.state === 'succeeded' || item.state === 'failed'), 0) - 100;
            return next.filter(item => {
              if (remainingToDrop <= 0 || item.state !== 'succeeded' && item.state !== 'failed') return true;
              remainingToDrop -= 1;
              return false;
            });
          });
        },
        onPluginResource: approved => {
          if (active.current) {
            setResource(approved);
            setResourceVisible(Boolean(approved));
          }
        },
        onClose: cleanup => {
          if (session.current === next) session.current = null;
          gate?.dispose();
          if (externalGate.current === gate) externalGate.current = null;
          pendingReview.current?.(false);
          pendingReview.current = null;
          if (active.current) {
            setResource(null);
            setDialog(null);
            setPhase(current => current === 'unknown' ? current : 'ended');
            setMessage(current => current === errorMessage('OUTCOME_UNKNOWN') ? current : 'CPA console session closed. Reload to unlock a new trusted session.');
          }
          void Promise.resolve(cleanup).finally(() => releaseHost(ownedHost));
        },
      });
      session.current = next;
      try {
        await next.unlock(asset.generation);
        if (!active.current || session.current !== next) return;
        setPhase('connecting');
        setMessage('Opening verified CPA console…');
      } catch (error) {
        if (session.current === next) session.current = null;
        next.close({ notifyGuest: false });
        if (active.current) { setPhase('error'); setMessage(errorMessage(error?.code)); }
      }
    }, [asset, capturedProfile, releaseHost, review]);

    const onVerificationError = React.useCallback(() => {
      closeSession();
      setPhase('error');
      setMessage('The CPA console document could not be verified.');
    }, [closeSession]);

    const onFrameLoad = React.useCallback(() => {
      if (!session.current || session.current.closed) return;
      const current = frame.current?.contentWindow;
      if (!current) {
        setPhase('error');
        setMessage('The verified CPA frame is unavailable.');
      }
    }, []);

    const documentFrame = asset && session.current?.unlocked
      ? SandboxedFrame?.supportsVerifiedDocument === true
        ? h(SandboxedFrame, {
          ref: frame,
          title: 'CPA Management Console',
          verifiedDocument: { html: asset.html, sha256: asset.sha256 },
          sandbox: 'allow-scripts',
          onLoad: onFrameLoad,
          onVerificationError,
          style: { position: 'absolute', inset: 0, width: '100%', height: '100%', border: 0 },
        })
        : dashboard
          ? h(BrowserVerifiedFrame, { React, asset, frameRef: frame, onLoad: onFrameLoad, onVerificationError })
          : null
      : null;

    const resourceSource = resource?.kind === 'native-resource' ? resource.url
      : resource?.kind === 'external-resource' ? resource.path
        : null;
    const resourceFrame = resourceSource
      ? h('section', { hidden: !resourceVisible, style: { position: 'absolute', inset: 0, zIndex: 1, minHeight: 0, background: 'Canvas', colorScheme: 'normal' } },
        !dashboard
          ? h(SandboxedFrame, {
            title: 'CPA plugin resource', src: resourceSource, sandbox: 'allow-scripts allow-forms',
            style: { position: 'absolute', inset: 0, width: '100%', height: '100%', border: 0 },
          })
          : h('iframe', {
            title: 'CPA plugin resource', src: resourceSource, sandbox: 'allow-scripts allow-forms', referrerPolicy: 'no-referrer',
            style: { position: 'absolute', inset: 0, width: '100%', height: '100%', border: 0 },
          }),
      )
      : null;
    const resourceNotice = resource?.kind === 'native-resource'
      ? h('p', { role: 'status' }, 'Installed plugin code is isolated from CPA console authority and may perform the approved native effects.')
      : resource?.kind === 'external-resource'
        ? h('p', { role: 'status' }, 'Third-party plugin content is isolated and has no CPA console authority.')
        : null;

    const dialogNode = dialog && ConfirmDialog
      ? h(ConfirmDialog, {
        key: dialog.id,
        open: true,
        title: dialog.kind === 'unlock' ? 'Unlock CPA management?' : 'Review CPA effect',
        description: dialog.kind === 'unlock'
          ? 'Open a time-limited credential-management session for this Hermes profile and allow fixed Codex, Claude and Antigravity quota/profile reads for its currently verified accounts until Lock or expiry. Other network calls, credential changes, resets, credit consumption and plugin actions still require approval.'
          : dialog.summary,
        confirmLabel: dialog.kind === 'unlock' ? 'Unlock console' : 'Approve exact effects',
        onClose: () => dialog.kind === 'unlock'
          ? setDialog(current => current === dialog ? null : current)
          : resolveDialog(false, dialog.resolve),
        onConfirm: () => dialog.kind === 'unlock' ? void unlock() : resolveDialog(true, dialog.resolve),
        children: dialog.kind === 'review'
          ? h('div', null,
            dialog.risk ? h('p', { role: 'note' }, dialog.risk) : null,
            h('ul', null, dialog.effects.map(effect => h('li', { key: effect }, effect))),
          )
          : null,
      })
      : null;

    const completeExternalOpen = () => { setTimeout(() => externalGate.current?.activate(), 0); };
    const externalOpenPanel = externalOpen
      ? h('section', {
        role: 'group',
        'aria-label': 'Open external page',
        'data-cpa-external-open': '',
        onKeyDown: event => { if (event.key === 'Escape') externalGate.current?.cancel(); },
        style: { flex: 'none', display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 12, padding: '6px 12px' },
      },
      h('p', null, 'The CPA console asked to open this page in a new browser tab. Nothing opens until you activate the link.'),
      h('code', { style: { overflowWrap: 'anywhere' } }, externalOpen.url),
      h('a', {
        ref: externalLink,
        href: externalOpen.url,
        target: '_blank',
        rel: 'noopener noreferrer',
        referrerPolicy: 'no-referrer',
        'data-cpa-action': 'open-external',
        onClick: completeExternalOpen,
        onAuxClick: completeExternalOpen,
      }, 'Open external page'),
      h('button', { type: 'button', 'data-cpa-action': 'cancel-external-open', onClick: () => externalGate.current?.cancel() }, 'Do not open'),
      )
      : null;

    return h('section', { style: { display: 'flex', flexDirection: 'column', width: '100%', height: dashboard ? 'calc(100dvh - 9rem)' : '100%', minHeight: '24rem' } },
      h('div', { style: { flex: 'none', display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 12, padding: '6px 12px' } },
        h('p', { role: phase === 'error' || phase === 'unknown' ? 'alert' : 'status', 'data-cpa-phase': phase }, message),
        phase === 'locked' && asset ? h('button', { type: 'button', onClick: () => setDialog({ kind: 'unlock', id: ++dialogSequence.current }) }, 'Unlock CPA Console') : null,
        (phase === 'ready' || phase === 'connecting') ? h('button', { type: 'button', onClick: () => void closeSession() }, 'Lock CPA Console') : null,
        resourceFrame ? h('button', { type: 'button', onClick: () => setResourceVisible(current => !current) }, resourceVisible ? 'Show CPA Console' : 'Show plugin resource') : null,
        resourceFrame ? h('button', { type: 'button', onClick: () => void session.current?.closeResource() }, 'Close plugin resource') : null,
        phase === 'error' && initializationFailed ? h('button', {
          type: 'button',
          onClick: () => {
            setInitializationFailed(false);
            setInitialization(current => current + 1);
          },
        }, 'Retry loading console') : null,
        (phase === 'error' && !initializationFailed || phase === 'ended' || phase === 'unknown')
          ? h('button', { type: 'button', onClick: () => window.location.reload() }, 'Reload') : null,
      ),
      externalOpenPanel,
      h('div', { style: { flex: '1 1 0', minHeight: 0, position: 'relative' } },
        h('div', { hidden: Boolean(resourceFrame && resourceVisible), style: { position: 'absolute', inset: 0 } }, documentFrame),
        resourceFrame),
      resourceNotice,
      operationReceipts.length ? h('details', { style: { flex: 'none', padding: '6px 12px', maxHeight: '10rem', overflow: 'auto' } },
        h('summary', null, 'Operation receipts'),
        h('p', null, 'Accepted is not a completion result. Preserve unresolved references before reloading; do not repeat an operation with an unknown outcome.'),
        h('ul', null, operationReceipts.map(receipt => h('li', { key: receipt.operation_id },
          h('code', null, receipt.operation_id), ` — ${receipt.state === 'succeeded' ? 'response received' : receipt.state ?? 'accepted'}`,
          Number.isInteger(receipt.status) ? ` (HTTP ${receipt.status})` : '',
          receipt.result_withheld ? ' (protected result withheld)' : '',
          h('div', null, 'Document: ', h('code', null, receipt.document_id)),
        ))),
      ) : null,
      dialogNode,
    );
  };
}
