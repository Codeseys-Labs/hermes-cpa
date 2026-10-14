import { useEffect, useState } from 'react';
import { isHosted } from '@/hosted/runtime';
import { apiClient } from '@/services/api/client';

/** Plugin logos are small images; anything larger is not a logo. */
export const MAX_PLUGIN_ASSET_BYTES = 2 * 1024 * 1024;

export type PluginAssetPlan =
  | { kind: 'direct'; src: string }
  | { kind: 'fetch'; path: string }
  | { kind: 'none' };

/**
 * How a plugin-declared asset reference becomes displayable.
 *
 * The reference app points the image straight at the declared URL. A hosted console runs
 * in an opaque document with `img-src data:` and no network, so only inline `data:`
 * images and CPA-relative paths (read through the host-mediated management client) can
 * be shown; remote and `blob:` references fall back to the caller's placeholder.
 */
export function planPluginAsset(src: string, hosted: boolean): PluginAssetPlan {
  const value = src.trim();
  if (!value) return { kind: 'none' };
  if (!hosted) return { kind: 'direct', src: value };
  if (/^data:image\//i.test(value)) return { kind: 'direct', src: value };
  if (value.startsWith('/') && !value.startsWith('//')) return { kind: 'fetch', path: value };
  return { kind: 'none' };
}

const readAsDataUrl = (blob: Blob) =>
  new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error('Failed to read plugin asset'));
    reader.readAsDataURL(blob);
  });

const hostedAssets = new Map<string, Promise<string>>();

const loadHostedPluginAsset = (path: string): Promise<string> => {
  const cached = hostedAssets.get(path);
  if (cached) return cached;
  const pending = (async () => {
    const response = await apiClient.getRaw(path, { responseType: 'blob' });
    const blob: unknown = response.data;
    if (!(blob instanceof Blob) || blob.size > MAX_PLUGIN_ASSET_BYTES) {
      throw new Error('Plugin asset is not a bounded image');
    }
    if (!blob.type.toLowerCase().startsWith('image/')) {
      throw new Error('Plugin asset is not an image');
    }
    return readAsDataUrl(blob);
  })();
  // A failed read must stay retryable, not poison the cache.
  pending.catch(() => hostedAssets.delete(path));
  hostedAssets.set(path, pending);
  return pending;
};

/** Displayable `src` for a resolved plugin asset reference; empty means "show the placeholder". */
export function usePluginAssetSrc(src: string): string {
  const plan = planPluginAsset(src, isHosted());
  const path = plan.kind === 'fetch' ? plan.path : null;
  const [loaded, setLoaded] = useState<{ path: string; dataUrl: string } | null>(null);

  useEffect(() => {
    if (!path) return;
    let cancelled = false;
    loadHostedPluginAsset(path).then(
      (dataUrl) => {
        if (!cancelled) setLoaded({ path, dataUrl });
      },
      () => {
        if (!cancelled) setLoaded({ path, dataUrl: '' });
      }
    );
    return () => {
      cancelled = true;
    };
  }, [path]);

  if (plan.kind === 'direct') return plan.src;
  return plan.kind === 'fetch' && loaded?.path === plan.path ? loaded.dataUrl : '';
}
