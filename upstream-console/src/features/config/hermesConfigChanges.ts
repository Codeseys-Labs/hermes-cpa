// Pure helpers for the host-reviewed configuration form; no UI, transport or storage.
export type ConfigProjection = {
  strategy: string;
  request_retry: number;
  request_log: boolean;
  revision: string;
};

export type ConfigChanges = Partial<Pick<ConfigProjection, 'strategy' | 'request_retry' | 'request_log'>>;

export type ApplyOutcome = 'accepted' | 'unchanged';

/** Narrow a host `config.read` result; the guest never trusts a shape it has not checked. */
export function parseProjection(value: unknown): ConfigProjection | null {
  if (typeof value !== 'object' || value === null) return null;
  const strategy = 'strategy' in value ? value.strategy : undefined;
  const requestRetry = 'request_retry' in value ? value.request_retry : undefined;
  const requestLog = 'request_log' in value ? value.request_log : undefined;
  const revision = 'revision' in value ? value.revision : undefined;
  if (typeof strategy !== 'string' || typeof requestRetry !== 'number' ||
      typeof requestLog !== 'boolean' || typeof revision !== 'string') return null;
  return { strategy, request_retry: requestRetry, request_log: requestLog, revision };
}

/** Only a managed instance is editable; external CPA is read-only. */
export function isManaged(status: unknown): boolean {
  return typeof status === 'object' && status !== null && 'mode' in status && status.mode === 'managed';
}

/** Only fields whose draft value differs from the last host projection; `{}` means nothing to apply. */
export function changedFields(baseline: ConfigProjection, draft: ConfigProjection): ConfigChanges {
  const changes: ConfigChanges = {};
  if (draft.strategy !== baseline.strategy) changes.strategy = draft.strategy;
  if (draft.request_retry !== baseline.request_retry) changes.request_retry = draft.request_retry;
  if (draft.request_log !== baseline.request_log) changes.request_log = draft.request_log;
  return changes;
}

/** The host answers an apply that changes nothing with `unchanged`, without prompting or restarting. */
export function applyOutcome(result: unknown): ApplyOutcome {
  if (typeof result === 'object' && result !== null &&
      'accepted' in result && result.accepted === false &&
      'unchanged' in result && result.unchanged === true) return 'unchanged';
  return 'accepted';
}
