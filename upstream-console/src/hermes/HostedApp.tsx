import { useEffect, useState } from 'react';
import { RouterProvider, createHashRouter } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { EmptyState } from '@/components/ui/EmptyState';
import { LoadingSpinner } from '@/components/ui/LoadingSpinner';
import { MainLayout } from '@/components/layout/MainLayout';
import { RootShell } from '@/components/layout/RootShell';
import { useAppPreferences } from '@/hooks/useAppPreferences';
import { openExternal } from '@/hosted/browser';
import { useNotificationStore } from '@/stores';
import { initializeHostedSession, useAuthStore } from '@/stores/useAuthStore';
import { getErrorMessage } from '@/utils/helpers';
import { installExternalLinkHandler } from './externalLinks';
import { createHashRouterWindow } from './routerWindow';

/** Emitted on the guest window when the host revokes or the user locks the console. */
const CONSOLE_LOCKED_EVENT = 'hermes-console-locked';

const routes = [
  {
    element: <RootShell />,
    children: [{ path: '/*', element: <MainLayout /> }],
  },
];

/** The full pinned management app, mounted only for an unlocked hosted session. */
function HostedConsole() {
  const { t } = useTranslation();
  const showNotification = useNotificationStore((state) => state.showNotification);
  const [router] = useState(() =>
    createHashRouter(routes, { window: createHashRouterWindow(window) })
  );

  useEffect(() => () => router.dispose(), [router]);

  useEffect(
    () =>
      installExternalLinkHandler({
        document,
        open: openExternal,
        onError: (error) =>
          showNotification(getErrorMessage(error, t('notification.open_link_failed')), 'error'),
      }),
    [showNotification, t]
  );

  return <RouterProvider router={router} />;
}

type SessionPhase = 'unlocking' | 'ready' | 'unavailable';

/**
 * Mounts the console only after the host confirms an unlocked session, and tears it down
 * for good the moment that session ends. Unlocking is the trusted outer UI's job: a locked
 * guest never offers a way back in.
 */
export function HostedBootstrap() {
  const { t } = useTranslation();
  useAppPreferences();
  const authenticated = useAuthStore((state) => state.isAuthenticated);
  const [phase, setPhase] = useState<SessionPhase>('unlocking');
  const [locked, setLocked] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const handleLocked = () => setLocked(true);
    window.addEventListener(CONSOLE_LOCKED_EVENT, handleLocked);
    initializeHostedSession().then(
      (ok) => {
        if (!cancelled) setPhase(ok ? 'ready' : 'unavailable');
      },
      () => {
        if (!cancelled) setPhase('unavailable');
      }
    );
    return () => {
      cancelled = true;
      window.removeEventListener(CONSOLE_LOCKED_EVENT, handleLocked);
    };
  }, []);

  if (locked || (phase === 'ready' && !authenticated)) {
    return (
      <main className="main-content">
        <EmptyState title={t('hosted.locked_title')} description={t('hosted.locked_desc')} />
      </main>
    );
  }
  if (phase === 'unavailable') {
    return (
      <main className="main-content">
        <EmptyState
          title={t('hosted.unavailable_title')}
          description={t('hosted.unavailable_desc')}
        />
      </main>
    );
  }
  if (phase === 'unlocking') {
    return (
      <main className="main-content">
        <LoadingSpinner />
      </main>
    );
  }
  return <HostedConsole />;
}
