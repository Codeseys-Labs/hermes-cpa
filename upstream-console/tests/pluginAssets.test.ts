import { describe, expect, test } from 'bun:test';
import { planPluginAsset } from '../src/features/plugins/pluginAssets';

const PIXEL = 'data:image/png;base64,iVBORw0KGgo=';

describe('plugin asset display plan', () => {
  test('the reference app points the image straight at whatever the plugin declares', () => {
    expect(planPluginAsset('https://cdn.example/logo.png', false)).toEqual({
      kind: 'direct',
      src: 'https://cdn.example/logo.png',
    });
    expect(planPluginAsset('  http://127.0.0.1:8317/logo.svg ', false)).toEqual({
      kind: 'direct',
      src: 'http://127.0.0.1:8317/logo.svg',
    });
    expect(planPluginAsset('', false)).toEqual({ kind: 'none' });
  });

  test('a hosted console shows inline images as they are', () => {
    expect(planPluginAsset(PIXEL, true)).toEqual({ kind: 'direct', src: PIXEL });
  });

  test('a hosted console reads CPA-relative logos through the mediated client', () => {
    expect(planPluginAsset('/v0/resource/plugins/demo/logo.png', true)).toEqual({
      kind: 'fetch',
      path: '/v0/resource/plugins/demo/logo.png',
    });
  });

  test('a hosted console never points an image at a remote or opaque URL', () => {
    for (const declared of [
      'https://cdn.example/logo.png',
      'http://cdn.example/logo.png',
      '//cdn.example/logo.png',
      'blob:https://example.com/id',
      'data:text/html,<p>x</p>',
      'logo.png',
      '',
      '   ',
    ]) {
      expect(planPluginAsset(declared, true)).toEqual({ kind: 'none' });
    }
  });
});
