from __future__ import annotations


def _lot(client, code, quantity=500.0, tmin=0.0, tmax=8.0):
    response = client.post(
        "/api/food/lots",
        json={
            "lot_code": code,
            "product_name": "菠菜",
            "category": "叶菜",
            "supplier": "安心农场",
            "origin": "山东寿光",
            "harvest_date": "2026-09-20",
            "quantity_kg": quantity,
            "trace_code": code + "-TRACE",
            "storage_temp_min": tmin,
            "storage_temp_max": tmax,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _release(client, lot_id, operator="监管员"):
    response = client.post(f"/api/food/lots/{lot_id}/risk", json={"decision": "release", "reason": "检测合格", "operator": operator})
    assert response.status_code == 200, response.text


def _vehicle(client, no="鲁V1", capacity=1000.0, zones=None):
    payload = {"vehicle_no": no, "carrier": "冷链物流", "capacity_kg": capacity}
    if zones is not None:
        payload["zones"] = zones
    response = client.post("/api/food/vehicles", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _stops():
    return [
        {"stop_order": 1, "node": "城东市场", "planned_arrival_at": "2026-09-23T06:00:00+00:00"},
        {"stop_order": 2, "node": "城西超市", "planned_arrival_at": "2026-09-23T09:00:00+00:00"},
    ]


def _plan(client, code, vehicle_id, items, stops=None, expected=201):
    response = client.post(
        "/api/food/load-plans",
        json={
            "plan_code": code,
            "vehicle_id": vehicle_id,
            "departure_at": "2026-09-23T02:00:00+00:00",
            "stops": stops if stops is not None else _stops(),
            "items": items,
        },
    )
    assert response.status_code == expected, response.text
    return response.json()


def test_multi_batch_plan_publish_and_confirm(client):
    lot1 = _lot(client, "LOT-P1", 500)
    lot2 = _lot(client, "LOT-P2", 300)
    _release(client, lot1["id"])
    _release(client, lot2["id"])
    vehicle = _vehicle(client)
    plan = _plan(
        client,
        "PLAN-001",
        vehicle["id"],
        [
            {"lot_id": lot1["id"], "quantity_kg": 200, "stop_order": 1},
            {"lot_id": lot2["id"], "quantity_kg": 100, "stop_order": 2},
        ],
    )
    assert plan["total_kg"] == 300
    assert {group["zone_code"] for group in plan["zone_groups"]} == {"DEFAULT"}

    published = client.post(f"/api/food/load-plans/{plan['id']}/publish")
    assert published.status_code == 200, published.text
    body = published.json()
    assert body["status"] == "published" and body["content_revision"] == 1

    # 发布后预留数量占用，可用数量减少
    loading = client.get(f"/api/food/lots/{lot1['id']}/loadings").json()
    assert loading["reserved_kg"] == 200 and loading["available_kg"] == 300
    assert loading["plans"][0]["quantity_kg"] == 200

    confirmed = client.post(f"/api/food/load-plans/{plan['id']}/confirm-loading")
    assert confirmed.status_code == 200, confirmed.text
    loaded = confirmed.json()
    assert loaded["status"] == "loaded"
    loading = client.get(f"/api/food/lots/{lot1['id']}/loadings").json()
    assert loading["reserved_kg"] == 0 and loading["loaded_kg"] == 200 and loading["available_kg"] == 300

    # 重复确认幂等，不重复扣减
    again = client.post(f"/api/food/load-plans/{plan['id']}/confirm-loading")
    assert again.status_code == 200 and again.json()["idempotent"] is True
    loading = client.get(f"/api/food/lots/{lot1['id']}/loadings").json()
    assert loading["loaded_kg"] == 200


def test_overload_rejected_at_publish(client):
    lot = _lot(client, "LOT-CAP", 5000)
    _release(client, lot["id"])
    vehicle = _vehicle(client, "鲁V2", capacity=1000)
    plan = _plan(client, "PLAN-CAP", vehicle["id"], [{"lot_id": lot["id"], "quantity_kg": 1200, "stop_order": 1}], stops=_stops()[:1])
    assert any(b.get("reason") == "vehicle_capacity_exceeded" for b in plan["blockers"])
    response = client.post(f"/api/food/load-plans/{plan['id']}/publish")
    assert response.status_code == 409
    reasons = [b.get("reason") for b in response.json()["detail"]["details"]["blockers"]]
    assert "vehicle_capacity_exceeded" in reasons


def test_unreleased_and_quarantined_lots_blocked(client):
    pending = _lot(client, "LOT-PEND", 100)
    held = _lot(client, "LOT-HELD", 100)
    _release(client, held["id"])
    client.post(f"/api/food/lots/{held['id']}/risk", json={"decision": "hold", "reason": "复检异常", "operator": "监管员"})
    vehicle = _vehicle(client, "鲁V3")
    plan = _plan(
        client,
        "PLAN-BLOCK",
        vehicle["id"],
        [
            {"lot_id": pending["id"], "quantity_kg": 50, "stop_order": 1},
            {"lot_id": held["id"], "quantity_kg": 50, "stop_order": 2},
        ],
    )
    by_lot = {b["lot_id"]: b["reasons"] for b in plan["blockers"]}
    assert "lot_pending_inspection" in by_lot[pending["id"]]
    assert "lot_quarantined" in by_lot[held["id"]]
    response = client.post(f"/api/food/load-plans/{plan['id']}/publish")
    assert response.status_code == 409


def test_temperature_zone_grouping_and_incompatibility(client):
    chilled = _lot(client, "LOT-CHILL", 200, 0, 4)
    frozen = _lot(client, "LOT-FROZEN", 200, -20, -18)
    _release(client, chilled["id"])
    _release(client, frozen["id"])
    vehicle = _vehicle(
        client,
        "鲁V4",
        1000,
        zones=[
            {"zone_code": "CHILL", "zone_name": "冷藏区", "temp_min": 0, "temp_max": 6},
            {"zone_code": "FROZEN", "zone_name": "冷冻区", "temp_min": -22, "temp_max": -16},
        ],
    )
    plan = _plan(
        client,
        "PLAN-ZONE",
        vehicle["id"],
        [
            {"lot_id": chilled["id"], "quantity_kg": 100, "stop_order": 1, "zone_code": "CHILL"},
            {"lot_id": frozen["id"], "quantity_kg": 100, "stop_order": 2, "zone_code": "FROZEN"},
        ],
    )
    groups = {group["zone_code"]: group["total_kg"] for group in plan["zone_groups"]}
    assert groups == {"CHILL": 100, "FROZEN": 100}
    assert plan["items"][0]["temp_min"] == 0 and plan["items"][0]["temp_max"] == 6

    # 冷冻货要求放进冷藏区 => 温区不兼容
    bad = _plan(
        client,
        "PLAN-ZONE-BAD",
        vehicle["id"],
        [{"lot_id": frozen["id"], "quantity_kg": 50, "stop_order": 1, "zone_code": "CHILL"}],
        stops=_stops()[:1],
    )
    assert bad["blockers"][0]["reasons"] == ["temperature_zone_incompatible"]


def test_stop_sequence_validation(client):
    lot = _lot(client, "LOT-SEQ", 100)
    _release(client, lot["id"])
    vehicle = _vehicle(client, "鲁V5")
    bad_stops = [
        {"stop_order": 1, "node": "A", "planned_arrival_at": "2026-09-23T09:00:00+00:00"},
        {"stop_order": 2, "node": "B", "planned_arrival_at": "2026-09-23T08:00:00+00:00"},
    ]
    plan = _plan(client, "PLAN-SEQ", vehicle["id"], [{"lot_id": lot["id"], "quantity_kg": 10, "stop_order": 1}], stops=bad_stops)
    assert any(b.get("reason") == "stop_sequence_time_invalid" for b in plan["blockers"])
    assert client.post(f"/api/food/load-plans/{plan['id']}/publish").status_code == 409

    # 引用不存在的节点顺序
    plan2 = _plan(client, "PLAN-SEQ2", vehicle["id"], [{"lot_id": lot["id"], "quantity_kg": 10, "stop_order": 9}], stops=_stops())
    assert "stop_not_found" in plan2["blockers"][0]["reasons"]


def test_publish_then_amend_generates_diff_and_requires_reconfirm(client):
    lot1 = _lot(client, "LOT-D1", 500)
    lot2 = _lot(client, "LOT-D2", 500)
    _release(client, lot1["id"])
    _release(client, lot2["id"])
    vehicle = _vehicle(client, "鲁V6")
    plan = _plan(client, "PLAN-DIFF", vehicle["id"], [{"lot_id": lot1["id"], "quantity_kg": 100, "stop_order": 1}], stops=_stops()[:1])
    client.post(f"/api/food/load-plans/{plan['id']}/publish")

    response = client.put(
        f"/api/food/load-plans/{plan['id']}",
        json={
            "stops": _stops(),
            "items": [
                {"lot_id": lot1["id"], "quantity_kg": 150, "stop_order": 2},
                {"lot_id": lot2["id"], "quantity_kg": 80, "stop_order": 1},
            ],
        },
    )
    assert response.status_code == 200, response.text
    amended = response.json()
    assert amended["status"] == "published" and amended["content_revision"] == 2
    revisions = amended["revisions"]
    assert revisions[-1]["change_type"] == "amend"
    diff = revisions[-1]["diff_json"]
    assert diff["total_kg"] == {"from": 100, "to": 230}
    assert diff["items_added"] == [{"lot_id": lot2["id"], "quantity_kg": 80, "stop_order": 1}]
    assert diff["items_changed"][0]["changes"]["quantity_kg"] == {"from": 100, "to": 150}

    # 修订后预留数量按新计划核算
    loading1 = client.get(f"/api/food/lots/{lot1['id']}/loadings").json()
    loading2 = client.get(f"/api/food/lots/{lot2['id']}/loadings").json()
    assert loading1["reserved_kg"] == 150 and loading2["reserved_kg"] == 80

    # 已装车的计划不可再修改
    client.post(f"/api/food/load-plans/{plan['id']}/confirm-loading")
    again = client.put(
        f"/api/food/load-plans/{plan['id']}",
        json={"stops": _stops(), "items": [{"lot_id": lot1["id"], "quantity_kg": 150, "stop_order": 2}]},
    )
    assert again.status_code == 409 and again.json()["detail"]["code"] == "plan_not_editable"


def test_amend_validation_failure_keeps_published_version(client):
    lot = _lot(client, "LOT-AM", 500)
    _release(client, lot["id"])
    vehicle = _vehicle(client, "鲁V7", capacity=1000)
    plan = _plan(client, "PLAN-AM", vehicle["id"], [{"lot_id": lot["id"], "quantity_kg": 100, "stop_order": 1}], stops=_stops()[:1])
    client.post(f"/api/food/load-plans/{plan['id']}/publish")
    # 超载修订必须被拒绝
    response = client.put(
        f"/api/food/load-plans/{plan['id']}",
        json={"stops": _stops()[:1], "items": [{"lot_id": lot["id"], "quantity_kg": 1200, "stop_order": 1}]},
    )
    assert response.status_code == 409
    current = client.get(f"/api/food/load-plans/{plan['id']}").json()
    assert current["content_revision"] == 1 and current["total_kg"] == 100
    loading = client.get(f"/api/food/lots/{lot['id']}/loadings").json()
    assert loading["reserved_kg"] == 100


def test_quarantine_after_publish_blocks_loading(client):
    lot = _lot(client, "LOT-Q", 300)
    _release(client, lot["id"])
    vehicle = _vehicle(client, "鲁V8")
    plan = _plan(client, "PLAN-Q", vehicle["id"], [{"lot_id": lot["id"], "quantity_kg": 100, "stop_order": 1}], stops=_stops()[:1])
    client.post(f"/api/food/load-plans/{plan['id']}/publish")
    # 发布后批次被隔离
    client.post(f"/api/food/lots/{lot['id']}/risk", json={"decision": "hold", "reason": "接到投诉", "operator": "监管员"})
    response = client.post(f"/api/food/load-plans/{plan['id']}/confirm-loading")
    assert response.status_code == 409
    assert response.json()["detail"]["details"]["blockers"][0]["reasons"] == ["lot_quarantined"]
    # 未装车，预留仍在但未转成已装
    loading = client.get(f"/api/food/lots/{lot['id']}/loadings").json()
    assert loading["reserved_kg"] == 100 and loading["loaded_kg"] == 0

    # 召回同样阻断
    client.post(f"/api/food/lots/{lot['id']}/risk", json={"decision": "release", "reason": "解除隔离", "operator": "监管员"})
    client.post(f"/api/food/lots/{lot['id']}/risk", json={"decision": "recall", "reason": "紧急召回", "operator": "监管员"})
    response = client.post(f"/api/food/load-plans/{plan['id']}/confirm-loading")
    assert response.status_code == 409
    assert response.json()["detail"]["details"]["blockers"][0]["reasons"] == ["lot_recalled"]


def test_reservation_across_plans_and_cancel_release(client):
    lot = _lot(client, "LOT-RSV", 300)
    _release(client, lot["id"])
    vehicle = _vehicle(client, "鲁V9", 5000)
    plan1 = _plan(client, "PLAN-R1", vehicle["id"], [{"lot_id": lot["id"], "quantity_kg": 200, "stop_order": 1}], stops=_stops()[:1])
    client.post(f"/api/food/load-plans/{plan1['id']}/publish")

    # 另一辆车的计划只能使用剩余 100
    vehicle2 = _vehicle(client, "鲁V10", 5000)
    plan2 = _plan(client, "PLAN-R2", vehicle2["id"], [{"lot_id": lot["id"], "quantity_kg": 150, "stop_order": 1}], stops=_stops()[:1])
    assert "quantity_exceeds_available" in plan2["blockers"][0]["reasons"]
    assert client.post(f"/api/food/load-plans/{plan2['id']}/publish").status_code == 409

    # 取消已发布计划释放预留
    assert client.post(f"/api/food/load-plans/{plan1['id']}/cancel").status_code == 200
    loading = client.get(f"/api/food/lots/{lot['id']}/loadings").json()
    assert loading["reserved_kg"] == 0 and loading["available_kg"] == 300

    # 取消后 plan2 可发布
    response = client.post(f"/api/food/load-plans/{plan2['id']}/publish")
    assert response.status_code == 200, response.text


def test_lot_loadings_query_shape(client):
    lot = _lot(client, "LOT-VIEW", 400)
    _release(client, lot["id"])
    vehicle = _vehicle(client, "鲁V11", zones=[{"zone_code": "CHILL", "zone_name": "冷藏区", "temp_min": 0, "temp_max": 8}])
    plan = _plan(client, "PLAN-VIEW", vehicle["id"], [{"lot_id": lot["id"], "quantity_kg": 120, "stop_order": 2, "zone_code": "CHILL"}], stops=_stops())
    client.post(f"/api/food/load-plans/{plan['id']}/publish")
    view = client.get(f"/api/food/lots/{lot['id']}/loadings")
    assert view.status_code == 200
    data = view.json()
    entry = data["plans"][0]
    assert entry["quantity_kg"] == 120 and entry["zone_code"] == "CHILL"
    assert entry["temp_min"] == 0 and entry["temp_max"] == 8 and entry["vehicle_no"] == "鲁V11"
    assert entry["blocking_reasons"] == []

    plan_detail = client.get(f"/api/food/load-plans/{plan['id']}").json()
    item = plan_detail["items"][0]
    assert item["lot_code"] == "LOT-VIEW" and item["quantity_kg"] == 120
    assert item["blocking_reasons"] == []
    assert plan_detail["stops"][1]["node"] == "城西超市"
