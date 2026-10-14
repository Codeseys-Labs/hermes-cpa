/** The only link schemes the host will present outside the console. */
const EXTERNAL_PROTOCOLS = new Set(['http:', 'https:']);

/**
 * The absolute http(s) URL an activated link points at, or null when the click must be left
 * alone (not a link, in-page fragment, or any scheme other than http/https).
 */
export function findExternalLink(target: unknown): string | null {
  if (typeof target !== 'object' || target === null || !('closest' in target)) return null;
  if (typeof target.closest !== 'function') return null;
  const anchor: unknown = target.closest('a[href]');
  if (typeof anchor !== 'object' || anchor === null || !('href' in anchor)) return null;
  if (typeof anchor.href !== 'string') return null;
  try {
    const url = new URL(anchor.href);
    return EXTERNAL_PROTOCOLS.has(url.protocol) ? url.href : null;
  } catch {
    return null;
  }
}

interface ExternalLinkEnvironment {
  document: Pick<Document, 'addEventListener' | 'removeEventListener'>;
  open: (url: string) => Promise<void>;
  onError: (error: unknown) => void;
}

/**
 * A sandboxed hosted document cannot open popups or navigate itself. Every http(s) link
 * click is cancelled and handed to the reviewed host capability instead.
 */
export function installExternalLinkHandler({
  document,
  open,
  onError,
}: ExternalLinkEnvironment): () => void {
  const handleClick = (event: MouseEvent) => {
    if (event.defaultPrevented) return;
    const url = findExternalLink(event.target);
    if (!url) return;
    event.preventDefault();
    open(url).catch(onError);
  };
  document.addEventListener('click', handleClick);
  return () => document.removeEventListener('click', handleClick);
}
