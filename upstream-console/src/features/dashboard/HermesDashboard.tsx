// Pruned DashboardPage.tsx at 5aa1ad64: heroPanel (259–303), runtime panel
// (483–517) and existing dashboard styles. Provider/credential/chart sections
// omitted because the host projection deliberately cannot supply their data.
import { useEffect, useRef, useState } from 'react';
import { createDashboardPolling } from './hermesPolling';
import { Link } from 'react-router-dom';
import { request } from '@/hermesTransport';
import styles from './dashboard.module.scss';

type Status = { mode: string; lifecycle: string; management_ready: boolean;
  inference_configured: boolean | null };
type Usage = { available: boolean; total_success: number | null; total_failed: number | null };
export function HermesDashboard() {
  const [status, setStatus] = useState<Status | null>(null);
  const [traffic, setTraffic] = useState<Usage | null>(null);
  const [error, setError] = useState(false);
  const polling = useRef<ReturnType<typeof createDashboardPolling> | null>(null);
  useEffect(() => {
    let active = true;
    const controller = createDashboardPolling({
      read: async () => {
        // Wait for BOTH requests, even when one rejects, before another batch.
        const [s, u] = await Promise.allSettled([request('status.read'), request('usage.read')]);
        if (!active) return false;
        if (s.status === 'rejected' || u.status === 'rejected') {
          setStatus(null); setTraffic(null); setError(true); return false;
        }
        const status = s.value as Status;
        setStatus(status); setTraffic(u.value as Usage); setError(false);
        return status.mode !== 'unavailable' && status.lifecycle !== 'unavailable';
      },
      hidden: () => document.hidden,
      subscribe: changed => {
        document.addEventListener('visibilitychange', changed);
        return () => document.removeEventListener('visibilitychange', changed);
      },
      schedule: (callback, delay) => window.setTimeout(callback, delay),
      cancel: timer => window.clearTimeout(timer),
    });
    polling.current = controller;
    return () => { active = false; controller.dispose(); polling.current = null; };
  }, []);
  const success = traffic?.available === true ? traffic.total_success : null;
  const failed = traffic?.available === true ? traffic.total_failed : null;
  const total = success !== null && failed !== null ? success + failed : null;
  const rows = [
    { label: 'Mode', value: status?.mode ?? 'Unavailable' },
    { label: 'Lifecycle', value: status?.lifecycle ?? 'Unavailable' },
    { label: 'Management ready', value: status ? String(status.management_ready) : 'Unknown' },
    { label: 'Inference configured', value: status?.inference_configured == null ? 'Unknown' : String(status.inference_configured) },
  ];
  return <div className={styles.page}>
    <section className={styles.hero}>
      <div className={styles.heroCopy}>
        <h1 className={styles.heroTitle}>CLI Proxy API Management Center</h1>
        <p className={styles.heroMeta}>{error ? 'Host transport unavailable' : 'Host-projected status and usage'}</p>
        <button onClick={() => polling.current?.refresh()}>Refresh status and usage</button>
      </div>
      <div className={styles.heroPanel}>
        <div className={styles.heroPanelTop}><span className={styles.heroPanelLabel}>Requests</span></div>
        <strong className={styles.heroFigure}>{total === null ? 'Unknown' : total.toLocaleString()}</strong>
        <span className={styles.heroPanelMeta}>No quota or provider data is inferred.</span>
        <div className={styles.heroSplit}>
          <span className={styles.heroSplitItem}><i className={`${styles.splitSwatch} ${styles.splitSuccess}`} aria-hidden="true" />Success<b>{success === null ? 'Unknown' : success.toLocaleString()}</b></span>
          <span className={styles.heroSplitItem}><i className={`${styles.splitSwatch} ${styles.splitFailure}`} aria-hidden="true" />Failure<b>{failed === null ? 'Unknown' : failed.toLocaleString()}</b></span>
        </div>
      </div>
    </section>
    <div className={styles.panel}>
      <header className={styles.panelHead}><span className={styles.eyebrow}>Runtime</span><h2 className={styles.panelTitle}>Status</h2></header>
      <dl className={styles.specList}>{rows.map(row => <div key={row.label} className={styles.specRow}><dt className={styles.specLabel}>{row.label}</dt><dd className={styles.specValue}>{row.value}</dd></div>)}</dl>
      <Link to="/config" className={styles.panelLink}>Configuration <span className={styles.linkArrow} aria-hidden="true">→</span></Link>
      <p>{status?.mode === 'external' ? 'External connection: read-only. Lifecycle and config writes are disabled.' : 'Onboarding and secret unlock belong to native host settings. No credentials are entered here.'}</p>
    </div>
  </div>;
}
