"""
订阅套餐/付费会员种子脚本（幂等，可重复执行）

QuantDB 付费会员规则（backend/services/trade/services/member_gate.py 执行）:
  user_subscriptions 存在 status='active' 且 end_date > now() 的记录,
  且对应 subscription_plans.price > 0（免费套餐不算付费会员）。

本脚本:
1. 确保标准套餐存在（免费版 + QuantDB 专业版月/年，ON CONFLICT 幂等）
2. 为管理员账号授予在期年卡（默认 quantdb_pro_yearly），保证持仓实时行情
   （TDX 实时行情 Feed / tick 持久化 / 止损止盈提醒）立即可用

用法: python backend/scripts/seed_subscriptions.py
环境变量: QM_OWNER_USER_ID(默认 00000001) / QM_OWNER_PLAN_CODE(默认 quantdb_pro_yearly)
"""
import asyncio
import json
import logging
import os

from sqlalchemy import text

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

PLANS = [
    {
        "code": "quantdb_free",
        "name": "免费版",
        "description": "基础行情与功能",
        "price": 0.00,
        "interval": "month",
        "features": [],
    },
    {
        "code": "quantdb_pro_monthly",
        "name": "QuantDB 专业版（月）",
        "description": "持仓实时行情/止损止盈提醒/tick 持久化",
        "price": 99.00,
        "interval": "month",
        "features": ["tdx_realtime_quotes", "tick_history"],
    },
    {
        "code": "quantdb_pro_yearly",
        "name": "QuantDB 专业版（年）",
        "description": "持仓实时行情/止损止盈提醒/tick 持久化",
        "price": 999.00,
        "interval": "year",
        "features": ["tdx_realtime_quotes", "tick_history"],
    },
]

OWNER_USER_ID = os.getenv("QM_OWNER_USER_ID", "00000001").strip()
OWNER_TENANT_ID = "default"
OWNER_PLAN_CODE = os.getenv("QM_OWNER_PLAN_CODE", "quantdb_pro_yearly").strip()


async def seed_plans(db) -> int:
    count = 0
    for p in PLANS:
        result = await db.execute(
            text(
                """
                INSERT INTO subscription_plans
                    (name, code, description, price, currency, interval, features, is_active)
                VALUES (:name, :code, :description, :price, 'CNY', :interval,
                        CAST(:features AS JSON), TRUE)
                ON CONFLICT (code) DO NOTHING
                """
            ),
            {
                "name": p["name"],
                "code": p["code"],
                "description": p["description"],
                "price": p["price"],
                "interval": p["interval"],
                "features": json.dumps(p["features"], ensure_ascii=False),
            },
        )
        count += result.rowcount
    return count


async def ensure_owner_subscription(db) -> bool:
    """管理员账号在期年卡：已有在期订阅则跳过，否则创建。"""
    exists = (
        await db.execute(
            text(
                """
                SELECT us.id
                FROM user_subscriptions us
                JOIN subscription_plans sp ON sp.id = us.plan_id
                WHERE us.user_id = :user_id
                  AND us.tenant_id = :tenant_id
                  AND us.status = 'active'
                  AND us.end_date > now()
                LIMIT 1
                """
            ),
            {"user_id": OWNER_USER_ID, "tenant_id": OWNER_TENANT_ID},
        )
    ).first()
    if exists:
        return False
    result = await db.execute(
        text(
            """
            INSERT INTO user_subscriptions
                (user_id, tenant_id, plan_id, status, start_date, end_date, auto_renew)
            SELECT :user_id, :tenant_id, id, 'active', now(),
                   now() + interval '1 year', TRUE
            FROM subscription_plans
            WHERE code = :plan_code
            """
        ),
        {
            "user_id": OWNER_USER_ID,
            "tenant_id": OWNER_TENANT_ID,
            "plan_code": OWNER_PLAN_CODE,
        },
    )
    return result.rowcount > 0


async def main() -> None:
    from backend.shared.database_manager_v2 import get_session

    async with get_session() as db:
        inserted = await seed_plans(db)
        logger.info("套餐: 新建 %d 个，其余已存在（幂等）", inserted)
        created = await ensure_owner_subscription(db)
        logger.info(
            "管理员会员(%s/%s, %s): %s",
            OWNER_TENANT_ID, OWNER_USER_ID, OWNER_PLAN_CODE,
            "新建在期年卡" if created else "已存在在期订阅，跳过",
        )
        await db.commit()


if __name__ == "__main__":
    asyncio.run(main())
