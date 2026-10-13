// Per-document nonsecret preferences. No persistence in the opaque guest.
import { create } from 'zustand';
import i18n from '@/i18n/hermesRead';
import type { Theme, Language } from '@/types';

export const useLanguageStore = create<{
  language: Language; setLanguage: (language: Language) => void;
}>((set) => ({
  language: 'en',
  setLanguage: (language) => {
    void i18n.changeLanguage(language);
    document.documentElement.lang = language;
    set({ language });
  },
}));
export const useThemeStore = create<{
  theme: Theme; setTheme: (theme: Theme) => void;
}>((set) => ({
  theme: 'auto',
  setTheme: (theme) => {
    const resolved = theme === 'auto'
      ? window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'white'
      : theme;
    if (resolved === 'light') document.documentElement.removeAttribute('data-theme');
    else document.documentElement.setAttribute('data-theme', resolved);
    set({ theme });
  },
}));
export function initializePreferences() {
  const media = window.matchMedia('(prefers-color-scheme: dark)');
  const update = () => {
    if (useThemeStore.getState().theme === 'auto') useThemeStore.getState().setTheme('auto');
  };
  update();
  document.documentElement.lang = useLanguageStore.getState().language;
  media.addEventListener('change', update);
  return () => media.removeEventListener('change', update);
}
