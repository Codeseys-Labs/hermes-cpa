import { getHostedTransport } from '@/hermesTransport';
import { encodeBase64, MAX_RESPONSE_BYTES } from './management';
import { isHosted } from './runtime';

export async function openExternal(url: string): Promise<void> {
  if (isHosted()) {
    await getHostedTransport().request('browser.open', { url });
    return;
  }
  window.open(url, '_blank', 'noopener,noreferrer');
}

export async function copyText(text: string): Promise<void> {
  if (isHosted()) {
    await getHostedTransport().request('browser.copy', { text });
    return;
  }
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
  } catch { /* Preserve the reference app's non-secure-context fallback. */ }
  const previous = document.activeElement;
  const input = document.createElement('textarea');
  input.value = text;
  input.readOnly = true;
  input.style.cssText = 'position:fixed;opacity:0;left:-9999px;top:0';
  document.body.appendChild(input);
  try {
    input.select();
    if (!document.execCommand('copy')) throw new Error('Clipboard copy failed');
  } finally {
    input.remove();
    if (previous instanceof HTMLElement) previous.focus();
  }
}

export async function saveFile(blob: Blob, filename: string): Promise<void> {
  if (isHosted()) {
    if (blob.size > MAX_RESPONSE_BYTES) throw new Error('File exceeds the console export limit');
    const base64 = encodeBase64(new Uint8Array(await blob.arrayBuffer()));
    await getHostedTransport().request('file.save', {
      filename, content_type: blob.type || 'application/octet-stream', base64,
    });
    return;
  }
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.rel = 'noopener';
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  setTimeout(() => { URL.revokeObjectURL(url); link.remove(); }, 1000);
}

/** Plugin code is opened outside the privileged console, never inside its document. */
export async function openPlugin(pluginId: string, menuIndex: number): Promise<void> {
  if (!Number.isInteger(menuIndex) || menuIndex < 0 || !pluginId) {
    throw new Error('Invalid plugin menu');
  }
  await getHostedTransport().request('plugin.open', { plugin_id: pluginId, menu_index: menuIndex });
}
