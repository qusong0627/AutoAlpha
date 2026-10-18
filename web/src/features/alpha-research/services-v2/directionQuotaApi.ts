/**
 * 方向配额预览 API 桥（T-FE-10）：GET /api/v1/alpha-agent/direction-quota
 *
 * 后端口径单源 = backend/services/engine/alpha_agent/direction_quota.py
 * （与 /evolve 的 quota 抽样**同一** allocate/preview 实现）：代理模型打分
 * （经验贝叶斯平滑成功率 × 饱和惩罚）+ 三档名额切分（探索 30% / 利用 50% /
 * 变异 20%）。纯派生读时组装，零持久化；读取失败 → `available=false` 仍是
 * 200（不是接口错误）；口径 = 本用户 × 本市场。
 *
 * snake→camel 归一在本文件完成；**缺失一律 null**（面板落「—」，绝不上屏 0
 * ——「没有统计」与「统计为 0」是两回事；只有服务端真给了数才上屏）。
 */

import { apiClient } from '../../../services/aiStrategyClients';
import type { ApiResponse } from '../types-v2';

// ========================== 类型 ==========================

/** 三档键（顺序即展示顺序；与后端 TIER_ORDER 同源同序）。 */
export const QUOTA_TIERS = ['explore', 'exploit', 'mutate'] as const;
export type QuotaTier = (typeof QUOTA_TIERS)[number];

export interface DirectionScoreRow {
  direction: string;
  /** 全状态任务数（空白度口径，与 direction_sampling 同源） */
  attempts: number | null;
  /** 终态任务数（成功率分母：进行中任务尚无结局） */
  nTasks: number | null;
  /** completed ∧ factor_count ≥ 1 */
  nSuccess: number | null;
  /** 经验贝叶斯平滑成功率（Jeffreys 先验；无史 = 0.5 中性） */
  p: number | null;
  dupHits90d: number | null;
  satPenalty: number | null;
  /** score = p × (1 − sat_penalty) */
  score: number | null;
  /** 饱和（90 日重复命中 ≥ 硬阈值）：三档一律剔除 */
  saturated: boolean;
}

export interface TierPlanView {
  pool: string[];
  /** blankness / proxy_score / uniform */
  weighting: string | null;
  /** saturated=剔尽回退同档全集；mutate_empty=无近期成功方向退利用档 */
  fallback: string | null;
}

export interface QuotaPreviewView {
  available: boolean;
  market: string;
  n: number | null;
  quota: Record<QuotaTier, number | null>;
  slots: Record<QuotaTier, number | null>;
  saturatedThreshold: number | null;
  recentSuccessTasks: number | null;
  directions: DirectionScoreRow[];
  /** 候选词表为空时为 null（只出全史打分表，无档位预演） */
  tiers: Record<QuotaTier, TierPlanView> | null;
  recentSuccessDirs: string[];
  /** 读取失败的稳定文案（服务端给出；内部错误原文只进日志） */
  error: string | null;
}

export interface DirectionQuotaParams {
  market?: string;
  /** 候选方向词表（设置页选中列表）；留空 = 只出全史打分表 */
  directions?: string[];
  /** 预演名额数（1..10，与 /evolve num_directions 同闸） */
  n?: number;
}

// ========================== 映射 ==========================

/** 数值缺失 → null（不上屏 0；0 与「没有统计」是两回事）。 */
function numOrNull(v: unknown): number | null {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function strOrNull(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null;
}

function strArr(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [];
}

function mapScoreRow(raw: any): DirectionScoreRow {
  return {
    direction: String(raw?.direction ?? ''),
    attempts: numOrNull(raw?.attempts),
    nTasks: numOrNull(raw?.n_tasks),
    nSuccess: numOrNull(raw?.n_success),
    p: numOrNull(raw?.p),
    dupHits90d: numOrNull(raw?.dup_hits_90d),
    satPenalty: numOrNull(raw?.sat_penalty),
    score: numOrNull(raw?.score),
    saturated: raw?.saturated === true,
  };
}

function mapTier(raw: any): TierPlanView {
  return {
    pool: strArr(raw?.pool),
    weighting: strOrNull(raw?.weighting),
    fallback: strOrNull(raw?.fallback),
  };
}

function mapTiers(raw: any): Record<QuotaTier, TierPlanView> | null {
  if (!raw || typeof raw !== 'object') return null;
  const out = {} as Record<QuotaTier, TierPlanView>;
  for (const tier of QUOTA_TIERS) out[tier] = mapTier(raw[tier]);
  return out;
}

function mapQuotaField(raw: any): Record<QuotaTier, number | null> {
  const out = {} as Record<QuotaTier, number | null>;
  for (const tier of QUOTA_TIERS) out[tier] = numOrNull(raw?.[tier]);
  return out;
}

// ========================== 请求 ==========================

/** 默认市场与后端 Query 默认同词（仓库现行口径 a_share，非 CN）。 */
export const DIRECTION_QUOTA_DEFAULT_MARKET = 'a_share';

/** 与后端 DEFAULT_PREVIEW_N 同值（名额数预演默认）。 */
export const DIRECTION_QUOTA_DEFAULT_N = 3;

export async function getDirectionQuotaPreview(
  params: DirectionQuotaParams = {},
): Promise<ApiResponse<QuotaPreviewView>> {
  try {
    const qs = new URLSearchParams();
    const market = (params.market ?? '').trim();
    if (market) qs.set('market', market);
    for (const d of params.directions ?? []) {
      const label = typeof d === 'string' ? d.trim() : '';
      if (label) qs.append('directions', label);
    }
    if (Number.isFinite(params.n)) qs.set('n', String(Math.trunc(params.n as number)));
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    const res = await apiClient.get(`/alpha-agent/direction-quota${suffix}`);
    const raw = res.data?.data ?? {};
    return {
      success: true,
      data: {
        available: raw?.available === true,
        market: String(raw?.market ?? ''),
        n: numOrNull(raw?.n),
        quota: mapQuotaField(raw?.quota),
        slots: mapQuotaField(raw?.slots),
        saturatedThreshold: numOrNull(raw?.saturated_threshold),
        recentSuccessTasks: numOrNull(raw?.recent_success_tasks),
        directions: (Array.isArray(raw?.directions) ? raw.directions : []).map(
          mapScoreRow,
        ),
        tiers: mapTiers(raw?.tiers),
        recentSuccessDirs: strArr(raw?.recent_success_dirs),
        error: strOrNull(raw?.error),
      },
    };
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : '方向配额读取失败';
    return { success: false, error: message };
  }
}
