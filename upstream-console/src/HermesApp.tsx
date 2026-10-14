// Hosted entry: the complete pinned management app, adapted only at the browser and
// transport boundary. index.html marks the document as hosted before any module runs.
import { createRoot } from 'react-dom/client';
import '@/styles/global.scss';
import '@/i18n';
import { HostedBootstrap } from '@/hermes/HostedApp';

createRoot(document.getElementById('root')!).render(<HostedBootstrap />);
