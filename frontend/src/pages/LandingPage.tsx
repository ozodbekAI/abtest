import { ArrowRight, BarChart3, Check, FlaskConical, Images, ShieldCheck, Sparkles, TrendingUp } from 'lucide-react';
import { Link } from 'react-router-dom';

export function LandingPage() {
  return (
    <main className="landing-page">
      <nav className="landing-nav">
        <Link to="/" className="brand brand-light landing-brand" aria-label="На главную">
          <span className="brand-mark">W</span> WB Optimizer
        </Link>
        <div className="landing-nav-actions">
          <Link to="/login" className="landing-login">Войти</Link>
          <Link to="/register" className="light-button small">Начать работу <ArrowRight size={15} /></Link>
        </div>
      </nav>

      <section className="landing-hero">
        <div className="landing-hero-copy">
          <div className="eyebrow"><Sparkles size={14} /> Для амбициозных продавцов</div>
          <h1>Ваш каталог —<br /><em>ваше преимущество.</em></h1>
          <p>Принимайте точные решения в едином пространстве для карточек, рекламы и экспериментов Wildberries.</p>
          <div className="landing-actions">
            <Link to="/register" className="light-button">Создать рабочее пространство <ArrowRight size={16} /></Link>
            <Link to="/login" className="ghost-light">Уже есть аккаунт?</Link>
          </div>
          <div className="landing-trust">
            <span><ShieldCheck size={15} /> Шифрование по умолчанию</span>
            <span><Check size={15} /> Решения на основе данных</span>
          </div>
        </div>

        <div className="landing-hero-preview" aria-label="Предпросмотр рабочего пространства">
          <div className="hero-preview-orb" />
          <div className="hero-preview-window">
            <div className="fake-top"><span /><span /><span /></div>
            <div className="hero-preview-heading">
              <div><small>ОБЗОР РАБОЧЕГО ПРОСТРАНСТВА</small><strong>Доброе утро, продавец</strong></div>
              <span className="fake-badge"><BarChart3 size={14} /> Сегодня</span>
            </div>
            <div className="hero-preview-metrics">
              <div><span>Показы</span><strong>12 480</strong><small><TrendingUp size={11} /> +18,6%</small></div>
              <div><span>Клики</span><strong>428</strong><small><TrendingUp size={11} /> +12,4%</small></div>
              <div><span>CTR</span><strong>3,43%</strong><small><TrendingUp size={11} /> +0,8%</small></div>
            </div>
            <div className="hero-preview-chart">
              <div className="hero-preview-chart-title"><span>Динамика показателей</span><b>7 дней</b></div>
              <div className="hero-chart-grid"><i /><i /><i /><i /><i /><i /><i /><i /><i /></div>
            </div>
            <div className="hero-preview-footer">
              <span><Images size={14} /> Карточки</span>
              <span><FlaskConical size={14} /> A/B-тесты</span>
              <b>Всё под контролем <Check size={13} /></b>
            </div>
          </div>
          <div className="hero-floating-note"><span className="hero-floating-icon"><FlaskConical size={14} /></span><span><small>ЭКСПЕРИМЕНТ</small><strong>Новый победитель найден</strong></span><Check size={15} /></div>
        </div>
      </section>

      <div className="landing-dashboard">
        <div className="fake-top"><span /><span /><span /></div>
        <div className="fake-chart-title"><div><small>РАБОЧЕЕ ПРОСТРАНСТВО</small><strong>Все ключевые показатели в одном месте</strong></div><span className="fake-badge"><BarChart3 size={14} /> Живые показатели</span></div>
        <div className="fake-metrics"><div /><div /><div /><div /></div>
        <div className="fake-chart"><i /><i /><i /><i /><i /><i /><i /><i /></div>
      </div>
    </main>
  );
}
