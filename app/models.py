"""Pydantic 请求 / 响应模型与输入校验。"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

MAX_BATCH_SIZE = 50
MIN_CANDIDATE_ENTRIES = 2


class Link(BaseModel):
    """双向链路：a/b 两个端点，新旧两版正整数代价。"""

    a: str = Field(..., description="路由器 A 标识")
    b: str = Field(..., description="路由器 B 标识")
    old_cost: int = Field(..., ge=1, description="旧版代价（正整数）")
    new_cost: int = Field(..., ge=1, description="新版代价（正整数）")


class PrecheckRequest(BaseModel):
    target: str = Field(..., description="目标路由器标识")
    links: list[Link] = Field(..., min_length=1, description="双向链路列表")
    batches: list[list[str]] = Field(..., min_length=1, description="分批计划，按切换顺序排列")

    @model_validator(mode="after")
    def _validate(self) -> "PrecheckRequest":
        # 路由器标识不能为空
        if not self.target or not self.target.strip():
            raise ValueError("target 不能为空")
        for lk in self.links:
            if not lk.a or not lk.a.strip() or not lk.b or not lk.b.strip():
                raise ValueError("链路端点标识不能为空")
            if lk.a == lk.b:
                raise ValueError(f"不允许自环链路：{lk.a}")

        nodes: set[str] = set()
        undirected: set[frozenset[str]] = set()
        directed: set[tuple[str, str]] = set()
        for lk in self.links:
            key = frozenset((lk.a, lk.b))
            if key in undirected:
                raise ValueError(f"链路 {lk.a} <-> {lk.b} 重复定义")
            undirected.add(key)
            if (lk.a, lk.b) in directed:
                raise ValueError(f"链路 {lk.a} -> {lk.b} 重复定义")
            directed.add((lk.a, lk.b))
            directed.add((lk.b, lk.a))
            nodes.add(lk.a)
            nodes.add(lk.b)

        if self.target not in nodes:
            raise ValueError(f"目标路由器 {self.target} 不在拓扑中")

        # 分批计划校验
        if any(len(batch) == 0 for batch in self.batches):
            raise ValueError("不允许空批次")
        for i, batch in enumerate(self.batches, start=1):
            if len(batch) > MAX_BATCH_SIZE:
                raise ValueError(f"第 {i} 批包含 {len(batch)} 台设备，超过单批 {MAX_BATCH_SIZE} 台上限")
            if len(set(batch)) != len(batch):
                raise ValueError(f"第 {i} 批内部存在重复设备")
            for router in batch:
                if not router or not router.strip():
                    raise ValueError(f"第 {i} 批存在空设备标识")
                if router not in nodes:
                    raise ValueError(f"第 {i} 批中的设备 {router} 不在拓扑中")
        planned = [r for batch in self.batches for r in batch]
        if len(set(planned)) != len(planned):
            raise ValueError("分批计划中存在跨批次重复的设备")
        if set(planned) != nodes:
            missing = sorted(nodes - set(planned))
            raise ValueError(f"分批计划必须覆盖所有路由器，缺少：{missing}")
        return self


class Hop(BaseModel):
    router: str
    next_hop: str
    via: str = Field(..., description="该出边来自 old / new / both 哪一版下一跳")


class Danger(BaseModel):
    batch_index: int = Field(..., description="首个危险批次（从 1 开始）")
    switched_devices: list[str] = Field(..., description="该批已切换到新版下一跳的设备")
    cycle_routers: list[str] = Field(..., description="闭合转发路径上的路由器（首尾相同）")
    forwarding_cycle: list[Hop] = Field(..., description="逐跳转发路径及所用代价版本")


class PrecheckResponse(BaseModel):
    status: str = Field(..., description="pass 或 danger")
    target: str
    batches_total: int
    batches_checked: int = Field(..., description="实际检查到第几批（危险时即危险批次）")
    old_next_hops: dict[str, list[str]]
    new_next_hops: dict[str, list[str]]
    danger: Danger | None = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "status": "pass",
                    "target": "T",
                    "batches_total": 2,
                    "batches_checked": 2,
                    "old_next_hops": {"A": ["T"], "B": ["T"], "T": []},
                    "new_next_hops": {"A": ["B"], "B": ["T"], "T": []},
                    "danger": None,
                }
            ]
        }
    }


# ============================================================
# 新业务接入带宽离线试算（/capacity-plan）
# ============================================================
class CapacityLink(BaseModel):
    """带带宽的双向链路：按新版代价参与最短路径计算。"""

    id: str = Field(..., description="链路唯一标识，响应中按该标识报告链路负载")
    a: str = Field(..., description="路由器 A 标识")
    b: str = Field(..., description="路由器 B 标识")
    new_cost: int = Field(..., ge=1, description="新版链路代价（正整数）")
    bandwidth: float = Field(..., gt=0, description="链路接入带宽（正数）")


class Conduit(BaseModel):
    """光缆管道：一条管道包含若干链路，停用时其中链路全部移除。"""

    id: str = Field(..., description="管道唯一标识")
    links: list[str] = Field(..., min_length=1, description="该管道包含的链路 id 列表")


class ExistingEntry(BaseModel):
    """已有业务入口及其固定速率。"""

    router: str = Field(..., description="入口路由器标识")
    rate: float = Field(..., gt=0, description="已有流量速率（正数）")


class CapacityPlanRequest(BaseModel):
    target: str = Field(..., description="共同的目的路由器标识")
    links: list[CapacityLink] = Field(..., min_length=1, description="带带宽的双向链路列表")
    conduits: list[Conduit] = Field(..., min_length=1, description="光缆管道列表（逐条停用形成故障场景）")
    existing: list[ExistingEntry] = Field(default_factory=list, description="各入口已有流量")

    @model_validator(mode="after")
    def _validate(self) -> "CapacityPlanRequest":
        if not self.target or not self.target.strip():
            raise ValueError("target 不能为空")

        nodes: set[str] = set()
        link_ids: set[str] = set()
        undirected: set[frozenset[str]] = set()
        for lk in self.links:
            if not lk.id or not lk.id.strip():
                raise ValueError("链路 id 不能为空")
            if lk.id in link_ids:
                raise ValueError(f"链路 id 重复：{lk.id}")
            link_ids.add(lk.id)
            if not lk.a or not lk.a.strip() or not lk.b or not lk.b.strip():
                raise ValueError("链路端点标识不能为空")
            if lk.a == lk.b:
                raise ValueError(f"不允许自环链路：{lk.id}（{lk.a}）")
            key = frozenset((lk.a, lk.b))
            if key in undirected:
                raise ValueError(f"链路 {lk.a} <-> {lk.b} 重复定义")
            undirected.add(key)
            nodes.add(lk.a)
            nodes.add(lk.b)

        if self.target not in nodes:
            raise ValueError(f"目标路由器 {self.target} 不在拓扑中")

        conduit_ids: set[str] = set()
        for cd in self.conduits:
            if not cd.id or not cd.id.strip():
                raise ValueError("管道 id 不能为空")
            if cd.id in conduit_ids:
                raise ValueError(f"管道 id 重复：{cd.id}")
            conduit_ids.add(cd.id)
            if len(set(cd.links)) != len(cd.links):
                raise ValueError(f"管道 {cd.id} 内部存在重复链路")
            for lid in cd.links:
                if lid not in link_ids:
                    raise ValueError(f"管道 {cd.id} 引用了不存在的链路 {lid}")

        entry_routers: set[str] = set()
        for en in self.existing:
            if not en.router or not en.router.strip():
                raise ValueError("已有入口路由器标识不能为空")
            if en.router not in nodes:
                raise ValueError(f"已有入口 {en.router} 不在拓扑中")
            if en.router == self.target:
                raise ValueError("目的路由器不能作为已有流量入口")
            if en.router in entry_routers:
                raise ValueError(f"已有入口重复：{en.router}")
            entry_routers.add(en.router)

        candidates = nodes - entry_routers - {self.target}
        if len(candidates) < MIN_CANDIDATE_ENTRIES:
            raise ValueError(
                f"除目的路由器与已有入口外，至少需要 {MIN_CANDIDATE_ENTRIES} 台可选新入口路由器"
            )
        return self


class LinkLoad(BaseModel):
    link_id: str
    bandwidth: float = Field(..., description="链路带宽")
    load: float = Field(..., description="该场景下链路上的总流量（两个方向之和）")
    utilization: float = Field(..., description="负载率 load / bandwidth")
    saturated: bool = Field(..., description="是否满载（负载率达到约束取值）")


class ScenarioReport(BaseModel):
    scenario: str = Field(..., description="normal 或 down:<管道 id>")
    disabled_conduit: str | None = Field(None, description="停用的管道 id，常态为 null")
    removed_links: list[str] = Field(..., description="该场景移除的链路 id")
    next_hops: dict[str, list[str]] = Field(..., description="新版代价下各路由器到目的的等价下一跳")
    existing_unreachable: list[str] = Field(..., description="送不到目的的已有入口")
    overloaded_links: list[LinkLoad] = Field(..., description="仅已有流量就超载的链路")
    new_entry_unreachable: list[str] = Field(..., description="入选新入口中该场景不可达者（其带宽按零计）")
    link_loads: list[LinkLoad] = Field(..., description="选定方案下全部链路的负载（按链路 id 排序）")


class InfeasibleReason(BaseModel):
    scenario: str
    kind: str = Field(..., description="unreachable_existing 或 existing_overload")
    existing_routers: list[str] = Field(default_factory=list, description="不可达的已有入口（不可达场景）")
    overloaded_links: list[LinkLoad] = Field(default_factory=list, description="超载链路（超载场景）")


class BindingConstraint(BaseModel):
    scenario: str = Field(..., description="生效场景：normal 或 down:<管道 id>")
    kind: str = Field(..., description="saturated_link 或 unreachable_entry")
    link_id: str | None = Field(None, description="满载链路 id（saturated_link 时）")
    entry: str | None = Field(None, description="不可达入口路由器（unreachable_entry 时）")


class CapacityPlanResponse(BaseModel):
    status: str = Field(..., description="feasible 或 infeasible")
    target: str
    scenarios: list[str] = Field(..., description="全部试算场景（常态在前，其余按管道 id 排序）")
    infeasible_reasons: list[InfeasibleReason] = Field(
        default_factory=list, description="无可行方案时，各场景的不可达 / 超载原因"
    )
    selected_entries: list[str] = Field(default_factory=list, description="选中的两台新入口路由器（按 id 排序）")
    entry_capacities: dict[str, float] = Field(
        default_factory=dict, description="每台入选入口分配的接入带宽"
    )
    total_capacity: float = Field(0, description="两路新入口带宽合计（目标最大化值）")
    binding_constraints: list[BindingConstraint] = Field(
        default_factory=list, description="限制最终带宽取值的场景与满载链路 / 不可达入口"
    )
    scenario_reports: list[ScenarioReport] = Field(
        default_factory=list, description="各场景逐跳下一跳与链路负载明细"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "status": "feasible",
                    "target": "T",
                    "scenarios": ["normal", "down:P1", "down:P2"],
                    "infeasible_reasons": [],
                    "selected_entries": ["A", "C"],
                    "entry_capacities": {"A": 30.0, "C": 30.0},
                    "total_capacity": 60.0,
                    "binding_constraints": [
                        {"scenario": "down:P1", "kind": "saturated_link", "link_id": "L2", "entry": None}
                    ],
                    "scenario_reports": [],
                }
            ]
        }
    }
