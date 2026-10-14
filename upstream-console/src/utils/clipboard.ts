import { copyText } from '@/hosted/browser';

/**
 * Copy through the shared browser capability: the clipboard API in the reference app,
 * the reviewed host clipboard when the console is hosted. Resolves false on denial so
 * callers keep their own failure notification.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await copyText(text);
    return true;
  } catch {
    return false;
  }
}
