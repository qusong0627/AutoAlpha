import React from 'react';
import { cn, metricToneClass } from '../features/alpha-research/utils-v2';

/** 原版 PageHeader：渐变图标 + 标题 + 描述 + 右侧 actions */
export function PageHeader(props: {
  title: string;
  desc?: string;
  actions?: React.ReactNode;
  icon?: React.ReactNode;
}) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, margin: '4px 0 16px' }}>
      <div
        style={{
          height: 28,
          width: 28,
          borderRadius: 8,
          background: 'linear-gradient(135deg,#8b5cf6,#d946ef)',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          color: '#fff',
          fontSize: 14,
          fontWeight: 800,
        }}
      >
        {props.icon ?? '✦'}
      </div>
      <div style={{ minWidth: 0 }}>
        <h2
          style={{
            margin: 0,
            fontSize: 16,
            fontWeight: 800,
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {props.title}
        </h2>
        {props.desc && (
          <p style={{ margin: 0, fontSize: 10, fontWeight: 700, color: '#94a3b8' }}>{props.desc}</p>
        )}
      </div>
      <div style={{ marginLeft: 'auto' }}>{props.actions}</div>
    </div>
  );
}

/** 原版 StatCard：glass 白卡 + 大数字 + 副行 */
export function StatCard(props: { label: string; value: string; tone?: string; sub?: string }) {
  return (
    <div
      className="qa-glass"
      style={{ borderRadius: 12, padding: 16, height: 96, overflow: 'hidden' }}
    >
      <div style={{ fontSize: 12, color: '#64748b', fontWeight: 600 }}>{props.label}</div>
      <div
        className={cn('qm-num', props.tone)}
        style={{ fontSize: 22, fontWeight: 800, marginTop: 2 }}
      >
        {props.value}
      </div>
      {props.sub && (
        <div style={{ fontSize: 11, color: '#94a3b8', marginTop: 2 }}>{props.sub}</div>
      )}
    </div>
  );
}

/** 有方向数值（A 股红涨绿跌），缺失显 — */
export function ToneNum(props: { value: number | undefined; decimals?: number; suffix?: string }) {
  const v = props.value;
  if (typeof v !== 'number' || !Number.isFinite(v)) {
    return <span style={{ color: '#98a2b3' }}>—</span>;
  }
  const d = props.decimals ?? 4;
  return (
    <span className={cn('qm-num', metricToneClass(v))}>
      {v.toFixed(d)}
      {props.suffix ?? ''}
    </span>
  );
}

/** 质量徽标（独立色语言：高绿/中 amber/低红） */
export function QualityBadge(props: { quality: string }) {
  const q = props.quality;
  const style: React.CSSProperties =
    q === 'high'
      ? { background: 'rgba(16,185,129,.12)', color: '#059669', border: '1px solid rgba(16,185,129,.3)' }
      : q === 'medium'
        ? { background: 'rgba(245,158,11,.12)', color: '#d97706', border: '1px solid rgba(245,158,11,.3)' }
        : q === 'low'
          ? { background: 'rgba(244,63,94,.12)', color: '#e11d48', border: '1px solid rgba(244,63,94,.3)' }
          : { background: 'rgba(100,116,139,.12)', color: '#64748b', border: '1px solid rgba(100,116,139,.3)' };
  const label = q === 'high' ? '高' : q === 'medium' ? '中' : q === 'low' ? '低' : '未知';
  return (
    <span
      style={{
        ...style,
        display: 'inline-flex',
        borderRadius: 9999,
        padding: '2px 10px',
        fontSize: 11,
        fontWeight: 700,
      }}
    >
      {label}
    </span>
  );
}

/** 任务/回测状态徽标 */
export function StatusBadge(props: { status: string }) {
  const map: Record<string, { label: string; cls: string }> = {
    queued: { label: '排队中', cls: 'bg-slate-100 text-slate-500 border-slate-200' },
    running: { label: '运行中', cls: 'bg-indigo-50 text-indigo-600 border-indigo-200' },
    backtesting: { label: '回测中', cls: 'bg-indigo-50 text-indigo-600 border-indigo-200' },
    completed: { label: '已完成', cls: 'bg-emerald-50 text-emerald-600 border-emerald-200' },
    failed: { label: '失败', cls: 'bg-rose-50 text-rose-600 border-rose-200' },
    cancelled: { label: '已取消', cls: 'bg-amber-50 text-amber-600 border-amber-200' },
  };
  const m = map[props.status] ?? {
    label: props.status || '未知',
    cls: 'bg-slate-100 text-slate-500 border-slate-200',
  };
  return (
    <span
      className={cn('inline-flex rounded-full border px-2.5 py-0.5 text-xs font-semibold', m.cls)}
    >
      {m.label}
    </span>
  );
}

function logLevel(line: string): { color: string; marker: string } {
  const l = line.toLowerCase();
  if (l.includes('error') || l.includes('traceback') || l.includes('failed') || l.includes('失败')) {
    return { color: '#f87171', marker: '●' };
  }
  if (l.includes('warn') || l.includes('警告')) return { color: '#fbbf24', marker: '●' };
  if (l.includes('llm') || l.includes('hypothesis') || l.includes('假设')) {
    return { color: '#c084fc', marker: '◆' };
  }
  if (l.includes('factor') || l.includes('因子')) return { color: '#fbbf24', marker: '◆' };
  if (l.includes('code') || l.includes('exec') || l.includes('代码')) {
    return { color: '#22d3ee', marker: '◆' };
  }
  return { color: '#64748b', marker: '●' };
}

/** 原版深色日志盒：等宽 + level 着色 + 距底跟随 */
export function LogView(props: { lines: string[]; height?: number }) {
  const ref = React.useRef<HTMLDivElement>(null);
  React.useEffect(() => {
    const el = ref.current;
    if (!el) return;
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 100) {
      el.scrollTop = el.scrollHeight;
    }
  }, [props.lines]);
  return (
    <div
      ref={ref}
      style={{
        background: '#020617',
        borderRadius: 12,
        padding: '10px 12px',
        height: props.height ?? 260,
        overflowY: 'auto',
        fontFamily: "'JetBrains Mono','Fira Code',ui-monospace,Menlo,Consolas,monospace",
        fontSize: 11,
        lineHeight: 1.7,
      }}
    >
      {props.lines.length === 0 ? (
        <span style={{ color: '#475569' }}>（暂无日志）</span>
      ) : (
        props.lines.map((line, i) => {
          const lv = logLevel(line);
          return (
            <div key={i} style={{ color: '#cbd5e1', wordBreak: 'break-all' }}>
              <span style={{ color: lv.color, marginRight: 8 }}>{lv.marker}</span>
              {line}
            </div>
          );
        })
      )}
    </div>
  );
}

/** 渐变进度条（blue→indigo） */
export function GradientProgress(props: { percent: number; failed?: boolean }) {
  return (
    <div style={{ height: 6, borderRadius: 9999, background: '#e2e8f0', overflow: 'hidden' }}>
      <div
        style={{
          height: '100%',
          width: `${Math.max(0, Math.min(100, props.percent))}%`,
          borderRadius: 9999,
          background: props.failed
            ? '#f43f5e'
            : 'linear-gradient(90deg,#3b82f6,#6366f1,#a855f7)',
          transition: 'width .4s',
        }}
      />
    </div>
  );
}
