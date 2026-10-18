-- ============================================================================
-- 全局股票池模块 (Global Stock Pool) v2 —— 单表元信息 + TXT 成员
-- 幂等：可反复执行（启动期 ensure_tables 与升级脚本共用本文件）。
--
-- v2 破坏性简化（相对 v1）：
--   - 成员唯一事实源 = TXT 文件（前缀式一行一个，/data/stock_pool/*.txt），
--     不再建 qm_stock_pool_version / qm_stock_pool_member（存在则 DROP）；
--   - 无草稿/发布版本模型；qm_stock_pool 增加 file_path 列（旧库 ALTER 补齐，
--     残留的 current_version 列不再被读写，保留仅为避免迁移破坏）。
--
-- 口径约定（重要）：
--   TXT 成员为前缀式 SH600036（人可读可手改）；进程内解析后统一经
--   normalize.py 转后缀式 600036.SH（DB/Qlib/parquet 层）再消费。
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. 池元信息表（成员不在这里，在 file_path 指向的 TXT）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS qm_stock_pool (
    pool_id         TEXT PRIMARY KEY,
    code            TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    market          TEXT NOT NULL DEFAULT 'CN',
    pool_type       TEXT NOT NULL DEFAULT 'static',
    scope           TEXT NOT NULL DEFAULT 'global',
    tenant_id       TEXT,
    owner_user_id   TEXT,
    status          TEXT NOT NULL DEFAULT 'active',
    file_path       TEXT,
    symbol_count    INTEGER NOT NULL DEFAULT 0,
    checksum        TEXT,
    source_kind     TEXT,
    source_ref      TEXT,
    is_system       BOOLEAN NOT NULL DEFAULT FALSE,
    created_by      TEXT,
    updated_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 旧 v1 库升级路径（v2 新库无这两列时补齐；已存在则 no-op）
ALTER TABLE qm_stock_pool ADD COLUMN IF NOT EXISTS file_path TEXT;

-- 同 scope + tenant 下 code 唯一（tenant_id 为 NULL 时按空串归组）
CREATE UNIQUE INDEX IF NOT EXISTS uq_qm_stock_pool_code
    ON qm_stock_pool (scope, COALESCE(tenant_id, ''), code);

CREATE INDEX IF NOT EXISTS idx_qm_stock_pool_list
    ON qm_stock_pool (market, pool_type, status);

CREATE INDEX IF NOT EXISTS idx_qm_stock_pool_scope
    ON qm_stock_pool (scope, tenant_id, owner_user_id);

-- ---------------------------------------------------------------------------
-- 2. 引用表（哪个功能在用哪个池；被引用的池不可归档/删除）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS qm_stock_pool_binding (
    id              BIGSERIAL PRIMARY KEY,
    pool_id         TEXT NOT NULL REFERENCES qm_stock_pool(pool_id) ON DELETE CASCADE,
    target_type     TEXT NOT NULL,
    target_id       TEXT NOT NULL,
    mode            TEXT NOT NULL DEFAULT 'filter',
    priority        INTEGER NOT NULL DEFAULT 100,
    tenant_id       TEXT,
    user_id         TEXT,
    created_by      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (pool_id, target_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_qm_stock_pool_binding_target
    ON qm_stock_pool_binding (target_type, target_id);

CREATE INDEX IF NOT EXISTS idx_qm_stock_pool_binding_pool
    ON qm_stock_pool_binding (pool_id);

-- ---------------------------------------------------------------------------
-- 3. v1 遗留表清理（v2 成员在 TXT、无版本模型）
-- ---------------------------------------------------------------------------
DROP TABLE IF EXISTS qm_stock_pool_member;
DROP TABLE IF EXISTS qm_stock_pool_version;
