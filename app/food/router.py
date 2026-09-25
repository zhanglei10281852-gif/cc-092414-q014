from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.food.schemas import (
    LoadPlanCreate,
    LoadPlanUpdate,
    LotCreate,
    RiskDecision,
    SampleCreate,
    ShipmentCreate,
    TemperatureRecord,
    TestResultCreate,
    VehicleRegister,
)
from app.food.service import FoodService, PlanValidationError

router = APIRouter(prefix="/api/food", tags=["食品安全"])


def service() -> FoodService:
    return FoodService()


@router.post("/lots", status_code=201)
def create_lot(payload: LotCreate):
    try:
        return service().create_lot(payload.model_dump(), actor=payload.supplier)
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409, detail="批次编码或追溯码已存在") from exc
        raise


@router.get("/lots/{lot_id}")
def get_lot(lot_id: int, details: bool = True):
    value = service().get_lot(lot_id, details)
    if value is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return value


@router.get("/lots/{lot_id}/summary")
def summary(lot_id: int):
    try:
        return service().summary(lot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc


@router.delete("/lots/{lot_id}")
def delete_lot(lot_id: int):
    try:
        service().delete_lot(lot_id)
        return {"message": "批次已删除"}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc


@router.post("/lots/{lot_id}/samples", status_code=201)
def add_sample(lot_id: int, payload: SampleCreate):
    try:
        return service().add_sample(lot_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc


@router.post("/samples/{sample_id}/results", status_code=201)
def add_result(sample_id: int, payload: TestResultCreate):
    try:
        return service().add_result(sample_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="样品不存在") from exc


@router.post("/lots/{lot_id}/shipments", status_code=201)
def create_shipment(lot_id: int, payload: ShipmentCreate):
    try:
        return service().create_shipment(lot_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/shipments/{shipment_id}/temperatures", status_code=201)
def add_temperature(shipment_id: int, payload: TemperatureRecord):
    try:
        return service().add_temperature(shipment_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="运输单不存在") from exc


@router.post("/lots/{lot_id}/risk", status_code=200)
def decide_risk(lot_id: int, payload: RiskDecision):
    try:
        return service().decide_risk(lot_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc


def _conflict(exc: PlanValidationError) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": exc.code, "message": exc.code, "details": exc.details})


@router.post("/vehicles", status_code=201)
def register_vehicle(payload: VehicleRegister):
    try:
        return service().register_vehicle(payload.model_dump())
    except PlanValidationError as exc:
        raise _conflict(exc) from exc
    except Exception as exc:
        if "UNIQUE" in str(exc).upper():
            raise HTTPException(status_code=409, detail="车牌号已存在") from exc
        raise


@router.post("/load-plans", status_code=201)
def create_plan(payload: LoadPlanCreate):
    try:
        return service().create_plan(payload.model_dump())
    except KeyError as exc:
        detail = "车辆不存在" if exc.args[0] == "vehicle_not_found" else "批次不存在"
        raise HTTPException(status_code=404, detail=detail) from exc
    except PlanValidationError as exc:
        raise _conflict(exc) from exc


@router.get("/load-plans")
def list_plans(status: str | None = None):
    return service().list_plans(status)


@router.get("/load-plans/{plan_id}")
def get_plan(plan_id: int):
    value = service().get_plan(plan_id)
    if value is None:
        raise HTTPException(status_code=404, detail="装载计划不存在")
    return value


@router.put("/load-plans/{plan_id}")
def update_plan(plan_id: int, payload: LoadPlanUpdate):
    try:
        return service().update_plan(plan_id, payload.model_dump())
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="装载计划不存在") from exc
    except PlanValidationError as exc:
        raise _conflict(exc) from exc


@router.post("/load-plans/{plan_id}/publish")
def publish_plan(plan_id: int):
    try:
        return service().publish_plan(plan_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="装载计划不存在") from exc
    except PlanValidationError as exc:
        raise _conflict(exc) from exc


@router.post("/load-plans/{plan_id}/confirm-loading")
def confirm_loading(plan_id: int):
    try:
        return service().confirm_loading(plan_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="装载计划不存在") from exc
    except PlanValidationError as exc:
        raise _conflict(exc) from exc


@router.post("/load-plans/{plan_id}/cancel")
def cancel_plan(plan_id: int):
    try:
        return service().cancel_plan(plan_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="装载计划不存在") from exc
    except PlanValidationError as exc:
        raise _conflict(exc) from exc


@router.get("/lots/{lot_id}/loadings")
def lot_loadings(lot_id: int):
    try:
        return service().lot_loadings(lot_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc
