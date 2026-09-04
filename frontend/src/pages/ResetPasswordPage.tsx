import { useState } from 'react';
import { ArrowLeft, CheckCircle2, Eye, EyeOff, LockKeyhole } from 'lucide-react';
import { Link, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { AuthLayout } from '../components/AuthLayout';
import { api } from '../api/client';

export function ResetPasswordPage() {
  const [email, setEmail] = useState(sessionStorage.getItem('wb_reset_email') || ''); const [code, setCode] = useState(''); const [password, setPassword] = useState(''); const [showPassword, setShowPassword] = useState(false); const [busy, setBusy] = useState(false); const navigate = useNavigate();
  const submit = async (event: React.FormEvent) => { event.preventDefault(); setBusy(true); try { await api.resetPassword(email, code, password); sessionStorage.removeItem('wb_reset_email'); toast.success('Пароль обновлён'); navigate('/login'); } catch (error: any) { toast.error(error?.message || 'Не удалось сбросить пароль'); } finally { setBusy(false); } };
  return <AuthLayout><div className="auth-box"><div className="mobile-brand brand"><span className="brand-mark">W</span> WB Optimizer</div><div className="auth-heading"><span className="icon-bubble"><LockKeyhole size={19} /></span><h2>Новый пароль</h2><p>Введите 6-значный код из письма.</p></div><form onSubmit={submit} className="form-stack"><label>Электронная почта<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required /></label><label>Код восстановления<input className="code-input" inputMode="numeric" pattern="[0-9]{6}" maxLength={6} value={code} onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))} placeholder="000000" required /></label><label>Новый пароль<div className="input-with-action"><input type={showPassword ? 'text' : 'password'} minLength={8} value={password} onChange={(e) => setPassword(e.target.value)} placeholder="Не менее 8 символов" required /><button type="button" onClick={() => setShowPassword((value) => !value)} aria-label={showPassword ? 'Скрыть пароль' : 'Показать пароль'}>{showPassword ? <EyeOff size={17} /> : <Eye size={17} />}</button></div></label><button className="primary-button" disabled={busy || code.length !== 6}>{busy ? 'Обновление…' : 'Обновить пароль'}<CheckCircle2 size={17} /></button></form><Link className="back-link" to="/login"><ArrowLeft size={15} /> Вернуться ко входу</Link></div></AuthLayout>;
}
