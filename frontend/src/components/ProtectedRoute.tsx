import { Navigate, Outlet } from 'react-router-dom';
import { useAuth } from '../contexts/AuthContext';

export function ProtectedRoute() {
  const { authenticated, loading } = useAuth();
  if (loading) return <div className="screen-loader"><div className="loader-dot" /></div>;
  return authenticated ? <Outlet /> : <Navigate to="/login" replace />;
}

