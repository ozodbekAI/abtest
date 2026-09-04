import { useState } from 'react';
import { ArrowRight, Eye, EyeOff, LockKeyhole, Mail } from 'lucide-react';
import { Link, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { AuthLayout } from '../components/AuthLayout';
import { useAuth } from '../contexts/AuthContext';

export function LoginPage() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [show, setShow] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault(); setBusy(true);
    try { await login(email, password); toast.success('С возвращением'); navigate('/dashboard'); }
    catch (error: any) { toast.error(error?.message || 'Не удалось войти'); }
    finally { setBusy(false); }
  };
  return <AuthLayout><div className="auth-box"><div className="mobile-brand brand"><span className="brand-mark">W</span> WB Optimizer</div><div className="auth-heading"><span className="icon-bubble"><LockKeyhole size={19} /></span><h2>С возвращением</h2><p>Войдите, чтобы продолжить работу с магазином.</p></div><form onSubmit={submit} className="form-stack"><label>Электронная почта<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="ваша@компания.ru" required autoComplete="email" /><Mail size={17} /></label><label>Пароль<div className="input-with-action"><input type={show ? 'text' : 'password'} value={password} onChange={(e) => setPassword(e.target.value)} placeholder="••••••••" required minLength={8} autoComplete="current-password" /><button type="button" onClick={() => setShow((value) => !value)} aria-label={show ? 'Скрыть пароль' : 'Показать пароль'}>{show ? <EyeOff size={17} /> : <Eye size={17} />}</button></div></label><div className="form-options"><label className="check-label"><input type="checkbox" /> <span>Запомнить меня</span></label><Link to="/forgot-password">Забыли пароль?</Link></div><button className="primary-button" disabled={busy}>{busy ? 'Выполняется вход…' : 'Войти'}<ArrowRight size={17} /></button></form><p className="auth-switch">Впервые в WB Optimizer? <Link to="/register">Создать аккаунт</Link></p></div></AuthLayout>;
}
