import { describe, expect, test } from 'bun:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { HermesDashboardView } from '../src/features/dashboard/HermesDashboard';
import {
  parseHermesStatus,
  parseHermesTraffic,
  type HermesTrafficState,
} from '../src/features/dashboard/hermesTraffic';
import { resources } from '../src/i18n/hermesRead';
import upstreamEn from '../src/i18n/locales/en.json';

const i18n = createInstance();
await i18n.init({
  lng: 'en',
  fallbackLng: 'en',
  resources: {
    en: { translation: { ...resources.en.translation, ...upstreamEn } },
  },
  interpolation: { escapeValue: false },
});

function fixture(recent = true) {
  const buckets = Array.from({ length: 20 }, (_, index) => ({
    success: index === 19 ? 3 : 0,
    failed: index === 0 ? 1 : 0,
  }));
  return {
    available: true,
    scope: 'api_key_credentials',
    credential_entries: 1,
    total_success: 30,
    total_failed: 10,
    providers: [
      {
        provider: 'codex',
        credential_entries: 1,
        success: 30,
        failed: 10,
        buckets: recent ? buckets : [],
      },
    ],
    window: { available: recent, bucket_seconds: 600, buckets: recent ? buckets : [] },
  };
}
const status = parseHermesStatus({
  mode: 'external',
  lifecycle: 'external_ready',
  management_ready: true,
  inference_configured: null,
});
function render(traffic: HermesTrafficState) {
  return renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n },
      createElement(
        MemoryRouter,
        null,
        createElement(HermesDashboardView, { status, traffic, onRefresh() {} })
      )
    )
  );
}

// Exercise the same unknown parser and presentational component used by polling, not a copy.
function renderPayload(payload: unknown) {
  return render(parseHermesTraffic(payload));
}

describe('Hermes API-key traffic consumer', () => {
  test('keeps lifetime totals separate from the valid recent chart and renders upstream fleet controls', () => {
    const parsed = parseHermesTraffic(fixture());
    expect(parsed.kind).toBe('available');
    if (parsed.kind !== 'available') throw new Error('Expected available usage');
    expect(parsed.total).toBe(40);
    expect(parsed.window).toMatchObject({
      total: 4,
      totalSuccess: 3,
      totalFailure: 1,
      peakIndex: 19,
      peakTotal: 3,
      windowMinutes: 200,
    });
    const html = render(parsed);
    expect(html).toContain('4 requests over the last 200 minutes: 3 succeeded, 1 failed.');
    expect(html).toContain('Codex');
    expect(html).toContain('API-key usage entries: 1');
    expect(html).toContain('Lifetime API-key requests');
    expect(html).toContain('aria-valuenow="75"');
    expect(html).toContain('aria-label="Codex request volume"');
    expect(html).toContain('190m ago');
    expect(html).toContain('Current interval');
    expect(html).not.toContain('-200m');
    expect(html).toContain('href="/config"');
    expect(html).toContain('External connection: read-only.');
  });

  test('canonicalizes multiple providers and checks exact per-index aggregation', () => {
    const payload = fixture();
    payload.credential_entries = 3;
    payload.total_success = 37;
    payload.total_failed = 12;
    payload.providers.push({
      provider: 'gemini',
      credential_entries: 2,
      success: 7,
      failed: 2,
      buckets: Array.from({ length: 20 }, (_, index) => ({
        success: index === 4 ? 7 : 0,
        failed: index === 4 ? 2 : 0,
      })),
    });
    payload.window.buckets = payload.window.buckets.map((bucket, index) => ({
      success: bucket.success + (index === 4 ? 7 : 0),
      failed: bucket.failed + (index === 4 ? 2 : 0),
    }));
    const parsed = parseHermesTraffic(payload);
    if (parsed.kind !== 'available') throw new Error('Expected available usage');
    expect(parsed.providers.map((provider) => provider.id)).toEqual(['gemini', 'codex']);
    expect(parsed.total).toBe(49);
    expect(parsed.window).toMatchObject({ total: 13, peakIndex: 4, peakTotal: 9 });
    expect(render(parsed).indexOf('Gemini')).toBeLessThan(render(parsed).indexOf('Codex'));
    payload.window.buckets[4].success += 1;
    expect(parseHermesTraffic(payload)).toEqual({ kind: 'unavailable' });
  });

  test('no entries is not a quiet chart and explicitly excludes OAuth', () => {
    const html = renderPayload({
      available: true,
      scope: 'api_key_credentials',
      credential_entries: 0,
      total_success: 0,
      total_failed: 0,
      providers: [],
      window: { available: false, bucket_seconds: 600, buckets: [] },
    });
    expect(html).toContain('No observed API-key credentials.');
    expect(html).toContain('OAuth and auth-file activity is excluded.');
    expect(html).not.toContain('role="img"');
    expect(html).not.toContain('No requests in this valid API-key window.');
  });

  test('unavailable windows preserve lifetime fleet counters without fake zero sparklines', () => {
    const html = renderPayload(fixture(false));
    expect(html).toContain('Recent API-key window unavailable, not zero.');
    expect(html).toContain('Codex');
    expect(html).toContain('aria-valuenow="75"');
    expect(html).not.toContain('role="img"');
    expect(html).not.toContain('No requests in this valid API-key window.');
  });

  test('valid all-zero windows really are quiet and have no invented success rate', () => {
    const payload = fixture();
    payload.total_success = 0;
    payload.total_failed = 0;
    payload.providers[0].success = 0;
    payload.providers[0].failed = 0;
    payload.window.buckets = Array.from({ length: 20 }, () => ({ success: 0, failed: 0 }));
    payload.providers[0].buckets = payload.window.buckets;
    const html = renderPayload(payload);
    expect(html).toContain('No requests in this valid API-key window.');
    expect(html).toContain('0 requests over the last 200 minutes: 0 succeeded, 0 failed.');
    expect(html).toContain('aria-label="Codex request volume"');
    expect(html).not.toContain('aria-valuenow=');
    expect(html).not.toContain('No observed API-key credentials.');
  });

  test('loading, transport failure and unavailable projections remain distinct', () => {
    expect(render({ kind: 'loading' })).toContain('Loading');
    const failed = render({ kind: 'transport-failure' });
    expect(failed).toContain('Host transport unavailable');
    expect(failed).not.toContain('role="img"');
    const unavailable = renderPayload({
      available: false,
      scope: 'api_key_credentials',
      credential_entries: null,
      total_success: null,
      total_failed: null,
      providers: [],
      window: { available: false, bucket_seconds: 600, buckets: [] },
    });
    expect(unavailable).toContain('API-key usage is unavailable, not zero.');
    expect(unavailable).not.toContain('Host transport unavailable');
    expect(unavailable).not.toContain('role="img"');
  });

  test('strips raw labels, times and all extra fields instead of rendering them', () => {
    const payload = fixture();
    const bucket = { success: 0, failed: 1, time: 'SECRET_TIME_CANARY' };
    const injected = {
      ...payload,
      token: 'SECRET_TOKEN_CANARY',
      providers: [
        {
          ...payload.providers[0],
          provider: 'other',
          name: 'SECRET_NAME_CANARY',
          buckets: [bucket, ...payload.providers[0].buckets.slice(1)],
        },
      ],
      window: { ...payload.window, buckets: [bucket, ...payload.window.buckets.slice(1)] },
    };
    const parsed = parseHermesTraffic(injected);
    expect(JSON.stringify(parsed)).not.toContain('SECRET_');
    expect(render(parsed)).not.toContain('SECRET_');
    expect(render(parsed)).toContain('Other');
    expect(
      renderPayload({
        ...payload,
        providers: [{ ...payload.providers[0], provider: 'SECRET_PROVIDER_CANARY' }],
      })
    ).not.toContain('SECRET_');
    expect(
      parseHermesStatus({
        mode: 'SECRET_MODE',
        lifecycle: 'ready',
        management_ready: true,
        inference_configured: false,
      })
    ).toBeNull();
    expect(
      parseHermesStatus({
        mode: 'managed',
        lifecycle: 'SECRET_LIFECYCLE',
        management_ready: true,
        inference_configured: false,
      })
    ).toBeNull();
  });

  test('rejects invalid counts rather than coercing them', () => {
    for (const count of [
      true,
      false,
      -1,
      0.5,
      '30',
      null,
      undefined,
      NaN,
      Infinity,
      Number.MAX_SAFE_INTEGER + 1,
    ]) {
      const payload = fixture();
      for (const invalid of [
        { ...payload, total_success: count },
        { ...payload, credential_entries: count },
        { ...payload, providers: [{ ...payload.providers[0], success: count }] },
        {
          ...payload,
          window: {
            ...payload.window,
            buckets: [{ success: count, failed: 1 }, ...payload.window.buckets.slice(1)],
          },
        },
      ])
        expect(parseHermesTraffic(invalid)).toEqual({ kind: 'unavailable' });
    }
  });

  test('rejects malformed, duplicate, oversized and contradictory projections', () => {
    const payload = fixture();
    const cases: unknown[] = [
      null,
      [],
      {},
      { ...payload, scope: 'oauth' },
      { ...payload, available: 'true' },
      { ...payload, credential_entries: 1001 },
      { ...payload, total_success: 31 },
      { ...payload, credential_entries: 2 },
      { ...payload, total_success: Number.MAX_SAFE_INTEGER, total_failed: 1 },
      { ...payload, providers: [] },
      { ...payload, providers: [...payload.providers, ...payload.providers] },
      { ...payload, providers: Array.from({ length: 1001 }, () => payload.providers[0]) },
      { ...payload, providers: [{ ...payload.providers[0], credential_entries: 0 }] },
      { ...payload, providers: [{ ...payload.providers[0], buckets: [] }] },
      { ...payload, window: { ...payload.window, bucket_seconds: 60 } },
      { ...payload, window: { ...payload.window, available: false } },
      { ...payload, window: { ...payload.window, buckets: payload.window.buckets.slice(1) } },
      {
        ...payload,
        window: {
          ...payload.window,
          buckets: [...payload.window.buckets, { success: 0, failed: 0 }],
        },
      },
      { ...fixture(false), providers: payload.providers },
    ];
    for (const invalid of cases) {
      expect(parseHermesTraffic(invalid)).toEqual({ kind: 'unavailable' });
      const html = renderPayload(invalid);
      expect(html).toContain('API-key usage is unavailable, not zero.');
      expect(html).not.toContain('role="img"');
    }
  });

  test('accepts exact maximum counts but rejects accumulated window overflow', () => {
    const payload = fixture();
    payload.total_success = Number.MAX_SAFE_INTEGER;
    payload.total_failed = 0;
    payload.providers[0].success = Number.MAX_SAFE_INTEGER;
    payload.providers[0].failed = 0;
    const buckets = Array.from({ length: 20 }, (_, index) => ({
      success: index === 0 ? Number.MAX_SAFE_INTEGER : 0,
      failed: 0,
    }));
    payload.providers[0].buckets = buckets;
    payload.window.buckets = buckets;
    expect(parseHermesTraffic(payload).kind).toBe('available');
    buckets[1].success = 1;
    expect(parseHermesTraffic(payload)).toEqual({ kind: 'unavailable' });
  });

  test('rejects a single bucket whose success and failed counts overflow together', () => {
    const payload = fixture();
    const bucket = { success: Number.MAX_SAFE_INTEGER, failed: 1 };
    payload.total_success = Number.MAX_SAFE_INTEGER;
    payload.total_failed = 1;
    payload.providers[0].success = Number.MAX_SAFE_INTEGER;
    payload.providers[0].failed = 1;
    payload.providers[0].buckets = [bucket, ...payload.providers[0].buckets.slice(1)];
    payload.window.buckets = [bucket, ...payload.window.buckets.slice(1)];
    expect(parseHermesTraffic(payload)).toEqual({ kind: 'unavailable' });
  });
});
