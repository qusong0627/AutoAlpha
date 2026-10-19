import { useEffect, useRef, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Alert, App, Button, Checkbox, Input, Popconfirm, Progress, Select, Table } from 'antd';
import { Activity, Trash2 } from 'lucide-react';
import { ALPHA, algorithmName, dateTime, del, downloadJson, errorText, get, getLatestTaskLog, isActive, number, post, universeName, type MiningTask, type Row } from './api';
import { useResource, useStored } from './hooks';
import { DRAFT_KEY, initialDraft } from './MiningForm';
import { useWorkspace } from './WorkspaceContext';
import { CopyableBlock, EmptyState, JsonDetails, LoadState, Page, Panel, Refresh, Status } from './ui';

const PHASE_LABELS: Record<string, string> = { hypothesis: '假设', experiment: '实验', coding: '编码', running: '回测', feedback: '反馈' };
const durationText = (seconds?: number | null) => { if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return ''; if (seconds < 1) return '<1s'; if (seconds < 60) return `${seconds.toFixed(1)}s`; const m = Math.floor(seconds / 60); const s = String(Math.round(seconds % 60)).padStart(2, '0'); return `${m}m${s}s`; };
const taskErrorMessage = (value: unknown) => {
  const detail = String(value ?? '').trim();
  if (!detail || ['none', 'null', 'undefined'].includes(detail.toLowerCase())) return '';
  if (/Task owner lease expired|Mining task ownership lease was lost/i.test(detail)) return '任务运行状态长时间未更新，已自动停止。请重新发起任务。';
  return /^Process exited with code \d+:/i.test(detail)
    ? '任务运行过程中遇到问题，暂时未能完成。请查看下方“任务日志”了解详情，调整设置后重试。'
    : detail;
};

export default function TasksPage() {
  const navigate = useNavigate(); const [params, setParams] = useSearchParams(); const { message } = App.useApp(); const overview = useWorkspace();
  const taskId = params.get('taskId') || '';
  const [status, setStatus] = useStored<string | undefined>('autoalpha.tasks-filter', undefined); const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false); const [showTrajectory, setShowTrajectory] = useState(false);
  const [deletingTaskId, setDeletingTaskId] = useState('');
  const [followLogs, setFollowLogs] = useState(true);
  const logContainer = useRef<HTMLDivElement>(null);
  const history = useResource(signal => get<{ tasks: MiningTask[]; total: number }>(`${ALPHA}/tasks/history`, { status, limit: 5, offset: (page - 1) * 5 }, signal), [status, page], 10000);
  const detail = useResource(signal => get<Row>(`${ALPHA}/tasks/${encodeURIComponent(taskId)}`, undefined, signal), [taskId], 5000, Boolean(taskId));
  const persisted = history.data?.tasks.find(task => task.task_id === taskId) || overview.tasks.data?.tasks.find(task => task.task_id === taskId);
  const detailTask: Row | undefined = detail.data?.task || detail.data;
  const task = detailTask?.task_id === taskId ? detailTask : undefined;
  const effective = task && persisted && isActive(task.status) && ['completed', 'failed', 'cancelled'].includes(persisted.status) ? { ...task, ...persisted } : task || persisted;
  const logs = useResource(signal => getLatestTaskLog(taskId, signal), [taskId], effective && isActive(effective.status) ? 5000 : 0, Boolean(taskId));
  useEffect(() => { setFollowLogs(true); }, [taskId]);
  useEffect(() => {
    const box = logContainer.current?.querySelector<HTMLDivElement>('.log-box');
    if (box && followLogs) box.scrollTop = box.scrollHeight;
  }, [logs.data, followLogs]);
  const taskError = [effective?.error, effective?.error_message].map(taskErrorMessage).find(Boolean);
  const logPending = Boolean(effective && isActive(effective.status) && /log not found/i.test(logs.error || ''));
  const logUnavailable = Boolean(effective && !isActive(effective.status) && /log not found/i.test(logs.error || ''));
  const taskRows = history.data?.tasks.map(row => row.task_id === taskId && effective && !isActive(effective.status) ? { ...row, ...effective } : row);
  const trajectory = useResource(signal => get<Row>(`${ALPHA}/tasks/${encodeURIComponent(taskId)}/trajectory`, undefined, signal), [taskId], 0, Boolean(taskId) && showTrajectory);
  const rerun = (task: MiningTask) => {
    const config = task.params_snapshot || task;
    const draft = { ...initialDraft(), algorithm: task.algorithm || 'rdagent', algorithm_preference_saved: true, datasets_preference_saved: true, universe: task.universe || 'csi500', direction: task.direction || '', loop_n: config.loop_n || 5, freq: config.freq || 'day', mining_start: config.mining_start || '', mining_end: config.mining_end || '', valid_start: config.valid_start || '', valid_end: config.valid_end || '', validation: Boolean(config.valid_start), datasets: config.datasets || [], quality_gate_mode: config.quality_gate_mode || 'soft' };
    sessionStorage.setItem(DRAFT_KEY, JSON.stringify(draft)); navigate('/');
  };
  const cancel = async () => { setBusy(true); try { await post(`${ALPHA}/tasks/${taskId}/cancel`); message.success('已请求取消任务'); detail.refresh(); history.refresh(); overview.tasks.refresh(); } catch (e) { message.error(errorText(e)); } finally { setBusy(false); } };
  const deleteTask = async (row: MiningTask) => { setDeletingTaskId(row.task_id); try { const result = await del<{ local_files_deleted: boolean }>(`${ALPHA}/tasks/${encodeURIComponent(row.task_id)}`); message.success(result.local_files_deleted ? '任务记录和本地运行文件已删除，产出因子保留' : '任务记录已删除，本地文件清理待处理'); if (taskId === row.task_id) setParams({}); if (history.data?.tasks?.length === 1 && page > 1) setPage(page - 1); else history.refresh(); overview.tasks.refresh(); } catch (e) { message.error(errorText(e)); } finally { setDeletingTaskId(''); } };
  return <Page eyebrow={<><Activity size={14} />实验追踪 · 进度 · 日志 · 决策轨迹</>} title="任务中心" description="查看挖掘历史、运行进度、日志与决策轨迹。" actions={<><Refresh onClick={history.refresh} /><Button type="primary" onClick={() => navigate('/')}>新建挖掘</Button></>}>
    <Panel title="全部任务"><div className="filter-bar"><Select aria-label="任务状态" allowClear placeholder="全部状态" value={status} onChange={value => { setStatus(value); setPage(1); }} options={['queued', 'running', 'completed', 'failed', 'cancelled'].map(value => ({ value, label: { queued: '排队中', running: '运行中', completed: '已完成', failed: '失败', cancelled: '已取消' }[value] }))} /></div>
      <LoadState resource={history}><Table rowKey="task_id" dataSource={taskRows} scroll={{ x: 750 }} pagination={{ current: page, pageSize: 5, total: history.data?.total, onChange: setPage, showSizeChanger: false }} columns={[
        { title: '任务', render: (_, row) => <div><div className="item-title">{algorithmName(row.algorithm)}</div><div className="item-id">{row.task_id}</div><div className="item-description">{row.direction || '自动检验量价候选'}</div></div> },
        { title: '股票池', render: (_, row) => universeName(row.universe) }, { title: '状态', render: (_, row) => <Status value={row.status} /> }, { title: '产出', render: (_, row) => number(row.factor_count, 0) }, { title: '创建时间', render: (_, row) => dateTime(row.created_at) },
        { title: '操作', width: 260, render: (_, row) => <div className="actions"><Button type="link" onClick={() => { setShowTrajectory(false); setParams({ taskId: row.task_id }); }}>查看详情</Button><Button type="link" onClick={() => rerun(row)}>按此配置重跑</Button><Popconfirm title="永久删除此任务？" description="将删除任务记录和本地日志/工作区；已生成并保存在因子库中的因子会保留。运行中任务不能删除。" okText="删除" cancelText="取消" okButtonProps={{ danger: true }} onConfirm={() => deleteTask(row)}><Button type="link" danger loading={deletingTaskId === row.task_id} disabled={isActive(row.status)} icon={<Trash2 size={14} />}>删除</Button></Popconfirm></div> },
      ]} /></LoadState>
    </Panel>
    {taskId ? <Panel title="任务详情" extra={<><span className="item-id">{taskId}</span><Button size="small" onClick={() => setParams({})}>关闭详情</Button></>}>
      {detail.error && !persisted && <Alert type="error" message={detail.error} action={<Button onClick={detail.refresh}>重试</Button>} />}
      {effective && <><div className="actions"><Status value={effective.status} /><span>{algorithmName(effective.algorithm)} · {universeName(effective.universe)}</span>{isActive(effective.status) && <Button danger loading={busy} onClick={cancel}>取消任务</Button>}<Button onClick={() => navigate(`/factors?taskId=${taskId}`)}>查看产出因子</Button><Button onClick={() => downloadJson(effective, `task-${taskId}.json`)}>下载任务记录</Button></div>
        <div className="task-progress"><Progress percent={effective.status === 'completed' ? 100 : Math.min(100, effective.progress_pct || 0)} status={effective.status === 'failed' ? 'exception' : undefined} strokeColor={{ '0%': '#4f6cf5', '100%': '#a07ae8' }} /></div>
        {effective.status === 'failed' && <Alert type="error" showIcon message={taskError || '任务未能完成，请查看任务日志了解原因后重试。'} />}
        {isActive(effective.status) && effective.progress && <p className="subtle">{typeof effective.progress === 'string' ? effective.progress : ''}</p>}
        {detail.error && persisted && <p className="subtle">当前显示数据库保存的任务记录。</p>}
        <JsonDetails data={effective.params_snapshot || effective} label="查看实验参数与数据版本" />
      </>}
      <div className="section-heading"><h2>任务日志</h2><div className="actions"><Checkbox checked={followLogs} onChange={event => setFollowLogs(event.target.checked)}>跟随最新</Checkbox><Button size="small" onClick={logs.refresh}>刷新日志</Button></div></div>
      {logs.data && <p className="subtle">{isActive(effective?.status) ? '每5秒自动更新 · ' : ''}显示第 {logs.data.lines.length ? logs.data.offset + 1 : 0}–{logs.data.offset + logs.data.lines.length} 行 / 共 {logs.data.total} 行 · 最近读取 {new Date(logs.data.fetchedAt).toLocaleTimeString('zh-CN', { hour12: false })}</p>}
      <div ref={logContainer}>{logPending ? <Alert type="info" showIcon message="任务启动中，日志准备中…" description="日志生成后会自动显示，请稍候。" /> : logUnavailable ? <Alert type="info" showIcon message="任务已结束，未生成运行日志" description="请查看上方任务状态和失败原因。" /> : <LoadState resource={logs}><CopyableBlock className="log-box" text={logs.data?.lines?.join('\n').replace(/\u001b\[[0-9;]*m/g, '') || '暂无日志记录'} /></LoadState>}</div>
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
