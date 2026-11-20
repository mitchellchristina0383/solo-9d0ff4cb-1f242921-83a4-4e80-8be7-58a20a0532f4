"""FastAPI 入口：链路代价分批调整离线预检与新业务接入带宽离线试算。"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import RedirectResponse

from .capacity import (
    build_scenario_reports,
    collect_capacity_nodes,
    is_capacity_connected,
    run_capacity_plan,
    serialize_infeasible_reasons,
)
from .core import collect_nodes, is_connected, run_precheck
from .models import (
    CapacityPlanRequest,
    CapacityPlanResponse,
    PrecheckRequest,
    PrecheckResponse,
)

app = FastAPI(
    title="链路代价调整预检与接入带宽试算",
    version="1.1.0",
    description=(
        "离线网络规划服务：\n\n"
        "1. `/precheck` —— 提交新旧两版链路代价与分批计划，逐批判断错峰切换"
        "过程中是否可能出现转发环路。\n\n"
        "2. `/capacity-plan` —— 提交拓扑、链路带宽、光缆管道与目的路由器，"
        "在常态及任一管道单独停用的场景下，按新版代价 ECMP 路由试算两台新入口"
        "的最大可承载接入带宽。"
    ),
)


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/precheck", response_model=PrecheckResponse, summary="提交分批计划并执行环路预检")
def precheck(req: PrecheckRequest) -> PrecheckResponse:
    nodes = collect_nodes(req.links)
    if not is_connected(nodes, req.links, req.target):
        # 连通性在模型层之后二次确认（模型已保证端点一致，这里防御孤立分量）
        raise HTTPException(status_code=422, detail="拓扑必须连通，且目标路由器可达所有设备")

    result = run_precheck(nodes, req.links, req.target, req.batches)
    danger = result["dangerous"]
    return PrecheckResponse(
        status="danger" if danger else "pass",
        target=req.target,
        batches_total=len(req.batches),
        batches_checked=result["batches_checked"],
        old_next_hops=result["old_nh"],
        new_next_hops=result["new_nh"],
        danger=danger,
    )


@app.post(
    "/capacity-plan",
    response_model=CapacityPlanResponse,
    summary="新业务双入口接入带宽离线试算（含管道停用场景）",
)
def capacity_plan(req: CapacityPlanRequest) -> CapacityPlanResponse:
    nodes = collect_capacity_nodes(req.links)
    if not is_capacity_connected(nodes, req.links, req.target):
        raise HTTPException(status_code=422, detail="常态拓扑必须连通，且目的路由器可达所有设备")

    result = run_capacity_plan(nodes, req.links, req.conduits, req.existing, req.target)

    if result["status"] == "infeasible":
        return CapacityPlanResponse(
            status="infeasible",
            target=req.target,
            scenarios=result["scenarios"],
            infeasible_reasons=serialize_infeasible_reasons(result),
            scenario_reports=build_scenario_reports(result),
        )

    return CapacityPlanResponse(
        status="feasible",
        target=req.target,
        scenarios=result["scenarios"],
        selected_entries=list(result["pair"]),
        entry_capacities={router: float(bw) for router, bw in result["capacities"].items()},
        total_capacity=float(result["total"]),
        binding_constraints=result["binding"],
        scenario_reports=build_scenario_reports(result),
    )
