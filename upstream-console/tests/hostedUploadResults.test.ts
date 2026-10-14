import { afterEach, expect, spyOn, test } from 'bun:test';
import { apiClient } from '../src/services/api/client';
import { authFilesApi } from '../src/services/api/authFiles';
import { HostRequestError } from '../src/hermesTransport';

const documentDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'document');
afterEach(() => {
  if (documentDescriptor) Object.defineProperty(globalThis, 'document', documentDescriptor);
  else Reflect.deleteProperty(globalThis, 'document');
});

test('a partially completed upload batch retains terminal successes and never retries an uncertain file', async () => {
  Object.defineProperty(globalThis, 'document', { configurable: true, value: {
    querySelector: () => ({ getAttribute: () => '3' }),
  } });
  const attempted: string[] = [];
  const post = spyOn(apiClient, 'postForm').mockImplementation(async (_url, form) => {
    const file = form.get('file');
    if (!(file instanceof File)) throw new Error('Expected one actual file');
    attempted.push(file.name);
    if (file.name === 'uncertain.json') {
      throw new HostRequestError('outcome-unknown', 'Fixture acknowledgement lost');
    }
    return { status: 'ok', uploaded: 1, files: [file.name], failed: [] };
  });
  try {
    const result = await authFilesApi.uploadFiles([
      new File(['{}'], 'accepted.json'),
      new File(['{}'], 'uncertain.json'),
      new File(['{}'], 'not-attempted.json'),
    ]);
    expect(attempted).toEqual(['accepted.json', 'uncertain.json']);
    expect(result).toEqual({
      status: 'unknown', uploaded: 1, files: ['accepted.json'], failed: [],
      uncertain: ['uncertain.json'], notAttempted: ['not-attempted.json'],
    });
  } finally { post.mockRestore(); }
});
