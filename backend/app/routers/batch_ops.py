"""批量转换接口：字段映射整批试算，供试算页一次提交整批采集记录。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from app.schemas import BatchTrialPayload
from app.services import service_for

router = APIRouter(prefix="/api/field_mapping", tags=["字段映射批量转换"])


@router.post("/{mapping_id}/batch-trial")
def batch_trial(mapping_id: int, payload: BatchTrialPayload) -> dict[str, Any]:
    """整批试算：按映射当前版本转换全部记录，返回逐条判定明细。"""
    service = service_for("field_mapping")
    result = service.convert_batch(
        mapping_id, payload.records, source_field=payload.source_field)
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("reason", "映射规则不存在"))
    return result
