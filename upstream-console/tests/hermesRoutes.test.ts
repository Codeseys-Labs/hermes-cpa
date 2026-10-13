import { createElement } from 'react';
import { describe, expect, test } from 'bun:test';
import { renderToStaticMarkup } from 'react-dom/server';
import { MemoryRouter } from 'react-router-dom';
import '../src/i18n/hermesRead';
import { MainRoutes } from '../src/hermes/ConsoleRoutes';

describe('pinned MainRoutes adapted route identities, without rendering effectful pages', () => {
  for (const [path, text] of [
    ['/ai-providers', 'Persisted API-key group'], ['/system', 'Codex static catalog'],
    ['/logs', 'Latest 100 log lines'], ['/config', 'Request host review'],
    ['/auth-files', 'Auth-file and OAuth status unsupported'], ['/oauth', 'Auth-file and OAuth status unsupported'],
    ['/auth-files/oauth-excluded', 'Auth-file and OAuth status unsupported'],
    ['/auth-files/oauth-model-alias', 'Auth-file and OAuth status unsupported'],
    ['/quota', 'Quota observations unsupported'], ['/plugins', 'Plugin inventory unsupported'],
    ['/plugin-store', 'Plugin inventory unsupported'], ['/plugin-pages/untrusted/0', 'Plugin inventory unsupported'],
    ['/quick-start', 'Persisted API-key group'], ['/login', 'Unavailable'],
  ]) test(path, () => {
    const markup = renderToStaticMarkup(createElement(MemoryRouter, {initialEntries:[path]}, createElement(MainRoutes)));
    expect(markup).toContain(path === '/config' ? 'Only three supported fields' : text);
    expect(markup).not.toContain('type="password"');
  });
});
