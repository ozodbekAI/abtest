import { useState } from 'react';
import { ArrowRight, Check, Eye, EyeOff, LockKeyhole, Mail, UserRound } from 'lucide-react';
import { Link, useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { AuthLayout } from '../components/AuthLayout';
import { useAuth } from '../contexts/AuthContext';
import { ApiError } from '../api/client';

export function RegisterPage() {
  const { register } = useAuth(); const navigate = useNavigate();
  const [form, setForm] = useState({ first_name: '', last_name: '', email: '', password: '', confirm_password: '' });
  const [showPassword, setShowPassword] = useState(false); const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const [busy, setBusy] = useState(false);
  const [recovery, setRecovery] = useState(false);
  const update = (key: keyof typeof form) => (event: React.ChangeEvent<HTMLInputElement>) => setForm((old) => ({ ...old, [key]: event.target.value }));
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    if (form.password !== form.confirm_password) { toast.error('Пароли не совпадают'); return; }
    const payload = { ...form, first_name: form.first_name.trim(), last_name: form.last_name.trim(), email: form.email.trim().toLowerCase() };
    if (payload.first_name.length < 2 || payload.last_name.length < 2) { toast.error('Укажите имя и фамилию: минимум два символа без пробелов.'); return; }
    setBusy(true);
    setRecovery(false);
    try {
      await register(payload);
      sessionStorage.setItem('wb_verify_email', payload.email);
      navigate('/verify-email');
      toast.success('Код подтверждения отправлен');
    } catch (error: any) {
      const apiError = error instanceof ApiError ? error : null;
      const message = apiError?.message || 'Ответ регистрации не получен. Аккаунт мог быть создан; проверьте почту или войдите.';
      if ((apiError?.status === 409 || apiError?.status === 403) && /подтвержден|подтверд|регистрация.*начата/i.test(message)) {
        sessionStorage.setItem('wb_verify_email', payload.email);
        navigate('/verify-email');
      } else if (apiError?.status === 409) {
        navigate('/login');
      } else if (!apiError || apiError.status >= 500 || apiError.status === 429) {
        sessionStorage.setItem('wb_verify_email', payload.email);
        setRecovery(true);
      }
      toast.error(message);
    } finally { setBusy(false); }
  };

  return <AuthLayout><div className="auth-box auth-box-wide"><div className="mobile-brand brand"><span className="brand-mark">W</span> WB Optimizer</div><div className="auth-heading"><span className="icon-bubble"><UserRound size={19} /></span><h2>Создайте рабочее пространство</h2><p>Начните управлять магазином на основе данных.</p></div><>{recovery && <div className="ab-error-banner" role="alert"><div><strong>Результат регистрации не подтверждён</strong><p>Проверьте почту: если код пришёл, продолжите подтверждение. Для уже подтверждённого аккаунта используйте вход.</p><Link to="/verify-email">Ввести код или запросить новый</Link> · <Link to="/login">Войти</Link></div></div>}</><form onSubmit={submit} className="form-stack"><div className="form-grid"><label>Имя<input value={form.first_name} onChange={update('first_name')} placeholder="Озодбек" required minLength={2} autoComplete="given-name" /></label><label>Фамилия<input value={form.last_name} onChange={update('last_name')} placeholder="Эргашев" required minLength={2} autoComplete="family-name" /></label></div><label>Электронная почта<input type="email" value={form.email} onChange={update('email')} placeholder="ваша@компания.ru" required autoComplete="email" /><Mail size={17} /></label><label>Пароль<div className="input-with-action"><input type={showPassword ? 'text' : 'password'} value={form.password} onChange={update('password')} placeholder="Не менее 8 символов" required minLength={8} autoComplete="new-password" /><button type="button" onClick={() => setShowPassword((value) => !value)} aria-label={showPassword ? 'Скрыть пароль' : 'Показать пароль'}>{showPassword ? <EyeOff size={17} /> : <Eye size={17} />}</button></div></label><label>Подтвердите пароль<div className="input-with-action"><input type={showConfirmPassword ? 'text' : 'password'} value={form.confirm_password} onChange={update('confirm_password')} placeholder="Повторите пароль" required minLength={8} autoComplete="new-password" /><button type="button" onClick={() => setShowConfirmPassword((value) => !value)} aria-label={showConfirmPassword ? 'Скрыть подтверждение пароля' : 'Показать подтверждение пароля'}>{showConfirmPassword ? <EyeOff size={17} /> : <Eye size={17} />}</button></div></label><label className="check-label terms"><input type="checkbox" required /> <span>Я принимаю условия использования и политику конфиденциальности</span></label><button className="primary-button" disabled={busy}>{busy ? 'Создание аккаунта…' : 'Создать аккаунт'}<ArrowRight size={17} /></button></form><div className="secure-note"><Check size={14} /> Ваши данные зашифрованы и защищены</div><p className="auth-switch">Уже есть аккаунт? <Link to="/login">Войти</Link></p></div></AuthLayout>;
}
