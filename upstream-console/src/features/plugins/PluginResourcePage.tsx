import { useCallback, useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { Button } from '@/components/ui/Button';
import { EmptyState } from '@/components/ui/EmptyState';
import { useHeaderRefresh } from '@/hooks/useHeaderRefresh';
import { openPlugin } from '@/hosted/browser';
import { isHosted } from '@/hosted/runtime';
import { pluginsApi } from '@/services/api';
import { useAuthStore, useNotificationStore } from '@/stores';
import { getErrorMessage, isRecord } from '@/utils/helpers';
import type { PluginListResponse } from '@/types';
import {
  collectPluginResourceEntries,
  PLUGIN_RESOURCES_REFRESH_EVENT,
  resolvePluginAssetURL,
  type PluginResourceEntry,
} from './pluginResources';
import styles from './PluginResourcePage.module.scss';

const hasStatus = (error: unknown, status: number) => isRecord(error) && error.status === status;

const safeDecodeURIComponent = (value = '') => {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
};

const parseMenuIndex = (value = '') => {
  const index = Number.parseInt(value, 10);
  return Number.isInteger(index) && index >= 0 ? index : -1;
};

interface PluginResourceSurfaceProps {
  resource: PluginResourceEntry;
  hosted: boolean;
  iframeSrc: string;
  opening: boolean;
  onOpenInHost: () => void;
}

/**
 * One declared plugin page. Plugin code never runs inside the hosted, privileged console: the
 * host presents it in a separate reviewed window that carries no console or Hermes authority.
 */
export function PluginResourceSurface({
  resource,
  hosted,
  iframeSrc,
  opening,
  onOpenInHost,
}: PluginResourceSurfaceProps) {
  const { t } = useTranslation();

  if (hosted) {
    return (
      <div className={styles.stateShell}>
        <EmptyState
          title={resource.label}
          description={t('plugin_resource.host_presentation_desc', {
            plugin: resource.pluginTitle,
          })}
          action={
            <Button onClick={onOpenInHost} loading={opening}>
              {t('plugin_resource.host_open')}
            </Button>
          }
        />
      </div>
    );
  }

  if (!iframeSrc) {
    return (
      <div className={styles.stateShell}>
        <EmptyState
          title={t('plugin_resource.empty_src')}
          description={t('plugin_resource.empty_src_desc')}
        />
      </div>
    );
  }

  return (
    <iframe
      className={styles.frame}
      src={iframeSrc}
      title={resource.label}
      referrerPolicy="no-referrer"
      allow="clipboard-read; clipboard-write"
    />
  );
}

export function PluginResourcePage() {
  const { t } = useTranslation();
  const params = useParams<{ pluginId: string; menuIndex: string }>();
  const connectionStatus = useAuthStore((state) => state.connectionStatus);
  const apiBase = useAuthStore((state) => state.apiBase);
  const showNotification = useNotificationStore((state) => state.showNotification);

  const [data, setData] = useState<PluginListResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [opening, setOpening] = useState(false);

  const connected = connectionStatus === 'connected';
  const pluginID = useMemo(() => safeDecodeURIComponent(params.pluginId), [params.pluginId]);
  const menuIndex = useMemo(() => parseMenuIndex(params.menuIndex), [params.menuIndex]);

  const loadResource = useCallback(async () => {
    if (!connected) {
      setLoading(false);
      setError(t('notification.connection_required'));
      return;
    }

    setLoading(true);
    setError('');
    try {
      const plugins = await pluginsApi.list();
      setData(plugins);
    } catch (err: unknown) {
      setError(
        hasStatus(err, 404)
          ? t('plugin_management.unsupported_backend')
          : getErrorMessage(err, t('plugin_resource.load_failed'))
      );
    } finally {
      setLoading(false);
    }
  }, [connected, t]);

  useHeaderRefresh(loadResource, connected);

  useEffect(() => {
    void loadResource();
  }, [loadResource]);

  useEffect(() => {
    window.addEventListener(PLUGIN_RESOURCES_REFRESH_EVENT, loadResource);

    return () => {
      window.removeEventListener(PLUGIN_RESOURCES_REFRESH_EVENT, loadResource);
    };
  }, [loadResource]);

  const resource = useMemo(() => {
    const entries = collectPluginResourceEntries(data?.plugins ?? []);
    return entries.find((entry) => entry.pluginID === pluginID && entry.menuIndex === menuIndex);
  }, [data?.plugins, menuIndex, pluginID]);

  const hosted = isHosted();
  const iframeSrc = resource && !hosted ? resolvePluginAssetURL(resource.menu.path, apiBase) : '';

  const openInHost = async () => {
    if (!resource) return;
    setOpening(true);
    try {
      await openPlugin(resource.pluginID, resource.menuIndex);
    } catch (err: unknown) {
      showNotification(getErrorMessage(err, t('plugin_resource.host_open_failed')), 'error');
    } finally {
      setOpening(false);
    }
  };

  return (
    <div className={styles.page}>
      {loading ? (
        <div className={styles.stateShell}>
          <div className={styles.statusPanel}>{t('common.loading')}</div>
        </div>
      ) : error ? (
        <div className={styles.stateShell}>
          <EmptyState title={t('plugin_resource.unavailable')} description={error} />
        </div>
      ) : !resource ? (
        <div className={styles.stateShell}>
          <EmptyState
            title={t('plugin_resource.not_found')}
            description={t('plugin_resource.not_found_desc')}
          />
        </div>
      ) : (
        <PluginResourceSurface
          resource={resource}
          hosted={hosted}
          iframeSrc={iframeSrc}
          opening={opening}
          onOpenInHost={() => void openInHost()}
        />
      )}
    </div>
  );
}
