# 链路代价分批调整离线预检 / 新业务接入带宽离线试算

机房网络规划的两个 **离线** FastAPI 服务：

1. **链路代价分批调整环路预检**（`POST /precheck`）：提交连通路由拓扑、每条双向
   链路的新旧两版正整数代价、目标路由器和覆盖全部路由器的分批计划，服务按
   两版代价分别计算到目标的最短路径下一跳（等价路径的所有下一跳都可能被选
   中），逐批判断错峰切换过程中是否可能出现转发环路。
2. **新业务双入口接入带宽试算**（`POST /capacity-plan`）：提交拓扑、各链路带宽、
   每条光缆管道包含的链路、共同目的路由器与各入口已有流量速率，从其余路由器
   中选两台作为新业务入口，在常态与任一管道单独停用的全部场景下，求两路合计
   最大且各场景都能承载的一组接入带宽。

## 判环模型

对每一批 `k`，切换期间各路由器的可用出边为：

| 路由器所属 | 切换期间可用出边 |
| --- | --- |
| 早于第 `k` 批 | 仅新版下一跳（前一批已全部生效） |
| 晚于第 `k` 批 | 仅旧版下一跳 |
| 第 `k` 批（本批） | **旧版 ∪ 新版**下一跳（设备可按任意顺序切换） |

在该并集转发图中：

- **存在有向环** ⇒ 存在一种错峰切换顺序与下一跳选择使流量兜圈，本批危险；
  环上仅新版可达的出边对应的本批设备，即为构造该环所需的“已切换设备”。
- **不存在有向环** ⇒ 本批切换的任意中间状态（任意已切换子集、任意等价
  下一跳选择）都不可能成环。

该等价关系由随机小拓扑暴力枚举（枚举全部切换子集 × 全部下一跳组合）
回归测试守护，见 `tests/test_api.py::test_union_lemma_against_bruteforce`。

下一跳由 Dijkstra 最短距离推导：无向边 `(u, v, w)` 上，`v` 是 `u` 的下一跳
当且仅当 `dist(u) == w + dist(v)`，等号成立的邻居全部纳入等价下一跳集合。

## 目录结构

```
app/
  main.py      FastAPI 入口（/、/health、/precheck、/capacity-plan、/docs）
  core.py      Dijkstra、下一跳集合、并集图与三色 DFS 找环（/precheck）
  capacity.py  场景路由、ECMP 单位流量、二维线性规划与方案排序（/capacity-plan）
  models.py    请求/响应模型与输入校验
tests/         pytest 自测（含同批错峰、ECMP、跨批次、暴力对照、管道停用重分配）
examples/      可直接提交的示例请求
Dockerfile
docker-compose.yml
requirements.txt
```

## 启动（Docker Compose）

```bash
docker compose up --build
```

- 交互式文档：<http://localhost:8000/docs> （在 `/docs` 的 `POST /precheck`
  与 `POST /capacity-plan` 上 "Try it out" 提交请求）
- 健康检查：`GET http://localhost:8000/health`

无 Docker 时本地运行：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 请求格式

```json
{
  "target": "T",
  "links": [
    {"a": "T", "b": "A", "old_cost": 10, "new_cost": 30},
    {"a": "T", "b": "B", "old_cost": 20, "new_cost": 20},
    {"a": "A", "b": "B", "old_cost": 1, "new_cost": 1}
  ],
  "batches": [["A", "B", "T"]]
}
```

约束（违反返回 HTTP 422）：

- 代价为正整数；不允许自环、重复链路；拓扑必须连通。
- 分批计划覆盖所有路由器且不重不漏；单批最多 **50** 台；不允许空批次。
- 批次顺序即切换顺序，前一批全部生效后才进入下一批。

## 响应

安全：

```json
{"status": "pass", "target": "T", "batches_total": 3, "batches_checked": 3,
 "old_next_hops": {...}, "new_next_hops": {...}, "danger": null}
```

危险（首个危险批次）：

```json
{
  "status": "danger",
  "batches_checked": 1,
  "danger": {
    "batch_index": 1,
    "switched_devices": ["A"],
    "cycle_routers": ["A", "B", "A"],
    "forwarding_cycle": [
      {"router": "A", "next_hop": "B", "via": "new"},
      {"router": "B", "next_hop": "A", "via": "old"}
    ]
  }
}
```

`via` 标出该跳取自哪一版下一跳：`old` / `new` / `both`。

## 新业务接入带宽离线试算（POST /capacity-plan）

### 试算模型

机房从两台入口路由器接入新业务，要求**常态与任一光缆管道单独停用后都使用
同一组接入带宽**。服务枚举的场景为：

- `normal`：不移除任何链路；
- `down:<管道 id>`：移除该管道包含的全部链路后重算路由（每条管道一个场景）。

每个场景内：

1. 按 **新版链路代价**（`new_cost`，正整数）跑 Dijkstra，推导到共同目的
   路由器的全部等价下一跳（ECMP）；
2. 某台路由器的流量在其等价下一跳之间**按跳均分**（各 `1/N`），逐跳传播到目的；
3. 无向链路的负载为两个方向上经过该链路的流量之和，与 `bandwidth` 比较。

关键规则：

- **新入口在任一场景不可达 ⇒ 该入口带宽必须为 0**（同一组带宽适用所有场景）。
- **已有流量送不到目的，或仅凭已有流量就使链路超载 ⇒ 返回 `infeasible`**，
  并在 `infeasible_reasons` 中逐场景指出不可达入口或超载链路及负载率。
- 固定一对入口后，每场景每条链路给出一个关于两路带宽 `(x, y)` 的线性不等式
  `a·x + b·y <= 剩余带宽`，整体构成二维线性规划；服务枚举全部约束顶点求
  `x+y` 最大值，并在最优面上取两路最均衡的解（再并列取 x 较大者）。
- 全部运算使用 `fractions.Fraction` 精确有理数，避免浮点误差影响满载判定；
  响应中再转为普通 JSON 数字。
- 从除目的路由器与已有入口之外的路由器中枚举所有两台组合，按
  （合计最大 → 两路最均衡 → 首路带宽最大）确定性选优。

### 请求格式

```json
{
  "target": "T",
  "links": [
    {"id": "L1", "a": "T", "b": "A", "new_cost": 10, "bandwidth": 100},
    {"id": "L2", "a": "T", "b": "B", "new_cost": 10, "bandwidth": 100},
    {"id": "L3", "a": "A", "b": "C", "new_cost": 10, "bandwidth": 40},
    {"id": "L4", "a": "B", "b": "C", "new_cost": 10, "bandwidth": 40},
    {"id": "L5", "a": "A", "b": "B", "new_cost": 10, "bandwidth": 100}
  ],
  "conduits": [
    {"id": "P1", "links": ["L1", "L3"]},
    {"id": "P2", "links": ["L2", "L4"]}
  ],
  "existing": [
    {"router": "C", "rate": 20}
  ]
}
```

约束（违反返回 HTTP 422）：

- 链路 `id` 与管道 `id` 全局唯一且非空；不允许自环、重复无向链路；
  `new_cost` 为正整数，`bandwidth` 为正数。
- 管道至少一条，引用的链路 id 必须存在，管道内链路不重复；链路可以不属于
  任何管道（任何单管道停用都不会移除它）。
- 目的路由器必须在拓扑中，且常态拓扑连通；目的路由器不能作为已有入口。
- 已有入口速率为正、不重复且必须在拓扑中。
- 除目的路由器与已有入口外，至少剩余 **2** 台路由器作为新入口候选。

### 响应

可行（`status=feasible`）：

```json
{
  "status": "feasible",
  "target": "T",
  "scenarios": ["normal", "down:P1", "down:P2"],
  "infeasible_reasons": [],
  "selected_entries": ["A", "B"],
  "entry_capacities": {"A": 50.0, "B": 50.0},
  "total_capacity": 100.0,
  "binding_constraints": [
    {"scenario": "down:P1", "kind": "saturated_link", "link_id": "L2", "entry": null},
    {"scenario": "down:P1", "kind": "unreachable_entry", "link_id": null, "entry": "A"}
  ],
  "scenario_reports": [ /* 每场景下一跳、移除链路、逐链路负载与满载标记 */ ]
}
```

- `binding_constraints` 标出**限制最终带宽取值**的场景与原因：
  `saturated_link`（满载链路）或 `unreachable_entry`（不可达入口）。
- `scenario_reports` 逐场景给出移除链路、各路由器等价下一跳、已有入口
  不可达/超载明细、入选新入口的不可达情况，以及选定方案下全部链路的
  负载、负载率与满载标记（按链路 id 排序）。

无可行方案（`status=infeasible`）：`selected_entries` 为空、
`total_capacity` 为 0，`infeasible_reasons` 按场景列出
`unreachable_existing`（送不到目的的已有入口）或 `existing_overload`
（已有流量即超载的链路及实际负载）。

示例请求：`examples/capacity_feasible.json`、`examples/capacity_infeasible.json`。

## 自测

```bash
pip install -r requirements.txt pytest httpx
python -m pytest -q
```

覆盖（`tests/test_api.py`）：同批错峰成环、等价路径并集成环、安全多批次、
第二批才成环、单批 50 台上限等输入校验、断链拒绝、单路由器拓扑、结果确定性，
以及 40 组随机拓扑的并集判环 vs 全状态暴力枚举对照。

覆盖（`tests/test_capacity.py`）：菱形拓扑手算对照、ECMP 均分与管道停用后
流量重分配、不可达新入口带宽置零、已有流量不可达/超载的无可行方案报告、
已有流量挤占新业务容量、二维 LP 求解器平局规则、输入校验、常态断链拒绝、
结果确定性与 OpenAPI 暴露，以及 25 组随机拓扑的分数最优 vs 整数带宽暴力枚举对照。

## 复杂度

`/precheck`：每批构造并集图 `O(V+E)` 后做一次 `O(V+E)` 的确定性找环，共 `B` 批；
两次 Dijkstra 为 `O((V+E) log V)`。整体约 `O((V+E) log V + B(V+E))`。

`/capacity-plan`：设场景数 `S = 1 + 管道数`、候选路由器数 `k`。每场景一次
Dijkstra `O((V+E) log V)` 加一次单位流量计算 `O(V+E)`；候选对 `O(k²)`，
每对求解二维 LP（约束条数 `O(S·E)`，顶点枚举为其平方量级）。单位流量以
`Fraction` 精确计算，典型机房拓扑（数十节点、数条管道）下离线运行无压力。
