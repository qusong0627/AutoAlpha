import { lazy, Suspense, useState, type ReactNode, type CSSProperties } from 'react';
import { Alert, App, Button, Empty, Skeleton, Tag, Tooltip } from 'antd';
import { Check, Copy, RefreshCw, ArrowUpRight, Sparkles, Inbox } from 'lucide-react';
const ReactECharts = lazy(() => import('./Chart'));
import { number, percent, type Row } from './api';

export function Chart({ option, style }: { option: Row; style?: CSSProperties }) {
  return <Suspense fallback={<Skeleton active paragraph={{ rows: 5 }} />}><ReactECharts option={option} style={style} /></Suspense>;
}

export function Page({ title, description, eyebrow, actions, children, className = '' }: { title: ReactNode; description?: ReactNode; eyebrow?: ReactNode; actions?: ReactNode; children: ReactNode; className?: string }) {
  return <div className={`workspace-page ${className}`.trim()}><header className="page-heading">{eyebrow && <span className="eyebrow">{eyebrow}</span>}<h1>{title}</h1>{description && <p>{description}</p>}{actions && <div className="actions">{actions}</div>}</header>{children}</div>;
}
/** The original "system environment / data spec" strip: labelled columns of facts. */
export function SpecPanel({ title, note, icon, items, columns }: { title: ReactNode; note?: ReactNode; icon?: ReactNode; items: [ReactNode, ReactNode, ReactNode?, string?][]; columns?: 2 | 3 | 4 | 5 }) {
  return <section className="surface spec-panel">
    <header className="spec-head">{icon && <span className="spec-icon">{icon}</span>}<b>{title}</b>{note && <span className="spec-note">{note}</span>}</header>
    <div className={`spec-grid${columns ? ` cols-${columns}` : ''}`}>{items.map(([label, value, hint, color], index) => <div className="spec" key={index}><span className="spec-label" style={color ? { color } : undefined}>{label}</span><strong className="spec-value">{value}</strong>{hint && <span className="spec-hint">{hint}</span>}</div>)}</div>
  </section>;
}
/** The original three-card group: icon, coloured tag, title, blurb, action link. */
export function CardGrid({ cards, columns = 3 }: { cards: { icon: ReactNode; tag?: string; color?: string; title: string; description?: ReactNode; link?: string; onClick?: () => void }[]; columns?: 2 | 3 | 4 }) {
  return <div className={`card-grid cols-${columns}`}>{cards.map(card => <div className="card" key={card.title} onClick={card.onClick} style={card.onClick ? { cursor: 'pointer' } : undefined}>
    <div className="card-top"><span className="card-icon" style={card.color ? { color: card.color, background: `${card.color}12`, borderColor: `${card.color}26` } : undefined}>{card.icon}</span>{card.tag && <span className="card-tag" style={card.color ? { color: card.color, background: `${card.color}14` } : undefined}>{card.tag}</span>}</div>
    <strong>{card.title}</strong>
    {card.description && <p>{card.description}</p>}
    {card.link && <div className="card-footer"><button type="button" className="card-link" style={card.color ? { color: card.color } : undefined} onClick={e => { e.stopPropagation(); card.onClick?.(); }}>{card.link}</button></div>}
  </div>)}</div>;
}
export function Panel({ title, extra, children, className = '' }: { title?: ReactNode; extra?: ReactNode; children: ReactNode; className?: string }) {
  return <section className={`surface ${className}`}>{(title || extra) && <header className="section-heading">{title && <h2>{title}</h2>}{extra && <div className="actions">{extra}</div>}</header>}{children}</section>;
}
export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return <div className="stat surface"><span className="stat-label">{label}</span><strong>{value ?? '—'}</strong>{hint && <span className="subtle">{hint}</span>}</div>;
}
export function Status({ value }: { value?: string }) {
  const statuses: Record<string, [string, string]> = { ok: ['正常', 'success'], partial: ['未完成', 'warning'], absent: ['未执行', 'default'], 实验: ['实验', 'default'], 观察: ['观察', 'processing'], 在用: ['在用', 'success'], 退役: ['退役', 'default'], 验证中: ['验证中', 'processing'], running: ['运行中', 'processing'], backtesting: ['回测中', 'processing'], queued: ['排队中', 'default'], pending: ['待处理', 'gold'], completed: ['已完成', 'success'], done: ['已完成', 'success'], failed: ['失败', 'error'], cancelled: ['已取消', 'default'], draft: ['草稿', 'gold'], published: ['已发布', 'success'], archived: ['已归档', 'default'], unavailable: ['数据未就绪', 'gold'], unsupported: ['不兼容', 'gold'], data_unsupported: ['数据不兼容', 'gold'], insufficient: ['样本不足', 'gold'] };
  const entry = statuses[value || ''] || [value || '未知', 'default'];
  return <Tag color={entry[1]}>{entry[0]}</Tag>;
}
export function LoadState({ resource, children, empty }: { resource: { loading: boolean; error?: string; data?: unknown; refresh: () => void }; children: ReactNode; empty?: boolean }) {
  if (resource.loading && !resource.data) return <div className="surface"><Skeleton active paragraph={{ rows: 3 }} /></div>;
  return <>{resource.error && <Alert type="error" showIcon message="暂时无法加载" description={resource.error} action={<Button size="small" onClick={resource.refresh}>重试</Button>} />}{resource.data !== undefined && (empty ? <EmptyState title="暂无记录" description="完成相关操作后，结果会显示在这里。" /> : children)}</>;
}
export function EmptyState({ title, description, action, icon }: { title: string; description: ReactNode; action?: ReactNode; icon?: ReactNode }) {
  return <div className="empty-state"><div className="empty-icon">{icon || <Inbox size={25} />}</div><h3>{title}</h3><div className="subtle">{description}</div>{action && <div className="actions">{action}</div>}</div>;
}
export const Refresh = ({ onClick, loading = false }: { onClick: () => void; loading?: boolean }) => <Button icon={<RefreshCw size={14} />} loading={loading} onClick={onClick}>刷新</Button>;
export function MetricGrid({ metrics, items }: { metrics?: Row; items: [string, string, 'percent'?][] }) {
  return <div className="metric-grid">{items.map(([key, label, kind]) => <div className="metric" key={key}><span>{label}</span><strong>{kind === 'percent' ? percent(metrics?.[key]) : number(metrics?.[key])}</strong></div>)}</div>;
}
async function copyText(text: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', '');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand('copy');
  textarea.remove();
  if (!copied) throw new Error('clipboard unavailable');
}
export function CopyButton({ text }: { text: string }) {
  const { message } = App.useApp();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await copyText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1800);
    } catch {
      message.error('复制失败，请检查浏览器剪贴板权限');
    }
  };
  return <Button size="small" type="text" className="copy-button" icon={copied ? <Check size={13} /> : <Copy size={13} />} onClick={copy}>{copied ? '已复制' : '复制'}</Button>;
}
export function CopyableBlock({ text, className }: { text: string; className: string }) {
  return <div className="copyable-content"><div className="copyable-toolbar"><CopyButton text={text} /></div><div className={className}>{text}</div></div>;
}
function stringifyDisplay(value: unknown, depth = 0): string {
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(2);
  if (value === null || typeof value !== 'object') return JSON.stringify(value) ?? 'null';
  const indent = '  '.repeat(depth);
  const childIndent = '  '.repeat(depth + 1);
  if (Array.isArray(value)) return value.length ? `[\n${value.map(item => `${childIndent}${stringifyDisplay(item, depth + 1)}`).join(',\n')}\n${indent}]` : '[]';
  const entries = Object.entries(value as Row);
  return entries.length ? `{\n${entries.map(([key, item]) => `${childIndent}${JSON.stringify(key)}: ${stringifyDisplay(item, depth + 1)}`).join(',\n')}\n${indent}}` : '{}';
}
export function JsonDetails({ data, label = '查看详细结果' }: { data: unknown; label?: string }) {
  const text = stringifyDisplay(data);
  return <details className="json-details"><summary>{label}</summary><div className="copyable-content"><div className="copyable-toolbar"><CopyButton text={text} /></div><pre>{text}</pre></div></details>;
}
type CurveTrace = { name: string; type: string; data: unknown[]; showSymbol: boolean; barMaxWidth?: number; itemStyle?: { color: string }; lineStyle?: { color: string; width: number } };

// 收益曲线三线配色（显著区分）：基准=橙、因子=蓝、超额=红
const RETURN_COLORS = { bench: '#f59e0b', factor: '#4f63f3', excess: '#ef4444' };

// IC 柱状图：A 股口径，红=正、绿=负（与 workspace.css 的 --up/--down 同值）
const IC_POS_COLOR = '#ef4444';
const IC_NEG_COLOR = '#10b981';

// 图表默认调色板：前 4 色沿用历史配色（≤4 条线的图不变），第 5 色起补足
// 多序列图——分位组合是 Q1..Q5 共 5 条，只有 4 色时 Q5 会回绕成 Q1 的蓝色。
const CHART_PALETTE = ['#4f63f3', '#17a88b', '#a07be9', '#e5a84f', '#ef4444', '#0ea5e9', '#f472b6', '#84cc16'];

/** 净值序列（起点 1.0）→ 累计收益（%，起点 0）。 */
function navToReturnPct(values: unknown[]): (number | null)[] {
  return values.map(value => (typeof value === 'number' && Number.isFinite(value) ? Number(((value - 1) * 100).toFixed(2)) : null));
}

/**
 * 基准收益 / 因子收益 / 超额收益 三条曲线（数据面：nav_bench / nav_long（无则 nav_ls）/ 相对强弱 nav_long÷nav_bench）。
 * 超额 = 因子累计收益 ÷ 基准累计收益 − 1（标准「超额收益」口径）。
 * 缺哪条就不画哪条——不拿 0 冒充缺失。
 */
function buildReturnTraces(series: Row): CurveTrace[] {
  const values = (key: string): unknown[] | null => (Array.isArray(series[key]) ? series[key] : null);
  const factor = values('nav_long') || values('nav_ls');
  const bench = values('nav_bench');
  const traces: CurveTrace[] = [];
  const push = (name: string, data: (number | null)[], color: string, width: number) => { if (data.some(value => value !== null)) traces.push({ name, type: 'line', data, showSymbol: false, itemStyle: { color }, lineStyle: { color, width } }); };
  if (bench) push('基准收益', navToReturnPct(bench), RETURN_COLORS.bench, 2);
  if (factor) push('因子收益', navToReturnPct(factor), RETURN_COLORS.factor, 2.5);
  if (factor && bench) push('超额收益', factor.map((value, index) => { const base = bench[index]; return typeof value === 'number' && typeof base === 'number' && base !== 0 ? Number(((value / base - 1) * 100).toFixed(2)) : null; }), RETURN_COLORS.excess, 2.5);
  return traces;
}

export function CurveChart({ series, title = '净值曲线', kind = 'nav' }: { series?: Row; title?: string; kind?: 'nav' | 'returns' | 'ic' | 'turnover' | 'quantile' }) {
  if (!series) return null;
  const dates = series.dates || series.date || [];
  const labels: Row = { nav: '净值', nav_net: '扣费净值', ic: 'IC', rank_ic: 'Rank IC', turnover: '换手率', nav_long: '多头净值', nav_ls: '多空净值', nav_bench: '基准净值', long_nav: '多头净值', long_short_nav: '多空净值', bench_nav: '基准净值' };
  const traces: CurveTrace[] = kind === 'returns' ? buildReturnTraces(series) : [];
  if (kind !== 'returns') {
    const candidates = kind === 'nav' ? ['nav', 'nav_net', 'nav_long', 'nav_ls', 'nav_bench', 'long_nav', 'long_short_nav', 'bench_nav'] : kind === 'ic' ? ['ic', 'rank_ic'] : kind === 'turnover' ? ['turnover'] : Object.keys(series.q_curves || {});
    const source = kind === 'quantile' ? series.q_curves || {} : series;
    for (const key of candidates) if (Array.isArray(source[key])) {
      const name = labels[key] || key.toUpperCase();
      // IC 用柱状图并逐点按正负着色（红正 / 绿负）——比单色线更能一眼看出正负切换
      if (kind === 'ic') traces.push({ name, type: 'bar', barMaxWidth: 8, showSymbol: false, data: (source[key] as unknown[]).map(value => ({ value, itemStyle: { color: typeof value === 'number' && value < 0 ? IC_NEG_COLOR : IC_POS_COLOR } })) });
      else traces.push({ name, type: 'line', data: source[key], showSymbol: false });
    }
  }
  if (!traces.length) return <JsonDetails data={series} label="查看曲线数据" />;
  const percent = kind === 'returns';
  return <div className="chart" aria-label={title}><Suspense fallback={<Skeleton active paragraph={{ rows: 5 }} />}><ReactECharts style={{ height: 320 }} option={{ color: CHART_PALETTE, tooltip: { trigger: 'axis', valueFormatter: (value: unknown) => typeof value === 'number' ? (percent ? `${value.toFixed(2)}%` : number(value)) : String(value) }, legend: { bottom: 0 }, grid: { left: 60, right: 25, top: 30, bottom: 55 }, xAxis: { type: 'category', name: '日期', data: dates }, yAxis: { type: 'value', name: percent ? '累计收益（%）' : kind === 'ic' ? '相关系数' : kind === 'turnover' ? '换手率' : '净值', scale: kind !== 'ic', axisLabel: { formatter: (value: number) => (percent ? `${value}%` : number(value)) } }, series: traces }} /></Suspense></div>;
}
