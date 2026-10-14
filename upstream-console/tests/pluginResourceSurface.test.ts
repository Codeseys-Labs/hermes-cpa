import { describe, expect, test } from 'bun:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { I18nextProvider } from 'react-i18next';
import { createInstance } from 'i18next';
import { PluginResourceSurface } from '../src/features/plugins/PluginResourcePage';
import type { PluginResourceEntry } from '../src/features/plugins/pluginResources';
import en from '../src/i18n/locales/en.json';

const i18n = createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: en } } });

const resource: PluginResourceEntry = {
  pluginID: 'demo',
  pluginTitle: 'Demo Plugin',
  pluginLogo: '',
  menuIndex: 0,
  menu: { path: '/plugins/demo/index.html', menu: 'Demo Dashboard', description: '' },
  label: 'Demo Dashboard',
  description: '',
  route: '/plugin-pages/demo/0',
};

const render = (props: { hosted: boolean; iframeSrc: string; opening?: boolean }) =>
  renderToStaticMarkup(
    createElement(
      I18nextProvider,
      { i18n },
      createElement(PluginResourceSurface, {
        resource,
        opening: false,
        onOpenInHost: () => undefined,
        ...props,
      })
    )
  );

describe('plugin resource surface', () => {
  test('a hosted console never frames plugin code, even when a page URL is known', () => {
    const markup = render({ hosted: true, iframeSrc: 'https://plugin.example/index.html' });

    expect(markup).not.toContain('<iframe');
    expect(markup).not.toContain('plugin.example');
    expect(markup).toContain('Demo Dashboard');
    expect(markup).toContain('Open plugin page');
    expect(markup).toContain('Demo Plugin');
  });

  test('the reference app frames the declared page without referrer or opener ties', () => {
    const markup = render({ hosted: false, iframeSrc: 'http://127.0.0.1:8317/plugins/demo/' });

    expect(markup).toContain('<iframe');
    expect(markup).toContain('src="http://127.0.0.1:8317/plugins/demo/"');
    expect(markup).toMatch(/referrerpolicy="no-referrer"/i);
    expect(markup).not.toContain('Open plugin page');
  });

  test('the reference app explains a menu with no embeddable page', () => {
    const markup = render({ hosted: false, iframeSrc: '' });

    expect(markup).not.toContain('<iframe');
    expect(markup).toContain('Plugin page URL is empty');
  });
});
