from __future__ import annotations

import os

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .db import init_db
from .routers.api import router
from .routers.demand_api import router as demand_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="园区能耗数据产品 SLO 与事故影响管理平台",
    version="1.0.0",
    description=(
        "来源登记、产品运行、消费目录与事故指挥四模块联动。\n\n"
        "核心约定：\n"
        "- 健康状态由窗口完整重算得出（新鲜度 + 覆盖 + 核验），不由采集任务存活或人工关闭事故决定。\n"
        "- 预期槽位按窗口实际推导：频率/安装关系改变拆段计算分母；重复样本只占一个槽；声明零用量是有效值；未到截止的槽位不填零。\n"
        "- 维护只扣除规则允许的时段；事后补录维护不撤销已发生的违约事实（BreachFact 为追加式）。\n"
        "- 累计表回零、换表倍率无法衔接、跨缺口表码差单列核验缺口，负差不得计为节能。\n"
        "- 同一产品版本上的不同消费者按各自字段/楼栋/时段与容忍范围分别判定，影响区间合并去重。\n"
        "- 事故关闭必须对应已完成的恢复评估；关闭前复查发现新缺口会重新打开事故；关闭后同因再发形成新区间并关联既往事故。\n"
        "- 峰值需量产品只给可证实/未知范围：只有完整 15 分钟格能确认平均值，部分覆盖只作保守下界；"
        "最高已确认格低于合同但存在缺口格时只说“尚未证实超限”；报告与读数规范化绑定同一数据截止点，"
        "迟到读数生成新版本并标出峰值结论变化。不执行限电或计费。"
    ),
    lifespan=lifespan,
    openapi_tags=[
        {"name": "meta", "description": "平台元数据、模拟时钟与登录"},
        {"name": "registry", "description": "来源登记：采样承诺、维护窗口、产品依赖"},
        {"name": "health", "description": "窗口完整重算与健康评估"},
        {"name": "products", "description": "近实时曲线与核验日汇总的产品运行版本"},
        {"name": "impact", "description": "消费目录与按消费者阈值的独立影响判定"},
        {"name": "incidents", "description": "事故指挥：区间、证据、恢复评估、关闭与重开"},
        {"name": "simulator", "description": "模拟表计到达、时钟推进与审计时间线"},
        {"name": "acceptance", "description": "验收场景重放与报告"},
        {"name": "demand", "description": "峰值需量证据、风险区间与合同容量复核"},
    ],
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(demand_router)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


from .spa import mount_spa  # noqa: E402

mount_spa(app, os.getenv("SPA_DIST", ""))
