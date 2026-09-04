import type { ReactNode } from 'react';
import { BarChart3, Check, Sparkles } from 'lucide-react';

export function AuthLayout({ children }: { children: ReactNode }) {
  return (
    <main className="auth-page">
      <section className="auth-visual">
        <div className="visual-orb orb-one" />
        <div className="visual-orb orb-two" />
        <div className="brand brand-light"><span className="brand-mark">W</span> WB Optimizer</div>
        <div className="visual-copy">
          <div className="eyebrow"><Sparkles size={14} /> Интеллект для маркетплейса</div>
          <h1>Карточки, которые<br /><em>продают больше.</em></h1>
          <p>Единое рабочее пространство для карточек товаров, рекламы и умных A/B-тестов.</p>
          <div className="visual-points">
            <span><Check size={15} /> Безопасное подключение WB</span>
            <span><Check size={15} /> Статистика в одном экране</span>
            <span><Check size={15} /> Решения на основе данных</span>
          </div>
        </div>
        <div className="visual-chart"><BarChart3 size={17} /><span>Ваш следующий лучший результат</span><strong>+24,8%</strong><div className="chart-bars"><i /><i /><i /><i /><i /><i /><i /></div></div>
      </section>
      <section className="auth-panel">{children}</section>
    </main>
  );
}
