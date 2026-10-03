"""样式层级调整迁移规划。

在一棵 DOM 样本树上枚举现有分层的全部可行声明顺序，按层先后约束过滤后，
对每种顺序复用整树分析内核重新计算整棵树。important 声明的反向层序不在约束
中额外处理，而由层叠引擎在每次复算时按 CSS 规则裁决。

保护项用于保证指定节点的某个属性签名不变（胜出声明、状态、取值与失效原因）。
方案选择先最小化相对当前层序变化的「节点 + 属性」签名数，再按层序字典序去重。
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import permutations

from .analysis import NodeAnalysis, RuleSet, TreeAnalysis, solve_forest
from .audit import AuditValidationError, flatten_tree, validate_rules
from .dom_impact import _diff_node
from .models import (
    LayerConstraint,
    LayerConstraintOut,
    LayerGuardOut,
    LayerMigrationNodeOut,
    LayerMigrationRequest,
    LayerMigrationResponse,
    LayerOrderImpactOut,
    VariableEvidenceOut,
)
from .variables import VarEvent


class LayerMigrationValidationError(AuditValidationError):
    """层级迁移请求非法（整次请求 422）。"""


@dataclass(frozen=True)
class GuardTarget:
    index: int
    node_index: int
    property: str
    is_custom: bool


# ---------- 校验 ----------


def _tree_paths(root) -> tuple[tuple[object, ...], ...]:
    """返回先序每个节点的根 -> 本节点原始 id 路径。"""
    paths: list[tuple[object, ...]] = []

    def walk(node, path: tuple[object, ...]) -> None:
        current_path = path + (node.id,)
        paths.append(current_path)
        for child in node.children:
            walk(child, current_path)

    walk(root, ())
    return tuple(paths)


def _validate_rule_layers(req: LayerMigrationRequest) -> None:
    layers = set(req.layers)
    for ri, rule in enumerate(req.rules):
        if rule.layer is not None and rule.layer not in layers:
            raise LayerMigrationValidationError(
                f"rules[{ri}] 引用了未在 layers 中声明的层：{rule.layer!r}"
            )


def _validate_constraints(req: LayerMigrationRequest) -> None:
    layers = set(req.layers)
    for ci, constraint in enumerate(req.constraints):
        pair = (constraint.earlier_layer, constraint.later_layer)
        if pair[0] not in layers or pair[1] not in layers:
            unknown = pair[0] if pair[0] not in layers else pair[1]
            raise LayerMigrationValidationError(
                f"constraints[{ci}] 引用了未知层：{unknown!r}"
            )
        if pair[0] == pair[1]:
            raise LayerMigrationValidationError(
                f"constraints[{ci}] 非法：层 {pair[0]!r} 不能要求排在自身之前"
            )


def _signature_for(node: NodeAnalysis, prop: str, is_custom: bool):
    signatures = node.signature()
    table = dict(signatures.custom if is_custom else signatures.normal)
    return table.get(prop)


def _validate_guards(
    req: LayerMigrationRequest,
    baseline: TreeAnalysis,
    node_indexes: dict[object, int],
) -> list[GuardTarget]:
    targets: list[GuardTarget] = []
    for gi, guard in enumerate(req.guards):
        node_index = node_indexes.get(guard.node_id)
        if node_index is None:
            raise LayerMigrationValidationError(
                f"guards[{gi}] 的保护节点 id {guard.node_id!r} 不存在"
            )

        prop = guard.property.strip()
        is_custom = prop.startswith("--")
        normalized_prop = prop if is_custom else prop.lower()
        if guard.result == "custom_properties":
            if not is_custom:
                raise LayerMigrationValidationError(
                    f"guards[{gi}] 非法：custom_properties 保护项的属性必须以 '--' 开头"
                )
            exists = prop in baseline.nodes[node_index].customs
        else:
            if is_custom:
                raise LayerMigrationValidationError(
                    f"guards[{gi}] 非法：自定义属性 {prop!r} 不能作为 "
                    f"{guard.result} 保护项"
                )
            sig = _signature_for(baseline.nodes[node_index], normalized_prop, False)
            exists = sig is not None and sig.status == guard.result

        if not exists:
            raise LayerMigrationValidationError(
                f"guards[{gi}] 不存在：节点 {guard.node_id!r} 当前没有 "
                f"{guard.result} 结果 {prop!r}"
            )
        targets.append(
            GuardTarget(
                index=gi,
                node_index=node_index,
                property=normalized_prop,
                is_custom=(guard.result == "custom_properties"),
            )
        )
    return targets


# ---------- 约束 ----------


def _first_cycle_constraint(constraints: list[LayerConstraint], layers: list[str]):
    """按下标逐条加入有向边，返回首个使图成环的约束。"""
    graph: dict[str, set[str]] = {layer: set() for layer in layers}
    for ci, constraint in enumerate(constraints):
        graph[constraint.earlier_layer].add(constraint.later_layer)
        if _reaches(graph, constraint.later_layer, constraint.earlier_layer):
            return ci
    return None


def _reaches(graph: dict[str, set[str]], start: str, target: str) -> bool:
    seen: set[str] = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        stack.extend(graph.get(current, ()))
    return False


def _satisfies_constraints(order: tuple[str, ...], constraints: list[LayerConstraint]) -> bool:
    positions = {layer: i for i, layer in enumerate(order)}
    return all(
        positions[c.earlier_layer] < positions[c.later_layer] for c in constraints
    )


# ---------- 差异与证据 ----------


def _node_changes(before: NodeAnalysis, after: NodeAnalysis, rules) -> list:
    return _diff_node(before, after, rules)


def _guard_change(
    baseline: TreeAnalysis,
    candidate: TreeAnalysis,
    target: GuardTarget,
    rules,
):
    changes = _node_changes(
        baseline.nodes[target.node_index],
        candidate.nodes[target.node_index],
        rules,
    )
    for change in changes:
        if change.property == target.property and change.is_custom == target.is_custom:
            return change
    return None



def _format_var_event(event: VarEvent) -> list[str]:
    if event.kind == "ok":
        return [f"var({event.name}) 引用有效，替换为 {event.value!r}"]
    if event.kind == "fallback":
        return [
            f"var({event.name}) 引用状态为 {event.ref_status}，改走 fallback，"
            f"得到 {event.value!r}"
        ]
    return [f"var({event.name}) 引用状态为 {event.ref_status}，且没有 fallback"]


def _custom_variable_lines(
    analysis: NodeAnalysis, paths: tuple[tuple[object, ...], ...], node_index: int, name: str
) -> list[str]:
    """沿根 -> 目标链描述一个自定义属性的声明、继承与 fallback。"""
    lines: list[str] = []
    chain_path = paths[node_index]
    if not any(name in data.resolved for data in analysis.depths):
        return [f"节点 {chain_path[-1]!r} 的祖先链上不存在 {name}"]
    for depth, data in enumerate(analysis.depths):
        node_id = chain_path[depth]
        if name in data.winners:
            winner, _ = data.winners[name]
            status, value = data.resolved[name]
            layer = winner.layer if winner.layer is not None else "未分层"
            lines.append(
                f"节点 {node_id!r} 本元素胜出 {name}：规则 {winner.rule_index} "
                f"第 {winner.decl_index} 条，层 {layer!r}，原始值 {winner.value!r}，"
                f"状态 {status}，解析值 {value!r}"
            )
            for event in data.events.get(name, ()):
                lines.extend(f"节点 {node_id!r}：{line}" for line in _var_event_lines(event))
            if status == "invalid" and data.notes.get(name):
                lines.append(f"节点 {node_id!r}：{data.notes[name]}")
        elif name in data.resolved:
            status, value = data.resolved[name]
            lines.append(
                f"节点 {node_id!r} 本元素未声明 {name}，继承状态为 {status}，"
                f"值为 {value!r}"
            )
    return lines


def _var_event_lines(event: VarEvent) -> list[str]:
    """从 var() 事件生成主分支 / fallback 证据（失败事件也保留）。"""
    lines = _format_var_event(event)
    for child in event.child_events:
        lines.extend(f"fallback 内：{line}" for line in _var_event_lines(child))
    return lines


def _normal_variable_lines(
    analysis: NodeAnalysis, paths: tuple[tuple[object, ...], ...], node_index: int, prop: str
) -> list[str]:
    if prop not in analysis.normals:
        return []
    info = analysis.normals[prop]
    target_id = paths[node_index][-1]
    lines = [f"节点 {target_id!r}：{step}" for step in info.var_steps]

    referenced: set[str] = set()
    for event in info.var_events:
        lines.extend(f"节点 {target_id!r}：{line}" for line in _var_event_lines(event))
        for child in event.iter_all():
            referenced.add(child.name)
    for name in sorted(referenced):
        state = analysis.customs.get(name)
        if state is None:
            lines.append(f"节点 {target_id!r}：引用 {name} 时祖先链上未定义该变量")
            continue
        chain_lines = _custom_variable_lines(analysis, paths, node_index, name)
        for line in chain_lines:
            if line not in lines:
                lines.append(line)
    return lines


def _variable_evidence(
    baseline: TreeAnalysis,
    candidate: TreeAnalysis,
    target: GuardTarget,
    paths: tuple[tuple[object, ...], ...],
) -> VariableEvidenceOut:
    if target.is_custom:
        before = _custom_variable_lines(
            baseline.nodes[target.node_index], paths, target.node_index, target.property
        )
        after = _custom_variable_lines(
            candidate.nodes[target.node_index], paths, target.node_index, target.property
        )
    else:
        before = _normal_variable_lines(
            baseline.nodes[target.node_index], paths, target.node_index, target.property
        )
        after = _normal_variable_lines(
            candidate.nodes[target.node_index], paths, target.node_index, target.property
        )
    return VariableEvidenceOut(
        node_id=paths[target.node_index][-1],
        property=target.property,
        is_custom=target.is_custom,
        before=before,
        after=after,
    )


# ---------- 入口 ----------


def plan_layer_migration(req: LayerMigrationRequest) -> LayerMigrationResponse:
    flat = flatten_tree(req.root)
    paths = _tree_paths(req.root)
    validate_rules(req.rules)
    _validate_rule_layers(req)
    ruleset = RuleSet.build(req.rules, req.layers)
    _validate_constraints(req)

    node_indexes = {path[-1]: i for i, path in enumerate(paths)}
    chains = [(node.element, node.ancestors) for node in flat]
    baseline = solve_forest(ruleset, chains)
    guards = _validate_guards(req, baseline, node_indexes)

    current_layers = list(req.layers)
    total = 1
    for n in range(1, len(current_layers) + 1):
        total *= n

    cycle_index = _first_cycle_constraint(req.constraints, current_layers)
    if cycle_index is not None:
        constraint = req.constraints[cycle_index]
        return LayerMigrationResponse(
            nodes_checked=len(flat),
            current_layers=current_layers,
            feasible=False,
            evaluated_orders=0,
            total_permutations=total,
            conflicting_constraint=LayerConstraintOut(
                constraint_index=cycle_index,
                earlier_layer=constraint.earlier_layer,
                later_layer=constraint.later_layer,
                detail=(
                    f"约束 {cycle_index} 要求 {constraint.earlier_layer!r} 早于 "
                    f"{constraint.later_layer!r}，但此前约束已形成反向路径"
                ),
            ),
        )

    constraint_feasible = [
        order
        for order in permutations(tuple(sorted(current_layers)))
        if _satisfies_constraints(order, req.constraints)
    ]
    trees = {
        order: solve_forest(ruleset.with_layers(order), chains)
        for order in constraint_feasible
    }

    survivors: list[tuple[str, ...]] = constraint_feasible
    conflicting_guard: LayerGuardOut | None = None
    variable_evidence: list[VariableEvidenceOut] = []

    for target in guards:
        next_survivors: list[tuple[str, ...]] = []
        for order in survivors:
            sig = _signature_for(
                trees[order].nodes[target.node_index], target.property, target.is_custom
            )
            base_sig = _signature_for(
                baseline.nodes[target.node_index], target.property, target.is_custom
            )
            if sig == base_sig:
                next_survivors.append(order)

        if not next_survivors:
            blocker_order = survivors[0]
            blocker_tree = trees[blocker_order]
            change = _guard_change(baseline, blocker_tree, target, req.rules)
            detail = change.detail if change else "保护项在候选层序下发生变化"
            conflicting_guard = LayerGuardOut(
                guard_index=target.index,
                node_id=req.guards[target.index].node_id,
                property=target.property,
                result=req.guards[target.index].result,
                detail=detail,
            )
            variable_evidence = [
                _variable_evidence(baseline, blocker_tree, target, paths)
            ]
            survivors = []
            break
        survivors = next_survivors

    if conflicting_guard is not None:
        return LayerMigrationResponse(
            nodes_checked=len(flat),
            current_layers=current_layers,
            feasible=False,
            evaluated_orders=len(constraint_feasible),
            total_permutations=total,
            conflicting_guard=conflicting_guard,
            variable_evidence=variable_evidence,
        )

    summaries: list[tuple[int, tuple[str, ...], LayerOrderImpactOut]] = []
    affected_by_order: dict[tuple[str, ...], list[LayerMigrationNodeOut]] = {}
    for order in survivors:
        tree = trees[order]
        affected_nodes: list[LayerMigrationNodeOut] = []
        signature_count = 0
        for node_index, (before, after) in enumerate(zip(baseline.nodes, tree.nodes)):
            changes = _node_changes(before, after, req.rules)
            if changes:
                signature_count += len(changes)
                affected_nodes.append(
                    LayerMigrationNodeOut(node_id=paths[node_index][-1], changes=changes)
                )
        summary = LayerOrderImpactOut(
            layers=list(order),
            affected_signature_count=signature_count,
            affected_node_count=len(affected_nodes),
        )
        summaries.append((signature_count, order, summary))
        affected_by_order[order] = affected_nodes

    summaries.sort(key=lambda item: (item[0], item[1]))
    if not summaries:
        return LayerMigrationResponse(
            nodes_checked=len(flat),
            current_layers=current_layers,
            feasible=True,
            selected_order=current_layers,
            affected_signature_count=0,
            affected_nodes=[],
            feasible_orders=[],
            evaluated_orders=len(constraint_feasible),
            total_permutations=total,
        )
    selected_count, selected_order, _ = summaries[0]
    return LayerMigrationResponse(
        nodes_checked=len(flat),
        current_layers=current_layers,
        feasible=True,
        selected_order=list(selected_order),
        affected_signature_count=selected_count,
        affected_nodes=affected_by_order[selected_order],
        feasible_orders=[item[2] for item in summaries],
        evaluated_orders=len(constraint_feasible),
        total_permutations=total,
    )
