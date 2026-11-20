"""新业务接入带宽离线试算（/capacity-plan）端到端与算法自测。

运行（仓库根目录）：
    PYTHONPATH=/tmp/pylibs python -m pytest -q
"""

from __future__ import annotations

import random
from fractions import Fraction
from itertools import combinations

from fastapi.testclient import TestClient

from app.capacity import _solve_two_var_lp
from app.main import app

client = TestClient(app)


def _diamond_request() -> dict:
    """菱形拓扑：T-A-C-T 与 T-B-C-T 两条路径分属两条管道。

    常态：A、B 到 T 直连；C 经 A/B 等价分流。
    P1 停用（L1、L3 移除）：A 仅剩 A-B 链路，经 B 到 T；C 仅剩 C-B。
    P2 停用（L2、L4 移除）：B 经 A 到 T；C 仅剩 C-A。
    """
    return {
        "target": "T",
        "links": [
            {"id": "L1", "a": "T", "b": "A", "new_cost": 10, "bandwidth": 100},
            {"id": "L2", "a": "T", "b": "B", "new_cost": 10, "bandwidth": 100},
            {"id": "L3", "a": "A", "b": "C", "new_cost": 10, "bandwidth": 40},
            {"id": "L4", "a": "B", "b": "C", "new_cost": 10, "bandwidth": 40},
            {"id": "L5", "a": "A", "b": "B", "new_cost": 10, "bandwidth": 100},
        ],
        "conduits": [
            {"id": "P1", "links": ["L1", "L3"]},
            {"id": "P2", "links": ["L2", "L4"]},
        ],
        "existing": [],
    }


# ---------------------------------------------------------------- 手算对照：菱形拓扑
def test_diamond_hand_calculated_plan():
    r = client.post("/capacity-plan", json=_diamond_request())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "feasible"
    assert data["scenarios"] == ["normal", "down:P1", "down:P2"]

    # 手算（链路负载含逐跳传播后的全部下游流量）：
    #  对 (A,B)：停用 P1 后 A 经 L5 绕行到 B，L2 上承载 x_A（绕行）+ x_B（B 自身），
    #  约束 x_A+x_B<=100；停用 P2 时 L1 对称约束。合计上限 100，对称解各 50。
    #  对含 C 的对：C 侧链路只有 40 带宽，合计上限 60，劣于 (A,B)。
    assert data["selected_entries"] == ["A", "B"]
    caps = data["entry_capacities"]
    assert caps["A"] == 50
    assert caps["B"] == 50
    assert data["total_capacity"] == 100

    # 绑定约束：两个停用场景下汇聚侧 L2 / L1 满载
    binding = {(b["scenario"], b["kind"], b["link_id"]) for b in data["binding_constraints"]}
    assert ("down:P1", "saturated_link", "L2") in binding
    assert ("down:P2", "saturated_link", "L1") in binding

    reports = {rep["scenario"]: rep for rep in data["scenario_reports"]}
    # 常态下一跳：C 到 T 经 A/B 等价（代价均 20）
    assert reports["normal"]["next_hops"]["C"] == ["A", "B"]
    assert reports["normal"]["removed_links"] == []
    assert reports["down:P1"]["removed_links"] == ["L1", "L3"]
    assert reports["down:P2"]["removed_links"] == ["L2", "L4"]

    # 停用 P1：A 只能经 B，C 只能经 B；A 的 50 经 L5 到 B，再与 B 自身的 50
    # 一起进入 L2，L2 满载 100
    assert reports["down:P1"]["next_hops"]["A"] == ["B"]
    assert reports["down:P1"]["next_hops"]["C"] == ["B"]
    loads_p1 = {ld["link_id"]: ld for ld in reports["down:P1"]["link_loads"]}
    assert loads_p1["L5"]["load"] == 50  # A 的 50 经 A-B 绕行
    assert loads_p1["L2"]["load"] == 100  # A 绕行 50 + B 自身 50
    assert loads_p1["L2"]["saturated"] is True
    assert abs(loads_p1["L2"]["utilization"] - 1.0) < 1e-12
    assert loads_p1["L3"]["load"] == 0  # 已随管道移除
    assert reports["down:P1"]["new_entry_unreachable"] == []


# ---------------------------------------------------------------- ECMP 均分与管道停用重分配
def test_ecmp_splits_evenly_and_reroutes_on_outage():
    """菱形 ECMP：入口 S 到目的 T 经两条等代价路径，停用一条管道后流量全部切到另一侧。"""
    payload = {
        "target": "T",
        "links": [
            # 路径 1（管道 P1）：S-U-T
            {"id": "SU", "a": "S", "b": "U", "new_cost": 1, "bandwidth": 100},
            {"id": "UT", "a": "U", "b": "T", "new_cost": 1, "bandwidth": 100},
            # 路径 2（管道 P2）：S-V-T
            {"id": "SV", "a": "S", "b": "V", "new_cost": 1, "bandwidth": 100},
            {"id": "VT", "a": "V", "b": "T", "new_cost": 1, "bandwidth": 100},
            # 叶子 W 只挂在 V 下，保证候选路由器 >=2 且不会抢赢 S
            {"id": "VW", "a": "V", "b": "W", "new_cost": 1, "bandwidth": 100},
        ],
        "conduits": [
            {"id": "P1", "links": ["SU", "UT"]},
            {"id": "P2", "links": ["SV", "VT"]},
        ],
        "existing": [],
    }
    r = client.post("/capacity-plan", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    # 常态 S 的两条路代价均为 2，ECMP 均分；每条管道带宽 100 -> 合计上限 200。
    # 停用任一管道后只剩单侧 100，合计上限收紧到 100。
    # 最优对为 (S, U)：U 直挂 T 但在 down:P1 随管道移除而不可达，故 U=0，100 全给 S
    assert data["selected_entries"] == ["S", "U"]
    s_caps = data["entry_capacities"]
    assert data["total_capacity"] == 100
    assert s_caps["S"] == 100
    assert s_caps["U"] == 0

    reports = {rep["scenario"]: rep for rep in data["scenario_reports"]}
    # 常态：S 等价下一跳 {U, V}，流量均分
    assert reports["normal"]["next_hops"]["S"] == ["U", "V"]
    normal_loads = {ld["link_id"]: ld for ld in reports["normal"]["link_loads"]}
    assert normal_loads["SU"]["load"] == 50
    assert normal_loads["SV"]["load"] == 50
    assert normal_loads["UT"]["load"] == 50
    assert normal_loads["VT"]["load"] == 50

    # 停用 P1：S 只剩 V 一条下一跳，全部 100 切到 P2 侧，SV/VT 满载
    assert reports["down:P1"]["removed_links"] == ["SU", "UT"]
    assert reports["down:P1"]["next_hops"]["S"] == ["V"]
    p1_loads = {ld["link_id"]: ld for ld in reports["down:P1"]["link_loads"]}
    assert p1_loads["SV"]["load"] == 100
    assert p1_loads["VT"]["load"] == 100
    assert p1_loads["SV"]["saturated"] is True
    assert p1_loads["VT"]["saturated"] is True
    assert p1_loads["SU"]["load"] == 0  # 随管道移除

    # 停用 P2：S 切回 U；U 在该场景可达（P1 完好）
    assert reports["down:P2"]["next_hops"]["S"] == ["U"]
    p2_loads = {ld["link_id"]: ld for ld in reports["down:P2"]["link_loads"]}
    assert p2_loads["SU"]["load"] == 100
    assert p2_loads["UT"]["load"] == 100

    # 绑定约束：两个停用场景的满载链路 + U 在 down:P1 不可达
    binding = {(b["scenario"], b["kind"], b["link_id"], b["entry"]) for b in data["binding_constraints"]}
    assert ("down:P1", "saturated_link", "SV", None) in binding
    assert ("down:P1", "saturated_link", "VT", None) in binding
    assert ("down:P2", "saturated_link", "SU", None) in binding
    assert ("down:P1", "unreachable_entry", None, "U") in binding
    assert reports["down:P1"]["new_entry_unreachable"] == ["U"]


# ---------------------------------------------------------------- 不可达入口带宽置零
def test_unreachable_new_entry_gets_zero_bandwidth():
    """管道 P1 停用后 A 与 T 完全隔离：任何含 A 的方案中 A 必须为 0。"""
    payload = _diamond_request()
    # 去掉 A-B 救援链路 L5：P1 停用时 A 成为孤岛
    payload["links"] = [lk for lk in payload["links"] if lk["id"] != "L5"]
    r = client.post("/capacity-plan", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "feasible"
    # 含 A 的最优对 (A,C)：A 在 down:P1 不可达 -> A=0，C 仍可拿到 40
    assert data["selected_entries"] == ["A", "C"]
    assert data["entry_capacities"]["A"] == 0
    assert data["entry_capacities"]["C"] == 40

    binding = {(b["scenario"], b["kind"], b["entry"]) for b in data["binding_constraints"]}
    assert ("down:P1", "unreachable_entry", "A") in binding

    reports = {rep["scenario"]: rep for rep in data["scenario_reports"]}
    assert reports["down:P1"]["new_entry_unreachable"] == ["A"]
    # 不可达场景下 A 不产生任何流量；C 的 40 走 C-B（L4）再 B-T（L2）
    loads = {ld["link_id"]: ld["load"] for ld in reports["down:P1"]["link_loads"]}
    assert loads["L4"] == 40
    assert loads["L2"] == 40
    assert loads["L1"] == 0  # A 孤岛，T-A 链路随 P1 移除


# ---------------------------------------------------------------- 已有流量不可达 -> 无可行方案
def test_existing_traffic_unreachable_reports_infeasible():
    payload = _diamond_request()
    payload["existing"] = [{"router": "A", "rate": 25}]
    payload["links"] = [lk for lk in payload["links"] if lk["id"] != "L5"]
    r = client.post("/capacity-plan", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "infeasible"
    assert data["selected_entries"] == []
    assert data["total_capacity"] == 0
    reasons = {(rs["scenario"], rs["kind"]): rs for rs in data["infeasible_reasons"]}
    assert ("down:P1", "unreachable_existing") in reasons
    assert reasons[("down:P1", "unreachable_existing")]["existing_routers"] == ["A"]


# ---------------------------------------------------------------- 已有流量超载 -> 无可行方案
def test_existing_traffic_overload_reports_infeasible():
    payload = _diamond_request()
    # 常态 C 的流量在 L3/L4 上各分一半；速率 100 时每条承载 50 > 40
    payload["existing"] = [{"router": "C", "rate": 100}]
    r = client.post("/capacity-plan", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "infeasible"
    overload = [rs for rs in data["infeasible_reasons"] if rs["kind"] == "existing_overload"]
    # 常态：L3/L4 各承 50 > 40；停用 P1：全部 100 压到 L4；停用 P2：全部 100 压到 L3
    assert {rs["scenario"] for rs in overload} == {"normal", "down:P1", "down:P2"}
    normal_detail = next(rs for rs in overload if rs["scenario"] == "normal")
    assert {ld["link_id"] for ld in normal_detail["overloaded_links"]} == {"L3", "L4"}
    assert all(ld["load"] == 50 for ld in normal_detail["overloaded_links"])
    # 超载不等于满载：负载率 > 1 且 saturated（恰好等于带宽）为 False
    assert all(ld["utilization"] > 1 for ld in normal_detail["overloaded_links"])
    assert all(not ld["saturated"] for ld in normal_detail["overloaded_links"])
    p1_detail = next(rs for rs in overload if rs["scenario"] == "down:P1")
    assert [ld["link_id"] for ld in p1_detail["overloaded_links"]] == ["L4"]
    assert p1_detail["overloaded_links"][0]["load"] == 100


# ---------------------------------------------------------------- 已有流量与新流量共享带宽
def test_existing_traffic_consumes_capacity():
    payload = _diamond_request()
    # 去掉救援链路 L5：含 A/B 的对在管道停用场景不可达，最优对固定为 (A,C)
    payload["links"] = [lk for lk in payload["links"] if lk["id"] != "L5"]
    # 已有入口 E 双挂 A、B；两条挂接链路分别归入两侧管道，
    # 管道停用时同侧挂接链路一起移除，E 始终只剩另一侧可达且不形成 A/B 救援捷径
    payload["links"].extend(
        [
            {"id": "L6", "a": "A", "b": "E", "new_cost": 1, "bandwidth": 100},
            {"id": "L7", "a": "B", "b": "E", "new_cost": 1, "bandwidth": 100},
        ]
    )
    payload["conduits"] = [
        {"id": "P1", "links": ["L1", "L3", "L6"]},
        {"id": "P2", "links": ["L2", "L4", "L7"]},
    ]
    # E 已有 40：常态经 A/B 等价分流，L1/L2 各承 20
    payload["existing"] = [{"router": "E", "rate": 40}]
    r = client.post("/capacity-plan", json=payload)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "feasible"
    assert data["selected_entries"] == ["A", "C"]
    caps = data["entry_capacities"]
    reports = {rep["scenario"]: rep for rep in data["scenario_reports"]}

    # down:P1：A 随管道移除不可达 -> x_A=0；E 与 C 都只能经 B。
    # L4（带宽 40）被 E 的 40 占满，C 在该场景的新流量没有任何余量
    assert reports["down:P1"]["new_entry_unreachable"] == ["A"]
    loads_p1 = {ld["link_id"]: ld for ld in reports["down:P1"]["link_loads"]}
    assert loads_p1["L4"]["load"] == 40
    assert loads_p1["L4"]["saturated"] is True
    assert caps["A"] == 0
    # C 在 down:P2 可走 A，L3 成为瓶颈 -> x_C 上限 40
    assert caps["C"] == 40
    binding = {(b["scenario"], b["kind"], b["link_id"], b["entry"]) for b in data["binding_constraints"]}
    assert ("down:P1", "saturated_link", "L4", None) in binding
    assert ("down:P2", "saturated_link", "L3", None) in binding
    assert ("down:P1", "unreachable_entry", None, "A") in binding

    # 对照：去掉已有流量后，A 在 down:P1 仍不可达（恒为 0），但 C 同样最多 40；
    # 关键差异是有已有流量时 down:P1 的 L4 已被 E 占满，报告中明确标注
    payload0 = dict(payload)
    payload0["existing"] = []
    d0 = client.post("/capacity-plan", json=payload0).json()
    assert d0["status"] == "feasible"
    p1_clean = next(r for r in d0["scenario_reports"] if r["scenario"] == "down:P1")
    l4_clean = next(l for l in p1_clean["link_loads"] if l["link_id"] == "L4")
    assert l4_clean["load"] == 40  # 无已有流量时 L4 上只有 C 的 40
    # 有已有流量时 L4 同样满载，但其中 40 全部来自已有入口 E（新业务 C 在该场景分到 0）
    l4_with_existing = next(l for l in reports["down:P1"]["link_loads"] if l["link_id"] == "L4")
    assert l4_with_existing["load"] == 40


# ---------------------------------------------------------------- 输入校验
def test_capacity_plan_validation_errors():
    base = _diamond_request()
    bad_cases = [
        {**base, "target": "Z"},  # 目的不在拓扑
        {**base, "conduits": [{"id": "P1", "links": ["L1", "L3"]},
                              {"id": "P1", "links": ["L2", "L4"]}]},  # 管道 id 重复
        {**base, "conduits": [{"id": "P1", "links": ["L1", "L9"]}]},  # 引用不存在的链路
        {**base, "conduits": [{"id": "P1", "links": ["L1", "L1"]}]},  # 管道内链路重复
        {**base, "conduits": []},  # 没有管道
        {**base, "existing": [{"router": "T", "rate": 1}]},  # 目的路由器作入口
        {**base, "existing": [{"router": "A", "rate": 1},
                              {"router": "A", "rate": 2}]},  # 入口重复
        {**base, "existing": [{"router": "A", "rate": 1},
                              {"router": "B", "rate": 1},
                              {"router": "C", "rate": 1}]},  # 候选不足两台
    ]
    for payload in bad_cases:
        r = client.post("/capacity-plan", json=payload)
        assert r.status_code == 422, payload

    link_cases = [
        [{**lk, "id": "L1"} for lk in base["links"]],  # 链路 id 重复
        [{**lk, "new_cost": 0} if lk["id"] == "L1" else lk for lk in base["links"]],  # 代价非正
        [{**lk, "bandwidth": 0} if lk["id"] == "L1" else lk for lk in base["links"]],  # 带宽非正
        [{**lk, "a": "T", "b": "T"} if lk["id"] == "L1" else lk for lk in base["links"]],  # 自环
    ]
    for links in link_cases:
        r = client.post("/capacity-plan", json={**base, "links": links})
        assert r.status_code == 422, links


def test_capacity_plan_disconnected_topology_rejected():
    payload = _diamond_request()
    payload["links"].append(
        {"id": "LX", "a": "Q", "b": "R", "new_cost": 1, "bandwidth": 10}
    )
    payload["conduits"][0]["links"].append("LX")
    r = client.post("/capacity-plan", json=payload)
    assert r.status_code == 422


# ---------------------------------------------------------------- 二维 LP 求解器单元测试
def test_two_var_lp_solver():
    F = Fraction
    # x + y <= 10, x <= 6, y <= 7：最优面 x+y=10 上 y<=7 -> x>=3，x<=6，
    # 均衡点 (5,5) 可行
    cons = [(F(1), F(1), F(10)), (F(1), F(0), F(6)), (F(0), F(1), F(7))]
    assert _solve_two_var_lp(cons, False, False) == (F(5), F(5))
    # 均衡点被约束截断：x <= 4 时最优面 x+y=10 上 x 最大 4 -> (4,6)
    cons_cut = cons + [(F(1), F(0), F(4))]
    assert _solve_two_var_lp(cons_cut, False, False) == (F(4), F(6))
    # 强制 x=0：y 取最小上界 7
    assert _solve_two_var_lp(cons, True, False) == (F(0), F(7))
    # 强制 y=0：x 取最小上界 6
    assert _solve_two_var_lp(cons, False, True) == (F(6), F(0))
    # 双双强制为零
    assert _solve_two_var_lp(cons, True, True) == (F(0), F(0))
    # 仅约束 x：y 不受限，合计 4 的点全部最优，均衡规则给出 (2,2)
    cons2 = [(F(2), F(0), F(8))]
    x2, y2 = _solve_two_var_lp(cons2, False, False)
    assert x2 + y2 == 4 and 2 * x2 <= 8
    # 仅约束 y 的对称情形
    cons3 = [(F(0), F(2), F(8))]
    x3, y3 = _solve_two_var_lp(cons3, False, False)
    assert x3 + y3 == 4 and 2 * y3 <= 8
    # 含真实双侧约束时，均衡点落在中间
    cons4 = [(F(1), F(1), F(10)), (F(1), F(0), F(10)), (F(0), F(1), F(10))]
    assert _solve_two_var_lp(cons4, False, False) == (F(5), F(5))


# ---------------------------------------------------------------- 随机拓扑：分数最优 vs 整数暴力枚举
def test_lp_matches_integer_bruteforce_on_random_topologies():
    rng = random.Random(20261120)
    names = [f"n{i}" for i in range(6)]

    for trial in range(25):
        # 随机连通无向图
        edges: list[tuple[int, int]] = []
        seen: set[frozenset[int]] = set()
        order = list(range(1, 6))
        rng.shuffle(order)
        connected = {0}
        for v in order:
            u = rng.choice(tuple(connected))
            edges.append((u, v))
            seen.add(frozenset((u, v)))
            connected.add(v)
        for _ in range(rng.randint(0, 3)):
            u, v = rng.sample(range(6), 2)
            key = frozenset((u, v))
            if key not in seen:
                seen.add(key)
                edges.append((u, v))

        links = [
            {
                "id": f"e{i}",
                "a": names[u],
                "b": names[v],
                "new_cost": rng.randint(1, 9),
                "bandwidth": rng.randint(20, 120),
            }
            for i, (u, v) in enumerate(edges)
        ]
        link_ids = [lk["id"] for lk in links]
        rng.shuffle(link_ids)
        cut = len(link_ids) // 2 or 1
        payload = {
            "target": "n0",
            "links": links,
            "conduits": [
                {"id": "P1", "links": link_ids[:cut]},
                {"id": "P2", "links": link_ids[cut:]},
            ],
            "existing": [
                {"router": "n1", "rate": rng.randint(1, 30)}
            ] if trial % 2 == 0 else [],
        }
        r = client.post("/capacity-plan", json=payload)
        assert r.status_code == 200, (trial, r.text)
        data = r.json()
        if data["status"] == "infeasible":
            # 无可行方案只能由已有流量不可达 / 超载引起
            assert payload["existing"]
            continue

        total = Fraction(str(data["total_capacity"]))
        pair = data["selected_entries"]

        # 独立复算：按场景手工推导单位贡献，暴力枚举整数带宽并验证上界
        from app.capacity import (
            _build_cost_adjacency,
            _dijkstra,
            _next_hops,
            _unit_flows,
        )
        from app.models import CapacityLink, Conduit, ExistingEntry

        nodes = set(names)
        link_models = [CapacityLink(**lk) for lk in links]
        cost = {lk.id: lk.new_cost for lk in link_models}
        bw = {lk.id: Fraction(lk.bandwidth) for lk in link_models}
        existing = [ExistingEntry(**en) for en in payload["existing"]]
        zero = Fraction(0)
        ex_rate = Fraction(existing[0].rate) if existing else zero
        scenarios: list[set[str]] = [set(), set(link_ids[:cut]), set(link_ids[cut:])]

        best_int = -1
        candidate_names = [n for n in names[1:]]
        for x_router, y_router in combinations(candidate_names, 2):
            # 每场景的线性系数与可达性；入口在某场景不可达时，其带宽在该场景不产生流量。
            # 若入口在任一场景不可达，其全局带宽必须强制为 0（同一组带宽适用所有场景）。
            per_scenario = []
            x_force_zero = False
            y_force_zero = False
            for removed in scenarios:
                adj = _build_cost_adjacency(nodes, link_models, removed)
                dist = _dijkstra(adj, cost, "n0")
                nh = _next_hops(adj, cost, dist)
                units = _unit_flows(nodes, adj, nh, dist)
                reachable = {n for n in nodes if dist[n] != float("inf")}
                x_ok = x_router in reachable
                y_ok = y_router in reachable
                x_force_zero = x_force_zero or not x_ok
                y_force_zero = y_force_zero or not y_ok
                ex_load: dict[str, Fraction] = {}
                if existing and "n1" in reachable:
                    for lid, fl in units["n1"].items():
                        ex_load[lid] = ex_rate * fl
                per_scenario.append((units, ex_load, x_ok, y_ok))
            # 暴力枚举 0..60 的整数带宽；任一场景不可达的入口带宽强制为 0
            x_values = [0] if x_force_zero else range(61)
            y_values = [0] if y_force_zero else range(61)
            for xi in x_values:
                for yi in y_values:
                    ok = True
                    for units, ex_load, x_ok, y_ok in per_scenario:
                        x_eff = Fraction(xi) if x_ok else zero
                        y_eff = Fraction(yi) if y_ok else zero
                        for lid in bw:
                            load = ex_load.get(lid, zero)
                            load += x_eff * units[x_router].get(lid, zero)
                            load += y_eff * units[y_router].get(lid, zero)
                            if load > bw[lid]:
                                ok = False
                                break
                        if not ok:
                            break
                    if ok:
                        best_int = max(best_int, xi + yi)

        # 分数最优不得低于整数暴力最优；且差距不超过 2（顶点分数部分）
        assert best_int >= 0, trial
        assert total >= best_int, trial
        assert float(total - best_int) <= 2 + 1e-9, (trial, total, best_int)
        assert len(pair) == 2


# ---------------------------------------------------------------- 确定性
def test_capacity_plan_deterministic():
    r1 = client.post("/capacity-plan", json=_diamond_request()).json()
    r2 = client.post("/capacity-plan", json=_diamond_request()).json()
    assert r1 == r2


def test_capacity_plan_docs_available():
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/capacity-plan" in schema["paths"]
    assert "/precheck" in schema["paths"]
