import { isRecord } from '@/utils/helpers';
import type { ProviderTraffic, TrafficWindow } from './types';
import { TRAFFIC_BUCKET_MINUTES } from './types';

const PROVIDERS = [
  'gemini',
  'gemini-interactions',
  'vertex',
  'codex',
  'claude',
  'xai',
  'meta',
  'other',
];
type Provider =
  'gemini' | 'gemini-interactions' | 'vertex' | 'codex' | 'claude' | 'xai' | 'meta' | 'other';
type Bucket = { success: number; failed: number };
export type HermesProviderTraffic = Omit<ProviderTraffic, 'id' | 'buckets'> & {
  id: Provider;
  buckets: Bucket[];
};
export type HermesTraffic =
  | { kind: 'unavailable' }
  | {
      kind: 'available';
      credentialEntries: number;
      totalSuccess: number;
      totalFailure: number;
      total: number;
      providers: HermesProviderTraffic[];
      window: TrafficWindow | null;
    };
export type HermesTrafficState =
  HermesTraffic | { kind: 'loading' } | { kind: 'transport-failure' };
export type HermesStatus = {
  mode: 'managed' | 'external' | 'unavailable';
  lifecycle:
    | 'unconfigured'
    | 'stopped'
    | 'starting'
    | 'ready'
    | 'degraded'
    | 'stopping'
    | 'failed'
    | 'external_unreachable'
    | 'external_ready'
    | 'ownership_lost'
    | 'unavailable';
  management_ready: boolean;
  inference_configured: boolean | null;
};

function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
}
function isProvider(value: unknown): value is Provider {
  return (
    value === 'gemini' ||
    value === 'gemini-interactions' ||
    value === 'vertex' ||
    value === 'codex' ||
    value === 'claude' ||
    value === 'xai' ||
    value === 'meta' ||
    value === 'other'
  );
}

export function parseHermesStatus(value: unknown): HermesStatus | null {
  if (!isRecord(value)) return null;
  const { mode, lifecycle, management_ready, inference_configured } = value;
  if (mode !== 'managed' && mode !== 'external' && mode !== 'unavailable') return null;
  if (
    lifecycle !== 'unconfigured' &&
    lifecycle !== 'stopped' &&
    lifecycle !== 'starting' &&
    lifecycle !== 'ready' &&
    lifecycle !== 'degraded' &&
    lifecycle !== 'stopping' &&
    lifecycle !== 'failed' &&
    lifecycle !== 'external_unreachable' &&
    lifecycle !== 'external_ready' &&
    lifecycle !== 'ownership_lost' &&
    lifecycle !== 'unavailable'
  )
    return null;
  if (
    typeof management_ready !== 'boolean' ||
    (inference_configured !== null && typeof inference_configured !== 'boolean')
  )
    return null;
  return { mode, lifecycle, management_ready, inference_configured };
}

function parseBuckets(value: unknown, length: number): Bucket[] | null {
  if (!Array.isArray(value) || value.length !== length) return null;
  const buckets: Bucket[] = [];
  let total = 0;
  for (const bucket of value) {
    if (
      !isRecord(bucket) ||
      !isCount(bucket.success) ||
      !isCount(bucket.failed) ||
      !isCount(bucket.success + bucket.failed)
    )
      return null;
    total += bucket.success + bucket.failed;
    if (!isCount(total)) return null;
    // Time and all other upstream fields are deliberately discarded.
    buckets.push({ success: bucket.success, failed: bucket.failed });
  }
  return buckets;
}

function trafficWindow(buckets: Bucket[]): TrafficWindow {
  let totalSuccess = 0;
  let totalFailure = 0;
  let peakTotal = 0;
  let peakIndex = -1;
  for (const [index, bucket] of buckets.entries()) {
    totalSuccess += bucket.success;
    totalFailure += bucket.failed;
    const total = bucket.success + bucket.failed;
    if (total > peakTotal) {
      peakTotal = total;
      peakIndex = index;
    }
  }
  const total = totalSuccess + totalFailure;
  return {
    buckets,
    totalSuccess,
    totalFailure,
    total,
    peakTotal,
    peakIndex,
    successRate: total > 0 ? (totalSuccess / total) * 100 : null,
    windowMinutes: buckets.length * TRAFFIC_BUCKET_MINUTES,
  };
}

/** Closed passive API-key projection. Invalid data is unavailable, never a zero chart. */
export function parseHermesTraffic(value: unknown): HermesTraffic {
  const unavailable: HermesTraffic = { kind: 'unavailable' };
  if (
    !isRecord(value) ||
    value.scope !== 'api_key_credentials' ||
    !Array.isArray(value.providers) ||
    value.providers.length > PROVIDERS.length ||
    !isRecord(value.window) ||
    value.window.bucket_seconds !== 600 ||
    typeof value.window.available !== 'boolean'
  )
    return unavailable;
  const buckets = parseBuckets(value.window.buckets, value.window.available ? 20 : 0);
  if (buckets === null) return unavailable;
  if (value.available === false) return unavailable;
  if (
    value.available !== true ||
    !isCount(value.credential_entries) ||
    value.credential_entries > 1000 ||
    !isCount(value.total_success) ||
    !isCount(value.total_failed) ||
    !isCount(value.total_success + value.total_failed)
  )
    return unavailable;
  if (value.credential_entries === 0 && (value.providers.length !== 0 || value.window.available))
    return unavailable;

  const providers: HermesProviderTraffic[] = [];
  const seen = new Set<Provider>();
  let entries = 0;
  let success = 0;
  let failed = 0;
  const aggregate = buckets.map(() => ({ success: 0, failed: 0 }));
  for (const provider of value.providers) {
    if (
      !isRecord(provider) ||
      !isProvider(provider.provider) ||
      seen.has(provider.provider) ||
      !isCount(provider.credential_entries) ||
      provider.credential_entries === 0 ||
      provider.credential_entries > 1000 ||
      !isCount(provider.success) ||
      !isCount(provider.failed) ||
      !isCount(provider.success + provider.failed)
    )
      return unavailable;
    const providerBuckets = parseBuckets(provider.buckets, value.window.available ? 20 : 0);
    if (providerBuckets === null) return unavailable;
    seen.add(provider.provider);
    entries += provider.credential_entries;
    success += provider.success;
    failed += provider.failed;
    if (entries > 1000 || !isCount(success) || !isCount(failed) || !isCount(success + failed))
      return unavailable;
    for (const [index, bucket] of providerBuckets.entries()) {
      const target = aggregate[index];
      target.success += bucket.success;
      target.failed += bucket.failed;
      if (
        !isCount(target.success) ||
        !isCount(target.failed) ||
        !isCount(target.success + target.failed)
      )
        return unavailable;
    }
    const total = provider.success + provider.failed;
    providers.push({
      id: provider.provider,
      credentials: provider.credential_entries,
      success: provider.success,
      failure: provider.failed,
      total,
      successRate: total > 0 ? (provider.success / total) * 100 : null,
      buckets: providerBuckets,
    });
  }
  if (
    entries !== value.credential_entries ||
    success !== value.total_success ||
    failed !== value.total_failed ||
    aggregate.some(
      (bucket, index) =>
        bucket.success !== buckets[index].success || bucket.failed !== buckets[index].failed
    )
  )
    return unavailable;
  providers.sort((a, b) => PROVIDERS.indexOf(a.id) - PROVIDERS.indexOf(b.id));
  return {
    kind: 'available',
    credentialEntries: entries,
    totalSuccess: success,
    totalFailure: failed,
    total: success + failed,
    providers,
    window: value.window.available ? trafficWindow(buckets) : null,
  };
}
