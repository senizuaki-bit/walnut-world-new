import React from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import {
  ROLE_SUBJECT,
  useAuth,
} from '@lark-apaas/client-toolkit/auth';

import Layout from './components/Layout';
import {
  LearningOverviewPage,
  LearningRecordsPage,
  StudentDetailPage,
  StudentsPage,
  UnauthorizedPage,
} from './pages/learning-insights';
import NotFound from './pages/NotFound/NotFound';

interface ProtectedRouteProps {
  children: React.ReactNode;
}

const ProtectedRoute: React.FC<ProtectedRouteProps> = ({ children }) => {
  const { ability, isLoading } = useAuth();

  if (isLoading) {
    return <div className="p-8 text-sm text-muted-foreground">正在核验教师权限…</div>;
  }

  if (!ability.can('walnut_teacher', ROLE_SUBJECT)) {
    return <Navigate to="/unauthorized" replace />;
  }

  return <>{children}</>;
};

const RoutesComponent: React.FC = () => {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route
          index
          element={
            <ProtectedRoute>
              <LearningOverviewPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="students"
          element={
            <ProtectedRoute>
              <StudentsPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="students/:learnerKey"
          element={
            <ProtectedRoute>
              <StudentDetailPage />
            </ProtectedRoute>
          }
        />
        <Route
          path="records"
          element={
            <ProtectedRoute>
              <LearningRecordsPage />
            </ProtectedRoute>
          }
        />
        <Route path="unauthorized" element={<UnauthorizedPage />} />
      </Route>
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
};

export default RoutesComponent;
