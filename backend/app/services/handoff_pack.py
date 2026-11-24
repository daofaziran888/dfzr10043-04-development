"""交接包服务：批次打包、清单校验与下游交接。

把已确认批次连同字段映射、校验摘要和清单打包成交接包，
供下游数仓直接消费并回溯来源。退回后批次不得再次交接。

交接明细逐条落整数/空值的判定口径与映射版本；落库回执的确认结果
回写到交接清单，交接方据此核对实际记录和映射版本。恢复落库后的
交接明细由单个映射版本完整复算，不混用版本。
"""
from __future__ import annotations

from typing import Any

from app.dedup import now_iso, sha256_hex
from app.services.base import BaseModuleService


class HandoffPackService(BaseModuleService):
    MODULE = "handoff_pack"
    ENTITY = "交接包"
    OPERATION_TERM = "关联"
    LIST_FIELDS = [
        "交接编号", "关联批次", "目标系统", "清单文件数", "记录行数",
        "校验摘要", "映射版本", "交接版本", "交接时间",
    ]
    REQUIRED_FIELDS = ["交接编号", "关联批次", "目标系统"]
    STATUS_ORDER = ["待打包", "打包中", "已交接", "交接退回"]
    ACTION_RULES = {"启动打包": "打包中", "确认交接": "已交接", "记录退回": "交接退回"}
    NEGATIVE_ACTIONS = []

    def reject_handoff(self, pack_id: int, reason: str) -> dict[str, Any]:
        """驳回交接包，退回后批次不得进入交接。"""
        entry = self.get_entry(pack_id)
        if entry is None:
            return {"ok": False, "reason": "交接包不存在"}
        self.run_action(pack_id, "记录退回")
        return {"ok": True, "pack_id": pack_id, "reason": reason, "action": "交接退回"}

    def get_delivered_packs(self, target_system: str | None = None) -> list[dict[str, Any]]:
        """获取已交接的包列表，可按目标系统筛选。"""
        items, _ = self.list_entries(size=10000)
        delivered = [it for it in items if it.get("status") == "已交接"]
        if target_system:
            delivered = [it for it in delivered if it.get("目标系统") == target_system]
        return delivered

    # ── 交接明细与回执回写 ───────────────────────────────────

    def write_back_confirmation(self, pack_id: int, landing: dict[str, Any]) -> dict[str, Any]:
        """把落库回执的确认结果回写到交接清单。

        - 交接明细（_details）按落库单版本复算结果逐条落判定口径；
        - 交接清单确认（_manifest）登记确认行数、映射版本、回执摘要，
          交接方据此核对实际记录和映射版本。
        """
        entry = self.get_entry(pack_id)
        if entry is None:
            return {"ok": False, "reason": "交接包不存在"}
        receipts = [dict(r) for r in landing.get("_receipts", [])]
        details = [dict(d) for d in landing.get("_details", [])]
        version = str(landing.get("映射版本") or "")
        digest = sha256_hex(
            "|".join(
                f"{r.get('source_id')}={r.get('output')}@{r.get('映射版本')}"
                for r in receipts
            )
        )
        entry["映射版本"] = version
        entry["记录行数"] = str(len(receipts))
        entry["_details"] = details
        entry["_manifest"] = {
            "确认行数": len(receipts),
            "映射版本": version,
            "落库编号": landing.get("落库编号", ""),
            "回执摘要": digest,
            "确认时间": now_iso(),
            "receipts": receipts,
        }
        entry["交接时间"] = now_iso()
        self.run_action(pack_id, "启动打包")
        self.run_action(pack_id, "确认交接")
        return {
            "ok": True,
            "pack_id": pack_id,
            "confirmed_rows": len(receipts),
            "映射版本": version,
            "回执摘要": digest,
        }

    def verify_handoff(self, pack_id: int) -> dict[str, Any]:
        """交接方核对：交接明细单一映射版本、回执与确认行数一致。"""
        entry = self.get_entry(pack_id)
        if entry is None:
            return {"ok": False, "reason": "交接包不存在"}
        details = entry.get("_details", [])
        manifest = entry.get("_manifest", {})
        receipts = manifest.get("receipts", [])
        detail_versions = {str(d.get("映射版本") or "") for d in details}
        receipt_versions = {str(r.get("映射版本") or "") for r in receipts}
        mismatches: list[str] = []
        if len(detail_versions) > 1:
            mismatches.append(f"交接明细映射版本不唯一: {sorted(detail_versions)}")
        if len(receipt_versions) > 1:
            mismatches.append(f"回执映射版本不唯一: {sorted(receipt_versions)}")
        if manifest.get("确认行数") != len(receipts):
            mismatches.append("确认行数与回执条数不一致")
        if str(entry.get("记录行数") or "") != str(manifest.get("确认行数") or ""):
            mismatches.append("交接清单记录行数与确认行数不一致")
        if str(entry.get("映射版本") or "") != str(manifest.get("映射版本") or ""):
            mismatches.append("交接包映射版本与清单确认版本不一致")
        return {
            "ok": not mismatches,
            "pack_id": pack_id,
            "映射版本": entry.get("映射版本") or "",
            "confirmed_rows": manifest.get("确认行数", 0),
            "single_version": len(detail_versions) <= 1,
            "mismatches": mismatches,
        }
