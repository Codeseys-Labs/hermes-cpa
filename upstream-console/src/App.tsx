import { RouterProvider, createHashRouter } from 'react-router-dom';
import { LoginPage } from '@/pages/LoginPage';
import { MainLayout } from '@/components/layout/MainLayout';
import { RootShell } from '@/components/layout/RootShell';
import { useAppPreferences } from '@/hooks/useAppPreferences';
import { ProtectedRoute } from '@/router/ProtectedRoute';

const router = createHashRouter([
  {
    element: <RootShell />,
    children: [
      { path: '/login', element: <LoginPage /> },
      {
        path: '/*',
        element: (
          <ProtectedRoute>
            <MainLayout />
          </ProtectedRoute>
        ),
      },
    ],
  },
]);

function App() {
  useAppPreferences();

  return <RouterProvider router={router} />;
}

export default App;
