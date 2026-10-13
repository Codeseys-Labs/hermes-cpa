import { describe, expect, test } from 'bun:test';
import {
  applyOutcome,
  changedFields,
  isManaged,
  parseProjection,
} from '../src/features/config/hermesConfigChanges';

const baseline = { strategy: 'round-robin', request_retry: 3, request_log: false, revision: 'a'.repeat(64) };

describe('host-reviewed configuration changes', () => {
  test('an untouched form has nothing to apply, so it cannot restart CPA', () => {
    expect(changedFields(baseline, { ...baseline })).toEqual({});
  });

  test('retyping the same value is still nothing to apply', () => {
    expect(changedFields(baseline, { ...baseline, request_retry: Number('3') })).toEqual({});
  });

  test('only fields that differ are submitted for host review', () => {
    expect(changedFields(baseline, { ...baseline, request_retry: 4 })).toEqual({ request_retry: 4 });
    expect(
      changedFields(baseline, { ...baseline, strategy: 'fill-first', request_log: true })
    ).toEqual({ strategy: 'fill-first', request_log: true });
  });

  test('an unchanged host answer is never reported as an accepted restart', () => {
    expect(applyOutcome({ accepted: false, unchanged: true })).toBe('unchanged');
    expect(applyOutcome({ accepted: true })).toBe('accepted');
  });

  test('malformed projections are refused instead of trusted', () => {
    expect(parseProjection(baseline)).toEqual(baseline);
    for (const bad of [null, 'x', {}, { ...baseline, request_retry: '3' }, { ...baseline, request_log: 0 }]) {
      expect(parseProjection(bad)).toBeNull();
    }
  });

  test('only a managed instance is editable', () => {
    expect(isManaged({ mode: 'managed' })).toBe(true);
    for (const status of [{ mode: 'external' }, {}, null, 'managed']) expect(isManaged(status)).toBe(false);
  });
});
