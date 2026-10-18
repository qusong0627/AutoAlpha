import { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Alert, App, Button, Input, Progress, Select, Table } from 'antd';
import { Activity } from 'lucide-react';
import { ALPHA, algorithmName, dateTime, downloadJson, errorText, get, isActive, number, post, universeName, type MiningTask, type Row } from './api';
import { useResource, useStored } from './hooks';
import { DRAFT_KEY, initialDraft } from './MiningForm';
import { useWorkspace } from './WorkspaceContext';
import { CopyableBlock, EmptyState, JsonDetails, LoadState, Page, Panel, Refresh, Status } from './ui';

const PHASE_LABELS: Record<string, string> = { hypothesis: '假设', experiment: '实验', coding: '编码', running: '回测', feedback: '反馈' };
const durationText = (seconds?: number | null) => { if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return ''; if (seconds < 1) return '<1s'; if (seconds < 60) return `${seconds.toFixed(1)}s`; const m = Math.floor(seconds / 60); const s = String(Math.round(seconds % 60)).padStart(2, '0'); return `${m}m${s}s`; };

export default function TasksPage() {
  const navigate = useNavigate(); const [params, setParams] = useSearchParams(); const { message } = App.useApp(); const overview = useWorkspace();
  const taskId = params.get('taskId') || '';
  const [status, setStatus] = useStored<string | undefined>('autoalpha.tasks-filter', undefined); const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false); const [showTrajectory, setShowTrajectory] = useState(false);
  const history = useResource(signal => get<{ tasks: MiningTask[]; total: number }>(`${ALPHA}/tasks/history`, { status, limit: 20, offset: (page - 1) * 20 }, signal), [status, page], 10000);
  const detail = useResource(signal => get<Row>(`${ALPHA}/tasks/${encodeURIComponent(taskId)}`, undefined, signal), [taskId], 5000, Boolean(taskId));
  const persisted = history.data?.tasks.find(task => task.task_id === taskId) || overview.tasks.data?.tasks.find(task => task.task_id === taskId);
  const task: Row | undefined = detail.data?.task || detail.data || persisted;
  const effective = task?.task_id ? task : persisted;
  const logs = useResource(signal => get<{ lines: string[]; total: number }>(`${ALPHA}/tasks/${encodeURIComponent(taskId)}/log`, { tail: 300 }, signal), [taskId], effective && isActive(effective.status) ? 5000 : 0, Boolean(taskId));
  const trajectory = useResource(signal => get<Row>(`${ALPHA}/tasks/${encodeURIComponent(taskId)}/trajectory`, undefined, signal), [taskId], 0, Boolean(taskId) && showTrajectory);
  const rerun = (task: MiningTask) => {
    const config = task.params_snapshot || task;
    const draft = { ...initialDraft(), algorithm: task.algorithm || 'rdagent', algorithm_preference_saved: true, datasets_preference_saved: true, universe: task.universe || 'csi300', direction: task.direction || '', loop_n: config.loop_n || 5, freq: config.freq || 'day', mining_start: config.mining_start || '', mining_end: config.mining_end || '', valid_start: config.valid_start || '', valid_end: config.valid_end || '', validation: Boolean(config.valid_start), datasets: config.datasets || [], quality_gate_mode: config.quality_gate_mode || 'soft' };
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft)); navigate('/');
  };
  const cancel = async () => { setBusy(true); try { await post(`${ALPHA}/tasks/${taskId}/cancel`); message.success('已请求取消任务'); detail.refresh(); history.refresh(); overview.tasks.refresh(); } catch (e) { message.error(errorText(e)); } finally { setBusy(false); } };
  return <Page eyebrow={<><Activity size={14} />实验追踪 · 进度 · 日志 · 决策轨迹</>} title="任务中心" description="查看挖掘历史、运行进度、日志与决策轨迹。" actions={<><Refresh onClick={history.refresh} /><Button type="primary" onClick={() => navigate('/')}>新建挖掘</Button></>}>
    <Panel title="全部任务"><div className="filter-bar"><Select aria-label="任务状态" allowClear placeholder="全部状态" value={status} onChange={value => { setStatus(value); setPage(1); }} options={['queued', 'running', 'completed', 'failed', 'cancelled'].map(value => ({ value, label: { queued: '排队中', running: '运行中', completed: '已完成', failed: '失败', cancelled: '已取消' }[value] }))} /></div>
      <LoadState resource={history}><Table rowKey="task_id" dataSource={history.data?.tasks} scroll={{ x: 750 }} pagination={{ current: page, pageSize: 20, total: history.data?.total, onChange: setPage, showSizeChanger: false }} columns={[
        { title: '任务', render: (_, row) => <div><div className="item-title">{algorithmName(row.algorithm)}</div><div className="item-id">{row.task_id}</div><div className="item-description">{row.direction || '自动检验量价候选'}</div></div> },
        { title: '股票池', render: (_, row) => universeName(row.universe) }, { title: '状态', render: (_, row) => <Status value={row.status} /> }, { title: '产出', render: (_, row) => number(row.factor_count, 0) }, { title: '创建时间', render: (_, row) => dateTime(row.created_at) },
        { title: '操作', render: (_, row) => <div className="actions"><Button type="link" onClick={() => { setShowTrajectory(false); setParams({ taskId: row.task_id }); }}>查看详情</Button><Button type="link" onClick={() => rerun(row)}>按此配置重跑</Button></div> },
      ]} /></LoadState>
    </Panel>
    {taskId ? <Panel title="任务详情" extra={<><span className="item-id">{taskId}</span><Button size="small" onClick={() => setParams({})}>关闭详情</Button></>}>
      {detail.error && !persisted && <Alert type="error" message={detail.error} action={<Button onClick={detail.refresh}>重试</Button>} />}
      {effective && <><div className="actions"><Status value={effective.status} /><span>{algorithmName(effective.algorithm)} · {universeName(effective.universe)}</span>{isActive(effective.status) && <Button danger loading={busy} onClick={cancel}>取消任务</Button>}<Button onClick={() => navigate(`/factors?taskId=${taskId}`)}>查看产出因子</Button><Button onClick={() => downloadJson(effective, `task-${taskId}.json`)}>下载任务记录</Button></div>
        <div className="task-progress"><Progress percent={effective.status === 'completed' ? 100 : Math.min(100, effective.progress_pct || 0)} status={effective.status === 'failed' ? 'exception' : undefined} strokeColor={{ '0%': '#4f6cf5', '100%': '#a07ae8' }} /></div>
        {(effective.error || effective.error_message) && <Alert type="error" showIcon message={effective.error || effective.error_message} />}
        {effective.progress && <p className="subtle">{typeof effective.progress === 'string' ? effective.progress : ''}</p>}
        {detail.error && persisted && <p className="subtle">当前显示数据库保存的任务记录。</p>}
        <JsonDetails data={effective.params_snapshot || effective} label="查看实验参数与数据版本" />
      </>}
      <div className="section-heading"><h2>任务日志</h2></div><LoadState resource={logs}><CopyableBlock className="log-box" text={logs.data?.lines?.join('\n') || '暂无日志记录'} /></LoadState>
      <div className="actions" style={{ marginTop: 18 }}><Button onClick={() => setShowTrajectory(value => !value)}>{showTrajectory ? '收起决策轨迹' : '查看决策轨迹'}</Button></div>
      {showTrajectory && <LoadState resource={trajectory}>{trajectory.data && <>
        <p className="subtle">每行 = 一轮演化（Loop_N），每列 = 一个环节（假设 → 实验 → 编码 → 回测 → 反馈）；格子里是「该环节是否跑到、是否完成」的只读事实（后端字段 phase）。</p>
        {(trajectory.data.loops || []).map((loop: Row, i: number) => <div key={i} className="trajectory-loop">
          <div className="stage-list">{(loop.nodes || []).map((node: Row, j: number) => { const name = PHASE_LABELS[node.phase] || node.phase || '环节'; const dur = durationText(node.duration_s); const detail = `第 ${loop.loop ?? i} 轮 · ${name} · ${node.status}${dur ? ` · 耗时 ${dur}` : ''}${node.pkl_count ? ` · ${node.pkl_count} 个产物` : ''}`; return <div className="stage-item" key={j} title={detail}><span className="stage-name">{name}</span><Status value={node.status} />{dur && <span className="stage-time">{dur}</span>}</div>; })}</div>
          {loop.flag_reason && <div className="stage-flag">本轮标记 · {loop.flag_reason}</div>}
        </div>)}
        <JsonDetails data={trajectory.data} label="查看完整轨迹" />
      </>}</LoadState>}
    </Panel> : <Panel><EmptyState title="选择一个任务查看详情" description="日志和决策轨迹与任务编号关联；历史任务在服务重启后仍可追溯。" /></Panel>}
  </Page>;
}
