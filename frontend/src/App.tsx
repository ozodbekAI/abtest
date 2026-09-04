import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { Toaster } from 'sonner';
import { AuthProvider } from './contexts/AuthContext';
import { ProtectedRoute } from './components/ProtectedRoute';
import { DashboardPage } from './pages/DashboardPage';
import { ABTestDetailPage } from './pages/ABTestDetailPage';
import { ABTestsPage } from './pages/ABTestsPage';
import { AdminPage } from './pages/AdminPage';
import { ForgotPasswordPage } from './pages/ForgotPasswordPage';
import { LandingPage } from './pages/LandingPage';
import { LoginPage } from './pages/LoginPage';
import { RegisterPage } from './pages/RegisterPage';
import { ResetPasswordPage } from './pages/ResetPasswordPage';
import { SettingsPage } from './pages/SettingsPage';
import { VerifyEmailPage } from './pages/VerifyEmailPage';

export default function App() {
  return <BrowserRouter><AuthProvider><Routes><Route path="/" element={<LandingPage />} /><Route path="/login" element={<LoginPage />} /><Route path="/register" element={<RegisterPage />} /><Route path="/verify-email" element={<VerifyEmailPage />} /><Route path="/forgot-password" element={<ForgotPasswordPage />} /><Route path="/reset-password" element={<ResetPasswordPage />} /><Route element={<ProtectedRoute />}><Route path="/dashboard" element={<DashboardPage />} /><Route path="/settings" element={<SettingsPage />} /><Route path="/ab-tests" element={<ABTestsPage />} /><Route path="/ab-tests/:testId" element={<ABTestDetailPage />} /><Route path="/admin" element={<AdminPage />} /></Route><Route path="*" element={<Navigate to="/" replace />} /></Routes><Toaster theme="light" position="top-right" richColors /></AuthProvider></BrowserRouter>;
}
