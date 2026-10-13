// Pruned SectionNetwork.tsx requestRetry + routingStrategy field blocks;
// reuse feature-owned FieldPrimitives, SectionCard and upstream Select/Input.
// No YAML/source mode, persisted document cache, credential or network fields.
import { useEffect, useId, useState } from 'react';
import { Input } from '@/components/ui/Input';
import { Select } from '@/components/ui/Select';
import { Button } from '@/components/ui/Button';
import { SectionCard } from './components/SectionCard';
import { FieldAnchor, FieldGrid, FieldShell, FieldStack, ToggleRow } from './components/fields/FieldPrimitives';
import { HostRequestError, request, type HostFailureKind } from '@/hermesTransport';
import { applyOutcome, changedFields, isManaged, parseProjection, type ConfigProjection } from './hermesConfigChanges';

// After an unknown outcome the form stays read-only: the write may or may not have landed, so
// resubmitting is never offered from this session.
const FAILURES: Record<HostFailureKind, { message: string; editable: boolean }> = {
  'outcome-unknown': {
    message: 'Outcome unknown. Do not resubmit; reopen the console and reconcile host status first.',
    editable: false,
  },
  'consent-expired': {
    message: 'Host review expired and nothing was submitted. Request host review again.',
    editable: true,
  },
  unavailable: {
    message: 'Host declined or could not apply configuration. Reopen to reconcile before retrying.',
    editable: true,
  },
};

export function HermesConfig() {
  const [config, setConfig] = useState<ConfigProjection | null>(null);
  const [baseline, setBaseline] = useState<ConfigProjection | null>(null);
  const [retry, setRetry] = useState('');
  const [editable, setEditable] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('Waiting for host…');
  const routingStrategyLabelId = useId();
  const routingStrategyHintId = `${routingStrategyLabelId}-hint`;
  useEffect(() => {
    let active = true;
    Promise.all([request('config.read'), request('status.read')]).then(([value, status]) => {
      if (!active) return;
      const projection = parseProjection(value);
      if (!projection) { setMessage('Host transport unavailable'); return; }
      setConfig(projection); setBaseline(projection); setRetry(String(projection.request_retry));
      setEditable(isManaged(status)); setMessage('');
    }).catch(() => { if (active) setMessage('Host transport unavailable'); });
    return () => { active = false; };
  }, []);
  const disabled = !editable || busy;
  const validRetry = /^(?:[0-9]|10)$/.test(retry);
  const changes = config && baseline && validRetry
    ? changedFields(baseline, { ...config, request_retry: Number(retry) }) : {};
  const unchanged = Object.keys(changes).length === 0;
  const apply = async () => {
    if (!config || disabled || unchanged) return;
    setBusy(true);
    try {
      const result = await request('config.apply', { expected_revision: config.revision, changes });
      if (applyOutcome(result) === 'unchanged') {
        setMessage('No changes to apply; CPA was not restarted.');
      } else {
        setMessage('Operation accepted, not yet confirmed persisted. Reopen after host restart and reconcile status; do not retry blindly.');
        setEditable(false);
      }
    } catch (error) {
      const failure = FAILURES[error instanceof HostRequestError ? error.kind : 'unavailable'];
      setMessage(failure.message);
      setEditable(failure.editable);
    }
    finally { setBusy(false); }
  };
  return <SectionCard title="Configuration" description="Host-native diff and explicit restart confirmation required. Only three supported fields can change.">
    {!editable && <p>Read-only until managed host configuration is available. External CPA cannot be edited.</p>}
    {config && <FieldStack><FieldGrid>
      <FieldAnchor fieldId="requestRetry"><Input label="Request retries" type="number" min={0} max={10} value={retry}
        onChange={e => setRetry(e.target.value)} disabled={disabled}
        error={validRetry ? undefined : 'Integer from 0 to 10 required'} /></FieldAnchor>
      <FieldAnchor fieldId="routingStrategy"><FieldShell label="Routing strategy" labelId={routingStrategyLabelId}
        hint="Only host-supported routing modes" hintId={routingStrategyHintId}>
        <Select value={config.strategy} options={[{ value: 'round-robin', label: 'Round robin' }, { value: 'fill-first', label: 'Fill first' }]}
          id={`${routingStrategyLabelId}-select`} disabled={disabled} ariaLabelledBy={routingStrategyLabelId}
          ariaDescribedBy={routingStrategyHintId} onChange={strategy => setConfig({ ...config, strategy })} />
      </FieldShell></FieldAnchor>
      <FieldAnchor fieldId="requestLog"><ToggleRow title="Request logging" checked={config.request_log}
        disabled={disabled} onChange={request_log => setConfig({ ...config, request_log })} /></FieldAnchor>
    </FieldGrid><Button disabled={disabled || unchanged} onClick={apply}>Request host review</Button>
    {editable && unchanged && <p>Change a field to request host review. Applying nothing never restarts CPA.</p>}</FieldStack>}
    <p role="status">{message}</p>
    <p>Configuration persists only in the host transaction engine, never browser storage. Display preferences reset on frame close.</p>
  </SectionCard>;
}
