import { describe, expect, test } from 'bun:test';
import { findExternalLink, installExternalLinkHandler } from '../src/hermes/externalLinks';

const target = (href: string | null) => ({
  closest: (selector: string) => (selector === 'a[href]' && href !== null ? { href } : null),
});

describe('hosted external links', () => {
  test('hands http and https links to the host with their absolute URL', () => {
    expect(findExternalLink(target('https://help.router-for.me/'))).toBe(
      'https://help.router-for.me/'
    );
    expect(findExternalLink(target('http://example.com/a?b=1#c'))).toBe(
      'http://example.com/a?b=1#c'
    );
  });

  test('never forwards schemes the host would not present', () => {
    for (const href of [
      'javascript:alert(1)',
      'data:text/html,<script>1</script>',
      'blob:https://example.com/id',
      'file:///etc/passwd',
      'mailto:a@example.com',
      'data:text/html;base64,AAAA#/config',
    ]) {
      expect(findExternalLink(target(href))).toBeNull();
    }
  });

  test('ignores clicks that are not on a link', () => {
    expect(findExternalLink(target(null))).toBeNull();
    expect(findExternalLink({})).toBeNull();
    expect(findExternalLink(null)).toBeNull();
    expect(findExternalLink('https://example.com/')).toBeNull();
  });

  test('cancels the click, opens it once, and reports a rejected open', async () => {
    let listener: ((event: MouseEvent) => void) | undefined;
    const documentStub = {
      addEventListener: (_type: string, handler: (event: MouseEvent) => void) => {
        listener = handler;
      },
      removeEventListener: () => {
        listener = undefined;
      },
    };
    const opened: string[] = [];
    const failures: unknown[] = [];
    const denied = new Error('host declined');
    const dispose = installExternalLinkHandler({
      document: documentStub,
      open: async (url) => {
        opened.push(url);
        throw denied;
      },
      onError: (error) => failures.push(error),
    });

    let prevented = 0;
    const click = (clickTarget: unknown, defaultPrevented = false) =>
      listener?.({
        defaultPrevented,
        target: clickTarget,
        preventDefault: () => prevented++,
      } as unknown as MouseEvent);

    click(target('https://github.com/router-for-me/CLIProxyAPI'));
    click(target('javascript:void(0)'));
    click(target('https://example.com/handled'), true);
    await Promise.resolve();
    await Promise.resolve();

    expect(opened).toEqual(['https://github.com/router-for-me/CLIProxyAPI']);
    expect(prevented).toBe(1);
    expect(failures).toEqual([denied]);

    dispose();
    expect(listener).toBeUndefined();
  });
});
