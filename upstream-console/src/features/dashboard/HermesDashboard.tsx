// Upstream traffic/fleet presentation over the host's passive API-key projection only.
import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { request } from '@/hermesTransport';
import { formatPercent } from '@/utils/format';
import { createDashboardPolling } from './hermesPolling';
import {
  parseHermesStatus,
  parseHermesTraffic,
  type HermesStatus,
  type HermesTrafficState,
} from './hermesTraffic';
import { ThroughputChart } from './components/ThroughputChart';
import { Sparkline } from './components/Sparkline';
import { Meter } from './components/Meter';
import { TRAFFIC_BUCKET_MINUTES } from './types';
import { providerLabel } from './utils';
import styles from './dashboard.module.scss';

export function HermesDashboard() {
  const [status, setStatus] = useState<HermesStatus | null>(null);
  const [traffic, setTraffic] = useState<HermesTrafficState>({ kind: 'loading' });
  const refresh = useRef<(() => void) | null>(null);
  useEffect(() => {
    let active = true;
    const controller = createDashboardPolling({
      read: async () => {
        // Wait for BOTH requests, even when one rejects, before another batch.
        const [s, u] = await Promise.allSettled([request('status.read'), request('usage.read')]);
        if (!active) return false;
        if (s.status === 'rejected' || u.status === 'rejected') {
          setStatus(null);
          setTraffic({ kind: 'transport-failure' });
          return false;
        }
        const status = parseHermesStatus(s.value);
        setStatus(status);
        if (
          status === null ||
          status.mode === 'unavailable' ||
          status.lifecycle === 'unavailable'
        ) {
          // Polling pauses here; the chart must not keep looking current.
          setTraffic({ kind: 'transport-failure' });
          return false;
        }
        setTraffic(parseHermesTraffic(u.value));
        return true;
      },
      hidden: () => document.hidden,
      subscribe: (changed) => {
        document.addEventListener('visibilitychange', changed);
        return () => document.removeEventListener('visibilitychange', changed);
      },
      schedule: (callback, delay) => window.setTimeout(callback, delay),
      cancel: (timer) => window.clearTimeout(timer),
    });
    refresh.current = controller.refresh;
    return () => {
      active = false;
      controller.dispose();
      refresh.current = null;
    };
  }, []);
  return (
    <HermesDashboardView status={status} traffic={traffic} onRefresh={() => refresh.current?.()} />
  );
}

export function HermesDashboardView({
  status,
  traffic,
  onRefresh,
}: {
  status: HermesStatus | null;
  traffic: HermesTrafficState;
  onRefresh: () => void;
}) {
  const { t } = useTranslation();
  const usage = traffic.kind === 'available' ? traffic : null;
  // Upstream's final bucket is the current, incomplete ten-minute interval.
  // These labels are generated locally; no server time strings enter the chart.
  const window = usage?.window
    ? {
        ...usage.window,
        buckets: usage.window.buckets.map((bucket, index, buckets) => ({
          success: bucket.success,
          failed: bucket.failed,
          time:
            index === buckets.length - 1
              ? t('hermes_traffic_current')
              : t('hermes_traffic_ago', {
                  minutes: (buckets.length - 1 - index) * TRAFFIC_BUCKET_MINUTES,
                }),
        })),
      }
    : null;
  const message =
    traffic.kind === 'loading'
      ? t('loading')
      : traffic.kind === 'transport-failure'
        ? t('hermes_transport_unavailable')
        : !usage
          ? t('hermes_traffic_unavailable')
          : usage.credentialEntries === 0
            ? t('hermes_traffic_no_entries')
            : !window
              ? t('hermes_traffic_window_unavailable')
              : window.total === 0
                ? t('hermes_traffic_quiet')
                : t('hermes_traffic_active');
  const numberLabel = (value: number | undefined) =>
    value === undefined ? t('unknown') : value.toLocaleString();
  const rows = [
    { label: 'Mode', value: status?.mode ?? t('unknown') },
    { label: 'Lifecycle', value: status?.lifecycle ?? t('unknown') },
    { label: 'Management ready', value: status ? String(status.management_ready) : t('unknown') },
    {
      label: 'Inference configured',
      value:
        status?.inference_configured == null ? t('unknown') : String(status.inference_configured),
    },
  ];
  return (
    <div className={styles.page}>
      <section className={styles.hero}>
        <div className={styles.heroCopy}>
          <h1 className={styles.heroTitle}>CLI Proxy API Management Center</h1>
          <p className={styles.heroMeta}>
            {traffic.kind === 'transport-failure'
              ? t('hermes_transport_unavailable')
              : t('hermes_host_projection')}
          </p>
          <p className={styles.heroMeta}>{t('hermes_traffic_scope')}</p>
          <button onClick={onRefresh}>{t('hermes_traffic_refresh')}</button>
        </div>
        <div className={styles.heroPanel}>
          <div className={styles.heroPanelTop}>
            <span className={styles.heroPanelLabel}>{t('hermes_traffic_lifetime')}</span>
          </div>
          <strong className={styles.heroFigure}>{numberLabel(usage?.total)}</strong>
          <span className={styles.heroPanelMeta}>
            {usage ? t('hermes_traffic_entries', { count: usage.credentialEntries }) : t('unknown')}
          </span>
          <div className={styles.heroSplit}>
            <span className={styles.heroSplitItem}>
              <i className={`${styles.splitSwatch} ${styles.splitSuccess}`} aria-hidden="true" />
              {t('stats.success')}
              <b>{numberLabel(usage?.totalSuccess)}</b>
            </span>
            <span className={styles.heroSplitItem}>
              <i className={`${styles.splitSwatch} ${styles.splitFailure}`} aria-hidden="true" />
              {t('stats.failure')}
              <b>{numberLabel(usage?.totalFailure)}</b>
            </span>
          </div>
        </div>
      </section>
      <section className={styles.section}>
        <header className={styles.sectionHead}>
          <span className={styles.eyebrow}>{t('hermes_traffic_eyebrow')}</span>
          <h2 className={styles.sectionTitle}>{t('dashboard.traffic_title')}</h2>
          <p className={styles.sectionDescription}>{t('hermes_traffic_window_note')}</p>
        </header>
        <div className={styles.panel}>
          <p role="status" className={styles.emptyNote}>
            {message}
          </p>
          {window && <ThroughputChart traffic={window} />}
        </div>
      </section>
      <section className={styles.section}>
        <header className={styles.sectionHead}>
          <span className={styles.eyebrow}>{t('dashboard.fleet_eyebrow')}</span>
          <h2 className={styles.sectionTitle}>{t('hermes_traffic_fleet_title')}</h2>
          <p className={styles.sectionDescription}>{t('hermes_traffic_fleet_note')}</p>
        </header>
        <div className={styles.panel}>
          {!usage || usage.providers.length === 0 ? (
            <p className={styles.emptyNote}>{message}</p>
          ) : (
            <ul className={styles.fleetList}>
              {usage.providers.map((provider, index) => {
                const label =
                  provider.id === 'other'
                    ? t('hermes_traffic_other')
                    : providerLabel(provider.id, t('dashboard.provider_unknown'));
                return (
                  <li key={provider.id} className={styles.fleetRow}>
                    <span className={styles.fleetRank} aria-hidden="true">
                      {String(index + 1).padStart(2, '0')}
                    </span>
                    <div className={styles.fleetIdentity}>
                      <span className={styles.fleetName}>{label}</span>
                      <span className={styles.fleetMeta}>
                        {t('hermes_traffic_entries', { count: provider.credentials })}
                      </span>
                    </div>
                    {window ? (
                      <Sparkline
                        points={provider.buckets.map((bucket) => bucket.success + bucket.failed)}
                        ariaLabel={t('dashboard.fleet_spark_label', { provider: label })}
                        className={styles.fleetSpark}
                      />
                    ) : (
                      <span className={styles.fleetMeta}>
                        {t('hermes_traffic_window_unavailable')}
                      </span>
                    )}
                    <div className={styles.fleetNumbers}>
                      <span className={styles.fleetTotal}>{provider.total.toLocaleString()}</span>
                      <span className={styles.fleetTotalLabel}>{t('hermes_traffic_lifetime')}</span>
                    </div>
                    <div className={styles.fleetRate}>
                      <span className={styles.fleetRateValue}>
                        {provider.successRate === null
                          ? t('unknown')
                          : formatPercent(provider.successRate)}
                      </span>
                      <Meter
                        value={provider.successRate}
                        ariaLabel={t('hermes_traffic_lifetime_rate')}
                        className={styles.fleetMeter}
                      />
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </section>
      <div className={styles.panel}>
        <header className={styles.panelHead}>
          <span className={styles.eyebrow}>Runtime</span>
          <h2 className={styles.panelTitle}>Status</h2>
        </header>
        <dl className={styles.specList}>
          {rows.map((row) => (
            <div key={row.label} className={styles.specRow}>
              <dt className={styles.specLabel}>{row.label}</dt>
              <dd className={styles.specValue}>{row.value}</dd>
            </div>
          ))}
        </dl>
        <Link to="/config" className={styles.panelLink}>
          Configuration{' '}
          <span className={styles.linkArrow} aria-hidden="true">
            →
          </span>
        </Link>
        <p>
          {status?.mode === 'external'
            ? 'External connection: read-only. Lifecycle and config writes are disabled.'
            : 'Onboarding and secret unlock belong to native host settings. No credentials are entered here.'}
        </p>
      </div>
    </div>
  );
}
