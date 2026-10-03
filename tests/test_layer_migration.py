"""POST /layer-migration 样式层级迁移规划端到端测试。"""
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def call(payload, expected=200):
    resp = client.post("/layer-migration", json=payload)
    assert resp.status_code == expected, resp.text
    return resp.json()


def call_422(payload):
    resp = client.post("/layer-migration", json=payload)
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]


def decl(prop, value, important=False):
    item = {"property": prop, "value": value}
    if important:
        item["important"] = True
    return item


def rule(selector, *decls, layer=None):
    item = {"selector": selector, "declarations": list(decls)}
    if layer is not None:
        item["layer"] = layer
    return item


def node(id, tag, children=None, **kwargs):
    item = {"id": id, "tag": tag}
    item.update(kwargs)
    if children is not None:
        item["children"] = children
    return item


def payload(root, layers, rules, constraints=None, guards=None):
    data = {"root": root, "layers": layers, "rules": rules}
    if constraints is not None:
        data["constraints"] = constraints
    if guards is not None:
        data["guards"] = guards
    return data


def order_map(data):
    return {tuple(item["layers"]): item for item in data["feasible_orders"]}


def test_enumerates_orders_and_selects_minimum_signatures():
    root = node("r", "p")
    rules = [
        rule("p", decl("color", "red"), layer="a"),
        rule("p", decl("color", "blue"), layer="b"),
    ]
    data = call(payload(root, ["a", "b"], rules))

    assert data["feasible"] is True
    assert data["total_permutations"] == 2
    assert data["evaluated_orders"] == 2
    # 当前顺序 a,b 无变化；另一个顺序虽然取值改变，但仍比受两个属性影响便宜
    assert data["selected_order"] == ["a", "b"]
    assert data["affected_signature_count"] == 0
    assert data["affected_nodes"] == []
    assert order_map(data)[("b", "a")]["affected_signature_count"] == 1


def test_lexicographic_tiebreak_relative_to_current_order():
    root = node("r", "p")
    rules = [rule("p", decl("color", "red"), layer="b")]
    data = call(payload(root, ["b", "a"], rules))

    # 未使用的 a 与实际生效的 b 互换不会改变任何签名，零影响下取字典序 a,b
    assert data["feasible"] is True
    assert data["selected_order"] == ["a", "b"]
    assert data["affected_signature_count"] == 0


def test_important_declarations_use_reverse_layer_order():
    root = node("r", "p")
    rules = [
        rule("p", decl("color", "red", important=True), layer="a"),
        rule("p", decl("color", "blue", important=True), layer="b"),
    ]
    data = call(payload(root, ["a", "b"], rules))

    # important 时早层 a 优先；交换层序后 b 更早，因此 blue 胜出
    changes = order_map(data)[("b", "a")]
    assert changes["affected_signature_count"] == 1
    selected = order_map(data)[("a", "b")]
    assert selected["affected_signature_count"] == 0
    assert data["selected_order"] == ["a", "b"]


def test_guards_filter_orders_and_same_value_winner_change_counts():
    root = node("r", "p")
    rules = [
        rule("p", decl("color", "red"), layer="a"),
        rule("p", decl("color", "red"), layer="b"),
    ]
    data = call(
        payload(
            root,
            ["a", "b"],
            rules,
            guards=[{"node_id": "r", "property": "color", "result": "computed"}],
        )
    )

    assert data["feasible"] is True
    assert data["selected_order"] == ["a", "b"]
    assert [item["layers"] for item in data["feasible_orders"]] == [["a", "b"]]


def test_custom_property_guard_protects_inheritance_and_reports_variable_evidence():
    root = node(
        "app", "div", children=[node("child", "p")],
    )
    rules = [
        rule("div", decl("--x", "1px"), layer="a"),
        rule("div", decl("--x", "2px"), layer="b"),
        rule("p", decl("margin", "var(--x, 9px)")),
    ]
    data = call(
        payload(
            root,
            ["a", "b"],
            rules,
            guards=[{"node_id": "child", "property": "--x", "result": "custom_properties"}],
        )
    )

    assert data["feasible"] is True
    assert data["selected_order"] == ["a", "b"]
    node_changes = {n["node_id"]: n["changes"] for n in data["affected_nodes"]}
    assert "child" not in node_changes


def test_no_solution_returns_earliest_guard_and_fallback_propagation_evidence():
    root = node("r", "div")
    rules = [
        rule("div", decl("--x", "var(--missing)"), layer="a"),
        rule("div", decl("--x", "1px"), layer="b"),
        rule("div", decl("color", "var(--x, red)")),
    ]
    constraints = [{"earlier_layer": "b", "later_layer": "a"}]
    data = call(
        payload(
            root,
            ["a", "b"],
            rules,
            constraints=constraints,
            guards=[{"node_id": "r", "property": "color", "result": "computed"}],
        )
    )
    assert data["feasible"] is False
    assert data["conflicting_constraint"] is None
    assert data["conflicting_guard"]["guard_index"] == 0
    evidence = data["variable_evidence"][0]
    assert evidence["node_id"] == "r"
    assert evidence["property"] == "color"
    assert any("var(--x) 引用有效" in line and "1px" in line for line in evidence["before"])
    assert any("改走 fallback" in line and "red" in line for line in evidence["after"])


def test_inheritance_evidence_identifies_ancestor_custom_property_winner():
    root = node("app", "div", children=[node("child", "p")])
    rules = [
        rule("div", decl("--x", "var(--missing)"), layer="a"),
        rule("div", decl("--x", "1px"), layer="b"),
        rule("p", decl("margin", "var(--x, 9px)")),
    ]
    constraints = [{"earlier_layer": "b", "later_layer": "a"}]
    data = call(
        payload(
            root,
            ["a", "b"],
            rules,
            constraints=constraints,
            guards=[{"node_id": "child", "property": "margin", "result": "computed"}],
        )
    )
    assert data["feasible"] is False
    evidence = data["variable_evidence"][0]
    assert evidence["node_id"] == "child"
    assert any("节点 'app'" in line and "继承状态为 ok" in line for line in evidence["before"])
    assert any("改走 fallback" in line and "9px" in line for line in evidence["after"])
    assert any("节点 'app'" in line and "继承状态为 invalid" in line for line in evidence["after"])


def test_cyclic_constraint_returns_earliest_conflict_and_does_not_enumerate():
    root = node("r", "p")
    constraints = [
        {"earlier_layer": "a", "later_layer": "b"},
        {"earlier_layer": "b", "later_layer": "a"},
    ]
    data = call(payload(root, ["a", "b"], [], constraints=constraints))
    assert data["feasible"] is False
    assert data["evaluated_orders"] == 0
    conflict = data["conflicting_constraint"]
    assert conflict["constraint_index"] == 1
    assert conflict["earlier_layer"] == "b"
    assert conflict["later_layer"] == "a"
    assert data["conflicting_guard"] is None


def test_satisfiable_constraint_restricts_permutations():
    root = node("r", "p")
    data = call(
        payload(
            root,
            ["b", "a"],
            [],
            constraints=[{"earlier_layer": "a", "later_layer": "b"}],
        )
    )
    assert data["feasible"] is True
    assert data["selected_order"] == ["a", "b"]
    assert [item["layers"] for item in data["feasible_orders"]] == [["a", "b"]]


def test_422_duplicate_id_unknown_layer_and_bad_constraint():
    root = node("r", "p", children=[node("r", "span")])
    assert "重复 id" in call_422(payload(root, ["a"], []))

    root = node("r", "p")
    assert "未知层" in call_422(
        payload(root, ["a"], [rule("p", decl("color", "red"), layer="x")])
    )
    assert "未知层" in call_422(
        payload(
            root, ["a"], [],
            constraints=[{"earlier_layer": "a", "later_layer": "x"}],
        )
    )
    assert "自身之前" in call_422(
        payload(
            root, ["a"], [],
            constraints=[{"earlier_layer": "a", "later_layer": "a"}],
        )
    )


def test_422_nonexistent_guard():
    root = node("r", "p")
    rules = [rule("p", decl("color", "red"))]
    msg = call_422(
        payload(
            root,
            ["a"],
            rules,
            guards=[{"node_id": "missing", "property": "color", "result": "computed"}],
        )
    )
    assert "不存在" in msg

    msg = call_422(
        payload(
            root,
            ["a"],
            rules,
            guards=[{"node_id": "r", "property": "margin", "result": "computed"}],
        )
    )
    assert "不存在" in msg


def test_more_than_six_layers_rejected():
    root = node("r", "p")
    payload_data = payload(root, ["a", "b", "c", "d", "e", "f", "g"], [])
    resp = client.post("/layer-migration", json=payload_data)
    assert resp.status_code == 422
