import axios, { type AxiosRequestConfig } from 'axios';
import { authService } from '../features/auth/services/authService';

export type Row = Record<string, any>;
export const ALPHA = '/alpha-agent';
export const TRAINING = '/admin/training-data';
const client = axios.create({ baseURL: '/api/v1', timeout: 30000 });
client.interceptors.request.use((config) => {
  const token = authService.getAccessToken();
  if (token) config.headers.set('Authorization', `Bearer ${token}`);
  return config;
});

export class ApiError extends Error {
  constructor(message: string, public status?: number) { super(message); }
}

export function errorText(error: unknown): string {
  if (axios.isAxiosError(error)) {
    const detail = error.response?.data?.detail;
    if (typeof detail === 'string') return detail === 'doc_mining_disabled' ? '当前部署尚未启用文档挖掘' : detail;
    if (Array.isArray(detail)) return detail.map((x: Row) => `${x.loc?.slice(1).join('.') || '参数'}：${x.msg}`).join('；');
    if (error.code === 'ECONNABORTED') return '请求超时，请稍后重试';
    if (!error.response) return '暂时无法连接后端，请检查服务后重试';
    return `服务请求失败（${error.response.status}）`;
  }
  return error instanceof Error ? error.message : '操作失败，请重试';
}

export async function request<T = Row>(path: string, config: AxiosRequestConfig = {}): Promise<T> {
  try {
    const { data } = await client.request({ url: path, ...config });
    if (typeof data?.code === 'number' && data.code >= 400) throw new ApiError(data.message || data.detail || '请求失败', data.code);
    return (data && typeof data === 'object' && 'code' in data && 'data' in data ? data.data : data) as T;
  } catch (error) {
    if (axios.isCancel(error)) throw error;
    throw new ApiError(errorText(error), axios.isAxiosError(error) ? error.response?.status : undefined);
  }
}
export const get = <T = Row>(path: string, params?: Row, signal?: AbortSignal) => request<T>(path, { params, signal });
export const post = <T = Row>(path: string, data?: unknown, params?: Row) => request<T>(path, { method: 'POST', data, params, timeout: 120000 });
export const put = <T = Row>(path: string, data: unknown) => request<T>(path, { method: 'PUT', data });
export const del = <T = Row>(path: string) => request<T>(path, { method: 'DELETE' });

interface TaskLog { lines: string[]; total: number; offset?: number; }
export async function getLatestTaskLog(taskId: string, signal: AbortSignal) {
  const path = `${ALPHA}/tasks/${encodeURIComponent(taskId)}/log`;
  const windowSize = 300;
  const initial = await get<TaskLog>(path, { tail: windowSize }, signal);
  if (typeof initial.offset === 'number' || initial.total <= windowSize) {
    return { ...initial, offset: initial.offset ?? 0, fetchedAt: Date.now() };
  }
  // Older running servers interpret tail as a page size. Resolve the last page
  // explicitly until the backend can be restarted after active mining finishes.
  const offset = Math.max(0, initial.total - windowSize);
  const latest = await get<TaskLog>(path, { tail: windowSize, offset }, signal);
  return { ...latest, offset, fetchedAt: Date.now() };
}

export interface Health { status: string; mounted: string[]; failed: Record<string, string>; startup: Record<string, string>; auth_disabled: boolean; startup_ddl: boolean; }
export async function health(signal?: AbortSignal): Promise<Health> {
  const { data } = await axios.get('/health', { signal, timeout: 10000 });
  return data;
}
export interface DataSummary { available: boolean; error?: string; stock_count: number; date_range?: { start: string; end: string; trading_days: number }; datasets: Record<string, { columns: number; categories?: string[] }>; universes: Record<string, { name: string; count: number }>; }
export interface Algorithm { key: string; name: string; desc: string; enabled: boolean; valid: boolean; uses_llm: boolean; uses_datasets: boolean; requires_gpu: boolean; markets: string[]; freqs: string[]; stages: { key: string; label: string }[]; }
export interface Factor extends Row { factor_id: string; factor_name: string; factor_name_cn?: string | null; factor_code?: string; factor_formulation?: string; status: string; ic_value?: number; rank_ic?: number; sharpe_ratio?: number; annual_return?: number; max_drawdown?: number; metadata?: Row; market?: string; universe?: string; date_range?: string; created_at?: string; }
export interface MiningTask extends Row { task_id: string; algorithm: string; direction?: string; status: string; universe: string; factor_count?: number; progress_pct?: number; current_loop?: number; error?: string; created_at: string; completed_at?: string; params_snapshot?: Row; }
export interface PageResult<T> { total: number; items?: T[]; factors?: T[]; tasks?: T[]; limit?: number; offset?: number; }
export interface WindowSpan { start: string; end: string; }
export interface MiningOptions { algorithms: Algorithm[]; datasets: { key: string; label: string }[]; freqs: { value: string; label: string }[]; coverage?: WindowSpan; defaults: { mining?: WindowSpan; validation?: WindowSpan }; windows?: { mining?: WindowSpan; validation?: WindowSpan }; segments?: Record<string, WindowSpan>; minute?: { ready: boolean; error?: string }; }
export interface Run extends Row { run_id: string; factor_id: string; factor_name: string; status: string; date_range: string; universe: string; market: string; ic_value?: number; rank_ic?: number; icir?: number; sharpe_ratio?: number; annual_return?: number; max_drawdown?: number; metrics?: Row; error?: string; created_at?: string; has_series?: boolean; }
export const universeNames: Record<string, string> = { csi300: '沪深 300', csi500: '中证 500', csi1000: '中证 1000', sse50: '上证 50', gem: '创业板', star: '科创板', csi800: '中证 800', all_a: '全部 A 股' };
export const datasetNames: Record<string, string> = { l1_factors: 'L1 基础因子', l2_factors: 'L2 因子', l1_l2_factors: 'L1 + L2 合并因子', features_daily: '日频特征', technical_indicators: '技术指标', valuation: '估值数据', market_sentiment: '市场情绪', alpha_library: 'Alpha 因子库', factor_defs: '因子清单库', rd_mined: '挖掘因子', tdxgs: '通达信指标', jq110: '聚宽因子', alpha360: 'Alpha360', gap_mined: '空档挖掘因子' };
export const isActive = (status?: string) => ['queued', 'pending', 'running', 'backtesting'].includes(status || '');
export const universeName = (id?: string) => universeNames[id || ''] || id || '—';
export const algorithmName = (key?: string) => key === 'statistical' ? '统计量价挖掘' : key === 'rdagent' ? 'AI 因子演化' : key || '—';
export const factorDisplayName = (factor: Row) => factor.factor_name_cn || factor.factor_name || factor.factor_id || '—';
export const number = (value: unknown, digits = 2) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits }) : '—';
export const percent = (value: unknown, digits = 2) => typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(digits)}%` : '—';
export const dateTime = (value?: string) => value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—';
export const downloadJson = (data: unknown, filename: string) => {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' }));
  const a = document.createElement('a'); a.href = url; a.download = filename; a.click(); URL.revokeObjectURL(url);
};
