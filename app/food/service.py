from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from app.database import get_connection, transaction


SCHEMA = """
CREATE TABLE IF NOT EXISTS food_lots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_code TEXT NOT NULL UNIQUE,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL,
    supplier TEXT NOT NULL,
    origin TEXT NOT NULL,
    harvest_date TEXT NOT NULL,
    quantity_kg REAL NOT NULL,
    trace_code TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','testing','released','held','recalled','destroyed')),
    risk_level TEXT NOT NULL DEFAULT 'unknown' CHECK(risk_level IN ('unknown','low','medium','high','critical')),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    sample_code TEXT NOT NULL UNIQUE,
    collected_at TEXT NOT NULL,
    collector TEXT NOT NULL,
    location TEXT NOT NULL,
    sample_weight_g REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'collected' CHECK(status IN ('collected','in_lab','complete','void')),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_test_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sample_id INTEGER NOT NULL REFERENCES food_samples(id) ON DELETE RESTRICT,
    analyte TEXT NOT NULL,
    method TEXT NOT NULL,
    value_mg_kg REAL NOT NULL,
    limit_mg_kg REAL NOT NULL,
    unit TEXT NOT NULL,
    lab_operator TEXT NOT NULL,
    tested_at TEXT NOT NULL,
    certificate_no TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL CHECK(verdict IN ('pass','fail')),
    result_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(sample_id, analyte, method, tested_at)
);
CREATE TABLE IF NOT EXISTS food_shipments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    shipment_code TEXT NOT NULL UNIQUE,
    carrier TEXT NOT NULL,
    vehicle_no TEXT NOT NULL,
    departure_at TEXT NOT NULL,
    arrival_due_at TEXT NOT NULL,
    destination TEXT NOT NULL,
    target_temp_min REAL NOT NULL,
    target_temp_max REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned' CHECK(status IN ('planned','in_transit','arrived','delayed','cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_temperatures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id INTEGER NOT NULL REFERENCES food_shipments(id) ON DELETE CASCADE,
    recorded_at TEXT NOT NULL,
    temperature_c REAL NOT NULL,
    source TEXT NOT NULL,
    in_range INTEGER NOT NULL CHECK(in_range IN (0,1)),
    created_at TEXT NOT NULL,
    UNIQUE(shipment_id, recorded_at)
);
CREATE TABLE IF NOT EXISTS food_risk_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    decision TEXT NOT NULL CHECK(decision IN ('release','hold','recall','destroy')),
    reason TEXT NOT NULL,
    operator TEXT NOT NULL,
    previous_status TEXT NOT NULL,
    new_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_load_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_code TEXT NOT NULL UNIQUE,
    carrier TEXT NOT NULL,
    vehicle_no TEXT NOT NULL,
    capacity_kg REAL NOT NULL,
    target_temp_min REAL NOT NULL,
    target_temp_max REAL NOT NULL,
    departure_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','confirmed','cancelled')),
    version INTEGER NOT NULL DEFAULT 1,
    reconfirm_required INTEGER NOT NULL DEFAULT 0 CHECK(reconfirm_required IN (0,1)),
    content_json TEXT NOT NULL DEFAULT '{}',
    published_json TEXT,
    last_diff_json TEXT,
    published_at TEXT,
    confirmed_at TEXT,
    cancelled_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_load_stops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES food_load_plans(id) ON DELETE CASCADE,
    stop_seq INTEGER NOT NULL CHECK(stop_seq >= 1),
    destination TEXT NOT NULL,
    arrive_due_at TEXT NOT NULL,
    UNIQUE(plan_id, stop_seq)
);
CREATE TABLE IF NOT EXISTS food_load_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES food_load_plans(id) ON DELETE CASCADE,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    stop_seq INTEGER NOT NULL,
    quantity_kg REAL NOT NULL CHECK(quantity_kg > 0),
    target_temp_min REAL NOT NULL,
    target_temp_max REAL NOT NULL,
    temp_zone TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(plan_id, lot_id)
);
CREATE TABLE IF NOT EXISTS food_load_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES food_load_plans(id) ON DELETE CASCADE,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    quantity_kg REAL NOT NULL CHECK(quantity_kg > 0),
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, lot_id)
);
CREATE INDEX IF NOT EXISTS idx_food_samples_lot ON food_samples(lot_id, collected_at);
CREATE INDEX IF NOT EXISTS idx_food_results_sample ON food_test_results(sample_id, tested_at);
CREATE INDEX IF NOT EXISTS idx_food_shipments_lot ON food_shipments(lot_id, departure_at);
CREATE INDEX IF NOT EXISTS idx_food_load_items_lot ON food_load_items(lot_id);
CREATE INDEX IF NOT EXISTS idx_food_load_reservations_lot ON food_load_reservations(lot_id);
"""


TEMP_ZONE_FROZEN = "frozen"
TEMP_ZONE_CHILLED = "chilled"
TEMP_ZONE_COOL = "cool"
TEMP_ZONE_AMBIENT = "ambient"

TEMP_ZONE_LABELS = {
    TEMP_ZONE_FROZEN: "冷冻",
    TEMP_ZONE_CHILLED: "冷藏",
    TEMP_ZONE_COOL: "阴凉",
    TEMP_ZONE_AMBIENT: "常温",
}

LOT_BLOCKER_MESSAGES = {
    "lot_not_released": "批次尚未放行，待检或待处理批次不能装车",
    "lot_held": "批次已被隔离，禁止装车",
    "lot_recalled": "批次已被召回，禁止装车",
    "lot_destroyed": "批次已销毁，不能装车",
    "lot_quantity_insufficient": "装载量超过批次当前可用数量",
    "stop_not_found": "批次指定的卸货节点不在计划节点序列中",
}

PLAN_BLOCKER_MESSAGES = {
    "capacity_exceeded": "装载总量超过车辆容量",
    "temp_zone_incompatible": "温区不兼容，存在温区交叉为空的批次",
    "stop_seq_duplicate": "节点顺序重复",
    "stop_order_invalid": "节点到达时间与顺序不一致",
}


class LoadPlanValidationError(ValueError):
    """装车计划结构性/状态校验失败，details 携带可展示的明细。"""

    def __init__(self, code: str, details: dict[str, Any] | None = None, status_code: int = 400):
        super().__init__(code)
        self.code = code
        self.details = details or {}
        self.status_code = status_code


def temp_zone_for(temp_min: float, temp_max: float) -> str:
    if temp_max <= -12:
        return TEMP_ZONE_FROZEN
    if temp_min >= 10:
        return TEMP_ZONE_AMBIENT
    if temp_max <= 5:
        return TEMP_ZONE_CHILLED
    return TEMP_ZONE_COOL


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    get_connection().executescript(SCHEMA)


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def _result_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class FoodService:
    """食品批次、检测与运输流程的事务边界。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    def create_lot(self, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO food_lots(lot_code,product_name,category,supplier,origin,harvest_date,quantity_kg,trace_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (payload["lot_code"], payload["product_name"], payload["category"], payload["supplier"], payload["origin"], payload["harvest_date"], payload["quantity_kg"], payload["trace_code"], now, now),
            )
            lot_id = cursor.lastrowid
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "lot.create", actor, json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()) or {}

    def get_lot(self, lot_id: int, details: bool = True) -> dict[str, Any] | None:
        lot = self.connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            return None
        result = dict(lot)
        if details:
            samples = self.connection.execute("SELECT * FROM food_samples WHERE lot_id=? ORDER BY collected_at,id", (lot_id,)).fetchall()
            shipments = self.connection.execute("SELECT * FROM food_shipments WHERE lot_id=? ORDER BY departure_at,id", (lot_id,)).fetchall()
            result["samples"] = []
            for sample in samples:
                item = dict(sample)
                item["results"] = [dict(row) for row in self.connection.execute("SELECT * FROM food_test_results WHERE sample_id=? ORDER BY tested_at,id", (sample["id"],)).fetchall()]
                result["samples"].append(item)
            result["shipments"] = [dict(row) for row in shipments]
        return result

    def add_sample(self, lot_id: int, payload: dict[str, Any], actor: str = "inspector") -> dict[str, Any]:
        if self.connection.execute("SELECT id FROM food_lots WHERE id=?", (lot_id,)).fetchone() is None:
            raise KeyError("lot_not_found")
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute("INSERT INTO food_samples(lot_id,sample_code,collected_at,collector,location,sample_weight_g,status,created_at) VALUES(?,?,?,?,?,?,?,?)", (lot_id, payload["sample_code"], payload["collected_at"], payload["collector"], payload["location"], payload["sample_weight_g"], "collected", now))
            connection.execute("UPDATE food_lots SET status='testing',version=version+1,updated_at=? WHERE id=? AND status='pending'", (now, lot_id))
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "sample.collect", actor, json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_samples WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def add_result(self, sample_id: int, payload: dict[str, Any], actor: str = "lab") -> dict[str, Any]:
        sample = self.connection.execute("SELECT * FROM food_samples WHERE id=?", (sample_id,)).fetchone()
        if sample is None:
            raise KeyError("sample_not_found")
        verdict = "pass" if payload["value_mg_kg"] <= payload["limit_mg_kg"] else "fail"
        result_hash = _result_hash({**payload, "verdict": verdict, "sample_id": sample_id})
        now = _now()
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM food_test_results WHERE sample_id=? AND analyte=? AND method=? AND tested_at=?", (sample_id, payload["analyte"], payload["method"], payload["tested_at"])).fetchone()
            if existing:
                return dict(existing)
            cursor = connection.execute("INSERT INTO food_test_results(sample_id,analyte,method,value_mg_kg,limit_mg_kg,unit,lab_operator,tested_at,certificate_no,verdict,result_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (sample_id, payload["analyte"], payload["method"], payload["value_mg_kg"], payload["limit_mg_kg"], payload["unit"], payload["lab_operator"], payload["tested_at"], payload["certificate_no"], verdict, result_hash, now))
            connection.execute("UPDATE food_samples SET status='complete' WHERE id=?", (sample_id,))
            lot_id = sample["lot_id"]
            failed = connection.execute("SELECT COUNT(*) FROM food_test_results WHERE sample_id=? AND verdict='fail'", (sample_id,)).fetchone()[0]
            if failed:
                connection.execute("UPDATE food_lots SET risk_level='high',status='held',version=version+1,updated_at=? WHERE id=?", (now, lot_id))
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "test.result", actor, json.dumps({**payload, "verdict": verdict}, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_test_results WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def create_shipment(self, lot_id: int, payload: dict[str, Any], actor: str = "dispatcher") -> dict[str, Any]:
        lot = self.connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            raise KeyError("lot_not_found")
        if lot["status"] in {"held", "recalled", "destroyed"}:
            raise ValueError("lot_not_releasable")
        if payload["target_temp_min"] > payload["target_temp_max"]:
            raise ValueError("temperature_range_invalid")
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute("INSERT INTO food_shipments(lot_id,shipment_code,carrier,vehicle_no,departure_at,arrival_due_at,destination,target_temp_min,target_temp_max,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (lot_id, payload["shipment_code"], payload["carrier"], payload["vehicle_no"], payload["departure_at"], payload["arrival_due_at"], payload["destination"], payload["target_temp_min"], payload["target_temp_max"], now, now))
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "shipment.plan", actor, json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_shipments WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def add_temperature(self, shipment_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        shipment = self.connection.execute("SELECT * FROM food_shipments WHERE id=?", (shipment_id,)).fetchone()
        if shipment is None:
            raise KeyError("shipment_not_found")
        in_range = int(shipment["target_temp_min"] <= payload["temperature_c"] <= shipment["target_temp_max"])
        now = _now()
        with transaction(immediate=True) as connection:
            existing = connection.execute("SELECT * FROM food_temperatures WHERE shipment_id=? AND recorded_at=?", (shipment_id, payload["recorded_at"])).fetchone()
            if existing:
                return dict(existing)
            cursor = connection.execute("INSERT INTO food_temperatures(shipment_id,recorded_at,temperature_c,source,in_range,created_at) VALUES(?,?,?,?,?,?)", (shipment_id, payload["recorded_at"], payload["temperature_c"], payload["source"], in_range, now))
            if not in_range:
                connection.execute("UPDATE food_shipments SET status='delayed',updated_at=? WHERE id=? AND status IN ('planned','in_transit')", (now, shipment_id))
            return _dict(connection.execute("SELECT * FROM food_temperatures WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def decide_risk(self, lot_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            lot = connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
            if lot is None:
                raise KeyError("lot_not_found")
            mapping = {"release": "released", "hold": "held", "recall": "recalled", "destroy": "destroyed"}
            new_status = mapping[payload["decision"]]
            now = _now()
            connection.execute("UPDATE food_lots SET status=?,version=version+1,updated_at=? WHERE id=?", (new_status, now, lot_id))
            connection.execute("INSERT INTO food_risk_actions(lot_id,decision,reason,operator,previous_status,new_status,created_at) VALUES(?,?,?,?,?,?,?)", (lot_id, payload["decision"], payload["reason"], payload["operator"], lot["status"], new_status, now))
            connection.execute("INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(?,?,?,?,?)", (lot_id, "risk." + payload["decision"], payload["operator"], json.dumps(payload, ensure_ascii=False), now))
            return _dict(connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()) or {}

    def summary(self, lot_id: int) -> dict[str, Any]:
        lot = self.get_lot(lot_id, details=False)
        if lot is None:
            raise KeyError("lot_not_found")
        sample_count = self.connection.execute("SELECT COUNT(*) FROM food_samples WHERE lot_id=?", (lot_id,)).fetchone()[0]
        result_count = self.connection.execute("SELECT COUNT(*) FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id WHERE s.lot_id=?", (lot_id,)).fetchone()[0]
        failed_count = self.connection.execute("SELECT COUNT(*) FROM food_test_results r JOIN food_samples s ON s.id=r.sample_id WHERE s.lot_id=? AND r.verdict='fail'", (lot_id,)).fetchone()[0]
        temperature_count = self.connection.execute("SELECT COUNT(*) FROM food_temperatures t JOIN food_shipments s ON s.id=t.shipment_id WHERE s.lot_id=?", (lot_id,)).fetchone()[0]
        return {"lot": lot, "sample_count": sample_count, "result_count": result_count, "failed_count": failed_count, "temperature_count": temperature_count}

    # ------------------------------------------------------------------
    # 多批次装车计划
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_ts(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise LoadPlanValidationError("time_format_invalid", {"value": value}) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    def _normalize_stops(self, stops: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not stops:
            raise LoadPlanValidationError("stops_required")
        normalized: list[dict[str, Any]] = []
        seen: set[int] = set()
        for stop in stops:
            seq = int(stop["stop_seq"])
            if seq < 1 or seq in seen:
                raise LoadPlanValidationError("stop_seq_invalid", {"stop_seq": seq})
            seen.add(seq)
            arrive = stop["arrive_due_at"]
            self._parse_ts(arrive)
            destination = str(stop["destination"]).strip()
            if not destination:
                raise LoadPlanValidationError("stop_destination_required", {"stop_seq": seq})
            normalized.append({"stop_seq": seq, "destination": destination, "arrive_due_at": arrive})
        normalized.sort(key=lambda item: item["stop_seq"])
        previous: datetime | None = None
        for stop in normalized:
            arrive = self._parse_ts(stop["arrive_due_at"])
            if previous is not None and arrive < previous:
                raise LoadPlanValidationError(
                    "stop_order_invalid",
                    {"stop_seq": stop["stop_seq"]},
                )
            previous = arrive
        return normalized

    def _normalize_items(self, payload_items: list[dict[str, Any]], stop_seqs: set[int]) -> list[dict[str, Any]]:
        if not payload_items:
            raise LoadPlanValidationError("items_required")
        normalized: list[dict[str, Any]] = []
        seen_lots: set[int] = set()
        for item in payload_items:
            lot_id = int(item["lot_id"])
            if lot_id in seen_lots:
                raise LoadPlanValidationError("lot_duplicate_in_plan", {"lot_id": lot_id})
            seen_lots.add(lot_id)
            seq = int(item["stop_seq"])
            if seq not in stop_seqs:
                raise LoadPlanValidationError("stop_not_found", {"lot_id": lot_id, "stop_seq": seq})
            qty = float(item["quantity_kg"])
            if qty <= 0:
                raise LoadPlanValidationError("quantity_invalid", {"lot_id": lot_id})
            tmin, tmax = float(item["target_temp_min"]), float(item["target_temp_max"])
            if tmin > tmax:
                raise LoadPlanValidationError("temperature_range_invalid", {"lot_id": lot_id})
            normalized.append(
                {
                    "lot_id": lot_id,
                    "stop_seq": seq,
                    "quantity_kg": qty,
                    "target_temp_min": tmin,
                    "target_temp_max": tmax,
                    "temp_zone": temp_zone_for(tmin, tmax),
                }
            )
        return normalized

    def _blockers(
        self,
        connection: sqlite3.Connection,
        vehicle: dict[str, Any],
        stops: list[dict[str, Any]],
        items: list[dict[str, Any]],
        exclude_plan_id: int | None,
    ) -> dict[str, Any]:
        """实时计算计划级与批次级阻断原因；不做结构校验（调用前已归一化）。"""
        item_blockers: dict[int, list[str]] = {}
        plan_blockers: list[str] = []
        stop_by_seq = {stop["stop_seq"]: stop for stop in stops}

        total_qty = 0.0
        zone_totals: dict[str, float] = {}
        departure = self._parse_ts(vehicle["departure_at"])
        for item in items:
            lot_id = item["lot_id"]
            codes: list[str] = []
            lot = connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
            if lot is None:
                raise KeyError("lot_not_found")
            status = lot["status"]
            if status == "held":
                codes.append("lot_held")
            elif status == "recalled":
                codes.append("lot_recalled")
            elif status == "destroyed":
                codes.append("lot_destroyed")
            elif status != "released":
                codes.append("lot_not_released")

            reserved = connection.execute(
                """
                SELECT COALESCE(SUM(r.quantity_kg),0)
                FROM food_load_reservations r
                JOIN food_load_plans p ON p.id = r.plan_id
                WHERE r.lot_id = ? AND p.status != 'cancelled' AND r.plan_id IS NOT ?
                """,
                (lot_id, exclude_plan_id or -1),
            ).fetchone()[0]
            available = float(lot["quantity_kg"]) - float(reserved)
            if item["quantity_kg"] > available + 1e-9:
                codes.append("lot_quantity_insufficient")

            if not (
                vehicle["target_temp_min"] <= item["target_temp_min"]
                and item["target_temp_max"] <= vehicle["target_temp_max"]
            ):
                codes.append("temp_zone_incompatible")
                if "temp_zone_incompatible" not in plan_blockers:
                    plan_blockers.append("temp_zone_incompatible")

            arrive = self._parse_ts(stop_by_seq[item["stop_seq"]]["arrive_due_at"])
            if arrive < departure:
                codes.append("stop_order_invalid")
                if "stop_order_invalid" not in plan_blockers:
                    plan_blockers.append("stop_order_invalid")

            total_qty += item["quantity_kg"]
            zone_totals[item["temp_zone"]] = zone_totals.get(item["temp_zone"], 0.0) + item["quantity_kg"]
            if codes:
                item_blockers[lot_id] = codes

        if total_qty > float(vehicle["capacity_kg"]) + 1e-9:
            plan_blockers.append("capacity_exceeded")

        return {
            "plan_blockers": plan_blockers,
            "item_blockers": item_blockers,
            "total_quantity_kg": round(total_qty, 6),
            "available_capacity_kg": round(float(vehicle["capacity_kg"]) - total_qty, 6),
            "zone_totals_kg": {zone: round(qty, 6) for zone, qty in sorted(zone_totals.items())},
        }

    def _content_snapshot(
        self,
        vehicle: dict[str, Any],
        stops: list[dict[str, Any]],
        items: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "vehicle": {
                key: vehicle[key]
                for key in ("carrier", "vehicle_no", "capacity_kg", "target_temp_min", "target_temp_max", "departure_at")
            },
            "stops": [
                {"stop_seq": stop["stop_seq"], "destination": stop["destination"], "arrive_due_at": stop["arrive_due_at"]}
                for stop in stops
            ],
            "items": [
                {
                    "lot_id": item["lot_id"],
                    "stop_seq": item["stop_seq"],
                    "quantity_kg": item["quantity_kg"],
                    "target_temp_min": item["target_temp_min"],
                    "target_temp_max": item["target_temp_max"],
                    "temp_zone": item["temp_zone"],
                }
                for item in items
            ],
        }

    @staticmethod
    def _diff_snapshots(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
        diff: dict[str, Any] = {"vehicle_changed": {}, "stops_added": [], "stops_removed": [], "stops_changed": [], "items_added": [], "items_removed": [], "items_changed": []}
        for key, value in new["vehicle"].items():
            if old.get("vehicle", {}).get(key) != value:
                diff["vehicle_changed"][key] = {"old": old.get("vehicle", {}).get(key), "new": value}
        old_stops = {stop["stop_seq"]: stop for stop in old.get("stops", [])}
        new_stops = {stop["stop_seq"]: stop for stop in new["stops"]}
        for seq, stop in new_stops.items():
            if seq not in old_stops:
                diff["stops_added"].append(stop)
            elif old_stops[seq] != stop:
                diff["stops_changed"].append({"stop_seq": seq, "old": old_stops[seq], "new": stop})
        for seq, stop in old_stops.items():
            if seq not in new_stops:
                diff["stops_removed"].append(stop)
        old_items = {item["lot_id"]: item for item in old.get("items", [])}
        new_items = {item["lot_id"]: item for item in new["items"]}
        for lot_id, item in new_items.items():
            if lot_id not in old_items:
                diff["items_added"].append(item)
            else:
                before = old_items[lot_id]
                changed = {
                    key: {"old": before.get(key), "new": item[key]}
                    for key in ("stop_seq", "quantity_kg", "target_temp_min", "target_temp_max", "temp_zone")
                    if before.get(key) != item[key]
                }
                if changed:
                    diff["items_changed"].append({"lot_id": lot_id, "changes": changed})
        for lot_id, item in old_items.items():
            if lot_id not in new_items:
                diff["items_removed"].append(item)
        return diff

    @staticmethod
    def _has_changes(diff: dict[str, Any]) -> bool:
        return any(
            diff[key]
            for key in ("vehicle_changed", "stops_added", "stops_removed", "stops_changed", "items_added", "items_removed", "items_changed")
        )

    def create_load_plan(self, payload: dict[str, Any], actor: str = "dispatcher") -> dict[str, Any]:
        if payload["target_temp_min"] > payload["target_temp_max"]:
            raise LoadPlanValidationError("temperature_range_invalid")
        stops = self._normalize_stops(payload["stops"])
        items = self._normalize_items(payload["items"], {stop["stop_seq"] for stop in stops})
        vehicle = {
            "carrier": payload["carrier"],
            "vehicle_no": payload["vehicle_no"],
            "capacity_kg": float(payload["capacity_kg"]),
            "target_temp_min": float(payload["target_temp_min"]),
            "target_temp_max": float(payload["target_temp_max"]),
            "departure_at": payload["departure_at"],
        }
        self._parse_ts(vehicle["departure_at"])
        now = _now()
        with transaction(immediate=True) as connection:
            blockers = self._blockers(connection, vehicle, stops, items, exclude_plan_id=None)
            cursor = connection.execute(
                """INSERT INTO food_load_plans(plan_code,carrier,vehicle_no,capacity_kg,target_temp_min,target_temp_max,
                   departure_at,status,content_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,'draft',?,?,?)""",
                (
                    payload["plan_code"], vehicle["carrier"], vehicle["vehicle_no"], vehicle["capacity_kg"],
                    vehicle["target_temp_min"], vehicle["target_temp_max"], vehicle["departure_at"],
                    json.dumps(self._content_snapshot(vehicle, stops, items), ensure_ascii=False), now, now,
                ),
            )
            plan_id = cursor.lastrowid
            for stop in stops:
                connection.execute(
                    "INSERT INTO food_load_stops(plan_id,stop_seq,destination,arrive_due_at) VALUES(?,?,?,?)",
                    (plan_id, stop["stop_seq"], stop["destination"], stop["arrive_due_at"]),
                )
            for item in items:
                connection.execute(
                    """INSERT INTO food_load_items(plan_id,lot_id,stop_seq,quantity_kg,target_temp_min,target_temp_max,
                       temp_zone,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (plan_id, item["lot_id"], item["stop_seq"], item["quantity_kg"], item["target_temp_min"],
                     item["target_temp_max"], item["temp_zone"], now, now),
                )
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(NULL,?,?,?,?)",
                ("load_plan.create", actor, json.dumps({"plan_id": plan_id, "plan_code": payload["plan_code"]}, ensure_ascii=False), now),
            )
        return self.get_load_plan(plan_id)

    def _load_plan_or_raise(self, connection: sqlite3.Connection, plan_id: int) -> sqlite3.Row:
        plan = connection.execute("SELECT * FROM food_load_plans WHERE id=?", (plan_id,)).fetchone()
        if plan is None:
            raise KeyError("load_plan_not_found")
        return plan

    def _replace_plan_content(
        self,
        connection: sqlite3.Connection,
        plan_id: int,
        vehicle: dict[str, Any],
        stops: list[dict[str, Any]],
        items: list[dict[str, Any]],
        now: str,
    ) -> None:
        connection.execute(
            """UPDATE food_load_plans SET carrier=?,vehicle_no=?,capacity_kg=?,target_temp_min=?,target_temp_max=?,
               departure_at=?,content_json=?,updated_at=? WHERE id=?""",
            (vehicle["carrier"], vehicle["vehicle_no"], vehicle["capacity_kg"], vehicle["target_temp_min"],
             vehicle["target_temp_max"], vehicle["departure_at"],
             json.dumps(self._content_snapshot(vehicle, stops, items), ensure_ascii=False), now, plan_id),
        )
        connection.execute("DELETE FROM food_load_stops WHERE plan_id=?", (plan_id,))
        connection.execute("DELETE FROM food_load_items WHERE plan_id=?", (plan_id,))
        for stop in stops:
            connection.execute(
                "INSERT INTO food_load_stops(plan_id,stop_seq,destination,arrive_due_at) VALUES(?,?,?,?)",
                (plan_id, stop["stop_seq"], stop["destination"], stop["arrive_due_at"]),
            )
        for item in items:
            connection.execute(
                """INSERT INTO food_load_items(plan_id,lot_id,stop_seq,quantity_kg,target_temp_min,target_temp_max,
                   temp_zone,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                (plan_id, item["lot_id"], item["stop_seq"], item["quantity_kg"], item["target_temp_min"],
                 item["target_temp_max"], item["temp_zone"], now, now),
            )

    def update_load_plan(self, plan_id: int, payload: dict[str, Any], actor: str = "dispatcher") -> dict[str, Any]:
        if payload["target_temp_min"] > payload["target_temp_max"]:
            raise LoadPlanValidationError("temperature_range_invalid")
        stops = self._normalize_stops(payload["stops"])
        items = self._normalize_items(payload["items"], {stop["stop_seq"] for stop in stops})
        vehicle = {
            "carrier": payload["carrier"],
            "vehicle_no": payload["vehicle_no"],
            "capacity_kg": float(payload["capacity_kg"]),
            "target_temp_min": float(payload["target_temp_min"]),
            "target_temp_max": float(payload["target_temp_max"]),
            "departure_at": payload["departure_at"],
        }
        self._parse_ts(vehicle["departure_at"])
        new_snapshot = self._content_snapshot(vehicle, stops, items)
        now = _now()
        with transaction(immediate=True) as connection:
            plan = self._load_plan_or_raise(connection, plan_id)
            if plan["status"] == "cancelled":
                raise LoadPlanValidationError("plan_cancelled", {"plan_id": plan_id}, status_code=409)
            if plan["status"] == "confirmed":
                raise LoadPlanValidationError("plan_already_confirmed", {"plan_id": plan_id}, status_code=409)

            old_snapshot = json.loads(plan["published_json"] or plan["content_json"] or "{}")
            diff = self._diff_snapshots(old_snapshot, new_snapshot)
            if not self._has_changes(diff):
                return self.get_load_plan(plan_id)

            self._replace_plan_content(connection, plan_id, vehicle, stops, items, now)
            if plan["published_json"]:
                # 已发布计划的修改：记录差异并要求重新确认
                connection.execute(
                    "UPDATE food_load_plans SET status='draft',reconfirm_required=1,last_diff_json=?,updated_at=? WHERE id=?",
                    (json.dumps(diff, ensure_ascii=False), now, plan_id),
                )
            else:
                connection.execute("UPDATE food_load_plans SET updated_at=? WHERE id=?", (now, plan_id))
            blockers = self._blockers(connection, vehicle, stops, items, exclude_plan_id=plan_id)
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(NULL,?,?,?,?)",
                ("load_plan.update", actor, json.dumps({"plan_id": plan_id, "diff": diff, "blockers": blockers}, ensure_ascii=False), now),
            )
        return self.get_load_plan(plan_id)

    def publish_load_plan(self, plan_id: int, actor: str = "dispatcher") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            plan = self._load_plan_or_raise(connection, plan_id)
            if plan["status"] == "cancelled":
                raise LoadPlanValidationError("plan_cancelled", {"plan_id": plan_id}, status_code=409)
            if plan["status"] == "confirmed":
                raise LoadPlanValidationError("plan_already_confirmed", {"plan_id": plan_id}, status_code=409)
            snapshot = json.loads(plan["content_json"])
            blockers = self._blockers(
                connection,
                snapshot["vehicle"],
                snapshot["stops"],
                snapshot["items"],
                exclude_plan_id=plan_id,
            )
            if blockers["plan_blockers"] or blockers["item_blockers"]:
                raise LoadPlanValidationError(
                    "plan_has_blockers",
                    {"plan_id": plan_id, **self._decorate_blockers(blockers)},
                    status_code=409,
                )
            connection.execute(
                "UPDATE food_load_plans SET status='published',published_json=?,published_at=COALESCE(published_at,?),updated_at=? WHERE id=?",
                (plan["content_json"], now, now, plan_id),
            )
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(NULL,?,?,?,?)",
                ("load_plan.publish", actor, json.dumps({"plan_id": plan_id}, ensure_ascii=False), now),
            )
        return self.get_load_plan(plan_id)

    def confirm_load_plan(self, plan_id: int, actor: str = "loader") -> dict[str, Any]:
        """装车确认：冻结阻断校验并扣减批次可用数量。重复确认幂等，不重复扣减。"""
        with transaction(immediate=True) as connection:
            plan = self._load_plan_or_raise(connection, plan_id)
            if plan["status"] == "cancelled":
                raise LoadPlanValidationError("plan_cancelled", {"plan_id": plan_id}, status_code=409)
            if plan["status"] == "confirmed":
                # 幂等：已确认过，直接返回当前状态，不再扣减。
                return self.get_load_plan(plan_id)
            if plan["status"] != "published":
                raise LoadPlanValidationError("plan_not_published", {"plan_id": plan_id}, status_code=409)

            snapshot = json.loads(plan["content_json"])
            blockers = self._blockers(
                connection,
                snapshot["vehicle"],
                snapshot["stops"],
                snapshot["items"],
                exclude_plan_id=plan_id,
            )
            if blockers["plan_blockers"] or blockers["item_blockers"]:
                raise LoadPlanValidationError(
                    "plan_has_blockers",
                    {"plan_id": plan_id, **self._decorate_blockers(blockers)},
                    status_code=409,
                )

            now = _now()
            for item in snapshot["items"]:
                connection.execute(
                    "INSERT INTO food_load_reservations(plan_id,lot_id,quantity_kg,created_at) VALUES(?,?,?,?)",
                    (plan_id, item["lot_id"], item["quantity_kg"], now),
                )
            connection.execute(
                "UPDATE food_load_plans SET status='confirmed',reconfirm_required=0,confirmed_at=?,updated_at=? WHERE id=?",
                (now, now, plan_id),
            )
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(NULL,?,?,?,?)",
                ("load_plan.confirm", actor, json.dumps({"plan_id": plan_id, "items": snapshot["items"]}, ensure_ascii=False), now),
            )
        return self.get_load_plan(plan_id)

    def cancel_load_plan(self, plan_id: int, actor: str = "dispatcher") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            plan = self._load_plan_or_raise(connection, plan_id)
            if plan["status"] == "cancelled":
                return self.get_load_plan(plan_id)
            was_confirmed = plan["status"] == "confirmed"
            connection.execute(
                "UPDATE food_load_plans SET status='cancelled',cancelled_at=?,updated_at=? WHERE id=?",
                (now, now, plan_id),
            )
            if was_confirmed:
                # 释放已扣减的可用数量，供后续装车计划重新占用。
                connection.execute("DELETE FROM food_load_reservations WHERE plan_id=?", (plan_id,))
            connection.execute(
                "INSERT INTO food_audit(lot_id,action,actor,payload_json,created_at) VALUES(NULL,?,?,?,?)",
                ("load_plan.cancel", actor, json.dumps({"plan_id": plan_id, "released_reservation": was_confirmed}, ensure_ascii=False), now),
            )
        return self.get_load_plan(plan_id)

    @staticmethod
    def _decorate_blockers(blockers: dict[str, Any]) -> dict[str, Any]:
        return {
            "plan_blockers": [
                {"code": code, "message": PLAN_BLOCKER_MESSAGES.get(code, code)} for code in blockers["plan_blockers"]
            ],
            "item_blockers": {
                str(lot_id): [{"code": code, "message": LOT_BLOCKER_MESSAGES.get(code, code) or PLAN_BLOCKER_MESSAGES.get(code, code)} for code in codes]
                for lot_id, codes in blockers["item_blockers"].items()
            },
        }

    def list_load_plans(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM food_load_plans ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def get_load_plan(self, plan_id: int) -> dict[str, Any]:
        plan = self.connection.execute("SELECT * FROM food_load_plans WHERE id=?", (plan_id,)).fetchone()
        if plan is None:
            raise KeyError("load_plan_not_found")
        result = dict(plan)
        result["stops"] = [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM food_load_stops WHERE plan_id=? ORDER BY stop_seq", (plan_id,)
            ).fetchall()
        ]
        item_rows = self.connection.execute(
            """SELECT i.*, l.lot_code, l.product_name, l.status AS lot_status, l.quantity_kg AS lot_quantity_kg,
                      s.destination
               FROM food_load_items i
               JOIN food_lots l ON l.id = i.lot_id
               JOIN food_load_stops s ON s.plan_id = i.plan_id AND s.stop_seq = i.stop_seq
               WHERE i.plan_id=? ORDER BY i.stop_seq, i.id""",
            (plan_id,),
        ).fetchall()
        snapshot = json.loads(plan["content_json"] or "{}")
        blockers = self._blockers(
            self.connection,
            snapshot.get("vehicle", result),
            snapshot.get("stops", result["stops"]),
            snapshot.get("items", []),
            exclude_plan_id=plan_id,
        ) if snapshot else {"item_blockers": {}, "plan_blockers": [], "total_quantity_kg": 0.0, "available_capacity_kg": result["capacity_kg"], "zone_totals_kg": {}}

        items: list[dict[str, Any]] = []
        for row in item_rows:
            item = dict(row)
            codes = blockers["item_blockers"].get(row["lot_id"], [])
            item["temp_zone_label"] = TEMP_ZONE_LABELS.get(row["temp_zone"], row["temp_zone"])
            item["blockers"] = [
                {"code": code, "message": LOT_BLOCKER_MESSAGES.get(code) or PLAN_BLOCKER_MESSAGES.get(code, code)}
                for code in codes
            ]
            item["blocked"] = bool(codes)
            items.append(item)
        result["items"] = items
        result["total_quantity_kg"] = blockers["total_quantity_kg"]
        result["available_capacity_kg"] = blockers["available_capacity_kg"]
        result["zone_totals_kg"] = blockers["zone_totals_kg"]
        result["temp_zones"] = [
            {"zone": zone, "label": TEMP_ZONE_LABELS.get(zone, zone), "quantity_kg": qty}
            for zone, qty in blockers["zone_totals_kg"].items()
        ]
        result["plan_blockers"] = [
            {"code": code, "message": PLAN_BLOCKER_MESSAGES.get(code, code)} for code in blockers["plan_blockers"]
        ]
        result["blocked"] = bool(blockers["plan_blockers"] or blockers["item_blockers"])
        result["last_diff"] = json.loads(result["last_diff_json"]) if result["last_diff_json"] else None
        result.pop("last_diff_json", None)
        result["published_snapshot"] = json.loads(result["published_json"]) if result["published_json"] else None
        result.pop("published_json", None)
        result["content"] = json.loads(result.pop("content_json") or "{}")
        return result

    def lot_loading_view(self, lot_id: int) -> dict[str, Any]:
        """单个批次在各装车计划中的装载量、温区与阻断原因汇总。"""
        lot = self.connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            raise KeyError("lot_not_found")
        rows = self.connection.execute(
            """SELECT i.plan_id, p.plan_code, p.status AS plan_status, i.quantity_kg, i.temp_zone,
                      i.target_temp_min, i.target_temp_max, i.stop_seq, s.destination,
                      r.quantity_kg AS reserved_kg
               FROM food_load_items i
               JOIN food_load_plans p ON p.id = i.plan_id
               JOIN food_load_stops s ON s.plan_id = i.plan_id AND s.stop_seq = i.stop_seq
               LEFT JOIN food_load_reservations r ON r.plan_id = i.plan_id AND r.lot_id = i.lot_id
               WHERE i.lot_id=? ORDER BY i.plan_id""",
            (lot_id,),
        ).fetchall()
        reserved_total = self.connection.execute(
            "SELECT COALESCE(SUM(quantity_kg),0) FROM food_load_reservations WHERE lot_id=?",
            (lot_id,),
        ).fetchone()[0]
        plans: list[dict[str, Any]] = []
        for row in rows:
            plan = self.get_load_plan(row["plan_id"])
            item = next(entry for entry in plan["items"] if entry["lot_id"] == lot_id)
            plans.append(
                {
                    "plan_id": row["plan_id"],
                    "plan_code": row["plan_code"],
                    "plan_status": row["plan_status"],
                    "load_quantity_kg": row["quantity_kg"],
                    "temp_zone": row["temp_zone"],
                    "temp_zone_label": TEMP_ZONE_LABELS.get(row["temp_zone"], row["temp_zone"]),
                    "stop_seq": row["stop_seq"],
                    "destination": row["destination"],
                    "confirmed": row["plan_status"] == "confirmed",
                    "blockers": item["blockers"],
                    "blocked": item["blocked"],
                }
            )
        lot_status = lot["status"]
        status_blocker = {
            "held": "lot_held",
            "recalled": "lot_recalled",
            "destroyed": "lot_destroyed",
        }.get(lot_status)
        if status_blocker is None and lot_status != "released":
            status_blocker = "lot_not_released"
        return {
            "lot_id": lot_id,
            "lot_code": lot["lot_code"],
            "product_name": lot["product_name"],
            "status": lot_status,
            "quantity_kg": lot["quantity_kg"],
            "reserved_kg": round(float(reserved_total), 6),
            "available_kg": round(float(lot["quantity_kg"]) - float(reserved_total), 6),
            "status_blocker": status_blocker,
            "status_blocker_message": LOT_BLOCKER_MESSAGES.get(status_blocker) if status_blocker else None,
            "plans": plans,
        }

    def delete_lot(self, lot_id: int) -> bool:
        """移除尚未关联记录的批次；关联记录的错误映射由上层负责。"""
        with transaction(immediate=True) as connection:
            cursor = connection.execute("DELETE FROM food_lots WHERE id=?", (lot_id,))
            if cursor.rowcount == 0:
                raise KeyError("lot_not_found")
            return True
