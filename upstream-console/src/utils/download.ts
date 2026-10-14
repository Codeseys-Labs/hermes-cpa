import { saveFile } from '@/hosted/browser';

export type DownloadBlobOptions = {
  filename: string;
  blob: Blob;
};

/**
 * Save through the shared browser capability: an anchor download in the reference app,
 * the host save dialog when the console is hosted. The promise rejects if the host
 * declines or fails, so callers must not report success before it resolves.
 */
export function downloadBlob({ filename, blob }: DownloadBlobOptions): Promise<void> {
  return saveFile(blob, filename);
}
