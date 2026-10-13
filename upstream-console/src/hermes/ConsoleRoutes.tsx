import { Navigate, useRoutes, type Location } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { HermesDashboard } from '@/features/dashboard/HermesDashboard';
import { HermesConfig } from '@/features/config/HermesConfig';
import { HermesInventory } from '@/features/HermesInventory';
import { SectionCard } from '@/features/config/components/SectionCard';

function Gated({ title, reason }: { title: string; reason: string }) {
  const { t } = useTranslation();
  return <SectionCard title={t(title)} description={t(reason)}>
    <p role="status">{t('disabled')}</p>
  </SectionCard>;
}
// Matches pinned App/MainRoutes identities. Plugin URLs deliberately display
// an explicit denial instead of silently redirecting; no plugin loaders mount.
export function MainRoutes({ location }: { location?: Location }) {
  return useRoutes([
    ...['/', '/dashboard', '/usage'].map(path => ({ path, element: <HermesDashboard /> })),
    ...['/settings', '/api-keys'].map(path => ({ path, element: <Navigate to="/config" replace /> })),
    { path: '/config', element: <HermesConfig /> },
    { path: '/ai-providers', element: <HermesInventory feature="providers" /> },
    { path: '/ai-providers/*', element: <Navigate to="/ai-providers" replace /> },
    { path: '/providers', element: <Navigate to="/ai-providers" replace /> },
    { path: '/quick-start', element: <Gated title="nav.quick_start" reason="providers_note" /> },
    { path: '/quick-start/*', element: <Navigate to="/quick-start" replace /> },
    ...['/auth-files', '/oauth', '/auth', '/auth-files/oauth-excluded', '/auth-files/oauth-model-alias'].map(path => ({ path, element: <Gated title="auth" reason="auth_note" /> })),
    { path: '/quota', element: <Gated title="quota" reason="quota_note" /> },
    ...['/plugins/*', '/plugin-store', '/plugin-pages/*'].map(path => ({ path, element: <Gated title="plugins" reason="plugins_note" /> })),
    ...['/system', '/models'].map(path => ({ path, element: <HermesInventory feature="models" /> })),
    { path: '/logs', element: <HermesInventory feature="logs" /> },
    { path: '/login', element: <Gated title="console" reason="unavailable" /> },
    { path: '*', element: <Navigate to="/" replace /> },
  ], location);
}
