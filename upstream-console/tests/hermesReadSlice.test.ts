import { expect, test } from 'bun:test';
import { readFileSync } from 'node:fs';
import i18n, { resources } from '../src/i18n/hermesRead';
const source = readFileSync(new URL('../src/features/HermesInventory.tsx', import.meta.url), 'utf8');

for (const feature of ['providers', 'auth', 'models', 'quota', 'logs', 'plugins'] as const) {
  test(`${feature} exposes a disabled mutation and precise limitation`, () => {
    expect(source).toContain('<Button disabled aria-describedby=');
    expect(source).toContain('description={t(`${feature}_note`)}');
    expect(resources.en.translation.disabled).toContain('Full CPAMC parity is deferred');
    expect(resources.en.translation[`${feature}_note`].length).toBeGreaterThan(100);
  });
}
test('all four isolated locales carry the complete read-slice key set', async () => {
  for (const [locale, value] of Object.entries(resources)) {
    expect(Object.keys(value.translation).sort()).toEqual(Object.keys(resources.en.translation).sort());
    await i18n.changeLanguage(locale);
    expect(i18n.t('providers')).toBe(value.translation.providers);
    expect(i18n.t('auth_note')).toBe(value.translation.auth_note);
  }
  await i18n.changeLanguage('en');
});
