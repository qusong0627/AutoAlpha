/**
 * 经验记忆/禁区 API 桥（T-FM-04）：GET /api/v1/alpha-agent/experience-memory
 *
 * 后端口径单源 = backend/services/engine/alpha_agent/experience_memory.py
 * （与 LLM 提示词注入同一 `build_memory`）：拒因聚类 + 饱和方向 + 方向任务史，
 * 纯派生读时组装，零持久化。全空 → `available=false` 仍是 200（「没跑过」
 * 不是接口错误）；口径 = 本用户 × 本市场。
 *
 * snake→camel 归一在本文件完成；缺失一律 null（面板落「—」，绝不上屏 0——
 * 计数 0 与「没有统计」是两回事，只有真值才上屏）。
 */

import { apiClient } from '../../../services/aiStrategyClients';
import type { ApiResponse } from '../types-v2';

// ========================== 类型 ==========================

export interface ExperienceReasonBucket {
  bucket: string;
  label: string;
  count: number | null;
  lastAt: string | null;
  sampleDirs: string[];
  sampleColumns: string[];
  /** 来源标注：manifest（物化清单）/ report（T-FM-01 统计派报告） */
  sources: string[];
}

export interface ExperienceSaturatedDirection {
  direction: string;
  label: string;
  dupHits: number | null;
  dupHits90d: number | null;
  topOpponent: string | null;
  materializedCount: number | null;
}

export interface ExperienceDirectionStat {
  direction: string;
  label: string;
  tasks: number | null;
  completed: number | null;
  factors: number | null;
}

export interface ExperienceMemorySources {
  tasks: number | null;
  manifestEntries: number | null;
  report: string | null;
  reportGeneratedAt: string | null;
}

export interface ExperienceMemoryView {
  available: boolean;
  market: string;
  generatedAt: string | null;
  reasonBuckets: ExperienceReasonBucket[];
  saturatedDirections: ExperienceSaturatedDirection[];
  directionStats: ExperienceDirectionStat[];
  sources: ExperienceMemorySources;
}

// ========================== 映射 ==========================

function count(v: unknown): number | null {
  // 缺失 → null（面板落「—」）：Number(undefined)=NaN 与 0 是两回事，
  // 归一器绝不替后端伪造 0（评审 code-review CR3，同 directionQuotaApi.numOrNull）
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function strOrNull(v: unknown): string | null {
  return typeof v === 'string' && v ? v : null;
}

function strArr(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : [];
}

function mapBucket(raw: any): ExperienceReasonBucket {
  return {
    bucket: String(raw?.bucket ?? ''),
    label: String(raw?.label ?? raw?.bucket ?? ''),
    count: count(raw?.count),
    lastAt: strOrNull(raw?.last_at),
    sampleDirs: strArr(raw?.sample_dirs),
    sampleColumns: strArr(raw?.sample_columns),
    sources: strArr(raw?.sources),
  };
}

function mapSaturated(raw: any): ExperienceSaturatedDirection {
  return {
    direction: String(raw?.direction ?? ''),
    label: String(raw?.label ?? raw?.direction ?? ''),
    dupHits: count(raw?.dup_hits),
    dupHits90d: count(raw?.dup_hits_90d),
    topOpponent: strOrNull(raw?.top_opponent),
    materializedCount: count(raw?.materialized_count),
  };
}

function mapDirectionStat(raw: any): ExperienceDirectionStat {
  return {
    direction: String(raw?.direction ?? ''),
    label: String(raw?.label ?? raw?.direction ?? ''),
    tasks: count(raw?.tasks),
    completed: count(raw?.completed),
    factors: count(raw?.factors),
  };
}

// ========================== 请求 ==========================

/** 默认市场与后端 Query 默认同词（仓库现行口径 a_share，非 CN）。 */
export const EXPERIENCE_MEMORY_DEFAULT_MARKET = 'a_share';

export async function getExperienceMemory(
  params: { market?: string } = {},
): Promise<ApiResponse<ExperienceMemoryView>> {
  try {
    const qs = new URLSearchParams();
    const market = (params.market ?? '').trim();
    if (market) qs.set('market', market);
    const suffix = qs.toString() ? `?${qs.toString()}` : '';
    const res = await apiClient.get(`/alpha-agent/experience-memory${suffix}`);
    const raw = res.data?.data ?? {};
    const sources = raw?.sources ?? {};
    return {
      success: true,
      data: {
        available: raw?.available === true,
        market: String(raw?.market ?? ''),
        generatedAt: strOrNull(raw?.generated_at),
        reasonBuckets: (Array.isArray(raw?.reason_buckets) ? raw.reason_buckets : []).map(
          mapBucket,
        ),
        saturatedDirections: (
          Array.isArray(raw?.saturated_directions) ? raw.saturated_directions : []
        ).map(mapSaturated),
        directionStats: (
          Array.isArray(raw?.direction_stats) ? raw.direction_stats : []
        ).map(mapDirectionStat),
        sources: {
          tasks: count(sources?.tasks),
          manifestEntries: count(sources?.manifest_entries),
          report: strOrNull(sources?.report),
          reportGeneratedAt: strOrNull(sources?.report_generated_at),
        },
      },
    };
  } catch (error: unknown) {
    const message = error instanceof Error ? error.message : '经验记忆读取失败';
    return { success: false, error: message };
  }
}
