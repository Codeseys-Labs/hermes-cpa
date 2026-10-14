import { Outlet } from 'react-router-dom';
import { NotificationContainer } from '@/components/common/NotificationContainer';
import { ConfirmationModal } from '@/components/common/ConfirmationModal';

/** Notification and confirmation surfaces shared by every route of the app. */
export function RootShell() {
  return (
    <>
      <NotificationContainer />
      <ConfirmationModal />
      <Outlet />
    </>
  );
}
