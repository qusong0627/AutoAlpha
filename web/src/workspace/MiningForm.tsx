import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, App, Button, Checkbox, Input, InputNumber, Modal, Select, Switch } from 'antd';
import { Send, Zap } from 'lucide-react';
import { ALPHA, algorithmName, errorText, get, post, request, universeName, type Algorithm, type MiningOptions, type Row } from './api';
import { useResource } from './hooks';
import { Panel } from './ui';
import { useWorkspace } from './WorkspaceContext';

export interface MiningDraft { algorithm: string; algorithm_preference_saved?: boolean; datasets_preference_saved?: boolean; universe: string; direction: string; freq: string; loop_n: number; mining_start: string; mining_end: string; valid_start: string; valid_end: string; datasets: string[]; validation: boolean; quality_gate_mode: string; data_source?: string; }
export const DRAFT_KEY = 'autoalpha.mining-draft';
const CONFIG_KEY = 'autoalpha.mining-config';
export const initialDraft = (): MiningDraft => ({ algorithm: 'rdagent', universe: localStorage.getItem('qm.preset.universe') || 'csi500', direction: '', freq: 'day', loop_n: 5, mining_start: '', mining_end: '', valid_start: '', valid_end: '', datasets: [], validation: true, quality_gate_mode: 'soft', data_source: 'parquet' });

const MARKETS = [
  { key: 'a_share', label: 'A股', enabled: true },
  { key: 'futures', label: '期货', enabled: false },
  { key: 'hong_kong', label: '港股', enabled: false },
  { key: 'us_stock', label: '美股', enabled: false },
] as const;

/** 方向配额读不到时的兜底文案（只在接口失败/空池时用，正常走后端打分表）。 */
const FALLBACK_DIRECTIONS = [
  '挖掘基于 5 日动量反转与成交量偏度组合的超额收益因子',
  '构建捕捉日内高频波动率非对称性与价格跳跃的量价特征',
  '寻找尾盘主力资金净流入与换手率背离的Alpha选股因子',
  '基于多周期均线发散度与流动性溢价挖掘中短线稳健因子',
];

interface DirectionScore { direction: string; n_tasks: number; n_success: number; p: number; saturated: boolean; }

/**
 * 推荐方向 chips：优先用后端方向配额（GET /alpha-agent/direction-quota）——与派发侧
 * 同源的经验贝叶斯成功率 + 饱和惩罚；接口失败或空池时退回静态文案。
 * 按空白归一化去重：`挖掘基于5日…` 与 `挖掘基于 5 日…` 在后端是两条历史，
 * 并排成两个 chip 只会看起来像重复项（保留分数更高的那条，即排序后的第一条）。
 */
function buildSuggestions(scored: DirectionScore[]) {
  if (!scored.length) {
    return FALLBACK_DIRECTIONS.map(text => ({ key: text, text, short: text, badge: '', weak: false, saturated: false, hint: text }));
  }
  const seen = new Set<string>();
  return scored
    .filter(item => {
      const normal = (item.direction || '').replace(/\s+/g, '');
      if (!normal || seen.has(normal)) return false;
      seen.add(normal);
      return true;
    })
    .slice(0, 5)
    .map(item => ({
      key: item.direction,
      text: item.direction,
      short: item.direction.length > 20 ? `${item.direction.slice(0, 20)}…` : item.direction,
      badge: item.n_tasks ? `${Math.round(item.p * 100)}%` : '未试',
      weak: item.p < 0.3,
      saturated: item.saturated,
      hint: [
        item.direction,
        '',
        `历史 ${item.n_tasks} 次 · 成功 ${item.n_success} 次 · 预估成功率 ${(item.p * 100).toFixed(0)}%`,
        item.saturated ? '近 90 天重复命中已饱和，建议另换方向' : '',
      ].filter(Boolean).join('\n'),
    }));
}

function readDraft(): MiningDraft {
  try {
    const stored = sessionStorage.getItem(DRAFT_KEY) || localStorage.getItem(CONFIG_KEY);
    if (!stored) return initialDraft();
    const saved = JSON.parse(stored);
    return { ...initialDraft(), ...saved, algorithm: saved.algorithm_preference_saved ? saved.algorithm : 'rdagent', datasets: saved.datasets_preference_saved ? saved.datasets || [] : [], data_source: 'parquet' };
  } catch { return initialDraft(); }
}

export default function MiningForm({ full = false, configOnly = false, onDraftChange }: { full?: boolean; configOnly?: boolean; onDraftChange?: (draft: MiningDraft) => void }) {
  const navigate = useNavigate(); const { message } = App.useApp(); const overview = useWorkspace();
  const [draft, setDraft] = useState<MiningDraft>(readDraft);
  const patch = (value: Partial<MiningDraft>) => setDraft(previous => ({ ...previous, ...value }));
  useEffect(() => {
    if (!configOnly) sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft));
    onDraftChange?.(draft);
  }, [draft, configOnly, onDraftChange]);
  const algorithms = useResource(signal => get<{ algorithms: Algorithm[] }>(`${ALPHA}/algorithms`, undefined, signal));
  // 推荐方向数据源：只读投影，失败不 5xx 也不报错上屏（available=false），前端退回静态文案。
  const directionQuota = useResource(signal => get<{ available: boolean; directions: DirectionScore[] }>(`${ALPHA}/direction-quota`, { market: 'a_share', n: 5 }, signal), [], 0, !configOnly);
  const live = Boolean(directionQuota.data?.available && directionQuota.data.directions?.length);
  const suggestions = buildSuggestions(live ? directionQuota.data!.directions : []);
  const algorithm = algorithms.data?.algorithms.find(item => item.key === draft.algorithm) || algorithms.data?.algorithms[0];
  const options = useResource(signal => request<MiningOptions>(`${ALPHA}/mining/config-options`, { params: { freq: draft.freq }, signal, timeout: 60000 }), [draft.freq]);
  const [preview, setPreview] = useState<{ config: Row; result: MiningOptions }>();
  const [loading, setLoading] = useState(false); const [submitting, setSubmitting] = useState(false); const [error, setError] = useState('');
  useEffect(() => {
    if (!full || !options.data) return;
    const defaults = options.data.defaults;
    const availableDatasets = options.data.datasets.map(item => item.key);
    setDraft(previous => ({
      ...previous,
      mining_start: previous.mining_start || defaults?.mining?.start || '',
      mining_end: previous.mining_end || defaults?.mining?.end || '',
      valid_start: previous.valid_start || defaults?.validation?.start || '',
      valid_end: previous.valid_end || defaults?.validation?.end || '',
      datasets: previous.datasets_preference_saved ? previous.datasets.filter(key => availableDatasets.includes(key)) : availableDatasets,
      validation: previous.mining_start || previous.mining_end ? previous.validation : Boolean(defaults?.validation),
    }));
  }, [options.data, full]);
  const usesLlm = algorithm?.uses_llm ?? draft.algorithm === 'rdagent';
  const availableAlgorithms = algorithms.data?.algorithms.filter(item => item.enabled && item.valid && item.markets.includes('a_share')).sort((a, b) => Number(a.uses_llm) - Number(b.uses_llm)) || [];
  const universes = overview.summary.data?.universes || {};
  const dataSource = 'parquet';
  const makeConfig = (): Row => {
    if (!algorithm) throw new Error('算法目录尚未加载，请稍后重试');
    if (!algorithm.freqs.includes(draft.freq)) throw new Error('所选算法不支持当前数据频率');
    if (usesLlm && !draft.direction.trim()) throw new Error('请描述要探索的因子方向');
    for (const [start, end, label] of [[draft.mining_start, draft.mining_end, '挖掘区间'], [draft.validation ? draft.valid_start : '', draft.validation ? draft.valid_end : '', '验证区间']]) {
      if (Boolean(start) !== Boolean(end)) throw new Error(`${label}的开始与结束日期需要同时填写`);
      if (start && start > end) throw new Error(`${label}的结束日期不能早于开始日期`);
    }
    const config: Row = { algorithm: algorithm.key, market: 'a_share', universe: draft.universe, freq: draft.freq, data_source: dataSource, quality_gate_mode: draft.quality_gate_mode, ...(usesLlm ? { direction: draft.direction.trim(), loop_n: draft.loop_n } : {}) };
    if (draft.mining_start) Object.assign(config, { mining_start: draft.mining_start, mining_end: draft.mining_end });
    if (draft.validation && draft.valid_start) Object.assign(config, { valid_start: draft.valid_start, valid_end: draft.valid_end });
    if (algorithm.uses_datasets && draft.datasets.length) config.datasets = draft.datasets;
    return config;
  };
  const previewTask = async () => {
    setError(''); setLoading(true);
    try {
      const config = makeConfig();
      const result = await request<MiningOptions>(`${ALPHA}/mining/config-options`, { params: { freq: config.freq, mining_start: config.mining_start, mining_end: config.mining_end, valid_start: config.valid_start, valid_end: config.valid_end }, timeout: 60000 });
      if (draft.freq === '1min' && !result.minute?.ready) throw new Error(result.minute?.error || '分钟数据尚未就绪');
      setPreview({ config, result });
    } catch (e) { const text = errorText(e); setError(text); if (!full) message.error(text); } finally { setLoading(false); }
  };
  const submit = async () => {
    if (!preview || submitting) return;
    setSubmitting(true);
    try {
      const result = await post<Row>(`${ALPHA}/evolve`, preview.config);
      const id = result.task_id || result.task_ids?.[0] || result.task?.task_id;
      if (!id) throw new Error('服务未返回任务编号，请刷新任务中心核对');
      message.success('挖掘任务已提交'); overview.refresh(); directionQuota.refresh(); setPreview(undefined); navigate(`/tasks?taskId=${encodeURIComponent(id)}`);
    } catch (e) { message.error(errorText(e)); } finally { setSubmitting(false); }
  };
  const saveConfig = () => {
    const config = { ...draft, algorithm_preference_saved: true, datasets_preference_saved: true, data_source: 'parquet' };
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(config));
    localStorage.setItem(CONFIG_KEY, JSON.stringify({ ...config, direction: '' }));
    message.success('挖掘参数已保存，首页新任务将使用这组配置');
  };
  const universeOptions = Object.keys(universes).length
    ? Object.entries(universes).map(([id, u]) => ({ value: id, label: `${universeName(id)}(${u.count})` }))
    : [
        { value: 'csi300', label: '沪深300(300)' },
        { value: 'csi500', label: '中证500(500)' },
        { value: 'csi1000', label: '中证1000(1000)' },
        { value: 'sse50', label: '上证50(50)' },
      ];
  return <>
    {!configOnly && <section className="composer-shell" aria-label="创建挖掘任务">
      <div className="composer-card">
        <div className="compose-toolbar">
          <span className="compose-group">
            <span className="compose-label">市场</span>
            {MARKETS.map(m => (
              <button
                key={m.key}
                type="button"
                disabled={!m.enabled}
                className={`compose-pill ${m.enabled ? 'active' : 'disabled'}`}
                title={m.enabled ? `${m.label}因子挖掘` : '暂只支持 A 股'}
              >
                <i className={`pill-dot ${m.enabled ? 'ready' : ''}`} />
                {m.label}
              </button>
            ))}
          </span>

          <span className="compose-divider" aria-hidden="true" />

          <span className="compose-group">
            <span className="compose-label">池</span>
            <select
              aria-label="股票池"
              className="compose-select"
              value={draft.universe}
              onChange={e => patch({ universe: e.target.value })}
            >
              {universeOptions.map(opt => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
              ))}
            </select>
          </span>

          {full && availableAlgorithms.length > 1 && (
            <>
              <span className="compose-divider" aria-hidden="true" />
              <span className="compose-group">
                <span className="compose-label">算法</span>
                {availableAlgorithms.map(item => (
                  <button
                    key={item.key}
                    type="button"
                    onClick={() => patch({ algorithm: item.key, freq: item.freqs.includes(draft.freq) ? draft.freq : 'day' })}
                    className={`compose-pill ${draft.algorithm === item.key ? 'active' : ''}`}
                  >
                    {item.uses_llm ? 'AI 演化' : '统计量价'}
                  </button>
                ))}
              </span>
            </>
          )}
        </div>

        {algorithms.error && <Alert type="error" showIcon message={algorithms.error} action={<Button onClick={algorithms.refresh}>重试</Button>} />}

        <div className="compose-body">
          <Input.TextArea
            aria-label="挖掘方向"
            className="compose-input"
            value={draft.direction}
            maxLength={8000}
            autoSize={{ minRows: 3, maxRows: 6 }}
            onChange={e => patch({ direction: e.target.value, ...(draft.algorithm === 'statistical' && e.target.value.trim() ? { algorithm: 'rdagent' } : {}) })}
            placeholder="描述因子挖掘需求 (如：挖掘基于5日动量反转与成交量偏度组合的Alpha因子)，按 Enter 发送"
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); void previewTask(); } }}
          />
          <button
            type="button"
            className="compose-send"
            disabled={loading || (full && !algorithm)}
            onClick={() => void previewTask()}
            title="发送 (Enter)"
            aria-label={full ? '预览挖掘任务' : '创建挖掘任务'}
          >
            <Send size={17} />
          </button>
        </div>
      </div>
    </section>}

    {!configOnly && <div className="suggestions">
      <span className="suggestions-label"><Zap size={13} /> 推荐方向{live && <em>按历史成功率</em>}</span>
      {suggestions.map(item => (
        <button
          key={item.key}
          type="button"
          className={`suggest-chip${item.saturated ? ' saturated' : ''}`}
          title={item.hint}
          onClick={() => patch({ direction: item.text, algorithm: 'rdagent' })}
        >
          <span className="suggest-text">{item.short}</span>
          {item.badge && <span className={`suggest-score${item.weak ? ' weak' : ''}`}>{item.badge}</span>}
        </button>
      ))}
    </div>}

    {full && <Panel title={configOnly ? '首页任务默认参数' : '实验参数'} className={configOnly ? 'mining-config-panel' : ''} extra={<span className="subtle">默认窗口由后端按实际数据覆盖生成</span>}>
      {options.loading && <p className="subtle">正在读取可用窗口与数据集…</p>}
      {options.error && <Alert type="warning" showIcon message={options.error} action={<Button onClick={options.refresh}>重试</Button>} />}
      <div className="form-grid">
        {configOnly && <>
          <div className="form-field"><label htmlFor="config-algorithm">挖掘算法</label><Select id="config-algorithm" value={draft.algorithm} options={availableAlgorithms.map(item => ({ value: item.key, label: item.uses_llm ? 'RD-Agent 因子演化' : '统计量价挖掘' }))} onChange={algorithm => patch({ algorithm, algorithm_preference_saved: true, freq: algorithms.data?.algorithms.find(item => item.key === algorithm)?.freqs.includes(draft.freq) ? draft.freq : 'day' })} /></div>
          <div className="form-field"><label htmlFor="config-universe">股票池</label><Select id="config-universe" value={draft.universe} options={universeOptions} onChange={universe => patch({ universe })} /></div>
        </>}
        <div className="form-field"><label htmlFor="mining-frequency">数据频率</label><Select id="mining-frequency" value={draft.freq} options={(algorithm?.freqs || ['day']).map(value => ({ value, label: value === 'day' ? '日频' : '1 分钟' }))} onChange={freq => patch({ freq, mining_start: '', mining_end: '', valid_start: '', valid_end: '' })} /></div>
        <div className="form-field"><label htmlFor="mining-gate">质量门禁</label><Select id="mining-gate" value={draft.quality_gate_mode} onChange={quality_gate_mode => patch({ quality_gate_mode })} options={[{ value: 'soft', label: '软门禁 · 记录告警' }, { value: 'hard', label: '硬门禁 · 拒绝不合格项' }, { value: 'off', label: '关闭门禁' }]} /></div>
        <div className="form-field"><label htmlFor="mining-start">挖掘开始日期</label><Input id="mining-start" type="date" value={draft.mining_start} min={options.data?.coverage?.start} max={options.data?.coverage?.end} onChange={e => patch({ mining_start: e.target.value })} /></div>
        <div className="form-field"><label htmlFor="mining-end">挖掘结束日期</label><Input id="mining-end" type="date" value={draft.mining_end} min={draft.mining_start} max={options.data?.coverage?.end} onChange={e => patch({ mining_end: e.target.value })} /></div>
        {draft.validation && <><div className="form-field"><label htmlFor="validation-start">验证开始日期</label><Input id="validation-start" type="date" value={draft.valid_start} onChange={e => patch({ valid_start: e.target.value })} /></div><div className="form-field"><label htmlFor="validation-end">验证结束日期</label><Input id="validation-end" type="date" value={draft.valid_end} min={draft.valid_start} max={options.data?.coverage?.end} onChange={e => patch({ valid_end: e.target.value })} /></div></>}
        {usesLlm && <div className="form-field"><label htmlFor="mining-loops">演化轮数</label><InputNumber id="mining-loops" min={1} max={20} value={draft.loop_n} onChange={value => patch({ loop_n: value || 1 })} /></div>}
        {algorithm?.uses_datasets && draft.freq !== '1min' && <div className="form-field wide"><label>参与挖掘的数据集</label><Checkbox.Group value={draft.datasets} onChange={datasets => patch({ datasets: datasets.map(String), datasets_preference_saved: true })} options={options.data?.datasets.map(item => ({ value: item.key, label: item.label }))} /><span className="helper">默认全选可用数据集；可按需取消。日频、财务和分钟数据会按因子需要通过 QuantDB 按需读取；财务字段按公告日对齐，分钟线仅支持限定股票读取。</span></div>}
        <div className="form-field verification-toggle wide"><label>样本外验证</label><Switch aria-label="启用样本外验证" checked={draft.validation} onChange={validation => patch({ validation })} /></div>
      </div>
      {options.data?.coverage && <p className="subtle">实际数据覆盖 {options.data.coverage.start} — {options.data.coverage.end}</p>}
      {error && <Alert type="error" showIcon message={error} />}
      <div className="actions">{configOnly ? <Button type="primary" disabled={!algorithm} onClick={saveConfig}>保存参数</Button> : <Button type="primary" loading={loading} disabled={!algorithm} onClick={previewTask}>预览并提交</Button>}</div>
    </Panel>}
    {!configOnly && <Modal title="核对挖掘任务" open={Boolean(preview)} onCancel={() => !submitting && setPreview(undefined)} onOk={submit} confirmLoading={submitting} okText="开始挖掘" cancelText="返回修改" width={650}>
      {preview && <><p>{algorithmName(draft.algorithm)} · {universeName(draft.universe)}</p>{usesLlm && <p className="helper">{preview.config.direction}</p>}<div className="metric-grid">{Object.entries(preview.result.segments || {}).map(([key, span]) => <div className="metric" key={key}><span>{{ train: '训练区间', valid: '验证区间', test: '测试区间' }[key] || key}</span><p>{span.start}<br />{span.end}</p></div>)}</div><p className="helper">{usesLlm ? `将执行 ${draft.loop_n} 轮演化，并调用已配置的 LLM 服务。` : '将运行统计检验与筛选，不调用 LLM。'}</p></>}
    </Modal>}
  </>;
}
