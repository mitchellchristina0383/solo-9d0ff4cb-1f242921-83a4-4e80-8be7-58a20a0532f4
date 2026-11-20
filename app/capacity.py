"""新业务接入带宽离线试算核心。

业务语义
--------
机房从两台入口路由器接入新业务，要求常态与任一光缆管道单独停用后，两台入口
都能使用同一组接入带宽（即在全部场景下，所选带宽都不能使任何链路超载）。

- 路由按 **新版链路代价** 计算到共同目的路由器的最短路径，等价下一跳（ECMP）
  之间按跳数均分流量：路由器 r 的某条下一跳上承载 r 发出流量的 1/|NH(r)|。
- 场景集合：常态（不移除链路）+ 每条管道单独停用（移除该管道包含的全部链路
  后重算路由）。
- 新入口在某一场景不可达时，该入口带宽必须为零（对所有场景成立的同一组带宽）。
- 已有流量若在任一场景送不到目的，或仅凭已有流量就使链路超载，则无可行方案。
- 在全部场景都可承载的前提下，选择两台新入口及其带宽，使两路合计最大。

实现要点
--------
- 流量对路由是线性的：预先算出每台可达路由器注入 1 单位流量时在各链路上的
  单位贡献（ECMP 均分为有理数），实际链路负载 = 已有负载 + Σ 入口带宽 × 单位贡献。
- 固定一对入口后，可行带宽 (x, y) 由每场景每条链路的线性不等式界定，构成
  二维线性规划；枚举约束直线与坐标轴的交点（顶点）求 x+y 最大值，全部用
  fractions.Fraction 精确计算，避免浮点误差影响满载判定。
"""

from __future__ import annotations

import heapq
from fractions import Fraction
from itertools import combinations

from .models import CapacityLink, Conduit, ExistingEntry

INF = float("inf")
ZERO = Fraction(0)
ONE = Fraction(1)


def _frac(value: float) -> Fraction:
    """把请求中的带宽 / 速率（JSON 数字）精确转为 Fraction。"""
    return Fraction(str(value))


def collect_capacity_nodes(links: list[CapacityLink]) -> set[str]:
    nodes: set[str] = set()
    for lk in links:
        nodes.add(lk.a)
        nodes.add(lk.b)
    return nodes


def is_capacity_connected(nodes: set[str], links: list[CapacityLink], source: str) -> bool:
    """常态拓扑连通性检查（管道停用后的连通性按场景另行处理）。"""
    adj: dict[str, list[str]] = {n: [] for n in nodes}
    for lk in links:
        adj[lk.a].append(lk.b)
        adj[lk.b].append(lk.a)
    seen = {source}
    stack = [source]
    while stack:
        u = stack.pop()
        for v in adj[u]:
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return seen == nodes


def _build_cost_adjacency(
    nodes: set[str], links: list[CapacityLink], removed: set[str]
) -> dict[str, dict[str, str]]:
    """构造场景邻接表：邻居 -> 链路 id，停用管道中的链路被移除。"""
    adj: dict[str, dict[str, str]] = {n: {} for n in nodes}
    for lk in links:
        if lk.id in removed:
            continue
        adj[lk.a][lk.b] = lk.id
        adj[lk.b][lk.a] = lk.id
    return adj


def _dijkstra(
    adj: dict[str, dict[str, str]], cost: dict[str, int], source: str
) -> dict[str, Fraction | float]:
    # float 无穷大仅作不可达哨兵；可达距离均为 Fraction，比较与排序均安全
    dist: dict[str, Fraction | float] = {n: INF for n in adj}
    dist[source] = ZERO
    pq: list[tuple[int, str]] = [(0, source)]
    while pq:
        d, u = heapq.heappop(pq)
        if d != dist[u]:
            continue
        for v, lid in adj[u].items():
            nd = d + cost[lid]
            if nd < dist[v]:
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


def _next_hops(
    adj: dict[str, dict[str, str]],
    cost: dict[str, int],
    dist: dict[str, Fraction | float],
) -> dict[str, list[str]]:
    """v 是 u 的下一跳当且仅当 dist[u] == cost(u,v) + dist[v]，结果排序确定。"""
    nh: dict[str, list[str]] = {n: [] for n in adj}
    for u in adj:
        for v, lid in adj[u].items():
            if dist[u] == cost[lid] + dist[v]:
                nh[u].append(v)
        nh[u].sort()
    return nh


def _unit_flows(
    nodes: set[str],
    adj: dict[str, dict[str, str]],
    nh: dict[str, list[str]],
    dist: dict[str, Fraction | float],
) -> dict[str, dict[str, Fraction]]:
    """每台可达路由器注入 1 单位流量时，各无向链路上的单位流量贡献。

    沿到目的的最短路径转发、ECMP 均分。按最短距离从小到大处理路由器，
    保证处理 u 时其全部下一跳 v（dist[v] < dist[u]）的下游贡献已计算完毕。
    返回 {router: {link_id: 流量}}，不可达路由器对应空字典。
    """
    reachable = [n for n in nodes if dist[n] != INF]
    units: dict[str, dict[str, Fraction]] = {n: {} for n in nodes}
    # 按最短距离从小到大处理：u 的下一跳 v 满足 dist[v] < dist[u]，
    # 故处理 u 时 units[v]（v 的全部下游链路贡献）已经计算完毕。
    for u in sorted(reachable, key=lambda n: (dist[n], n)):
        if not nh[u]:
            continue  # 目的路由器自身不发出流量
        share = ONE / len(nh[u])
        for v in nh[u]:
            lid = adj[u][v]
            # 1) u 到下一跳 v 这一段，承担均分后的份额
            units[u][lid] = units[u].get(lid, ZERO) + share
            # 2) 该份额进入 v 后，沿 v 的全部下游链路继续传播
            for downstream_lid, downstream_flow in units[v].items():
                units[u][downstream_lid] = (
                    units[u].get(downstream_lid, ZERO) + share * downstream_flow
                )
    return units


def _solve_two_var_lp(
    constraints: list[tuple[Fraction, Fraction, Fraction]],
    force_x_zero: bool,
    force_y_zero: bool,
) -> tuple[Fraction, Fraction] | None:
    """求解 max x+y s.t. a*x + b*y <= c（a,b >= 0，c >= 0），x,y >= 0。

    步骤：
    1. 枚举约束直线之间及与坐标轴的全部交点，保留可行顶点，取 x+y 最大值 S；
    2. 最优面 x+y=S 可能是一条线段（多个顶点并列），在该面上取 |x-y| 最小
       （两路最均衡）的点；再并列时取 x 较大者。
    全部使用 Fraction 精确计算；无解返回 None。
    """
    # 变量被强制为零时，退化为单变量可行区间，直接取上界
    if force_x_zero and force_y_zero:
        return ZERO, ZERO
    if force_x_zero or force_y_zero:
        var_pos = 1 if force_x_zero else 0  # 保留变量在 (a,b) 中的位置
        upper = [row[2] / row[var_pos] for row in constraints if row[var_pos] > 0]
        # 无约束时该变量理论上无界；实际业务中入口流量必经过链路，这里给 0 兜底
        val = min(upper) if upper else ZERO
        return (ZERO, val) if force_x_zero else (val, ZERO)

    def feasible(x: Fraction, y: Fraction) -> bool:
        if x < 0 or y < 0:
            return False
        # Fraction 精确运算，约束判定不引入容差
        return all(aa * x + bb * y <= cc for aa, bb, cc in constraints)

    candidates: list[tuple[Fraction, Fraction]] = [(ZERO, ZERO)]
    for idx, (a1, b1, c1) in enumerate(constraints):
        # 与 x 轴交点（y=0）
        if a1 > 0:
            p = (c1 / a1, ZERO)
            if feasible(*p):
                candidates.append(p)
        # 与 y 轴交点（x=0）
        if b1 > 0:
            p = (ZERO, c1 / b1)
            if feasible(*p):
                candidates.append(p)
        # 两条约束直线的交点
        for a2, b2, c2 in constraints[idx + 1 :]:
            det = a1 * b2 - a2 * b1
            if det == 0:
                continue
            x = (c1 * b2 - c2 * b1) / det
            y = (a1 * c2 - a2 * c1) / det
            if feasible(x, y):
                candidates.append((x, y))

    # 第一步：最大合计 S（由可行顶点枚举得到，保证至少有一个可行点取到 S）
    total = max(x + y for x, y in candidates)
    if total == 0:
        return ZERO, ZERO

    # 第二步：在最优面 x+y=total 上求真正可行的 x 区间。
    # 对每条约束代入 y=total-x：(a-b)x <= c-b*total，得到 x 的上/下界；
    # 坐标轴限制在该面上等价于 0 <= x <= total（x>=0 且 y=total-x>=0）。
    lower = ZERO
    upper = total
    for a, b, c in constraints:
        coef = a - b
        rhs = c - b * total
        if coef > 0:
            upper = min(upper, rhs / coef)
        elif coef < 0:
            lower = max(lower, rhs / coef)
    lower = max(lower, ZERO)
    upper = min(upper, total)

    def face_feasible(x: Fraction) -> bool:
        # Fraction 运算无浮点误差，面可行性严格判定即可，不引入容差
        y = total - x
        if x < 0 or y < 0:
            return False
        return all(aa * x + bb * y <= cc for aa, bb, cc in constraints)

    if lower > upper or not face_feasible(lower) or not face_feasible(upper):
        # 退化保护：回退到顶点级平局规则（合计相同取最均衡、再取 x 较大）
        return max(
            (p for p in candidates if p[0] + p[1] == total),
            key=lambda p: (-abs(p[0] - p[1]), p[0]),
        )
    balanced = total / 2
    x_star = min(max(balanced, lower), upper)
    return x_star, total - x_star


def run_capacity_plan(
    nodes: set[str],
    links: list[CapacityLink],
    conduits: list[Conduit],
    existing: list[ExistingEntry],
    target: str,
) -> dict:
    """执行完整试算，返回可直接序列化的结构化结果字典。"""
    cost = {lk.id: lk.new_cost for lk in links}
    bandwidth = {lk.id: _frac(lk.bandwidth) for lk in links}
    link_by_id = {lk.id: lk for lk in links}
    conduit_links = {cd.id: set(cd.links) for cd in conduits}

    existing_entries = [(en.router, _frac(en.rate)) for en in existing]
    existing_routers = {router for router, _ in existing_entries}
    candidates = sorted(nodes - existing_routers - {target})

    # 场景：常态在前，管道按 id 排序
    scenarios: list[tuple[str, str | None, set[str]]] = [
        ("normal", None, set())
    ] + [
        (f"down:{cd.id}", cd.id, set(conduit_links[cd.id]))
        for cd in sorted(conduits, key=lambda c: c.id)
    ]

    # 逐场景：路由、单位流量、可达性与已有流量负载
    scenario_data: list[dict] = []
    infeasible_reasons: list[dict] = []
    for name, conduit_id, removed in scenarios:
        adj = _build_cost_adjacency(nodes, links, removed)
        dist = _dijkstra(adj, cost, target)
        nh = _next_hops(adj, cost, dist)
        units = _unit_flows(nodes, adj, nh, dist)
        reachable = {n for n in nodes if dist[n] < INF}

        existing_unreachable = sorted(
            router for router, _ in existing_entries if router not in reachable
        )
        # 已有流量在各链路上的负载
        existing_load: dict[str, Fraction] = {}
        for router, rate in existing_entries:
            if router not in reachable:
                continue
            for lid, flow in units[router].items():
                existing_load[lid] = existing_load.get(lid, ZERO) + rate * flow

        overloaded = [
            lid
            for lid in sorted(bandwidth)
            if existing_load.get(lid, ZERO) > bandwidth[lid]
        ]

        if existing_unreachable:
            infeasible_reasons.append(
                {
                    "scenario": name,
                    "kind": "unreachable_existing",
                    "existing_routers": existing_unreachable,
                    "overloaded_links": [],
                }
            )
        if overloaded:
            infeasible_reasons.append(
                {
                    "scenario": name,
                    "kind": "existing_overload",
                    "existing_routers": [],
                    "overloaded_links": overloaded,
                }
            )

        scenario_data.append(
            {
                "name": name,
                "conduit_id": conduit_id,
                "removed": sorted(removed),
                "next_hops": nh,
                "units": units,
                "reachable": reachable,
                "existing_load": existing_load,
                "existing_unreachable": existing_unreachable,
                "overloaded": overloaded,
            }
        )

    if infeasible_reasons:
        return {
            "status": "infeasible",
            "target": target,
            "scenarios": [s["name"] for s in scenario_data],
            "infeasible_reasons": infeasible_reasons,
            "scenario_data": scenario_data,
            "bandwidth": bandwidth,
            "link_by_id": link_by_id,
        }

    # 枚举所有候选入口对，求每对的最优二维带宽分配
    # 注意 combinations 返回一次性迭代器，必须物化成列表，否则只枚举第一对
    best: dict | None = None
    for x_router, y_router in list(combinations(candidates, 2)):
        constraints: list[tuple[Fraction, Fraction, Fraction]] = []
        x_zero_scenarios: list[str] = []
        y_zero_scenarios: list[str] = []
        for sc in scenario_data:
            name = sc["name"]
            x_ok = x_router in sc["reachable"]
            y_ok = y_router in sc["reachable"]
            if not x_ok:
                x_zero_scenarios.append(name)
            if not y_ok:
                y_zero_scenarios.append(name)
            ux = sc["units"].get(x_router, {}) if x_ok else {}
            uy = sc["units"].get(y_router, {}) if y_ok else {}
            for lid in sorted(bandwidth):
                a = ux.get(lid, ZERO)
                b = uy.get(lid, ZERO)
                if a == 0 and b == 0:
                    continue
                c = bandwidth[lid] - sc["existing_load"].get(lid, ZERO)
                constraints.append((a, b, c, name, lid))

        lp = _solve_two_var_lp(
            [(a, b, c) for a, b, c, _, _ in constraints],
            force_x_zero=bool(x_zero_scenarios),
            force_y_zero=bool(y_zero_scenarios),
        )
        if lp is None:
            continue
        x_val, y_val = lp
        # 合计最大化是硬目标，排在最前；同合计再取两路最均衡、x 更大的解
        key = (x_val + y_val, -abs(x_val - y_val), x_val)
        if best is None or key > best["key"]:
            best = {
                "key": key,
                "pair": (x_router, y_router),
                "x": x_val,
                "y": y_val,
                "constraints": constraints,
                "x_zero_scenarios": x_zero_scenarios,
                "y_zero_scenarios": y_zero_scenarios,
            }

    assert best is not None  # (0, 0) 始终可行，至少存在一个候选对
    x_router, y_router = best["pair"]
    x_val, y_val = best["x"], best["y"]

    # 最优解处的绑定约束：满载链路与不可达入口（Fraction 精确运算，取等即绑定）
    binding: list[dict] = []
    for a, b, c, name, lid in best["constraints"]:
        if a * x_val + b * y_val == c:
            binding.append(
                {"scenario": name, "kind": "saturated_link", "link_id": lid, "entry": None}
            )
    for name in best["x_zero_scenarios"]:
        binding.append(
            {"scenario": name, "kind": "unreachable_entry", "link_id": None, "entry": x_router}
        )
    for name in best["y_zero_scenarios"]:
        binding.append(
            {"scenario": name, "kind": "unreachable_entry", "link_id": None, "entry": y_router}
        )
    binding.sort(key=lambda d: (d["scenario"], d["kind"], d["link_id"] or "", d["entry"] or ""))

    return {
        "status": "feasible",
        "target": target,
        "scenarios": [s["name"] for s in scenario_data],
        "infeasible_reasons": [],
        "pair": (x_router, y_router),
        "capacities": {x_router: x_val, y_router: y_val},
        "total": x_val + y_val,
        "binding": binding,
        "scenario_data": scenario_data,
        "bandwidth": bandwidth,
        "link_by_id": link_by_id,
    }


def build_scenario_reports(result: dict) -> list[dict]:
    """按选定方案生成各场景的逐跳下一跳与链路负载明细。"""
    feasible = result["status"] == "feasible"
    capacities = result.get("capacities", {}) if feasible else {}
    reports: list[dict] = []
    for sc in result["scenario_data"]:
        loads: dict[str, Fraction] = dict(sc["existing_load"])
        if feasible:
            for router, bw in capacities.items():
                if router in sc["reachable"] and bw > 0:
                    for lid, flow in sc["units"][router].items():
                        loads[lid] = loads.get(lid, ZERO) + bw * flow

        link_loads = []
        for lid in sorted(result["bandwidth"]):
            bw = result["bandwidth"][lid]
            load = loads.get(lid, ZERO)
            link_loads.append(
                {
                    "link_id": lid,
                    "bandwidth": float(bw),
                    "load": float(load),
                    "utilization": float(load / bw),
                    "saturated": load == bw,
                }
            )

        new_unreachable: list[str] = []
        if feasible:
            for router in capacities:
                if router not in sc["reachable"]:
                    new_unreachable.append(router)

        reports.append(
            {
                "scenario": sc["name"],
                "disabled_conduit": sc["conduit_id"],
                "removed_links": sc["removed"],
                "next_hops": {n: sc["next_hops"][n] for n in sorted(sc["next_hops"])},
                "existing_unreachable": sc["existing_unreachable"],
                "overloaded_links": [
                    _link_load_dict(lid, sc["existing_load"].get(lid, ZERO), result["bandwidth"])
                    for lid in sc["overloaded"]
                ],
                "new_entry_unreachable": sorted(new_unreachable),
                "link_loads": link_loads,
            }
        )
    return reports


def _link_load_dict(lid: str, load: Fraction, bandwidth: dict[str, Fraction]) -> dict:
    bw = bandwidth[lid]
    return {
        "link_id": lid,
        "bandwidth": float(bw),
        "load": float(load),
        "utilization": float(load / bw),
        "saturated": load == bw,
    }


def serialize_infeasible_reasons(result: dict) -> list[dict]:
    reasons = []
    for reason in result["infeasible_reasons"]:
        reasons.append(
            {
                "scenario": reason["scenario"],
                "kind": reason["kind"],
                "existing_routers": reason["existing_routers"],
                "overloaded_links": [
                    _link_load_dict(lid, _find_load(result, reason["scenario"], lid), result["bandwidth"])
                    for lid in reason["overloaded_links"]
                ],
            }
        )
    return reasons


def _find_load(result: dict, scenario: str, lid: str) -> Fraction:
    for sc in result["scenario_data"]:
        if sc["name"] == scenario:
            return sc["existing_load"].get(lid, ZERO)
    return ZERO
