import base from './vite.config';
import { defineConfig, mergeConfig } from 'vite';
import path from 'node:path';
// Test-only reference uses ORIGINAL pinned MainLayout source, not its Hermes
// derivative. Only effect dependencies are replaced; markup/styles unchanged.
export default mergeConfig(base, defineConfig({
  plugins: [{name:'passive-reference-only',enforce:'pre',transform(code, id) {
    if (id.endsWith('/src/HermesApp.tsx')) return code.replace("@/hermes/HermesMainLayout", "@/components/layout/MainLayout");
    if (id.endsWith('/components/layout/MainLayout.tsx')) return code
      .replace("@/stores", path.resolve('../../reference/stores.ts'))
      .replace("@/router/MainRoutes", '@/hermes/ConsoleRoutes')
      .replace("@/services/api", path.resolve('../../reference/denied-api.ts'));
  }}],
  build: {outDir:'../../reference/dist'},
}));
