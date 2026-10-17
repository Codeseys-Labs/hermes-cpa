import { defineConfig, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { viteSingleFile } from 'vite-plugin-singlefile';
import path from 'path';
import { execSync } from 'child_process';
import fs from 'fs';

// Get version from environment, git tag, or package.json
function getVersion(): string {
  // 1. Environment variable (set by GitHub Actions)
  if (process.env.VERSION) {
    return process.env.VERSION;
  }

  // 2. Try git tag
  try {
    const gitTag = execSync('git describe --tags --exact-match 2>/dev/null || git describe --tags 2>/dev/null || echo ""', { encoding: 'utf8' }).trim();
    if (gitTag) {
      return gitTag;
    }
  } catch {
    // Git not available or no tags
  }

  // 3. Fall back to package.json version
  try {
    const pkg = JSON.parse(fs.readFileSync(path.resolve(__dirname, 'package.json'), 'utf8'));
    if (pkg.version && pkg.version !== '0.0.0') {
      return pkg.version;
    }
  } catch {
    // package.json not readable
  }

  return 'dev';
}

// vite-plugin-singlefile inlines the JS/CSS chunks and, by default, deletes them from the bundle
// before Vite's license plugin (build.license) runs, which would leave the dependency license
// inventory empty. Keep the chunks in the bundle, then remove the written files so dist/ holds
// only index.html and the inventory that scripts/build_console.py publishes as notices.
function pruneInlinedFiles(): Plugin {
  return {
    name: 'hermes:prune-inlined-files',
    enforce: 'post',
    writeBundle(options, bundle) {
      const { dir } = options;
      if (!dir) {
        throw new Error('Single-file output requires an output directory');
      }
      for (const fileName of Object.keys(bundle)) {
        if (/\.(?:[mc]?js|css)$/.test(fileName)) {
          fs.rmSync(path.join(dir, fileName));
        }
      }
    }
  };
}

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [
    react(),
    viteSingleFile({
      removeViteModuleLoader: true,
      deleteInlinedFiles: false
    }),
    pruneInlinedFiles()
  ],
  define: {
    __APP_VERSION__: JSON.stringify(getVersion())
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src')
    }
  },
  css: {
    modules: {
      localsConvention: 'camelCase',
      generateScopedName: '[name]__[local]___[hash:base64:5]'
    },
    preprocessorOptions: {
      scss: {
        additionalData: `@use "@/styles/variables.scss" as *;`
      }
    }
  },
  build: {
    target: 'es2020',
    outDir: 'dist',
    assetsInlineLimit: 100000000,
    license: { fileName: '.vite/license.json' },
    chunkSizeWarningLimit: 100000000,
    cssCodeSplit: false,
    rolldownOptions: {
      output: {
        codeSplitting: false
      }
    }
  }
});
