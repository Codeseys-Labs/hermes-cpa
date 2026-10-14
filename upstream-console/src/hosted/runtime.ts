/** The host artifact sets this marker before importing any application modules. */
export function isHosted(): boolean {
  return typeof document !== 'undefined' && typeof document.querySelector === 'function' &&
    document.querySelector('meta[name="hermes-cpa-hosted"]')?.getAttribute('content') === '3';
}
