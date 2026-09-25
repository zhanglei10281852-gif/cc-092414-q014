from __future__ import annotations


def make_lot(client, code, quantity_kg=500.0):
    response = client.post(
        "/api/food/lots",
        json={
            "lot_code": code,
            "product_name": "菠菜",
            "category": "叶菜",
            "supplier": "安心农场",
            "origin": "山东寿光",
            "harvest_date": "2026-09-20",
            "quantity_kg": quantity_kg,
            "trace_code": code + "-TRACE",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def release(client, lot_id):
    response = client.post(
        f"/api/food/lots/{lot_id}/risk",
        json={"decision": "release", "reason": "检测合格", "operator": "监管员"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def hold(client, lot_id, decision="hold"):
    response = client.post(
        f"/api/food/lots/{lot_id}/risk",
        json={"decision": decision, "reason": "风险处置", "operator": "监管员"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def plan_payload(code, lots, capacity=1000.0, tmin=0, tmax=8, departure="2026-09-22T01:00:00+00:00"):
    """lots: list of (lot_id, qty, stop_seq, item_tmin, item_tmax)."""
    stop_seqs = sorted({entry[2] for entry in lots})
    return {
        "plan_code": code,
        "carrier": "冷链物流",
        "vehicle_no": "鲁A001",
        "capacity_kg": capacity,
        "target_temp_min": tmin,
        "target_temp_max": tmax,
        "departure_at": departure,
        "stops": [
            {
                "stop_seq": seq,
                "destination": f"节点{seq}",
                "arrive_due_at": f"2026-09-22T{5 + seq:02d}:00:00+00:00",
            }
            for seq in stop_seqs
        ],
        "items": [
            {
                "lot_id": lot_id,
                "stop_seq": seq,
                "quantity_kg": qty,
                "target_temp_min": itmin,
                "target_temp_max": itmax,
            }
            for lot_id, qty, seq, itmin, itmax in lots
        ],
    }


def create_plan(client, payload):
    response = client.post("/api/food/load-plans", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_multi_batch_plan_publish_confirm_and_capacity_accounting(client):
    lot_a = release(client, make_lot(client, "LP-001", 500)["id"])
    lot_b = release(client, make_lot(client, "LP-002", 300)["id"])
    payload = plan_payload(
        "PLAN-001",
        [
            (lot_a["id"], 200, 1, 0, 4),
            (lot_b["id"], 300, 2, 2, 8),
        ],
        capacity=600,
    )
    plan = create_plan(client, payload)
    assert plan["status"] == "draft"
    assert plan["total_quantity_kg"] == 500
    assert plan["available_capacity_kg"] == 100
    # 温区分组：0~4 为冷藏，2~8 为阴凉
    zones = {entry["zone"]: entry["quantity_kg"] for entry in plan["temp_zones"]}
    assert zones == {"chilled": 200, "cool": 300}
    # 每个批次都给出装载量、温区
    by_lot = {item["lot_id"]: item for item in plan["items"]}
    assert by_lot[lot_a["id"]]["quantity_kg"] == 200
    assert by_lot[lot_a["id"]]["temp_zone"] == "chilled"

    published = client.post(f"/api/food/load-plans/{plan['id']}/publish")
    assert published.status_code == 200, published.text
    assert published.json()["status"] == "published"

    confirmed = client.post(f"/api/food/load-plans/{plan['id']}/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"

    # 可用数量已扣减
    view = client.get(f"/api/food/lots/{lot_a['id']}/loading").json()
    assert view["reserved_kg"] == 200
    assert view["available_kg"] == 300

    # 重复确认幂等：不重复扣减
    again = client.post(f"/api/food/load-plans/{plan['id']}/confirm")
    assert again.status_code == 200
    view = client.get(f"/api/food/lots/{lot_a['id']}/loading").json()
    assert view["reserved_kg"] == 200
    assert view["available_kg"] == 300


def test_overloaded_plan_cannot_publish(client):
    lot = release(client, make_lot(client, "LP-010", 1000)["id"])
    payload = plan_payload("PLAN-010", [(lot["id"], 800, 1, 0, 4)], capacity=500)
    plan = create_plan(client, payload)
    assert plan["blocked"] is True
    assert "capacity_exceeded" in [b["code"] for b in plan["plan_blockers"]]

    response = client.post(f"/api/food/load-plans/{plan['id']}/publish")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "plan_has_blockers"


def test_pending_lot_blocked_from_loading(client):
    lot = make_lot(client, "LP-020", 500)  # pending，未放行
    payload = plan_payload("PLAN-020", [(lot["id"], 100, 1, 0, 4)])
    plan = create_plan(client, payload)
    item = plan["items"][0]
    assert item["blocked"] is True
    assert "lot_not_released" in [b["code"] for b in item["blockers"]]
    response = client.post(f"/api/food/load-plans/{plan['id']}/publish")
    assert response.status_code == 409
    # 放行后可发布
    release(client, lot["id"])
    response = client.post(f"/api/food/load-plans/{plan['id']}/publish")
    assert response.status_code == 200, response.text


def test_held_or_recalled_lot_blocks_confirm_even_after_publish(client):
    lot = release(client, make_lot(client, "LP-030", 500)["id"])
    payload = plan_payload("PLAN-030", [(lot["id"], 100, 1, 0, 4)])
    plan = create_plan(client, payload)
    assert client.post(f"/api/food/load-plans/{plan['id']}/publish").status_code == 200

    # 发布后批次被隔离：确认必须被阻止
    hold(client, lot["id"], "hold")
    response = client.post(f"/api/food/load-plans/{plan['id']}/confirm")
    assert response.status_code == 409
    detail = response.json()["detail"]["details"]
    assert "lot_held" in detail["item_blockers"][str(lot["id"])][0]["code"]
    assert client.get(f"/api/food/load-plans/{plan['id']}").json()["status"] == "published"

    # 取消隔离后可以确认
    release(client, lot["id"])
    assert client.post(f"/api/food/load-plans/{plan['id']}/confirm").status_code == 200

    # 已确认后召回：装车数量已占用，召回体现在批次装载视图的阻断标记上
    hold(client, lot["id"], "recall")
    view = client.get(f"/api/food/lots/{lot['id']}/loading").json()
    assert view["status_blocker"] == "lot_recalled"
    assert view["available_kg"] == 400  # 已确认扣减不受影响


def test_published_plan_change_generates_diff_and_requires_reconfirm(client):
    lot_a = release(client, make_lot(client, "LP-040", 500)["id"])
    lot_b = release(client, make_lot(client, "LP-041", 500)["id"])
    payload = plan_payload("PLAN-040", [(lot_a["id"], 100, 1, 0, 4)])
    plan = create_plan(client, payload)
    client.post(f"/api/food/load-plans/{plan['id']}/publish")
    client.post(f"/api/food/load-plans/{plan['id']}/confirm")
    # 已确认计划不允许直接修改
    changed = dict(payload)
    changed["items"] = [
        {"lot_id": lot_a["id"], "stop_seq": 1, "quantity_kg": 120, "target_temp_min": 0, "target_temp_max": 4}
    ]
    response = client.put(f"/api/food/load-plans/{plan['id']}", json=changed)
    assert response.status_code == 409

    # 新计划：发布后修改，回到草稿并产生差异，需要重新发布确认
    payload2 = plan_payload("PLAN-041", [(lot_a["id"], 100, 1, 0, 4)])
    plan2 = create_plan(client, payload2)
    client.post(f"/api/food/load-plans/{plan2['id']}/publish")
    changed2 = dict(payload2)
    changed2["items"] = [
        {"lot_id": lot_a["id"], "stop_seq": 1, "quantity_kg": 150, "target_temp_min": 0, "target_temp_max": 4},
        {"lot_id": lot_b["id"], "stop_seq": 1, "quantity_kg": 80, "target_temp_min": 0, "target_temp_max": 4},
    ]
    response = client.put(f"/api/food/load-plans/{plan2['id']}", json=changed2)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "draft"
    assert body["reconfirm_required"] == 1
    diff = body["last_diff"]
    assert diff["items_changed"][0]["lot_id"] == lot_a["id"]
    assert diff["items_changed"][0]["changes"]["quantity_kg"] == {"old": 100, "new": 150}
    added = {item["lot_id"] for item in diff["items_added"]}
    assert added == {lot_b["id"]}

    # 重新发布确认后才扣减（之前未确认，无占用）
    assert client.post(f"/api/food/load-plans/{plan2['id']}/publish").status_code == 200
    assert client.post(f"/api/food/load-plans/{plan2['id']}/confirm").status_code == 200
    view = client.get(f"/api/food/lots/{lot_b['id']}/loading").json()
    assert view["reserved_kg"] == 80


def test_temp_zone_incompatibility_blocked(client):
    # 车辆温区 0~8℃，批次要求 -18~-15（冷冻），不兼容
    lot = release(client, make_lot(client, "LP-050", 500)["id"])
    payload = plan_payload("PLAN-050", [(lot["id"], 100, 1, -18, -15)], tmin=0, tmax=8)
    plan = create_plan(client, payload)
    item = plan["items"][0]
    assert "temp_zone_incompatible" in [b["code"] for b in item["blockers"]]
    assert "temp_zone_incompatible" in [b["code"] for b in plan["plan_blockers"]]
    assert client.post(f"/api/food/load-plans/{plan['id']}/publish").status_code == 409


def test_temp_zone_grouping_frozen_chilled_cool(client):
    lot1 = release(client, make_lot(client, "LP-060", 500)["id"])
    lot2 = release(client, make_lot(client, "LP-061", 500)["id"])
    lot3 = release(client, make_lot(client, "LP-062", 500)["id"])
    payload = plan_payload(
        "PLAN-060",
        [
            (lot1["id"], 100, 1, -20, -15),  # frozen
            (lot2["id"], 100, 2, 0, 4),       # chilled
            (lot3["id"], 100, 2, 6, 10),      # cool
        ],
        capacity=1000,
        tmin=-25,
        tmax=12,
    )
    plan = create_plan(client, payload)
    zones = {entry["zone"]: entry["quantity_kg"] for entry in plan["temp_zones"]}
    assert zones == {"frozen": 100, "chilled": 100, "cool": 100}


def test_stop_order_validation(client):
    lot = make_lot(client, "LP-070", 500)
    payload = plan_payload("PLAN-070", [(lot["id"], 100, 2, 0, 4)])
    # 节点2到达时间早于出发时间 -> 阻断
    payload["departure_at"] = "2026-09-22T08:00:00+00:00"
    plan = create_plan(client, payload)
    assert "stop_order_invalid" in [b["code"] for b in plan["items"][0]["blockers"]]

    # 节点到达时间必须随顺序递增
    payload["departure_at"] = "2026-09-22T01:00:00+00:00"
    payload["stops"].append({"stop_seq": 3, "destination": "节点3", "arrive_due_at": "2026-09-22T05:00:00+00:00"})
    payload["items"][0]["stop_seq"] = 2
    response = client.post("/api/food/load-plans", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "stop_order_invalid"


def test_available_quantity_shared_across_plans(client):
    lot = release(client, make_lot(client, "LP-080", 100)["id"])
    payload1 = plan_payload("PLAN-080", [(lot["id"], 80, 1, 0, 4)], capacity=500)
    plan1 = create_plan(client, payload1)
    client.post(f"/api/food/load-plans/{plan1['id']}/publish")
    client.post(f"/api/food/load-plans/{plan1['id']}/confirm")

    # 另一辆车再装 50kg，超出剩余 20kg，阻断
    payload2 = plan_payload(
        "PLAN-081", [(lot["id"], 50, 1, 0, 4)], capacity=500, departure="2026-09-23T01:00:00+00:00"
    )
    payload2["stops"] = [
        {"stop_seq": 1, "destination": "节点1", "arrive_due_at": "2026-09-23T05:00:00+00:00"}
    ]
    plan2 = create_plan(client, payload2)
    assert "lot_quantity_insufficient" in [b["code"] for b in plan2["items"][0]["blockers"]]

    # 取消已确认计划后释放占用，可以再装
    client.post(f"/api/food/load-plans/{plan1['id']}/cancel")
    view = client.get(f"/api/food/lots/{lot['id']}/loading").json()
    assert view["available_kg"] == 100


def test_lot_loading_view_shows_quantity_zone_and_blocker(client):
    lot = make_lot(client, "LP-090", 500)
    payload = plan_payload("PLAN-090", [(lot["id"], 100, 1, 0, 4)])
    create_plan(client, payload)
    view = client.get(f"/api/food/lots/{lot['id']}/loading").json()
    entry = view["plans"][0]
    assert entry["load_quantity_kg"] == 100
    assert entry["temp_zone"] == "chilled"
    assert view["status_blocker"] == "lot_not_released"
    assert any(b["code"] == "lot_not_released" for b in entry["blockers"])


def test_missing_stop_rejected(client):
    lot = release(client, make_lot(client, "LP-100", 500)["id"])
    payload = plan_payload("PLAN-100", [(lot["id"], 100, 3, 0, 4)])  # 引用节点3
    payload["stops"] = [
        {"stop_seq": 1, "destination": "节点1", "arrive_due_at": "2026-09-22T06:00:00+00:00"}
    ]  # 但计划里只有节点1，没有节点3
    response = client.post("/api/food/load-plans", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "stop_not_found"


def test_duplicate_lot_in_plan_rejected(client):
    lot = release(client, make_lot(client, "LP-110", 500)["id"])
    payload = plan_payload("PLAN-110", [(lot["id"], 100, 1, 0, 4), (lot["id"], 50, 1, 0, 4)])
    response = client.post("/api/food/load-plans", json=payload)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "lot_duplicate_in_plan"
