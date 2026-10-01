"""管网故障隔离与复供的纯领域逻辑（不依赖数据库 / Web 框架）。

规则概述：
- 管网为无向图，节点分为水源(source)/用水点(sink)/连接点(junction)。
- 已关闭阀门的管段视为不可通行。
- 其他“待修复(open)”隔离事件的故障管段同样视为不可通行（故障仍在，不能通水）。
- 方案图 = 当前管网移除故障管段，再移除/关闭所有“拟关闭管段”，
  同时已关闭阀门、其他待修复故障管段继续保持阻断。
- 只有故障管段**两端**在方案图中都无法从任一水源到达，方案才可隔离(isolatable)。
- 失供用水点 = 方案前由任一水源可达、方案后不可达的 sink，按编号排序。

复供（restore）规则：
- 复供图 = 当前管网重新打开本事件关闭的阀门（从“仍关闭阀门”集合中剔除），
  再继续移除“其他待修复事件”的故障管段。
- 仅当重开后，其余每条待修复故障管段的**两端**都仍无法从水源到达，才允许复供。
- 预计恢复供水用水点 = 复供前不可达、复供图中可达的 sink，按编号排序。

关阀建议（suggest）规则：
- 操作员只提供故障管段编号，系统在当前管网上搜索“可直接提交”的关阀集合，
  候选仅限**尚未关闭的有阀管段**（故障管段本身、其他待修复事件的故障管段除外）。
- 方案优劣依次按：相对当前状态**新增失供用水点数最少** → **本次关阀数最少** →
  关阀编号排序后序列的**字典序最小**。
- 故障管段不存在、或即使关闭全部可用阀门仍无法断开其两端与所有水源的连接时，
  明确给出原因，不返回方案。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections import deque

# 问题代码 ---------------------------------------------------------------

# 阻断类问题：出现任一即不能隔离、提交时拒绝（422）
FAULT_PIPE_NOT_FOUND = "FAULT_PIPE_NOT_FOUND"
CLOSE_PIPE_NOT_FOUND = "CLOSE_PIPE_NOT_FOUND"
CLOSE_PIPE_NO_VALVE = "CLOSE_PIPE_NO_VALVE"
CLOSE_PIPE_DUPLICATE = "CLOSE_PIPE_DUPLICATE"
CLOSE_PIPE_IS_FAULT = "CLOSE_PIPE_IS_FAULT"
# 复供阻断：重开阀门后其他待修复故障管段有一端重新进水
FAULT_REWATER_RISK = "FAULT_REWATER_RISK"
# 导入期问题
DUPLICATE_NODE_ID = "DUPLICATE_NODE_ID"
DUPLICATE_PIPE_ID = "DUPLICATE_PIPE_ID"
PIPE_SELF_LOOP = "PIPE_SELF_LOOP"
PIPE_ENDPOINT_UNKNOWN = "PIPE_ENDPOINT_UNKNOWN"
NON_VALVE_PIPE_CLOSED = "NON_VALVE_PIPE_CLOSED"
# 关阀建议：关闭全部可用阀门后故障管段仍有一端可从水源到达
NO_FEASIBLE_PLAN = "NO_FEASIBLE_PLAN"

# 结构性阻断问题（预演会给出，提交据此拒绝）
BLOCKING_ISSUE_CODES = frozenset(
    {
        FAULT_PIPE_NOT_FOUND,
        CLOSE_PIPE_NOT_FOUND,
        CLOSE_PIPE_NO_VALVE,
        CLOSE_PIPE_DUPLICATE,
        CLOSE_PIPE_IS_FAULT,
    }
)


@dataclass(frozen=True)
class Node:
    id: str
    type: str  # source / sink / junction
    name: str | None = None


@dataclass(frozen=True)
class Pipe:
    id: str
    from_node: str
    to_node: str
    has_valve: bool = False
    valve_closed: bool = False
    name: str | None = None


@dataclass
class Issue:
    code: str
    message: str
    pipe_id: str | None = None

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "pipe_id": self.pipe_id}


@dataclass
class PlanResult:
    isolatable: bool
    affected_sink_ids: list[str]
    issues: list[Issue] = field(default_factory=list)
    # 方案中实际被阻断的管段（已关 ∪ 拟关 ∪ 待修复故障，故障管段单独移除）
    blocked_pipe_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass
class ReopenBlock:
    """复供阻断原因：某条其他待修复故障管段在重开阀门后有一端重新进水。"""

    fault_pipe_id: str
    reachable_endpoints: list[str]

    def as_dict(self) -> dict:
        return {
            "code": FAULT_REWATER_RISK,
            "message": (
                f"重开阀门后，待修复故障管段 {self.fault_pipe_id} 的 "
                f"{ '、'.join(self.reachable_endpoints) } 端将重新进水，禁止复供"
            ),
            "pipe_id": self.fault_pipe_id,
            "reachable_endpoints": list(self.reachable_endpoints),
        }


@dataclass
class RestoreResult:
    safe: bool
    restored_sink_ids: list[str]
    blocks: list[ReopenBlock] = field(default_factory=list)
    # 复供评估中仍不可通行的管段（其他待修复故障管段 ∪ 仍关闭阀门）
    blocked_pipe_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass
class SuggestionResult:
    """关阀建议结果。

    feasible=False 时 reason_code 说明原因（FAULT_PIPE_NOT_FOUND / NO_FEASIBLE_PLAN），
    close_pipe_ids 与 affected_sink_ids 均为空。
    feasible=True 时 close_pipe_ids 按编号排序，可直接用于隔离提交。
    """

    feasible: bool
    close_pipe_ids: list[str]
    affected_sink_ids: list[str]
    reason_code: str | None = None
    reason_message: str | None = None
    explored_nodes: int = 0


class SuggestionLimitExceeded(Exception):
    """建议搜索触达节点数上限（极端网状管网的保护措施，不代表不存在方案）。"""


# ---------------------------------------------------------------- 导入校验


def validate_import(
    nodes: list[Node], pipes: list[Pipe]
) -> list[Issue]:
    """校验整网导入数据：重号、自环、悬空端点、无阀管段初始关闭。"""
    issues: list[Issue] = []

    node_ids: set[str] = set()
    for n in nodes:
        if n.id in node_ids:
            issues.append(Issue(DUPLICATE_NODE_ID, f"节点编号重复：{n.id}", n.id))
        node_ids.add(n.id)

    pipe_ids: set[str] = set()
    for p in pipes:
        if p.id in pipe_ids:
            issues.append(Issue(DUPLICATE_PIPE_ID, f"管段编号重复：{p.id}", p.id))
        pipe_ids.add(p.id)

        if p.from_node == p.to_node:
            issues.append(Issue(PIPE_SELF_LOOP, f"管段自环（两端为同一节点）：{p.id}", p.id))
        for endpoint in (p.from_node, p.to_node):
            if endpoint not in node_ids:
                issues.append(
                    Issue(
                        PIPE_ENDPOINT_UNKNOWN,
                        f"管段 {p.id} 的端点 {endpoint} 未在节点列表中定义（悬空端点）",
                        p.id,
                    )
                )
        if p.valve_closed and not p.has_valve:
            issues.append(
                Issue(
                    NON_VALVE_PIPE_CLOSED,
                    f"管段 {p.id} 没有阀门，不能设置为关闭状态",
                    p.id,
                )
            )
    return issues


# ---------------------------------------------------------------- 可达性


def _build_adjacency(
    nodes: dict[str, Node], pipes: list[Pipe]
) -> dict[str, list[tuple[str, str]]]:
    """构建无向邻接表：节点 -> (对端节点, 管段编号)。"""
    adjacency: dict[str, list[tuple[str, str]]] = {nid: [] for nid in nodes}
    for p in pipes:
        if p.from_node not in adjacency or p.to_node not in adjacency:
            continue
        adjacency[p.from_node].append((p.to_node, p.id))
        adjacency[p.to_node].append((p.from_node, p.id))
    return adjacency


def _reachable_from(
    adjacency: dict[str, list[tuple[str, str]]],
    start_nodes,
    removed_pipe_ids: frozenset[str],
) -> set[str]:
    """无向 BFS：从给定起点集合出发，返回可达节点集合。

    removed_pipe_ids 中的管段视为不存在/不可通行。
    """
    reachable: set[str] = set()
    queue: deque[str] = deque()
    for nid in start_nodes:
        if nid not in reachable:
            reachable.add(nid)
            queue.append(nid)

    while queue:
        current = queue.popleft()
        for nxt, pipe_id in adjacency.get(current, ()):
            if pipe_id in removed_pipe_ids:
                continue
            if nxt not in reachable:
                reachable.add(nxt)
                queue.append(nxt)
    return reachable


def _reachable(
    nodes: dict[str, Node],
    pipes: list[Pipe],
    removed_pipe_ids: frozenset[str],
) -> set[str]:
    """无向 BFS：从全部水源出发，返回可达节点集合。

    removed_pipe_ids 中的管段视为不存在/不可通行。
    """
    adjacency = _build_adjacency(nodes, pipes)
    sources = [nid for nid, node in nodes.items() if node.type == "source"]
    return _reachable_from(adjacency, sources, removed_pipe_ids)


def _shortest_valve_path(
    adjacency: dict[str, list[tuple[str, str]]],
    sources: list[str],
    targets: list[str],
    blocked_pipe_ids: frozenset[str],
    weighted_pipe_ids: frozenset[str],
) -> tuple[list[str], str, int] | None:
    """多源 0-1 BFS：找“额外需关闭阀门数最少”的水源→故障端点路径。

    - blocked_pipe_ids 中的边视为不可通行（已关阀门/待修复故障/已选阀门）；
    - weighted_pipe_ids（尚未关闭的候选阀门）上的边代价为 1，其余可通行边代价为 0；
    - 返回 (路径管段编号序列, 到达目标, 路径代价)；目标在 blocked 之外完全不可达时
      返回 None（该端点已被断开，无需再处理）。
    同代价目标按节点编号字典序打破，保证结果确定。
    """
    distances: dict[str, int] = {}
    parents: dict[str, tuple[str, str]] = {}
    queue: deque[str] = deque()
    for src in sources:
        if src not in distances:
            distances[src] = 0
            queue.append(src)
    target_set = set(targets)

    found_target: str | None = None
    while queue:
        current = queue.popleft()
        if current in target_set and (
            found_target is None
            or (distances[current], current)
            < (distances[found_target], found_target)
        ):
            found_target = current
        d = distances[current]
        # 邻接按 (对端编号, 管段编号) 排序，使同代价扩展确定
        for nxt, pipe_id in sorted(adjacency.get(current, ())):
            if pipe_id in blocked_pipe_ids:
                continue
            nd = d + (1 if pipe_id in weighted_pipe_ids else 0)
            if nd < distances.get(nxt, 10**18):
                distances[nxt] = nd
                parents[nxt] = (current, pipe_id)
                if nd == d:
                    queue.appendleft(nxt)
                else:
                    queue.append(nxt)

    if found_target is None:
        return None

    source_set = set(sources)
    path_pipes: list[str] = []
    node = found_target
    while node not in source_set:
        prev, pipe_id = parents[node]
        path_pipes.append(pipe_id)
        node = prev
    path_pipes.reverse()
    return path_pipes, found_target, distances[found_target]


class _Dinic:
    """最小割用 Dinic 最大流（节点为字符串编号）。

    候选阀门管段容量为 1，其他可通行管段容量为 INF；blocked 管段不建边。
    无向容量 1 边通过“两条相互独立的有向容量 1 弧”表达（割一条即计一次）。
    """

    INF = 10**9

    def __init__(self, node_ids):
        self.graph = {nid: [] for nid in node_ids}

    def _add(self, u, v, cap, reverse_cap: int = 0):
        self.graph[u].append([v, cap, len(self.graph[v])])
        self.graph[v].append([u, reverse_cap, len(self.graph[u]) - 1])

    def add_undirected(self, u, v, cap):
        # 无向容量边：正反初始容量都为 cap，共用一对互为反向的弧，
        # 等价于标准的“无向单位容量”网络（割该边只计一次容量）。
        self._add(u, v, cap, cap)

    def max_flow_and_reachable(self, source: str, sink: str):
        flow = 0
        while True:
            level = {source: 0}
            queue: deque[str] = deque([source])
            while queue:
                u = queue.popleft()
                for v, cap, _rev in self.graph[u]:
                    if cap > 0 and v not in level:
                        level[v] = level[u] + 1
                        queue.append(v)
            if sink not in level:
                reachable = set(level)
                return flow, reachable
            it = {u: 0 for u in self.graph}

            def dfs(u: str, pushed: int) -> int:
                if u == sink:
                    return pushed
                edges = self.graph[u]
                while it[u] < len(edges):
                    v, cap, rev = edges[it[u]]
                    if cap > 0 and level.get(v) == level[u] + 1:
                        tr = dfs(v, min(pushed, cap))
                        if tr:
                            edges[it[u]][1] -= tr
                            self.graph[v][rev][1] += tr
                            return tr
                    it[u] += 1
                return 0

            while True:
                pushed = dfs(source, self.INF)
                if not pushed:
                    break
                flow += pushed


def _min_valve_cut(
    adjacency: dict[str, list[tuple[str, str]]],
    sources: list[str],
    target: str,
    blocked_pipe_ids: frozenset[str],
    weighted_pipe_ids: frozenset[str],
    pipe_by_id: dict[str, Pipe],
) -> tuple[int, frozenset[str]]:
    """从“所有水源”到单个目标节点的最小阀门割。

    返回 (割容量（候选阀数）, 割集中的候选管段编号)。
    目标在 blocked 之外不可达时容量为 0、割集为空。
    """
    super_source = "__super_source__"
    node_ids = list(adjacency) + [super_source]
    dinic = _Dinic(node_ids)
    for src in sources:
        # 超源 -> 水源：不可被割
        dinic._add(super_source, src, _Dinic.INF)
    for pipe_id in pipe_by_id:
        if pipe_id in blocked_pipe_ids:
            continue
        p = pipe_by_id[pipe_id]
        if p.from_node not in adjacency or p.to_node not in adjacency:
            continue
        cap = 1 if pipe_id in weighted_pipe_ids else _Dinic.INF
        dinic.add_undirected(p.from_node, p.to_node, cap)
    target_reachable = target in _reachable_from(adjacency, sources, blocked_pipe_ids)
    capacity, reachable = dinic.max_flow_and_reachable(super_source, target)
    if not target_reachable:
        return 0, frozenset()
    cut: set[str] = set()
    for u in reachable:
        for v, pipe_id in adjacency.get(u, ()):
            if (
                v not in reachable
                and pipe_id in weighted_pipe_ids
                and pipe_id not in blocked_pipe_ids
            ):
                cut.add(pipe_id)
    return capacity, frozenset(cut)


def _min_valve_cut_joint(
    adjacency: dict[str, list[tuple[str, str]]],
    sources: list[str],
    targets: list[str],
    blocked_pipe_ids: frozenset[str],
    weighted_pipe_ids: frozenset[str],
    pipe_by_id: dict[str, Pipe],
) -> int:
    """从所有水源到“任一目标”的最小阀门割容量（超汇点接 INF）。

    割必须同时把所有 target 与所有 source 断开，返回所需候选阀数下界。
    任一目标在 blocked 之外本就不可达时忽略该目标；若全部不可达返回 0。
    """
    reachable_now = _reachable_from(adjacency, sources, blocked_pipe_ids)
    live_targets = [t for t in targets if t in reachable_now]
    if not live_targets:
        return 0
    super_source = "__ss__"
    super_sink = "__tt__"
    node_ids = list(adjacency) + [super_source, super_sink]
    dinic = _Dinic(node_ids)
    for src in sources:
        dinic._add(super_source, src, _Dinic.INF)
    for tgt in live_targets:
        dinic._add(tgt, super_sink, _Dinic.INF)
    for pipe_id, p in pipe_by_id.items():
        if pipe_id in blocked_pipe_ids:
            continue
        if p.from_node not in adjacency or p.to_node not in adjacency:
            continue
        cap = 1 if pipe_id in weighted_pipe_ids else _Dinic.INF
        dinic.add_undirected(p.from_node, p.to_node, cap)
    capacity, _reachable = dinic.max_flow_and_reachable(super_source, super_sink)
    return capacity


# ---------------------------------------------------------------- 方案评估


def validate_plan_request(
    pipes: dict[str, Pipe],
    fault_pipe_id: str,
    close_pipe_ids: list[str],
    *,
    for_submit: bool,
) -> list[Issue]:
    """校验故障/拟关列表本身的合法性。

    for_submit=True 时额外校验“已关闭阀门不得重复关闭”。
    """
    issues: list[Issue] = []

    if fault_pipe_id not in pipes:
        issues.append(
            Issue(FAULT_PIPE_NOT_FOUND, f"故障管段不存在：{fault_pipe_id}", fault_pipe_id)
        )

    seen: set[str] = set()
    for pid in close_pipe_ids:
        if pid in seen:
            issues.append(Issue(CLOSE_PIPE_DUPLICATE, f"拟关闭管段重复：{pid}", pid))
            continue
        seen.add(pid)

        if pid == fault_pipe_id:
            issues.append(
                Issue(CLOSE_PIPE_IS_FAULT, "不能把故障管段本身作为关闭阀门", pid)
            )
            continue
        pipe = pipes.get(pid)
        if pipe is None:
            issues.append(Issue(CLOSE_PIPE_NOT_FOUND, f"拟关闭管段不存在：{pid}", pid))
        elif not pipe.has_valve:
            issues.append(Issue(CLOSE_PIPE_NO_VALVE, f"管段没有阀门，无法关闭：{pid}", pid))
        elif for_submit and pipe.valve_closed:
            issues.append(
                Issue(
                    CLOSE_PIPE_DUPLICATE,
                    f"阀门已处于关闭状态，不能重复关闭：{pid}",
                    pid,
                )
            )
    return issues


def evaluate_plan(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    fault_pipe_id: str,
    close_pipe_ids: list[str],
    *,
    for_submit: bool = False,
    pending_fault_pipe_ids: frozenset[str] = frozenset(),
) -> PlanResult:
    """评估隔离方案。

    预演(for_submit=False)：只做结构校验，已关闭阀门不会产生“重复关闭”问题；
    提交(for_submit=True)：对已关闭阀门再次关闭视为 CLOSE_PIPE_DUPLICATE。

    pending_fault_pipe_ids：其他“待修复”隔离事件的故障管段编号集合，
    无论方案前/方案后都视为不可通行（故障未修复，不能通水）。
    """
    issues = validate_plan_request(
        pipes, fault_pipe_id, close_pipe_ids, for_submit=for_submit
    )
    blocking = [i for i in issues if i.code in BLOCKING_ISSUE_CODES]

    fault = pipes.get(fault_pipe_id)
    if fault is None or blocking:
        # 结构不合法：无法进行可达性评估
        return PlanResult(
            isolatable=False, affected_sink_ids=[], issues=issues
        )

    pipe_list = list(pipes.values())
    pending_faults = frozenset(pid for pid in pending_fault_pipe_ids if pid in pipes)

    # 方案前（当前态）：已关闭阀门、其他待修复故障管段不可通行，本次故障尚未移除
    already_closed = frozenset(p.id for p in pipe_list if p.has_valve and p.valve_closed)
    base_removed = already_closed | pending_faults
    before_reachable = _reachable(nodes, pipe_list, base_removed)

    # 方案后：故障管段移除；已关阀门、其他待修复故障继续生效；拟关阀门全部关闭
    blocked = base_removed | frozenset(close_pipe_ids)
    after_removed = blocked | frozenset({fault_pipe_id})
    after_reachable = _reachable(nodes, pipe_list, after_removed)

    both_ends_isolated = (
        fault.from_node not in after_reachable
        and fault.to_node not in after_reachable
    )

    affected = sorted(
        nid
        for nid, node in nodes.items()
        if node.type == "sink" and nid in before_reachable and nid not in after_reachable
    )

    return PlanResult(
        isolatable=both_ends_isolated,
        affected_sink_ids=affected,
        issues=issues,
        blocked_pipe_ids=frozenset(blocked),
    )


# ---------------------------------------------------------------- 复供评估


def evaluate_restore(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    reopen_pipe_ids: frozenset[str],
    other_fault_pipe_ids: frozenset[str],
) -> RestoreResult:
    """评估复供安全性（只读，不改任何状态）。

    - reopen_pipe_ids：本事件提交时关闭的阀门管段，复供后重新通水；
    - other_fault_pipe_ids：其他“待修复”事件的故障管段，继续视为不可通行。

    仅当其余待修复故障管段的两端在复供图中仍均不可从水源到达，safe=True。
    预计恢复供水用水点 = 复供前不可达、复供后可达的 sink。
    """
    pipe_list = list(pipes.values())
    already_closed = frozenset(p.id for p in pipe_list if p.has_valve and p.valve_closed)
    other_faults = frozenset(
        pid for pid in other_fault_pipe_ids if pid in pipes
    )

    # 复供前（当前态）：仍关闭阀门 + 其他待修复故障管段不可通行
    before_removed = already_closed | other_faults
    before_reachable = _reachable(nodes, pipe_list, before_removed)

    # 复供后：本事件阀门重开（从阻断集合剔除）；其他待修复故障仍然阻断
    after_removed = (already_closed - frozenset(reopen_pipe_ids)) | other_faults
    after_reachable = _reachable(nodes, pipe_list, after_removed)

    blocks: list[ReopenBlock] = []
    for fid in sorted(other_faults):
        fault = pipes[fid]
        reachable_ends = [
            end
            for end in (fault.from_node, fault.to_node)
            if end in after_reachable
        ]
        if reachable_ends:
            blocks.append(
                ReopenBlock(fault_pipe_id=fid, reachable_endpoints=reachable_ends)
            )

    restored = sorted(
        nid
        for nid, node in nodes.items()
        if node.type == "sink" and nid not in before_reachable and nid in after_reachable
    )

    return RestoreResult(
        safe=not blocks,
        restored_sink_ids=restored,
        blocks=blocks,
        blocked_pipe_ids=frozenset(after_removed),
    )


# ---------------------------------------------------------------- 关阀建议


def suggest_plan(
    nodes: dict[str, Node],
    pipes: dict[str, Pipe],
    fault_pipe_id: str,
    *,
    pending_fault_pipe_ids: frozenset[str] = frozenset(),
    max_nodes: int = 100_000,
) -> SuggestionResult:
    """只读计算故障管段的最优关阀建议（不改任何状态）。

    候选阀门 = 当前**尚未关闭**的有阀管段；故障管段本身、其他待修复事件的
    故障管段均不可关闭（且在通水计算中视为不可通行）。

    方案排序：新增失供用水点数（相对当前态）最少 → 本次关阀数最少 →
    关阀编号序列字典序最小。

    故障管段不存在返回 FAULT_PIPE_NOT_FOUND；关闭全部候选阀门后故障管段
    仍有一端可从水源到达，返回 NO_FEASIBLE_PLAN。
    搜索触达 max_nodes 个分支节点时抛 SuggestionLimitExceeded。
    """
    fault = pipes.get(fault_pipe_id)
    if fault is None:
        return SuggestionResult(
            feasible=False,
            close_pipe_ids=[],
            affected_sink_ids=[],
            reason_code=FAULT_PIPE_NOT_FOUND,
            reason_message=f"故障管段不存在：{fault_pipe_id}",
        )

    pipe_list = list(pipes.values())
    adjacency = _build_adjacency(nodes, pipe_list)
    sources = sorted(nid for nid, node in nodes.items() if node.type == "source")
    sink_ids = sorted(nid for nid, node in nodes.items() if node.type == "sink")

    already_closed = frozenset(
        p.id for p in pipe_list if p.has_valve and p.valve_closed
    )
    pending_faults = frozenset(pid for pid in pending_fault_pipe_ids if pid in pipes)

    # 当前态（方案前）：已关阀门 + 其他待修复故障管段不可通行，本次故障仍在
    base_removed = already_closed | pending_faults
    before_reachable = _reachable_from(adjacency, sources, base_removed)

    # 候选阀门：尚未关闭、有阀，且不在故障/待修复故障集合内
    candidate_ids = sorted(
        p.id
        for p in pipe_list
        if p.has_valve
        and not p.valve_closed
        and p.id != fault_pipe_id
        and p.id not in pending_faults
    )
    candidate_set = frozenset(candidate_ids)

    def reachable_with(closed_extra: frozenset[str]) -> set[str]:
        return _reachable_from(adjacency, sources, base_removed | closed_extra)

    def is_isolated(reachable: set[str]) -> bool:
        return (
            fault.from_node not in reachable
            and fault.to_node not in reachable
        )

    def affected_of(reachable: set[str]) -> list[str]:
        return [
            nid for nid in sink_ids if nid in before_reachable and nid not in reachable
        ]

    explored = 0

    def tick() -> None:
        nonlocal explored
        explored += 1
        if explored > max_nodes:
            raise SuggestionLimitExceeded(
                f"关阀建议搜索超过节点上限 {max_nodes}，请收窄管网或联系管理员"
            )

    # 1) 空方案即可隔离（故障管段两端在当前态已与所有水源断开）
    if is_isolated(before_reachable):
        return SuggestionResult(
            feasible=True,
            close_pipe_ids=[],
            affected_sink_ids=[],
            explored_nodes=explored,
        )

    # 2) 关闭全部候选阀门仍隔离不了 => 无可行方案（故障管段始终另行移除）
    all_closed = frozenset(candidate_ids)
    reach_all = reachable_with(all_closed | frozenset({fault_pipe_id}))
    if not is_isolated(reach_all):
        return SuggestionResult(
            feasible=False,
            close_pipe_ids=[],
            affected_sink_ids=[],
            reason_code=NO_FEASIBLE_PLAN,
            reason_message=(
                f"即使关闭当前全部尚未关闭的有阀管段，故障管段 {fault_pipe_id} "
                f"仍有一端可从水源到达，无可提交方案"
            ),
            explored_nodes=explored,
        )

    # 3) 初始可行上界：
    #    (a) 两个故障端各自最小割（按候选阀计权）的并集——天然可行且割集小；
    #    (b) 贴故障端贪心：每步沿“最少候选阀路径”选阀，优先切断故障端、
    #        其次失供最少，并列取编号最小。

    def key_of(chosen: frozenset[str], reach: set[str]) -> tuple:
        return (len(affected_of(reach)), len(chosen), sorted(chosen))

    best = None

    def register(chosen_set: frozenset[str]):
        nonlocal best
        reach = reachable_with(chosen_set | frozenset({fault_pipe_id}))
        if is_isolated(reach):
            key = key_of(chosen_set, reach)
            if best is None or key < best[0]:
                best = (key, chosen_set, reach)

    # (a) 最小割并集
    cut_union: set[str] = set()
    for end in (fault.from_node, fault.to_node):
        _cap, cut_ids = _min_valve_cut(
            adjacency, sources, end,
            base_removed | frozenset({fault_pipe_id}),
            candidate_set, pipes,
        )
        cut_union.update(cut_ids)
    register(frozenset(cut_union))

    # (b) 贴故障端贪心
    chosen: set[str] = set()
    remaining = set(candidate_ids)
    reach = reachable_with(frozenset({fault_pipe_id}))
    while not is_isolated(reach):
        hot = sorted(e for e in (fault.from_node, fault.to_node) if e in reach)
        found = _shortest_valve_path(
            adjacency, sources, hot,
            base_removed | frozenset(chosen) | frozenset({fault_pipe_id}),
            remaining,
        )
        if found is None:
            break
        path_pipes, _target, _cost = found
        on_path = sorted(pid for pid in path_pipes if pid in remaining)
        if not on_path:
            break
        best_pid = None
        best_score = None
        for pid in on_path:
            r = reachable_with(
                frozenset(chosen) | frozenset({pid, fault_pipe_id})
            )
            cut_ends = sum(1 for e in hot if e not in r)
            new_affected = sum(
                1 for nid in sink_ids
                if nid in before_reachable and nid not in r
            )
            score = (-cut_ends, new_affected, pid)
            if best_score is None or score < best_score:
                best_score, best_pid = score, pid
        chosen.add(best_pid)
        remaining.discard(best_pid)
        reach = reachable_with(
            frozenset(chosen) | frozenset({fault_pipe_id})
        )
    register(frozenset(chosen))

    # 兜底：关闭全部候选一定可行（第 2 步已验证）
    register(all_closed)
    best_key, best_set, best_reach = best

    # 4) 精确搜索，分两阶段，均为只读：
    #    阶段一：求最优 (失供数 A*, 关阀数 K*)。同层并列立即整枝（不枚举并列解），
    #            并对每个阀门集合做记忆化，快速把最优 (A*, K*) 钉死；
    #    阶段二：固定 A<=A* 且关阀数 <=K*，沿最少候选阀路径按编号升序做
    #            字典序优先 DFS，首个可行完成集即 (A*, K*) 下字典序最小者。
    def eval_state(chosen: frozenset[str]):
        blocked = base_removed | chosen | frozenset({fault_pipe_id})
        reach = _reachable_from(adjacency, sources, blocked)
        return reach, affected_of(reach)

    def branch_valves(chosen: frozenset[str], reach: set[str]) -> list[str]:
        """水源到仍可达故障端的“最少候选阀路径”上，本次还能关的阀门。

        任何可行完成集都必须再关其中至少一个，故只需在这些阀门上分支。
        """
        hot = [e for e in (fault.from_node, fault.to_node) if e in reach]
        found = _shortest_valve_path(
            adjacency, sources, hot,
            base_removed | chosen | frozenset({fault_pipe_id}),
            candidate_set - chosen,
        )
        if found is None:
            return []
        path_pipes, _target, cost = found
        if cost == 0:
            return []  # 存在无阀通路，永远隔离不了
        return sorted(pid for pid in path_pipes if pid in candidate_set - chosen)

    # ---- 阶段一：钉死 (A*, K*) ----
    # memo_bad 只记忆“与不断收紧的最优值无关”的安全死状态：
    # 失供已超上界、存在无阀通路（永不可隔离）、分支集合为空。
    # 依赖 best_a/best_k 的整枝不做记忆化（最优值收紧后旧判定可能失效）。
    memo_bad: set[frozenset[str]] = set()
    best_a = best_key[0]
    best_k = best_key[1]

    def forced_affected_lb(reach: set[str]) -> int:
        """失供下界：与“仍可达的故障端”经不含候选阀门、不含故障管段的路径相连，
        且当前仍在供水的用水点数。

        这类用水点与故障端处于同一个“没有候选阀门”的连通域：要把它们与故障端
        分开，只能关掉候选阀，而该域内没有候选阀可关；若让它们继续供水，水源
        必能到达故障端，与隔离矛盾。故任何可行完成集都必然令其失供。
        """
        hot = [
            e for e in (fault.from_node, fault.to_node) if e in reach
        ]
        if not hot:
            return 0
        impassable = base_removed | candidate_set | frozenset({fault_pipe_id})
        zone = _reachable_from(adjacency, hot, impassable)
        return sum(
            1
            for nid in sink_ids
            if nid in zone and nid in before_reachable and nid in reach
        )

    def optimize(chosen: frozenset[str]) -> None:
        nonlocal best_a, best_k
        if chosen in memo_bad:
            return
        tick()
        reach, affected = eval_state(chosen)
        n_aff = len(affected)
        # 失供只会随关阀单调增加；一旦超过初始上界失供，任何扩展都更差（安全记忆）
        if n_aff > best_key[0]:
            memo_bad.add(chosen)
            return
        if is_isolated(reach):
            if n_aff < best_a or (n_aff == best_a and len(chosen) < best_k):
                best_a, best_k = n_aff, len(chosen)
            # 可行解无需再扩展；其可行性与最优值无关，可安全记忆
            memo_bad.add(chosen)
            return

        remaining = candidate_set - chosen

        # 失供下界：任何完成集至少还会让 forced 个当前供水用水点失供。
        # 这里用“初始上界失供”做安全上界（单调，不随搜索收紧），命中即可记忆。
        forced = forced_affected_lb(reach)
        if n_aff + forced > best_key[0]:
            memo_bad.add(chosen)
            return

        branches = branch_valves(chosen, reach)
        if not branches:
            # 无候选阀可阻断进水路径（含无阀通路）：永不可隔离（安全记忆）
            memo_bad.add(chosen)
            return

        # 阀数下界：必须同时断开两个仍可达的故障端（联合最小割）
        live_ends = [
            e for e in (fault.from_node, fault.to_node) if e in reach
        ]
        lb = _min_valve_cut_joint(
            adjacency, sources, live_ends,
            base_removed | chosen | frozenset({fault_pipe_id}),
            remaining, pipes,
        )
        # 关键整枝（依赖当前 best_a/best_k，只剪枝不记忆）：失供已不可能更优时，
        # 完成集阀数还必须严格小于当前最优阀数
        if n_aff >= best_a and len(chosen) + lb >= best_k:
            return

        for pid in branches:
            optimize(chosen | frozenset({pid}))

    optimize(frozenset())

    # 若阶段一刷新了更优 (A*, K*)，重算对应结果集用于兜底
    if (best_a, best_k) != best_key[:2]:
        best = None

    # ---- 阶段二：字典序优先 DFS，钉死字典序最小的最优方案 ----
    # 为保证字典序完备，必须在“全部剩余候选阀”上按编号升序分支
    # （阶段一只沿最短路径分支只能保证找到某个最优解，会漏掉字典序更小的并列解）。
    seen_bad: set[tuple[frozenset[str], int]] = set()
    sorted_candidates = candidate_ids  # 构造时已排序

    def lex_search(chosen: frozenset[str], start: int = 0):
        key = (chosen, start)
        if key in seen_bad:
            return None
        tick()
        reach, affected = eval_state(chosen)
        n_aff = len(affected)
        if n_aff > best_a:
            seen_bad.add(key)
            return None
        if is_isolated(reach):
            if n_aff == best_a and len(chosen) == best_k:
                return chosen
            seen_bad.add(key)
            return None
        slots = best_k - len(chosen)
        if slots <= 0:
            seen_bad.add(key)
            return None

        # 失供下界（与阶段一同款）：若必然超 best_a，整枝
        if n_aff + forced_affected_lb(reach) > best_a:
            seen_bad.add(key)
            return None

        # 只在编号严格递增的候选后缀上分支（组合枚举，天然去重、字典序优先）；
        # 后缀长度不足剩余名额时直接整枝
        for i in range(start, len(sorted_candidates)):
            if len(sorted_candidates) - i < slots:
                break
            pid = sorted_candidates[i]
            answer = lex_search(chosen | frozenset({pid}), i + 1)
            if answer is not None:
                return answer
        seen_bad.add(key)
        return None

    final_set = lex_search(frozenset())
    if final_set is None:
        # 理论不应发生（阶段一已证明存在 (best_a, best_k) 解）；回退到初始上界
        final_set = best_set
    final_reach = reachable_with(final_set | frozenset({fault_pipe_id}))

    best_sorted = sorted(final_set)
    return SuggestionResult(
        feasible=True,
        close_pipe_ids=best_sorted,
        affected_sink_ids=affected_of(final_reach),
        explored_nodes=explored,
    )

