import base from './vite.config';
import { defineConfig, mergeConfig } from 'vite';
import path from 'node:path';

// Test-only reference build: the pinned upstream entry (main.tsx -> App.tsx, with its own
// login and management key) served as a normal browser document. The shipped artifact is
// index.html -> HermesApp.tsx, which runs the same pages and routes behind the hosted
// adapters; this build exists to compare its behavior against the unmodified shell.
export default mergeConfig(
  base,
  defineConfig({
    build: {
      outDir: 'dist-reference',
      rolldownOptions: {
        input: path.resolve(__dirname, 'index.reference.html'),
      },
    },
  })
);
