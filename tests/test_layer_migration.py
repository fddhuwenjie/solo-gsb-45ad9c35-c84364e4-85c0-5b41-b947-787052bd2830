"""POST /layer-migration 样式层级迁移规划端到端测试。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def plan(payload, expected=200):
    resp = client.post("/layer-migration", json=payload)
    assert resp.status_code == expected, resp.text
    return resp.json()


def plan_422(payload):
    resp = client.post("/layer-migration", json=payload)
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]


def decl(prop, value, important=False):
    d = {"property": prop, "value": value}
    if important:
        d["important"] = True
    return d


def rule(selector, *decls, origin="author", layer=None):
    r = {"selector": selector, "origin": origin, "declarations": list(decls)}
    if layer is not None:
        r["layer"] = layer
    return r


def node(id, tag, classes=None, attributes=None, children=None):
    n = {"id": id, "tag": tag}
    if classes is not None:
        n["classes"] = classes
    if attributes is not None:
        n["attributes"] = attributes
    if children is not None:
        n["children"] = children
    return n


def payload(root, rules, layers, constraints=None, protections=None):
    p = {"root": root, "rules": rules, "layers": layers}
    if constraints is not None:
        p["constraints"] = constraints
    if protections is not None:
        p["protections"] = protections
    return p


# 共享样本：div#app.dark > (h1#title, p.text#lead > code#code)
TREE = node(
    "app", "div", classes=["dark"],
    children=[
        node("title", "h1"),
        node("lead", "p", classes=["text"], children=[node("code", "code")]),
    ],
)

RULES = [
    rule("*", decl("margin", "0"), layer="reset"),
    rule("div", decl("--gap", "8px"), decl("--accent", "red"), layer="base"),
    rule("div.dark", decl("--gap", "12px")),
    rule("p.text", decl("padding", "var(--gap, 4px)"), layer="components"),
    rule("code", decl("color", "var(--gap)"), layer="base"),
    rule("code", decl("color", "navy"), layer="components"),
    rule("h1", decl("font-size", "2em"), layer="reset"),
]

LAYERS = ["reset", "base", "components"]


# ---------- 成功：选定唯一新层序 ----------


def test_basic_migration_selects_the_only_feasible_order():
    data = plan(payload(
        TREE, RULES, LAYERS,
        constraints=[{"before": "reset", "after": "components"},
                     {"before": "components", "after": "base"}],
        protections=[{"node_id": "lead", "property": "padding"},
                     {"node_id": "app", "property": "--gap"}],
    ))
    assert data["status"] == "ok"
    assert data["nodes_checked"] == 4
    assert data["current_order"] == LAYERS
    # 唯一满足两条约束的层序
    assert data["selected_order"] == ["reset", "components", "base"]
    assert data["changed"] is True
    assert data["orders_evaluated"] == 1
    assert data["orders_satisfying_protections"] == 1

    # 唯一受影响签名：code 的 color 由 components 层 navy 换成 base 层 var(--gap)
    assert data["affected_count"] == 1
    ch = data["affected"][0]
    assert ch["node_id"] == "code"
    assert ch["property"] == "color"
    assert ch["reasons"] == ["cascade_winner"]
    assert (ch["before"]["value"], ch["after"]["value"]) == ("navy", "12px")
    assert ch["before"]["winner"]["rule_index"] == 5
    assert ch["after"]["winner"]["rule_index"] == 4

    # 保护项全部满足并回显
    assert data["protections"] == [
        {"node_id": "lead", "property": "padding", "is_custom": False, "satisfied": True},
        {"node_id": "app", "property": "--gap", "is_custom": True, "satisfied": True},
    ]


def test_current_order_kept_when_nothing_competes():
    # 两层规则作用于不同属性：任何层序都无影响，当前层序即字典序最小
    root = node("r", "p")
    rules = [
        rule("p", decl("color", "red"), layer="a"),
        rule("p", decl("margin", "0"), layer="b"),
    ]
    data = plan(payload(root, rules, ["a", "b"]))
    assert data["status"] == "ok"
    assert data["selected_order"] == ["a", "b"]
    assert data["changed"] is False
    assert data["affected_count"] == 0
    assert data["affected"] == []
    assert data["orders_evaluated"] == 2
    assert data["orders_satisfying_protections"] == 2


def test_lexicographic_tie_break_selects_smallest_order():
    # 两层互不竞争：两个层序受影响数同为 0，按层序字典序选定 ["a", "b"]
    root = node("r", "p")
    rules = [
        rule("p", decl("color", "red"), layer="a"),
        rule("p", decl("margin", "0"), layer="b"),
    ]
    data = plan(payload(root, rules, ["b", "a"]))
    assert data["selected_order"] == ["a", "b"]
    assert data["changed"] is True
    assert data["affected_count"] == 0


def test_min_affected_count_wins_over_lexicographic():
    # [y, x] 字典序更小但会让 color 换胜出者；[x, y]（当前层序）0 受影响，胜出
    root = node("r", "p")
    rules = [
        rule("p", decl("color", "red"), layer="x"),
        rule("p", decl("color", "blue"), layer="y"),
    ]
    data = plan(payload(root, rules, ["x", "y"]))
    assert data["selected_order"] == ["x", "y"]
    assert data["changed"] is False
    assert data["affected_count"] == 0


def test_important_declarations_adjudicated_by_reversed_layer_order():
    root = node("r", "p")
    rules = [
        rule("p", decl("color", "red", important=True), layer="early"),
        rule("p", decl("color", "blue", important=True), layer="late"),
        rule("p", decl("margin", "1px"), layer="early"),
        rule("p", decl("margin", "2px"), layer="late"),
    ]
    # 当前 [early, late]：important 反向 -> 早层 red 胜；普通 -> 晚层 2px 胜
    data = plan(payload(
        root, rules, ["early", "late"],
        constraints=[{"before": "late", "after": "early"}],
    ))
    assert data["selected_order"] == ["late", "early"]
    assert data["affected_count"] == 2
    by_prop = {ch["property"]: ch for ch in data["affected"]}
    # 新层序 [late, early]：important 反向 -> 现在的早层 late 的 blue 胜
    assert (by_prop["color"]["before"]["value"], by_prop["color"]["after"]["value"]) == ("red", "blue")
    # 普通声明正向 -> 现在的晚层 early 的 1px 胜
    assert (by_prop["margin"]["before"]["value"], by_prop["margin"]["after"]["value"]) == ("2px", "1px")


def test_same_value_winner_change_counts_as_affected():
    # 继任者取值恰好相同也算受影响：胜出声明定位属于被比对状态
    root = node("r", "p")
    rules = [
        rule("p", decl("color", "red"), layer="x"),
        rule("p", decl("color", "red"), layer="y"),
    ]
    data = plan(payload(
        root, rules, ["x", "y"],
        constraints=[{"before": "y", "after": "x"}],
    ))
    assert data["affected_count"] == 1
    ch = data["affected"][0]
    assert ch["reasons"] == ["cascade_winner"]
    assert ch["before"]["value"] == ch["after"]["value"] == "red"
    assert ch["before"]["winner"]["rule_index"] == 1
    assert ch["after"]["winner"]["rule_index"] == 0


def test_protection_filters_otherwise_better_orders():
    # 保护 color 的当前结果 blue：候选 [y, x] 字典序更小但会破坏保护项
    root = node("r", "p")
    rules = [
        rule("p", decl("color", "red"), layer="x"),
        rule("p", decl("color", "blue"), layer="y"),
    ]
    data = plan(payload(
        root, rules, ["x", "y"],
        protections=[{"node_id": "r", "property": "color"}],
    ))
    assert data["status"] == "ok"
    assert data["selected_order"] == ["x", "y"]
    assert data["orders_evaluated"] == 2
    assert data["orders_satisfying_protections"] == 1
    assert data["affected_count"] == 0


def test_protection_on_invalid_result_and_custom_property():
    # 保护项可以锁定 invalid 状态与自定义属性结果
    root = node("r", "div", children=[node("k", "p")])
    rules = [
        rule("div", decl("--x", "5px"), layer="a"),
        rule("p", decl("margin", "var(--x)"), decl("padding", "var(--missing)")),
    ]
    data = plan(payload(
        root, rules, ["a", "b"],
        protections=[{"node_id": "k", "property": "padding"},
                     {"node_id": "k", "property": "--x"}],
    ))
    assert data["status"] == "ok"
    props = {(p["node_id"], p["property"]): p for p in data["protections"]}
    assert props[("k", "padding")]["is_custom"] is False
    assert props[("k", "--x")]["is_custom"] is True


def test_property_name_normalized_like_compute():
    # 普通属性名按小写归一（自定义属性名大小写敏感）
    root = node("r", "p")
    rules = [rule("p", decl("color", "red"), layer="a")]
    data = plan(payload(
        root, rules, ["a"],
        protections=[{"node_id": "r", "property": "COLOR"}],
    ))
    assert data["protections"][0]["property"] == "color"


def test_integer_node_ids_and_six_layers_supported():
    root = node(1, "p")
    layers = ["l1", "l2", "l3", "l4", "l5", "l6"]
    rules = [rule("p", decl("color", "red"), layer="l1")]
    data = plan(payload(root, rules, layers,
                        protections=[{"node_id": 1, "property": "color"}]))
    assert data["status"] == "ok"
    assert data["orders_evaluated"] == 720
    assert data["selected_order"] == layers  # 全部 0 受影响，字典序最小即现状
    assert data["protections"][0]["node_id"] == 1


# ---------- 无方案：保护项不可满足 ----------


def test_no_plan_when_forced_order_breaks_protection():
    root = node("r", "p")
    rules = [
        rule("p", decl("color", "red"), layer="x"),
        rule("p", decl("color", "blue"), layer="y"),
    ]
    data = plan(payload(
        root, rules, ["x", "y"],
        constraints=[{"before": "y", "after": "x"}],
        protections=[{"node_id": "r", "property": "color"}],
    ))
    assert data["status"] == "no_plan"
    assert data["orders_evaluated"] == 1
    assert data["conflict"] is None
    assert data["best_effort_order"] == ["y", "x"]
    verdict = data["protections"][0]
    assert verdict["satisfiable"] is False
    assert verdict["baseline"]["value"] == "blue"
    ev = verdict["evidence"]
    assert ev["order"] == ["y", "x"]
    assert ev["reasons"] == ["cascade_winner"]
    assert (ev["before"]["value"], ev["after"]["value"]) == ("blue", "red")
    assert "层叠胜出声明" in ev["detail"]


def test_no_plan_evidence_covers_inheritance_and_var_chain():
    # 强制层序改变祖先 --gap 胜出者：后代自定义属性经继承变化、
    # padding 经 var() 链变化——证据分别标 inheritance / var_chain
    root = node("d", "div", children=[node("k", "p")])
    rules = [
        rule("div", decl("--gap", "1px"), layer="x"),
        rule("div", decl("--gap", "2px"), layer="y"),
        rule("p", decl("padding", "var(--gap)")),
    ]
    data = plan(payload(
        root, rules, ["x", "y"],
        constraints=[{"before": "y", "after": "x"}],
        protections=[{"node_id": "k", "property": "padding"},
                     {"node_id": "k", "property": "--gap"}],
    ))
    assert data["status"] == "no_plan"
    verdicts = {p["property"]: p for p in data["protections"]}
    pad = verdicts["padding"]
    assert pad["satisfiable"] is False
    assert pad["evidence"]["reasons"] == ["var_chain"]
    assert (pad["evidence"]["before"]["value"], pad["evidence"]["after"]["value"]) == ("2px", "1px")
    gap = verdicts["--gap"]
    assert gap["satisfiable"] is False
    assert gap["evidence"]["reasons"] == ["inheritance"]
    assert gap["evidence"]["before"]["winner"] is None  # 纯继承，无本元素胜出声明


def test_no_plan_evidence_covers_fallback_reroute_and_cycle():
    # 层序使 --a 的胜出声明换成 var(--b)，与 --b: var(--a) 形成循环：
    # --a 失效（cascade_winner），margin 改道 fallback（var_chain）
    root = node("r", "p")
    rules = [
        rule("p", decl("--a", "var(--b)"), layer="x"),
        rule("p", decl("--a", "1px"), layer="y"),
        rule("p", decl("--b", "var(--a)")),
        rule("p", decl("margin", "var(--a, 9px)")),
    ]
    data = plan(payload(
        root, rules, ["x", "y"],
        constraints=[{"before": "y", "after": "x"}],
        protections=[{"node_id": "r", "property": "--a"},
                     {"node_id": "r", "property": "margin"}],
    ))
    assert data["status"] == "no_plan"
    verdicts = {p["property"]: p for p in data["protections"]}
    a = verdicts["--a"]
    assert a["satisfiable"] is False
    assert a["evidence"]["reasons"] == ["cascade_winner"]
    assert (a["evidence"]["before"]["status"], a["evidence"]["after"]["status"]) == ("ok", "invalid")
    assert "循环" in a["evidence"]["after"]["reason"]
    margin = verdicts["margin"]
    assert margin["satisfiable"] is False
    assert margin["evidence"]["reasons"] == ["var_chain"]
    # 主分支 1px -> fallback 9px
    assert (margin["evidence"]["before"]["value"], margin["evidence"]["after"]["value"]) == ("1px", "9px")


def test_no_plan_protections_individually_satisfiable_but_jointly_conflicting():
    # 约束 c 先于 a 排除了当前层序 [a, b, c]；保护 color（需 a 先于 b）只在
    # [c, a, b] 成立，保护 margin（需 b 先于 c）只在 [b, c, a] 成立——各自
    # 可满足，联合不可满足
    root = node("r", "p")
    rules = [
        rule("p", decl("color", "red"), layer="a"),
        rule("p", decl("color", "blue"), layer="b"),
        rule("p", decl("margin", "1px"), layer="b"),
        rule("p", decl("margin", "2px"), layer="c"),
    ]
    data = plan(payload(
        root, rules, ["a", "b", "c"],
        constraints=[{"before": "c", "after": "a"}],
        protections=[{"node_id": "r", "property": "color"},
                     {"node_id": "r", "property": "margin"}],
    ))
    assert data["status"] == "no_plan"
    assert data["conflict"] is None
    assert data["orders_evaluated"] == 3
    # 参照层序：各可行层序均有 1 个受影响签名，按字典序取 [b, c, a]
    assert data["best_effort_order"] == ["b", "c", "a"]
    verdicts = {p["property"]: p for p in data["protections"]}
    # 两个保护项各自都可被某个可行层序满足，但无法同时满足：
    # satisfiable 为 True，不附破坏证据
    assert verdicts["color"]["satisfiable"] is True
    assert verdicts["color"]["evidence"] is None
    assert verdicts["margin"]["satisfiable"] is True
    assert verdicts["margin"]["evidence"] is None


# ---------- 无方案：约束自相矛盾 ----------


def test_no_plan_contradictory_constraints_reports_earliest_conflict():
    root = node("r", "p")
    rules = [rule("p", decl("color", "red"))]
    data = plan(payload(
        root, rules, ["a", "b", "c"],
        constraints=[{"before": "a", "after": "b"},
                     {"before": "b", "after": "c"},
                     {"before": "c", "after": "a"}],
        protections=[{"node_id": "r", "property": "color"}],
    ))
    assert data["status"] == "no_plan"
    assert data["orders_evaluated"] == 0
    assert data["best_effort_order"] is None
    conflict = data["conflict"]
    assert conflict["constraint_index"] == 2
    assert (conflict["before"], conflict["after"]) == ("c", "a")
    assert "a → b → c" in conflict["detail"]
    # 约束不可满足时保护项无法评估
    verdict = data["protections"][0]
    assert verdict["satisfiable"] is None
    assert verdict["evidence"] is None
    assert verdict["baseline"]["value"] == "red"


def test_earliest_conflict_is_the_first_cycle_closing_constraint():
    root = node("r", "p")
    rules = [rule("p", decl("color", "red"))]
    # 约束 1 与约束 2 互斥：最早冲突的是下标 2（b -> a 与已有 a -> b 成环）
    data = plan(payload(
        root, rules, ["a", "b"],
        constraints=[{"before": "a", "after": "b"},
                     {"before": "b", "after": "a"}],
    ))
    assert data["status"] == "no_plan"
    assert data["conflict"]["constraint_index"] == 1
    assert "a → b" in data["conflict"]["detail"]


# ---------- 422 校验（整批拒绝，不产生部分结果） ----------


def test_422_duplicate_id():
    root = node("a", "p", children=[node("a", "span")])
    detail = plan_422(payload(root, [], ["x"]))
    assert "重复 id" in detail


def test_422_layers_empty_too_many_blank_or_duplicated():
    root = node("r", "p")
    assert "不能为空" in plan_422(payload(root, [], []))
    assert "最多" in plan_422(
        payload(root, [], ["a", "b", "c", "d", "e", "f", "g"])
    )
    assert "空白" in plan_422(payload(root, [], ["a", "  "]))
    assert "重复层" in plan_422(payload(root, [], ["a", "b", "a"]))


def test_422_unknown_layer_in_constraint():
    root = node("r", "p")
    detail = plan_422(payload(
        root, [], ["a", "b"],
        constraints=[{"before": "a", "after": "zzz"}],
    ))
    assert "未知层" in detail and "zzz" in detail


def test_422_illegal_constraints():
    root = node("r", "p")
    # 自约束
    assert "自身" in plan_422(payload(
        root, [], ["a", "b"], constraints=[{"before": "a", "after": "a"}]
    ))
    # 重复约束
    assert "重复" in plan_422(payload(
        root, [], ["a", "b"],
        constraints=[{"before": "a", "after": "b"}, {"before": "a", "after": "b"}],
    ))
    # 空白层名（Pydantic 层拒绝）
    assert plan_422(payload(
        root, [], ["a", "b"], constraints=[{"before": " ", "after": "b"}]
    ))


def test_422_nonexistent_protections():
    root = node("r", "p")
    rules = [rule("p", decl("color", "red"))]
    # 节点不存在
    assert "不存在" in plan_422(payload(
        root, rules, ["a"], protections=[{"node_id": "zzz", "property": "color"}]
    ))
    # 属性结果不存在（computed / custom_properties / invalid 中均未出现）
    assert "保护项不存在" in plan_422(payload(
        root, rules, ["a"], protections=[{"node_id": "r", "property": "margin"}]
    ))
    assert "保护项不存在" in plan_422(payload(
        root, rules, ["a"], protections=[{"node_id": "r", "property": "--nope"}]
    ))
    # 重复保护项
    assert "重复" in plan_422(payload(
        root, rules, ["a"],
        protections=[{"node_id": "r", "property": "color"},
                     {"node_id": "r", "property": "COLOR"}],
    ))


def test_422_bad_selector_and_blank_declaration_property():
    root = node("r", "p")
    detail = plan_422(payload(root, [rule("p >", decl("color", "red"))], ["a"]))
    assert "选择器语法错误" in detail
    detail = plan_422(payload(root, [rule("p", decl("  ", "red"))], ["a"]))
    assert "property" in detail


def test_422_malformed_protection_fields():
    root = node("r", "p")
    rules = [rule("p", decl("color", "red"))]
    # 空白属性名 / 布尔 node_id（Pydantic 层拒绝）
    assert plan_422(payload(
        root, rules, ["a"], protections=[{"node_id": "r", "property": " "}]
    ))
    assert plan_422(payload(
        root, rules, ["a"], protections=[{"node_id": True, "property": "color"}]
    ))


def test_validation_failure_produces_no_partial_results():
    resp = client.post("/layer-migration", json=payload(
        node("a", "p", children=[node("a", "span")]), [], ["x"],
    ))
    assert resp.status_code == 422
    assert "selected_order" not in resp.json()
    assert "protections" not in resp.json()


# ---------- 兼容 ----------


def test_existing_endpoints_still_work():
    resp = client.post("/compute", json={
        "element": {"tag": "p"},
        "layers": ["base"],
        "rules": [rule("p", decl("color", "red"), layer="base")],
    })
    assert resp.status_code == 200
    assert resp.json()["computed"] == {"color": "red"}
    assert client.get("/health").json() == {"status": "ok"}
