import { useEffect, useState } from 'react';
import { ArrowLeft, CheckCircle2, MailCheck } from 'lucide-react';
import { Link, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { AuthLayout } from '../components/AuthLayout';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';

export function VerifyEmailPage() {
  const { verifyEmail } = useAuth(); const navigate = useNavigate();
  const [email, setEmail] = useState(sessionStorage.getItem('wb_verify_email') || '');
  const [code, setCode] = useState(''); const [busy, setBusy] = useState(false); const [resent, setResent] = useState(false);
  useEffect(() => { if (!email) navigate('/register', { replace: true }); }, [email, navigate]);
  const submit = async (event: React.FormEvent) => { event.preventDefault(); setBusy(true); try { await verifyEmail(email, code); sessionStorage.removeItem('wb_verify_email'); toast.success('Электронная почта подтверждена'); navigate('/dashboard?onboarding=1'); } catch (error: any) { toast.error(error?.message || 'Неверный код подтверждения'); } finally { setBusy(false); } };
  const resend = async () => { try { await api.resendCode(email); setResent(true); toast.success('Новый код отправлен'); } catch (error: any) { toast.error(error?.message || 'Не удалось отправить код повторно'); } };
  return <AuthLayout><div className="auth-box"><div className="mobile-brand brand"><span className="brand-mark">W</span> WB Optimizer</div><div className="auth-heading"><span className="icon-bubble icon-bubble-green"><MailCheck size={19} /></span><h2>Проверьте электронную почту</h2><p>Мы отправили 6-значный код на <strong>{email}</strong>.</p></div><form onSubmit={submit} className="form-stack"><label>Код подтверждения<input className="code-input" inputMode="numeric" pattern="[0-9]{6}" maxLength={6} value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} placeholder="000000" autoFocus required /></label><button className="primary-button" disabled={busy || code.length !== 6}>{busy ? 'Проверка…' : 'Подтвердить почту'}<CheckCircle2 size={17} /></button></form><div className="resend-row"><span>Код не пришёл?</span><button onClick={() => void resend()}>{resent ? 'Отправлен повторно' : 'Отправить код повторно'}</button></div><Link className="back-link" to="/register"><ArrowLeft size={15} /> Изменить почту</Link></div></AuthLayout>;
}
