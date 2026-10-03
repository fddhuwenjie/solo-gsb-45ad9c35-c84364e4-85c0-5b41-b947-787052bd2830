"""样式层级迁移规划：枚举可行层序、逐一整树重算并选定唯一新层序。

给定 DOM 样本树、当前层序（最多 6 个层）、结构化规则、必须满足的层先后
约束与需保护的节点属性结果（computed / custom_properties / invalid）：

1. 全部校验先于任何枚举求解：重复 id、未知层、非法约束或不存在的保护项
   整批 422，不产生部分结果；
2. 以当前层序用整树分析内核（与 /compute、/stylesheet-audit、/dom-impact
   同一套层叠、自定义属性继承与 var() 规则）求解基线；
3. 枚举满足全部约束的层序排列，按每个顺序**重算整棵树**——层序只进入
   层叠排序键，important 声明仍按反向层序裁决（共享层叠内核保证）；
4. 只保留全部保护项的共享签名（胜出声明定位 + 状态 + 取值 + 失效原因）
   与基线一致的层序；
5. 在保留层序中按「相对当前层序受影响 (节点, 属性) 签名数最少、再按层序
   字典序」选定唯一新层序——同值但胜出声明变化也算受影响；
6. 若无任何可行层序：约束自相矛盾时返回最早冲突约束；否则返回逐保护项的
   可满足性判定，以及参照层序（受影响最少、再按字典序）下保护项被破坏的
   变量继承 / fallback 传播证据（reasons 与 /dom-impact 同口径）。
"""
from __future__ import annotations

from dataclasses import replace
from itertools import permutations

from .analysis import RuleSet, solve_forest
from .audit import flatten_tree, validate_rules
from .dom_impact import (
    _candidate_locs,
    _diff_node,
    _diff_property,
    _signatures,
    _value_out,
)
from .models import (
    LayerAffectedOut,
    LayerConstraintConflictOut,
    LayerMigrationNoPlanResponse,
    LayerMigrationRequest,
    LayerMigrationResponse,
    LayerProtectionEvidenceOut,
    LayerProtectionOut,
    LayerProtectionVerdictOut,
    PropertyValueOut,
)

# 现有层最多 6 个：6! = 720 个排列可逐一整树重算
MAX_LAYERS = 6


class LayerMigrationValidationError(ValueError):
    """层 / 约束 / 保护项非法（整次请求 422，不产生部分结果）。"""


# ---------- 校验 ----------


def _validate_layers(layers: list[str]) -> list[str]:
    if not layers:
        raise LayerMigrationValidationError(
            "layers 不能为空：迁移规划至少声明 1 个现有层"
        )
    if len(layers) > MAX_LAYERS:
        raise LayerMigrationValidationError(
            f"layers 最多 {MAX_LAYERS} 个现有层，当前声明了 {len(layers)} 个"
        )
    seen: set[str] = set()
    for layer in layers:
        if not layer.strip():
            raise LayerMigrationValidationError("层名不能为空白")
        if layer in seen:
            raise LayerMigrationValidationError(
                f"层声明顺序中存在重复层：{layer!r}"
            )
        seen.add(layer)
    return list(layers)


def _validate_constraints(constraints, layers: list[str]) -> None:
    """未知层、自约束与重复约束整批 422；约束成环不在此拒绝（导致无方案）。"""
    known = set(layers)
    seen: set[tuple[str, str]] = set()
    for i, c in enumerate(constraints):
        for name in (c.before, c.after):
            if name not in known:
                raise LayerMigrationValidationError(
                    f"约束 {i} 引用了未知层 {name!r}（未在 layers 中声明）"
                )
        if c.before == c.after:
            raise LayerMigrationValidationError(
                f"约束 {i} 非法：层 {c.before!r} 不能要求先于自身"
            )
        pair = (c.before, c.after)
        if pair in seen:
            raise LayerMigrationValidationError(
                f"约束 {i} 非法：与之前的约束重复（{c.before!r} 先于 {c.after!r}）"
            )
        seen.add(pair)


def _normalize_property(prop: str) -> str:
    """普通属性名按小写归一；自定义属性名大小写敏感（与层叠内核同口径）。"""
    return prop if prop.startswith("--") else prop.lower()


def _validate_protections(protections, flat, baseline) -> list[tuple[int, str, bool]]:
    """保护项定位到 (先序下标, 归一属性名, 是否自定义)；不存在或重复整批 422。"""
    positions = {node.raw_id: i for i, node in enumerate(flat)}
    seen: set[tuple[object, str]] = set()
    located: list[tuple[int, str, bool]] = []
    for p in protections:
        if p.node_id not in positions:
            raise LayerMigrationValidationError(
                f"保护项引用的节点 id {p.node_id!r} 不存在于 DOM 树中"
            )
        prop = _normalize_property(p.property)
        if (p.node_id, prop) in seen:
            raise LayerMigrationValidationError(
                f"保护项重复：节点 {p.node_id!r} 的属性 {prop!r}"
            )
        seen.add((p.node_id, prop))
        index = positions[p.node_id]
        is_custom = prop.startswith("--")
        sig = baseline.nodes[index].signature()
        table = dict(sig.custom if is_custom else sig.normal)
        if prop not in table:
            kind = "自定义属性" if is_custom else "普通属性"
            raise LayerMigrationValidationError(
                f"保护项不存在：节点 {p.node_id!r} 上没有{kind} {prop!r} 的结果"
                "（computed / custom_properties / invalid 中均未出现）"
            )
        located.append((index, prop, is_custom))
    return located


# ---------- 层序枚举与约束 ----------


def _feasible_orders(layers: list[str], constraints) -> list[tuple[str, ...]]:
    """满足全部层先后约束的层序排列。"""
    orders = []
    for perm in permutations(layers):
        pos = {name: i for i, name in enumerate(perm)}
        if all(pos[c.before] < pos[c.after] for c in constraints):
            orders.append(perm)
    return orders


def _earliest_conflict(constraints) -> tuple[int, list[str]]:
    """按输入顺序逐条加入约束，返回第一条使约束集合成环的约束下标与已有约束链。

    约束 before -> after 表示 before 必须先于 after；若加入前已存在
    after -> ... -> before 的约束链，则新约束与之成环。只在无可行排列时
    调用（无环必有拓扑序，故此时必然找得到）。
    """
    edges: dict[str, list[str]] = {}

    def find_path(src: str, dst: str) -> list[str] | None:
        stack = [(src, [src])]
        visited: set[str] = set()
        while stack:
            node, path = stack.pop()
            if node == dst:
                return path
            if node in visited:
                continue
            visited.add(node)
            for nxt in edges.get(node, []):
                stack.append((nxt, path + [nxt]))
        return None

    for i, c in enumerate(constraints):
        chain = find_path(c.after, c.before)
        if chain is not None:
            return i, chain
        edges.setdefault(c.before, []).append(c.after)
    raise AssertionError("约束集无环时必然存在可行层序")


# ---------- 签名比对 ----------


def _affected_signatures(baseline_sigs, trial_sigs) -> list[tuple[int, str, bool]]:
    """整树签名比对，返回受影响的 (先序下标, 属性名, 是否自定义) 列表。

    共享签名口径：胜出声明定位 + 状态 + 取值 + 失效原因——同值但胜出声明
    变化也算受影响。
    """
    affected: list[tuple[int, str, bool]] = []
    for index, (b_sig, t_sig) in enumerate(zip(baseline_sigs, trial_sigs)):
        b_norm, t_norm = dict(b_sig.normal), dict(t_sig.normal)
        b_cust, t_cust = dict(b_sig.custom), dict(t_sig.custom)
        for name in sorted(set(b_norm) | set(t_norm)):
            if b_norm.get(name) != t_norm.get(name):
                affected.append((index, name, False))
        for name in sorted(set(b_cust) | set(t_cust)):
            if b_cust.get(name) != t_cust.get(name):
                affected.append((index, name, True))
    return affected


def _protections_hold(located, baseline_sigs, trial_sigs) -> bool:
    for index, prop, is_custom in located:
        b_sig, t_sig = baseline_sigs[index], trial_sigs[index]
        b_map = dict(b_sig.custom if is_custom else b_sig.normal)
        t_map = dict(t_sig.custom if is_custom else t_sig.normal)
        if b_map.get(prop) != t_map.get(prop):
            return False
    return True


# ---------- 差异渲染（复用 /dom-impact 的归因与中文说明） ----------


def _impact_for(baseline, trial, index, prop, is_custom, rules):
    """单个 (节点, 属性) 在基线与某层序下的差异（reasons + before/after + detail）。"""
    node_b, node_t = baseline.nodes[index], trial.nodes[index]
    return _diff_property(
        node_b,
        node_t,
        prop,
        is_custom,
        _signatures(node_b, is_custom),
        _signatures(node_t, is_custom),
        _candidate_locs(node_b, is_custom),
        _candidate_locs(node_t, is_custom),
        rules,
    )


def _baseline_value(baseline, index, prop, is_custom) -> PropertyValueOut:
    node = baseline.nodes[index]
    sigs = _signatures(node, is_custom)
    return _value_out(node, prop, is_custom, sigs.get(prop))


# ---------- 入口 ----------


def plan_layer_migration(req: LayerMigrationRequest):
    # 1) 全部结构 / 语法 / 语义校验先于任何枚举求解，失败即整体 422
    flat = flatten_tree(req.root)  # 重复 id / 空 tag 等在此抛
    validate_rules(req.rules)  # 空白 property
    layers = _validate_layers(req.layers)
    ruleset = RuleSet.build(req.rules, layers)  # 选择器语法非法抛 SelectorSyntaxError
    _validate_constraints(req.constraints, layers)

    # 2) 当前层序下的基线（整树，先序）
    chains = [(node.element, node.ancestors) for node in flat]
    baseline = solve_forest(ruleset, chains)
    baseline_sigs = baseline.signature()

    located = _validate_protections(req.protections, flat, baseline)

    # 3) 枚举满足约束的层序；约束成环时给出最早冲突约束
    orders = _feasible_orders(layers, req.constraints)
    if not orders:
        index, chain = _earliest_conflict(req.constraints)
        conflict = LayerConstraintConflictOut(
            constraint_index=index,
            before=req.constraints[index].before,
            after=req.constraints[index].after,
            detail=(
                f"约束 {index}（{req.constraints[index].before!r} 必须先于 "
                f"{req.constraints[index].after!r}）与已有约束冲突：已有约束链要求 "
                f"{' → '.join(chain)}，即 {req.constraints[index].after!r} 必须先于 "
                f"{req.constraints[index].before!r}，二者不能同时满足，无可行层序"
            ),
        )
        return LayerMigrationNoPlanResponse(
            nodes_checked=len(flat),
            current_order=layers,
            orders_evaluated=0,
            conflict=conflict,
            best_effort_order=None,
            protections=[
                LayerProtectionVerdictOut(
                    node_id=p.node_id,
                    property=_normalize_property(p.property),
                    is_custom=_normalize_property(p.property).startswith("--"),
                    satisfiable=None,
                    baseline=_baseline_value(
                        baseline, idx, prop, is_custom
                    ),
                )
                for p, (idx, prop, is_custom) in zip(req.protections, located)
            ],
        )

    # 4) 按每个可行层序重算整棵树（important 仍按反向层序裁决）
    evaluations = []  # (层序, 受影响签名列表, 是否满足全部保护项, 整树分析)
    for order in orders:
        variant = replace(ruleset, layers=order)
        trial = solve_forest(variant, chains)
        sigs = trial.signature()
        affected = _affected_signatures(baseline_sigs, sigs)
        ok = _protections_hold(located, baseline_sigs, sigs)
        evaluations.append((order, affected, ok, trial))

    # 5) 无方案：逐保护项判定可满足性，并给出参照层序下的传播证据
    survivors = [e for e in evaluations if e[2]]
    if not survivors:
        best = min(evaluations, key=lambda e: (len(e[1]), e[0]))
        verdicts = []
        for p, (index, prop, is_custom) in zip(req.protections, located):
            satisfiable = any(
                (index, prop, is_custom) not in affected
                for _, affected, _, _ in evaluations
            )
            evidence = None
            if not satisfiable:
                # 该保护项在任何可行层序下都被破坏：参照层序（受影响最少、
                # 再按字典序）下给出 before/after 与继承 / var() 链归因
                impact = _impact_for(
                    baseline, best[3], index, prop, is_custom, req.rules
                )
                assert impact is not None
                evidence = LayerProtectionEvidenceOut(
                    order=list(best[0]),
                    reasons=impact.reasons,
                    before=impact.before,
                    after=impact.after,
                    detail=impact.detail,
                )
            verdicts.append(
                LayerProtectionVerdictOut(
                    node_id=p.node_id,
                    property=prop,
                    is_custom=is_custom,
                    satisfiable=satisfiable,
                    baseline=_baseline_value(baseline, index, prop, is_custom),
                    evidence=evidence,
                )
            )
        return LayerMigrationNoPlanResponse(
            nodes_checked=len(flat),
            current_order=layers,
            orders_evaluated=len(orders),
            conflict=None,
            best_effort_order=list(best[0]),
            protections=verdicts,
        )

    # 6) 选定唯一层序：受影响签名最少，再按层序字典序
    selected_order, selected_affected, _, selected_tree = min(
        survivors, key=lambda e: (len(e[1]), e[0])
    )
    affected_out: list[LayerAffectedOut] = []
    for index in sorted({idx for idx, _, _ in selected_affected}):
        for change in _diff_node(
            baseline.nodes[index], selected_tree.nodes[index], req.rules
        ):
            affected_out.append(
                LayerAffectedOut(
                    node_id=flat[index].raw_id,
                    property=change.property,
                    is_custom=change.is_custom,
                    reasons=change.reasons,
                    before=change.before,
                    after=change.after,
                    detail=change.detail,
                )
            )

    return LayerMigrationResponse(
        nodes_checked=len(flat),
        current_order=layers,
        selected_order=list(selected_order),
        changed=list(selected_order) != layers,
        orders_evaluated=len(orders),
        orders_satisfying_protections=len(survivors),
        affected_count=len(selected_affected),
        affected=affected_out,
        protections=[
            LayerProtectionOut(
                node_id=p.node_id, property=prop, is_custom=is_custom, satisfied=True
            )
            for p, (_, prop, is_custom) in zip(req.protections, located)
        ],
    )
