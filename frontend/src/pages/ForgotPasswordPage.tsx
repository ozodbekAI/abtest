import { useState } from 'react';
import { ArrowLeft, ArrowRight, Mail } from 'lucide-react';
import { Link, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { AuthLayout } from '../components/AuthLayout';
import { api } from '../api/client';

export function ForgotPasswordPage() {
  const [email, setEmail] = useState(''); const [busy, setBusy] = useState(false); const navigate = useNavigate();
  const submit = async (event: React.FormEvent) => { event.preventDefault(); setBusy(true); try { await api.forgotPassword(email); sessionStorage.setItem('wb_reset_email', email); toast.success('Если аккаунт существует, код восстановления отправлен'); navigate('/reset-password'); } catch (error: any) { toast.error(error?.message || 'Не удалось запросить восстановление'); } finally { setBusy(false); } };
  return <AuthLayout><div className="auth-box"><div className="mobile-brand brand"><span className="brand-mark">W</span> WB Optimizer</div><div className="auth-heading"><span className="icon-bubble"><Mail size={19} /></span><h2>Восстановление пароля</h2><p>Введите электронную почту — мы отправим безопасный код восстановления.</p></div><form onSubmit={submit} className="form-stack"><label>Электронная почта<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="ваша@компания.ru" required autoFocus /><Mail size={17} /></label><button className="primary-button" disabled={busy}>{busy ? 'Отправка…' : 'Отправить код восстановления'}<ArrowRight size={17} /></button></form><Link className="back-link" to="/login"><ArrowLeft size={15} /> Вернуться ко входу</Link></div></AuthLayout>;
}
