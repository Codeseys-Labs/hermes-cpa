/**
 * Window facade for a router inside a hosted document.
 *
 * The hosted document is loaded from a `data:` (or `srcdoc`) URL, which has no hierarchical
 * base: only its fragment may change. The facade exposes a throwaway `https://hermes.invalid/`
 * location, built from the real fragment and never fetched, and rewrites every history URL
 * to a bare fragment so react-router never asks the browser to navigate anywhere else.
 */
export function createHashRouterWindow(source: Window): Window {
  const fragmentOf = (url?: string | URL | null) =>
    (url == null ? source.location.hash : (String(url).split('#')[1] ?? '')).replace(/^#/, '');

  const facade = {
    get location() {
      return new URL(`https://hermes.invalid/${source.location.hash}`);
    },
    history: {
      get state() {
        return source.history.state;
      },
      replaceState(state: unknown, unused: string, url?: string | URL | null) {
        source.history.replaceState(state, unused, `#${fragmentOf(url)}`);
      },
      pushState(state: unknown, unused: string, url?: string | URL | null) {
        source.history.pushState(state, unused, `#${fragmentOf(url)}`);
      },
      go(delta: number) {
        source.history.go(delta);
      },
    },
    document: source.document,
    addEventListener: source.addEventListener.bind(source),
    removeEventListener: source.removeEventListener.bind(source),
  };
  // The router reads only the members above; the facade is deliberately not a full Window.
  return facade as unknown as Window;
}
