import { describe, expect, test } from 'bun:test';
import { decodeBase64, encodeBase64, MAX_FILE_BYTES, MAX_RESPONSE_BYTES, MAX_WIRE_BYTES } from '../src/hosted/management';
import { saveFile } from '../src/hosted/browser';

describe('hosted export and upload bounds', () => {
  test('a 32 MiB export fits the 48 MiB wire as base64 and stays separate from the 10 MiB upload limit', () => {
    expect(MAX_FILE_BYTES).toBe(10 * 1024 * 1024);
    expect(MAX_RESPONSE_BYTES).toBe(32 * 1024 * 1024);
    const base64Length = Math.ceil(MAX_RESPONSE_BYTES / 3) * 4;
    expect(base64Length + 64 * 1024).toBeLessThan(MAX_WIRE_BYTES);
  });

  test('base64 chunks concatenate exactly across chunk boundaries and beyond the upload limit', () => {
    expect(encodeBase64(new Uint8Array())).toBe('');
    for (const length of [1, 2, 3, 4, 0x6000 - 1, 0x6000, 0x6000 + 1, 0x6000 * 2 + 2]) {
      const bytes = Uint8Array.from({ length }, (_, index) => (index * 31 + length) & 255);
      expect(encodeBase64(bytes)).toBe(Buffer.from(bytes).toString('base64'));
    }
    const length = MAX_FILE_BYTES + 0x6000 * 3 + 7;
    const bytes = new Uint8Array(length);
    for (let index = 0; index < length; index += 1) bytes[index] = (index * 31 + 7) & 255;
    const encoded = encodeBase64(bytes);
    expect(encoded).toBe(Buffer.from(bytes).toString('base64'));
    const decoded = decodeBase64(encoded);
    expect(decoded.byteLength).toBe(length);
    expect(Buffer.compare(Buffer.from(decoded), Buffer.from(bytes))).toBe(0);
  });

  test('saveFile refuses a blob above the export bound before touching the host', async () => {
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'document');
    Object.defineProperty(globalThis, 'document', {
      configurable: true,
      value: {
        querySelector: (selector: string) => selector === 'meta[name="hermes-cpa-hosted"]'
          ? { getAttribute: () => '3' } : null,
      },
    });
    try {
      await expect(saveFile(new Blob([new Uint8Array(MAX_RESPONSE_BYTES + 1)]), 'export.bin'))
        .rejects.toThrow('export limit');
    } finally {
      if (descriptor) Object.defineProperty(globalThis, 'document', descriptor);
      else Reflect.deleteProperty(globalThis, 'document');
    }
  });
});
