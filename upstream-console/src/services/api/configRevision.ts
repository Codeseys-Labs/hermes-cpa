import { isHosted } from '@/hosted/runtime';
import type { ManagementClientConfig } from './hostedAdapter';

let revisions = new WeakMap<object, string>();

/** Keep the CAS revision attached to the data read, not to the last unrelated request. */
export function rememberConfigRevision(value: unknown, revision: unknown): void {
  if (value !== null && typeof value === 'object' && typeof revision === 'string' &&
      /^[a-f0-9]{64}$/i.test(revision)) revisions.set(value, revision);
}

export function inheritConfigRevision(source: unknown, target: object): void {
  if (source !== null && typeof source === 'object') {
    rememberConfigRevision(target, revisions.get(source));
  }
}

export function configRevisionArgs(snapshot: unknown): [] | [ManagementClientConfig] {
  if (!isHosted()) return [];
  const revision = snapshot !== null && typeof snapshot === 'object' ? revisions.get(snapshot) : undefined;
  if (!revision) throw new Error('Configuration snapshot has no revision; reload before saving');
  return [{ hermesExpectedRevision: revision }];
}

export function clearConfigSnapshotRevisions(): void {
  revisions = new WeakMap<object, string>();
}
