"""数据落库服务：写入过程记录、冲突行处理、落库水位推进。

记录采集批次到目标存储的写入过程、冲突行处理和落库水位，
作为下游消费的唯一入口。水位单调递增，支持增量消费。

整批落库：采集记录经字段映射批量转换后写入，按实际转换成功的行
写回执，并在落库记录上登记实际使用的映射版本。写入中断后再恢复时，
已落库部分作为历史依据保留，整批记录由单一映射版本完整复算。
"""
from __future__ import annotations

from typing import Any

from app.dedup import now_iso
from app.services.base import BaseModuleService


class DataLandingService(BaseModuleService):
    MODULE = "data_landing"
    ENTITY = "落库记录"
    OPERATION_TERM = "采集"
    LIST_FIELDS = [
        "落库编号", "关联批次", "目标存储", "写入模式", "映射版本", "写入行数",
        "冲突行数", "落库水位", "完成时间",
    ]
    REQUIRED_FIELDS = ["落库编号", "关联批次", "目标存储"]
    STATUS_ORDER = ["待写入", "写入中", "已写入", "写入异常"]
    ACTION_RULES = {"启动写入": "写入中", "确认写入": "已写入", "标记异常": "写入异常"}
    NEGATIVE_ACTIONS = []

    def advance_watermark(self, landing_id: int, watermark: int) -> dict[str, Any]:
        """推进落库水位（仅当新水位大于当前水位时生效）。"""
        entry = self.get_entry(landing_id)
        if entry is None:
            return {"ok": False, "reason": "落库记录不存在"}
        current = int(entry.get("落库水位", "0") or "0")
        if watermark > current:
            entry["落库水位"] = str(watermark)
            return {"ok": True, "landing_id": landing_id, "new_watermark": watermark}
        return {"ok": False, "reason": f"新水位{watermark}不大于当前水位{current}"}

    def get_watermark(self, target_storage: str) -> int:
        """获取指定目标存储的当前最高水位。"""
        items, _ = self.list_entries(size=10000)
        max_wm = 0
        for it in items:
            if it.get("目标存储") == target_storage and it.get("status") == "已写入":
                wm = int(it.get("落库水位", "0") or "0")
                if wm > max_wm:
                    max_wm = wm
        return max_wm

    def handle_conflicts(self, landing_id: int, conflict_count: int) -> dict[str, Any]:
        """处理冲突行：冲突行数>0则标记写入异常，否则确认写入。"""
        entry = self.get_entry(landing_id)
        if entry is None:
            return {"ok": False, "reason": "落库记录不存在"}
        if conflict_count > 0:
            self.run_action(landing_id, "标记异常")
            return {
                "ok": True,
                "landing_id": landing_id,
                "conflicts": conflict_count,
                "handled_as": "异常标记",
            }
        self.run_action(landing_id, "确认写入")
        return {"ok": True, "landing_id": landing_id, "conflicts": 0, "handled_as": "已确认"}

    # ── 整批转换落库与回执 ───────────────────────────────────

    def _build_receipts(
        self, entry: dict[str, Any], items: list[dict[str, Any]], version: str,
    ) -> list[dict[str, Any]]:
        """按实际转换成功的行生成落库回执。"""
        receipts = []
        seq = 0
        for item in items:
            if item["status"] != "converted":
                continue
            seq += 1
            receipts.append({
                "receipt_no": f"RCPT-{entry.get('落库编号', '')}-{seq:03d}",
                "source_id": item["source_id"],
                "input": item["input"],
                "output": item["output"],
                "映射编号": item["映射编号"],
                "映射版本": version,
                "落库编号": entry.get("落库编号", ""),
                "confirmed_at": now_iso(),
            })
        return receipts

    def write_records(
        self,
        landing_id: int,
        mapping_id: int,
        records: list[Any],
        *,
        source_field: str | None = None,
        fail_after: int | None = None,
    ) -> dict[str, Any]:
        """把整批采集记录按映射规则转换落库，按实际转换成功的行写回执。

        - 转换引擎与试算页共用，整数/空值判定口径逐条落入 _details；
        - 落库记录登记实际使用的映射版本（映射版本字段）；
        - fail_after 模拟写入中断：成功写入 fail_after 行后中断，
          已落库的回执与版本作为历史依据存入 _write_history，等待恢复。
        """
        from app.services import service_for

        entry = self.get_entry(landing_id)
        if entry is None:
            return {"ok": False, "reason": "落库记录不存在"}
        mapping_svc = service_for("field_mapping")
        conversion = mapping_svc.convert_batch(mapping_id, records, source_field=source_field)
        if not conversion.get("ok"):
            return conversion

        version = conversion["映射版本"]
        details = conversion["items"]
        receipts = self._build_receipts(entry, details, version)

        # 留存原始记录与转换上下文，供中断恢复时完整复算
        entry["_records"] = list(records)
        entry["_source_field"] = source_field
        entry["_mapping_id"] = mapping_id
        entry["映射版本"] = version
        entry["冲突行数"] = str(conversion["failed"])

        self.run_action(landing_id, "启动写入")

        if fail_after is not None and fail_after < len(receipts):
            # 写入中断：已落库部分保留为历史依据，等待恢复后单版本复算
            landed = receipts[:fail_after]
            history = entry.setdefault("_write_history", [])
            history.append({
                "attempt": len(history) + 1,
                "映射版本": version,
                "written_rows": len(landed),
                "pending_rows": len(receipts) - len(landed),
                "receipts": [dict(r) for r in landed],
                "details": [dict(d) for d in details],
                "interrupted_at": now_iso(),
                "note": "写入中断，已落库部分保留为历史依据",
            })
            entry["_receipts"] = landed
            entry["_details"] = details
            entry["写入行数"] = str(len(landed))
            self.run_action(landing_id, "标记异常")
            return {
                "ok": True,
                "status": "interrupted",
                "landing_id": landing_id,
                "映射版本": version,
                "written_rows": len(landed),
                "pending_rows": len(receipts) - len(landed),
                "receipts": landed,
                "details": details,
            }

        entry["_receipts"] = receipts
        entry["_details"] = details
        entry["写入行数"] = str(len(receipts))
        entry["完成时间"] = now_iso()
        self.run_action(landing_id, "确认写入")
        return {
            "ok": True,
            "status": "landed",
            "landing_id": landing_id,
            "映射版本": version,
            "written_rows": len(receipts),
            "receipts": receipts,
            "details": details,
        }

    def resume_write(self, landing_id: int, mapping_id: int | None = None) -> dict[str, Any]:
        """恢复中断的落库：整批记录由单个映射版本完整复算。

        已落库的历史依据（_write_history）原样保留；恢复后的回执与交接
        明细全部来自当前这一次复算，保证同一交接明细不混用两个映射版本。
        """
        from app.services import service_for

        entry = self.get_entry(landing_id)
        if entry is None:
            return {"ok": False, "reason": "落库记录不存在"}
        records = entry.get("_records")
        if records is None:
            return {"ok": False, "reason": "落库记录没有待恢复的采集记录"}
        mid = mapping_id if mapping_id is not None else entry.get("_mapping_id")
        if mid is None:
            return {"ok": False, "reason": "落库记录缺少映射规则引用，无法复算"}

        mapping_svc = service_for("field_mapping")
        conversion = mapping_svc.convert_batch(
            mid, records, source_field=entry.get("_source_field"))
        if not conversion.get("ok"):
            return conversion

        version = conversion["映射版本"]
        details = conversion["items"]
        receipts = self._build_receipts(entry, details, version)

        # 单版本完整复算：整体覆盖当前回执与明细，历史依据不动
        entry["_receipts"] = receipts
        entry["_details"] = details
        entry["映射版本"] = version
        entry["写入行数"] = str(len(receipts))
        entry["冲突行数"] = str(conversion["failed"])
        entry["完成时间"] = now_iso()
        self.run_action(landing_id, "启动写入")
        self.run_action(landing_id, "确认写入")
        return {
            "ok": True,
            "status": "resumed",
            "landing_id": landing_id,
            "映射版本": version,
            "written_rows": len(receipts),
            "receipts": receipts,
            "details": details,
            "history_preserved": len(entry.get("_write_history", [])),
        }
