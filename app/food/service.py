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
    reserved_kg REAL NOT NULL DEFAULT 0,
    loaded_kg REAL NOT NULL DEFAULT 0,
    storage_temp_min REAL NOT NULL DEFAULT 0,
    storage_temp_max REAL NOT NULL DEFAULT 8,
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
CREATE TABLE IF NOT EXISTS food_vehicles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_no TEXT NOT NULL UNIQUE,
    carrier TEXT NOT NULL,
    capacity_kg REAL NOT NULL CHECK(capacity_kg > 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_vehicle_zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id INTEGER NOT NULL REFERENCES food_vehicles(id) ON DELETE CASCADE,
    zone_code TEXT NOT NULL,
    zone_name TEXT NOT NULL DEFAULT '',
    temp_min REAL NOT NULL,
    temp_max REAL NOT NULL,
    UNIQUE(vehicle_id, zone_code)
);
CREATE TABLE IF NOT EXISTS food_load_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_code TEXT NOT NULL UNIQUE,
    vehicle_id INTEGER NOT NULL REFERENCES food_vehicles(id) ON DELETE RESTRICT,
    carrier TEXT NOT NULL,
    departure_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','confirmed','loaded','in_transit','arrived','cancelled')),
    total_kg REAL NOT NULL DEFAULT 0,
    confirmed_at TEXT,
    confirmed_by TEXT NOT NULL DEFAULT '',
    loaded_at TEXT,
    loaded_by TEXT NOT NULL DEFAULT '',
    published_revision INTEGER NOT NULL DEFAULT 0,
    content_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS food_plan_stops (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES food_load_plans(id) ON DELETE CASCADE,
    stop_order INTEGER NOT NULL CHECK(stop_order > 0),
    node TEXT NOT NULL,
    planned_arrival_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, stop_order),
    UNIQUE(plan_id, node)
);
CREATE TABLE IF NOT EXISTS food_plan_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES food_load_plans(id) ON DELETE CASCADE,
    lot_id INTEGER NOT NULL REFERENCES food_lots(id) ON DELETE RESTRICT,
    quantity_kg REAL NOT NULL CHECK(quantity_kg > 0),
    stop_order INTEGER NOT NULL,
    temp_min REAL NOT NULL,
    temp_max REAL NOT NULL,
    zone_code TEXT NOT NULL DEFAULT '',
    loaded_kg REAL NOT NULL DEFAULT 0,
    loaded_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(plan_id, lot_id)
);
CREATE TABLE IF NOT EXISTS food_plan_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES food_load_plans(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    change_type TEXT NOT NULL CHECK(change_type IN ('publish','amend')),
    total_kg REAL NOT NULL,
    snapshot_json TEXT NOT NULL,
    diff_json TEXT NOT NULL DEFAULT '{}',
    operator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, revision)
);
CREATE TABLE IF NOT EXISTS food_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lot_id INTEGER,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_food_samples_lot ON food_samples(lot_id, collected_at);
CREATE INDEX IF NOT EXISTS idx_food_results_sample ON food_test_results(sample_id, tested_at);
CREATE INDEX IF NOT EXISTS idx_food_shipments_lot ON food_shipments(lot_id, departure_at);
CREATE INDEX IF NOT EXISTS idx_food_plan_items_lot ON food_plan_items(lot_id);
CREATE INDEX IF NOT EXISTS idx_food_plan_stops_plan ON food_plan_stops(plan_id, stop_order);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_schema() -> None:
    connection = get_connection()
    connection.executescript(SCHEMA)
    existing = {row["name"] for row in connection.execute("PRAGMA table_info(food_lots)")}
    for name, ddl in (
        ("reserved_kg", "ALTER TABLE food_lots ADD COLUMN reserved_kg REAL NOT NULL DEFAULT 0"),
        ("loaded_kg", "ALTER TABLE food_lots ADD COLUMN loaded_kg REAL NOT NULL DEFAULT 0"),
        ("storage_temp_min", "ALTER TABLE food_lots ADD COLUMN storage_temp_min REAL NOT NULL DEFAULT 0"),
        ("storage_temp_max", "ALTER TABLE food_lots ADD COLUMN storage_temp_max REAL NOT NULL DEFAULT 8"),
    ):
        if name not in existing:
            connection.execute(ddl)


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row else None


def _result_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class PlanValidationError(ValueError):
    """装载计划校验失败，code 为稳定错误码，details 携带核算明细。"""

    def __init__(self, code: str, details: dict[str, Any] | None = None):
        super().__init__(code)
        self.code = code
        self.details = details or {}


_RELEASABLE_STATUS = "released"


def _lot_block_reason(status: str) -> str | None:
    if status == "released":
        return None
    mapping = {
        "pending": "lot_pending_inspection",
        "testing": "lot_pending_inspection",
        "held": "lot_quarantined",
        "recalled": "lot_recalled",
        "destroyed": "lot_destroyed",
    }
    return mapping.get(status, "lot_not_released")


class FoodService:
    """食品批次、检测与运输流程的事务边界。"""

    def __init__(self, connection: sqlite3.Connection | None = None):
        self.connection = connection or get_connection()
        ensure_schema()

    def create_lot(self, payload: dict[str, Any], actor: str = "system") -> dict[str, Any]:
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO food_lots(lot_code,product_name,category,supplier,origin,harvest_date,quantity_kg,trace_code,storage_temp_min,storage_temp_max,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (payload["lot_code"], payload["product_name"], payload["category"], payload["supplier"], payload["origin"], payload["harvest_date"], payload["quantity_kg"], payload["trace_code"], payload.get("storage_temp_min", 0.0), payload.get("storage_temp_max", 8.0), now, now),
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
    # 车辆与温区
    # ------------------------------------------------------------------

    def register_vehicle(self, payload: dict[str, Any], actor: str = "dispatcher") -> dict[str, Any]:
        zones = payload.get("zones") or [{"zone_code": "DEFAULT", "zone_name": "常温区", "temp_min": 0.0, "temp_max": 8.0}]
        for zone in zones:
            if zone["temp_min"] > zone["temp_max"]:
                raise PlanValidationError("vehicle_temperature_range_invalid", {"zone_code": zone["zone_code"]})
        now = _now()
        with transaction(immediate=True) as connection:
            cursor = connection.execute(
                "INSERT INTO food_vehicles(vehicle_no,carrier,capacity_kg,created_at,updated_at) VALUES(?,?,?,?,?)",
                (payload["vehicle_no"], payload["carrier"], payload["capacity_kg"], now, now),
            )
            vehicle_id = cursor.lastrowid
            for zone in zones:
                connection.execute(
                    "INSERT INTO food_vehicle_zones(vehicle_id,zone_code,zone_name,temp_min,temp_max) VALUES(?,?,?,?,?)",
                    (vehicle_id, zone["zone_code"], zone.get("zone_name", ""), zone["temp_min"], zone["temp_max"]),
                )
            connection.execute(
                "INSERT INTO food_audit(action,actor,payload_json,created_at) VALUES(?,?,?,?)",
                ("vehicle.register", actor, json.dumps(payload, ensure_ascii=False), now),
            )
            return self._load_vehicle(connection, vehicle_id) or {}

    def _load_vehicle(self, connection: sqlite3.Connection, vehicle_id: int) -> dict[str, Any] | None:
        vehicle = connection.execute("SELECT * FROM food_vehicles WHERE id=?", (vehicle_id,)).fetchone()
        if vehicle is None:
            return None
        result = dict(vehicle)
        result["zones"] = [dict(row) for row in connection.execute("SELECT * FROM food_vehicle_zones WHERE vehicle_id=? ORDER BY id", (vehicle_id,)).fetchall()]
        return result

    # ------------------------------------------------------------------
    # 装载计划：草稿、发布、差异修订、装车确认
    # ------------------------------------------------------------------

    def _normalize_plan_payload(self, payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        stops = payload.get("stops") or []
        items = payload.get("items") or []
        if not items:
            raise PlanValidationError("plan_items_empty")
        stop_orders = [stop["stop_order"] for stop in stops]
        if len(stop_orders) != len(set(stop_orders)):
            raise PlanValidationError("stop_order_duplicated")
        return stops, items

    def _require_lots_exist(self, connection: sqlite3.Connection, items: list[dict[str, Any]]) -> None:
        lot_ids = list({item["lot_id"] for item in items})
        placeholders = ",".join("?" for _ in lot_ids)
        found = {row["id"] for row in connection.execute(f"SELECT id FROM food_lots WHERE id IN ({placeholders})", lot_ids).fetchall()}
        missing = next((lot_id for lot_id in lot_ids if lot_id not in found), None)
        if missing is not None:
            raise KeyError("lot_not_found")

    def create_plan(self, payload: dict[str, Any], actor: str = "dispatcher") -> dict[str, Any]:
        stops, items = self._normalize_plan_payload(payload)
        now = _now()
        with transaction(immediate=True) as connection:
            vehicle = connection.execute("SELECT * FROM food_vehicles WHERE id=?", (payload["vehicle_id"],)).fetchone()
            if vehicle is None:
                raise KeyError("vehicle_not_found")
            if not vehicle["is_active"]:
                raise PlanValidationError("vehicle_inactive")
            cursor = connection.execute(
                "INSERT INTO food_load_plans(plan_code,vehicle_id,carrier,departure_at,status,created_at,updated_at) VALUES(?,?,?,?,'draft',?,?)",
                (payload["plan_code"], payload["vehicle_id"], vehicle["carrier"], payload["departure_at"], now, now),
            )
            plan_id = cursor.lastrowid
            self._require_lots_exist(connection, items)
            zones = [dict(row) for row in connection.execute("SELECT * FROM food_vehicle_zones WHERE vehicle_id=? ORDER BY id", (payload["vehicle_id"],)).fetchall()]
            blockers = self._validate_plan(connection, plan_id, vehicle, zones, stops, items, hard=False)
            self._write_stops_items(connection, plan_id, stops, items, now=now)
            total_kg = sum(item["quantity_kg"] for item in items)
            connection.execute("UPDATE food_load_plans SET total_kg=? WHERE id=?", (total_kg, plan_id))
            connection.execute(
                "INSERT INTO food_audit(action,actor,payload_json,created_at) VALUES(?,?,?,?)",
                ("plan.create", actor, json.dumps({"plan_code": payload["plan_code"]}, ensure_ascii=False), now),
            )
            result = self._load_plan(connection, plan_id)
            result["blockers"] = blockers
            return result or {}

    def _write_stops_items(
        self,
        connection: sqlite3.Connection,
        plan_id: int,
        stops: list[dict[str, Any]],
        items: list[dict[str, Any]],
        *,
        now: str,
    ) -> None:
        for stop in stops:
            connection.execute(
                "INSERT INTO food_plan_stops(plan_id,stop_order,node,planned_arrival_at,created_at) VALUES(?,?,?,?,?)",
                (plan_id, stop["stop_order"], stop["node"], stop["planned_arrival_at"], now),
            )
        for item in items:
            connection.execute(
                "INSERT INTO food_plan_items(plan_id,lot_id,quantity_kg,stop_order,temp_min,temp_max,zone_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (plan_id, item["lot_id"], item["quantity_kg"], item["stop_order"], item.get("temp_min", 0.0), item.get("temp_max", 8.0), item.get("zone_code", ""), now, now),
            )

    def _compatible_zone(self, zones: list[dict[str, Any]], lot: sqlite3.Row, requested: str) -> dict[str, Any] | None:
        for zone in zones:
            if requested and zone["zone_code"] != requested:
                continue
            if zone["temp_min"] <= lot["storage_temp_min"] and zone["temp_max"] >= lot["storage_temp_max"]:
                return dict(zone)
        return None

    def _validate_plan(
        self,
        connection: sqlite3.Connection,
        plan_id: int,
        vehicle: sqlite3.Row,
        zones: list[dict[str, Any]],
        stops: list[dict[str, Any]],
        items: list[dict[str, Any]],
        *,
        hard: bool,
    ) -> list[dict[str, Any]]:
        """核算容量/温区/顺序/可放行状态；hard=True 时任一阻断都抛异常。

        校验通过时把每个 item 的 zone_code/temp_min/temp_max 归一化到实际温区。
        """
        blockers: list[dict[str, Any]] = []
        stop_map = {stop["stop_order"]: dict(stop) for stop in stops}
        ordered = sorted(stops, key=lambda row: row["stop_order"])
        if ordered and [stop["stop_order"] for stop in ordered] != list(range(1, len(ordered) + 1)):
            blockers.append({"scope": "plan", "reason": "stop_order_not_contiguous"})
        for previous, current in zip(ordered, ordered[1:]):
            if current["planned_arrival_at"] <= previous["planned_arrival_at"]:
                blockers.append({"scope": "route", "reason": "stop_sequence_time_invalid", "stop_order": current["stop_order"]})

        total_kg = 0.0
        lot_ids = [item["lot_id"] for item in items]
        if len(lot_ids) != len(set(lot_ids)):
            blockers.append({"scope": "plan", "reason": "lot_duplicated"})
        placeholders = ",".join("?" for _ in lot_ids)
        lots = (
            {row["id"]: row for row in connection.execute(f"SELECT * FROM food_lots WHERE id IN ({placeholders})", lot_ids).fetchall()}
            if lot_ids
            else {}
        )
        other_reserved = {
            row["lot_id"]: row["reserved"]
            for row in connection.execute(
                """
                SELECT i.lot_id AS lot_id, COALESCE(SUM(i.quantity_kg),0) AS reserved
                FROM food_plan_items i
                JOIN food_load_plans p ON p.id = i.plan_id
                WHERE i.plan_id != ? AND p.status = 'published'
                GROUP BY i.lot_id
                """,
                (plan_id,),
            ).fetchall()
        }

        for item in items:
            lot = lots.get(item["lot_id"])
            row_blockers: list[str] = []
            available: float | None = None
            if lot is None:
                row_blockers.append("lot_not_found")
            else:
                reason = _lot_block_reason(lot["status"])
                if reason:
                    row_blockers.append(reason)
                zone = self._compatible_zone(zones, lot, item.get("zone_code", ""))
                if zone is None:
                    row_blockers.append("temperature_zone_incompatible")
                    item["zone_code"] = item.get("zone_code", "")
                    item["temp_min"] = lot["storage_temp_min"]
                    item["temp_max"] = lot["storage_temp_max"]
                else:
                    item["zone_code"] = zone["zone_code"]
                    item["temp_min"] = zone["temp_min"]
                    item["temp_max"] = zone["temp_max"]
                available = lot["quantity_kg"] - lot["loaded_kg"] - other_reserved.get(lot["id"], 0.0)
                if item["quantity_kg"] <= 0:
                    row_blockers.append("quantity_invalid")
                elif item["quantity_kg"] > available + 1e-9:
                    row_blockers.append("quantity_exceeds_available")
                if item["stop_order"] not in stop_map:
                    row_blockers.append("stop_not_found")
                total_kg += item["quantity_kg"]
            if row_blockers:
                blockers.append({"scope": "lot", "lot_id": item.get("lot_id"), "reasons": row_blockers, "available_kg": locals().get("available"), "requested_kg": item["quantity_kg"]})

        if total_kg > vehicle["capacity_kg"] + 1e-9:
            blockers.append({"scope": "plan", "reason": "vehicle_capacity_exceeded", "total_kg": total_kg, "capacity_kg": vehicle["capacity_kg"]})
        if hard and blockers:
            raise PlanValidationError("plan_validation_failed", {"blockers": blockers, "total_kg": total_kg})
        return blockers

    def update_plan(self, plan_id: int, payload: dict[str, Any], actor: str = "dispatcher") -> dict[str, Any]:
        """草稿可直接编辑；已发布计划的修改生成差异并提升修订号，需重新确认装车。"""
        stops, items = self._normalize_plan_payload(payload)
        with transaction(immediate=True) as connection:
            plan = connection.execute("SELECT * FROM food_load_plans WHERE id=?", (plan_id,)).fetchone()
            if plan is None:
                raise KeyError("plan_not_found")
            if plan["status"] in ("loaded", "in_transit", "arrived", "cancelled"):
                raise PlanValidationError("plan_not_editable", {"status": plan["status"]})
            self._require_lots_exist(connection, items)
            vehicle = connection.execute("SELECT * FROM food_vehicles WHERE id=?", (plan["vehicle_id"],)).fetchone()
            zones = [dict(row) for row in connection.execute("SELECT * FROM food_vehicle_zones WHERE vehicle_id=? ORDER BY id", (plan["vehicle_id"],)).fetchall()]
            # 发布态强制校验，草稿态仅返回阻断原因供前端提示
            blockers = self._validate_plan(connection, plan_id, vehicle, zones, stops, items, hard=plan["status"] == "published")
            now = _now()
            total_kg = sum(item["quantity_kg"] for item in items)
            if plan["status"] == "published":
                diff = self._diff_plan(connection, plan, stops, items)
                new_revision = plan["content_revision"] + 1
                self._reapply_reservation(connection, plan_id, stops, items, now=now)
                connection.execute(
                    "UPDATE food_load_plans SET total_kg=?, content_revision=?, updated_at=? WHERE id=?",
                    (total_kg, new_revision, now, plan_id),
                )
                self._write_revision(connection, plan_id, new_revision, "amend", items, stops, diff, actor, now)
                connection.execute(
                    "INSERT INTO food_audit(action,actor,payload_json,created_at) VALUES(?,?,?,?)",
                    ("plan.amend", actor, json.dumps({"plan_id": plan_id, "revision": new_revision, "diff": diff}, ensure_ascii=False), now),
                )
            else:
                connection.execute("DELETE FROM food_plan_items WHERE plan_id=?", (plan_id,))
                connection.execute("DELETE FROM food_plan_stops WHERE plan_id=?", (plan_id,))
                self._write_stops_items(connection, plan_id, stops, items, now=now)
                connection.execute("UPDATE food_load_plans SET total_kg=?, updated_at=? WHERE id=?", (total_kg, now, plan_id))
            result = self._load_plan(connection, plan_id)
            result["blockers"] = blockers
            return result or {}

    def _reapply_reservation(self, connection: sqlite3.Connection, plan_id: int, stops: list[dict[str, Any]], new_items: list[dict[str, Any]], *, now: str) -> None:
        for old in connection.execute("SELECT lot_id, quantity_kg FROM food_plan_items WHERE plan_id=?", (plan_id,)).fetchall():
            connection.execute("UPDATE food_lots SET reserved_kg=reserved_kg-?, updated_at=? WHERE id=?", (old["quantity_kg"], now, old["lot_id"]))
        connection.execute("DELETE FROM food_plan_items WHERE plan_id=?", (plan_id,))
        connection.execute("DELETE FROM food_plan_stops WHERE plan_id=?", (plan_id,))
        for stop in stops:
            connection.execute(
                "INSERT INTO food_plan_stops(plan_id,stop_order,node,planned_arrival_at,created_at) VALUES(?,?,?,?,?)",
                (plan_id, stop["stop_order"], stop["node"], stop["planned_arrival_at"], now),
            )
        for item in new_items:
            connection.execute(
                "INSERT INTO food_plan_items(plan_id,lot_id,quantity_kg,stop_order,temp_min,temp_max,zone_code,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (plan_id, item["lot_id"], item["quantity_kg"], item["stop_order"], item["temp_min"], item["temp_max"], item.get("zone_code", ""), now, now),
            )
            connection.execute("UPDATE food_lots SET reserved_kg=reserved_kg+?, updated_at=? WHERE id=?", (item["quantity_kg"], now, item["lot_id"]))

    def _diff_plan(self, connection: sqlite3.Connection, plan: sqlite3.Row, stops: list[dict[str, Any]], items: list[dict[str, Any]]) -> dict[str, Any]:
        old_items = {row["lot_id"]: dict(row) for row in connection.execute("SELECT * FROM food_plan_items WHERE plan_id=?", (plan["id"],)).fetchall()}
        old_stops = {row["stop_order"]: dict(row) for row in connection.execute("SELECT * FROM food_plan_stops WHERE plan_id=?", (plan["id"],)).fetchall()}
        new_items = {item["lot_id"]: item for item in items}
        added = [{"lot_id": lot_id, "quantity_kg": item["quantity_kg"], "stop_order": item["stop_order"]} for lot_id, item in new_items.items() if lot_id not in old_items]
        removed = [{"lot_id": lot_id, "quantity_kg": old["quantity_kg"]} for lot_id, old in old_items.items() if lot_id not in new_items]
        changed = []
        for lot_id, new in new_items.items():
            old = old_items.get(lot_id)
            if old is None:
                continue
            changes = {}
            if abs(old["quantity_kg"] - new["quantity_kg"]) > 1e-9:
                changes["quantity_kg"] = {"from": old["quantity_kg"], "to": new["quantity_kg"]}
            if old["stop_order"] != new["stop_order"]:
                changes["stop_order"] = {"from": old["stop_order"], "to": new["stop_order"]}
            if old["zone_code"] != new.get("zone_code", ""):
                changes["zone_code"] = {"from": old["zone_code"], "to": new.get("zone_code", "")}
            if changes:
                changed.append({"lot_id": lot_id, "changes": changes})
        new_stop_orders = {stop["stop_order"] for stop in stops}
        stop_changes = []
        for new_stop in stops:
            old_stop = old_stops.get(new_stop["stop_order"])
            if old_stop is None:
                stop_changes.append({"stop_order": new_stop["stop_order"], "change": "added", "node": new_stop["node"]})
            elif old_stop["node"] != new_stop["node"] or old_stop["planned_arrival_at"] != new_stop["planned_arrival_at"]:
                stop_changes.append({"stop_order": new_stop["stop_order"], "change": "modified", "from": {"node": old_stop["node"], "planned_arrival_at": old_stop["planned_arrival_at"]}, "to": {"node": new_stop["node"], "planned_arrival_at": new_stop["planned_arrival_at"]}})
        for order, old_stop in old_stops.items():
            if order not in new_stop_orders:
                stop_changes.append({"stop_order": order, "change": "removed", "node": old_stop["node"]})
        new_total = sum(item["quantity_kg"] for item in items)
        return {
            "total_kg": {"from": plan["total_kg"], "to": new_total},
            "items_added": added,
            "items_removed": removed,
            "items_changed": changed,
            "stops_changed": stop_changes,
        }

    def _write_revision(self, connection: sqlite3.Connection, plan_id: int, revision: int, change_type: str, items: list[dict[str, Any]], stops: list[dict[str, Any]], diff: dict[str, Any], actor: str, now: str) -> None:
        snapshot = {"stops": stops, "items": items}
        connection.execute(
            "INSERT INTO food_plan_revisions(plan_id,revision,change_type,total_kg,snapshot_json,diff_json,operator,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (plan_id, revision, change_type, sum(item["quantity_kg"] for item in items), json.dumps(snapshot, ensure_ascii=False), json.dumps(diff, ensure_ascii=False), actor, now),
        )

    def publish_plan(self, plan_id: int, actor: str = "dispatcher") -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            plan = connection.execute("SELECT * FROM food_load_plans WHERE id=?", (plan_id,)).fetchone()
            if plan is None:
                raise KeyError("plan_not_found")
            if plan["status"] != "draft":
                raise PlanValidationError("plan_not_draft", {"status": plan["status"]})
            stops = [dict(row) for row in connection.execute("SELECT * FROM food_plan_stops WHERE plan_id=? ORDER BY stop_order", (plan_id,)).fetchall()]
            items = [dict(row) for row in connection.execute("SELECT * FROM food_plan_items WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()]
            vehicle = connection.execute("SELECT * FROM food_vehicles WHERE id=?", (plan["vehicle_id"],)).fetchone()
            zones = [dict(row) for row in connection.execute("SELECT * FROM food_vehicle_zones WHERE vehicle_id=? ORDER BY id", (plan["vehicle_id"],)).fetchall()]
            self._validate_plan(connection, plan_id, vehicle, zones, stops, items, hard=True)
            now = _now()
            total_kg = sum(item["quantity_kg"] for item in items)
            connection.execute("UPDATE food_load_plans SET status='published',total_kg=?,content_revision=1,published_revision=1,updated_at=? WHERE id=?", (total_kg, now, plan_id))
            for item in items:
                connection.execute("UPDATE food_lots SET reserved_kg=reserved_kg+?, updated_at=? WHERE id=?", (item["quantity_kg"], now, item["lot_id"]))
            self._write_revision(connection, plan_id, 1, "publish", items, stops, {}, actor, now)
            connection.execute(
                "INSERT INTO food_audit(action,actor,payload_json,created_at) VALUES(?,?,?,?)",
                ("plan.publish", actor, json.dumps({"plan_id": plan_id, "total_kg": total_kg}, ensure_ascii=False), now),
            )
            return self._load_plan(connection, plan_id) or {}

    def confirm_loading(self, plan_id: int, actor: str = "dispatcher") -> dict[str, Any]:
        """装车确认：硬校验通过后把预留数量转为已装数量；重复确认不重复扣减。"""
        with transaction(immediate=True) as connection:
            plan = connection.execute("SELECT * FROM food_load_plans WHERE id=?", (plan_id,)).fetchone()
            if plan is None:
                raise KeyError("plan_not_found")
            if plan["status"] == "loaded":
                result = self._load_plan(connection, plan_id)
                result["idempotent"] = True
                return result or {}
            if plan["status"] != "published":
                raise PlanValidationError("plan_not_published", {"status": plan["status"]})
            stops = [dict(row) for row in connection.execute("SELECT * FROM food_plan_stops WHERE plan_id=? ORDER BY stop_order", (plan_id,)).fetchall()]
            items = [dict(row) for row in connection.execute("SELECT * FROM food_plan_items WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()]
            vehicle = connection.execute("SELECT * FROM food_vehicles WHERE id=?", (plan["vehicle_id"],)).fetchone()
            zones = [dict(row) for row in connection.execute("SELECT * FROM food_vehicle_zones WHERE vehicle_id=? ORDER BY id", (plan["vehicle_id"],)).fetchall()]
            self._validate_plan(connection, plan_id, vehicle, zones, stops, items, hard=True)
            now = _now()
            for item in items:
                connection.execute(
                    "UPDATE food_lots SET reserved_kg=reserved_kg-?, loaded_kg=loaded_kg+?, updated_at=? WHERE id=?",
                    (item["quantity_kg"], item["quantity_kg"], now, item["lot_id"]),
                )
                connection.execute(
                    "UPDATE food_plan_items SET loaded_kg=quantity_kg, loaded_at=?, updated_at=? WHERE plan_id=? AND lot_id=? AND loaded_at IS NULL",
                    (now, now, plan_id, item["lot_id"]),
                )
            connection.execute(
                "UPDATE food_load_plans SET status='loaded',loaded_at=?,loaded_by=?,confirmed_at=?,confirmed_by=?,updated_at=? WHERE id=?",
                (now, actor, now, actor, now, plan_id),
            )
            connection.execute(
                "INSERT INTO food_audit(action,actor,payload_json,created_at) VALUES(?,?,?,?)",
                ("plan.load", actor, json.dumps({"plan_id": plan_id, "revision": plan["content_revision"]}, ensure_ascii=False), now),
            )
            result = self._load_plan(connection, plan_id)
            result["idempotent"] = False
            return result or {}

    def cancel_plan(self, plan_id: int, actor: str = "dispatcher") -> dict[str, Any]:
        with transaction(immediate=True) as connection:
            plan = connection.execute("SELECT * FROM food_load_plans WHERE id=?", (plan_id,)).fetchone()
            if plan is None:
                raise KeyError("plan_not_found")
            if plan["status"] in ("loaded", "in_transit", "arrived", "cancelled"):
                raise PlanValidationError("plan_not_cancellable", {"status": plan["status"]})
            now = _now()
            if plan["status"] == "published":
                for item in connection.execute("SELECT lot_id, quantity_kg FROM food_plan_items WHERE plan_id=?", (plan_id,)).fetchall():
                    connection.execute("UPDATE food_lots SET reserved_kg=reserved_kg-?, updated_at=? WHERE id=?", (item["quantity_kg"], now, item["lot_id"]))
            connection.execute("UPDATE food_load_plans SET status='cancelled',updated_at=? WHERE id=?", (now, plan_id))
            connection.execute(
                "INSERT INTO food_audit(action,actor,payload_json,created_at) VALUES(?,?,?,?)",
                ("plan.cancel", actor, json.dumps({"plan_id": plan_id}, ensure_ascii=False), now),
            )
            return self._load_plan(connection, plan_id) or {}

    def _load_plan(self, connection: sqlite3.Connection, plan_id: int) -> dict[str, Any] | None:
        plan = connection.execute("SELECT * FROM food_load_plans WHERE id=?", (plan_id,)).fetchone()
        if plan is None:
            return None
        result = dict(plan)
        vehicle = connection.execute("SELECT id,vehicle_no,carrier,capacity_kg FROM food_vehicles WHERE id=?", (plan["vehicle_id"],)).fetchone()
        result["vehicle"] = dict(vehicle) if vehicle else None
        result["stops"] = [dict(row) for row in connection.execute("SELECT stop_order,node,planned_arrival_at FROM food_plan_stops WHERE plan_id=? ORDER BY stop_order", (plan_id,)).fetchall()]
        zone_rows = {row["zone_code"]: dict(row) for row in connection.execute("SELECT * FROM food_vehicle_zones WHERE vehicle_id=?", (plan["vehicle_id"],)).fetchall()}
        items = []
        grouped: dict[str, dict[str, Any]] = {}
        for row in connection.execute(
            """
            SELECT i.*, l.lot_code, l.product_name, l.status AS lot_status, l.quantity_kg AS lot_quantity,
                   l.reserved_kg, l.loaded_kg, l.storage_temp_min, l.storage_temp_max
            FROM food_plan_items i JOIN food_lots l ON l.id=i.lot_id
            WHERE i.plan_id=? ORDER BY i.stop_order, i.id
            """,
            (plan_id,),
        ).fetchall():
            item = dict(row)
            reasons = []
            reason = _lot_block_reason(row["lot_status"])
            if reason:
                reasons.append(reason)
            zone = zone_rows.get(row["zone_code"])
            if zone is None or not (zone["temp_min"] <= row["storage_temp_min"] and zone["temp_max"] >= row["storage_temp_max"]):
                reasons.append("temperature_zone_incompatible")
            item["blocking_reasons"] = reasons
            items.append(item)
            group = grouped.setdefault(
                row["zone_code"],
                {
                    "zone_code": row["zone_code"],
                    "zone_name": (zone or {}).get("zone_name", ""),
                    "temp_min": (zone or {}).get("temp_min"),
                    "temp_max": (zone or {}).get("temp_max"),
                    "total_kg": 0.0,
                    "lot_ids": [],
                },
            )
            group["total_kg"] += row["quantity_kg"]
            group["lot_ids"].append(row["lot_id"])
        result["items"] = items
        result["zone_groups"] = list(grouped.values())
        result["revisions"] = [
            {**dict(row), "snapshot_json": json.loads(row["snapshot_json"]), "diff_json": json.loads(row["diff_json"])}
            for row in connection.execute(
                "SELECT id,revision,change_type,total_kg,snapshot_json,diff_json,operator,created_at FROM food_plan_revisions WHERE plan_id=? ORDER BY revision",
                (plan_id,),
            ).fetchall()
        ]
        result["blockers"] = [{"lot_id": item["lot_id"], "reasons": item["blocking_reasons"]} for item in items if item["blocking_reasons"]]
        if result["total_kg"] > (result["vehicle"] or {}).get("capacity_kg", float("inf")):
            result["blockers"].append({"scope": "plan", "reason": "vehicle_capacity_exceeded"})
        return result

    def get_plan(self, plan_id: int) -> dict[str, Any] | None:
        return self._load_plan(self.connection, plan_id)

    def list_plans(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id,plan_code,vehicle_id,carrier,departure_at,status,total_kg,content_revision,loaded_at FROM food_load_plans"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY departure_at,id"
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def lot_loadings(self, lot_id: int) -> dict[str, Any]:
        """给出某批次在各计划中的装载量、温区与实时阻断原因。"""
        lot = self.connection.execute("SELECT * FROM food_lots WHERE id=?", (lot_id,)).fetchone()
        if lot is None:
            raise KeyError("lot_not_found")
        rows = self.connection.execute(
            """
            SELECT p.id AS plan_id, p.plan_code, p.status, p.loaded_at, i.quantity_kg, i.loaded_kg,
                   i.zone_code, i.temp_min, i.temp_max, i.stop_order, v.vehicle_no, v.capacity_kg
            FROM food_plan_items i
            JOIN food_load_plans p ON p.id=i.plan_id
            JOIN food_vehicles v ON v.id=p.vehicle_id
            WHERE i.lot_id=? ORDER BY p.id
            """,
            (lot_id,),
        ).fetchall()
        plans = []
        for row in rows:
            item = dict(row)
            reasons = []
            reason = _lot_block_reason(lot["status"])
            if reason and row["status"] == "published":
                reasons.append(reason)
            item["blocking_reasons"] = reasons
            plans.append(item)
        return {
            "lot_id": lot_id,
            "lot_code": lot["lot_code"],
            "status": lot["status"],
            "quantity_kg": lot["quantity_kg"],
            "reserved_kg": lot["reserved_kg"],
            "loaded_kg": lot["loaded_kg"],
            "available_kg": lot["quantity_kg"] - lot["reserved_kg"] - lot["loaded_kg"],
            "storage_temp_min": lot["storage_temp_min"],
            "storage_temp_max": lot["storage_temp_max"],
            "blocking_reason": _lot_block_reason(lot["status"]),
            "plans": plans,
        }

    def delete_lot(self, lot_id: int) -> bool:
        """移除尚未关联记录的批次；关联记录的错误映射由上层负责。"""
        with transaction(immediate=True) as connection:
            cursor = connection.execute("DELETE FROM food_lots WHERE id=?", (lot_id,))
            if cursor.rowcount == 0:
                raise KeyError("lot_not_found")
            return True
