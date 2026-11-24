"""字段映射服务：映射规则管理、试算验证与转换函数。

维护采集记录到目标存储字段的映射、转换函数和空值策略，
试算通过后才允许生效。支持映射版本化管理。

批量转换规则：整批采集记录按单一映射版本过转换口径，
整数与空值的判定口径逐条落到转换明细，失败行可按来源与规则定位。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Iterable

from app.dedup import now_iso
from app.services.base import BaseModuleService


def _next_version(current: str) -> str:
    """递增映射版本：v1→v2→…；非 v 开头的历史标记视为 v1，改版后为 v2。"""
    match = re.fullmatch(r"v(\d+)", str(current or "").strip())
    if match:
        return f"v{int(match.group(1)) + 1}"
    return "v2"


class FieldMappingService(BaseModuleService):
    MODULE = "field_mapping"
    ENTITY = "映射规则"
    OPERATION_TERM = "采集"
    LIST_FIELDS = [
        "映射编号", "来源字段", "目标字段", "转换函数", "空值策略",
        "类型声明", "映射版本", "最近生效",
    ]
    REQUIRED_FIELDS = ["映射编号", "来源字段", "目标字段"]
    STATUS_ORDER = ["草稿", "试算中", "已生效", "已作废"]
    ACTION_RULES = {"提交试算": "试算中", "生效映射": "已生效", "作废映射": "已作废"}
    NEGATIVE_ACTIONS = ["作废映射"]

    _TRANSFORM_FUNCTIONS: dict[str, Callable[[str], Any]] = {
        "str_to_int": lambda v: int(v) if v else 0,
        "str_to_float": lambda v: float(v) if v else 0.0,
        "to_upper": lambda v: str(v).upper(),
        "to_lower": lambda v: str(v).lower(),
        "strip": lambda v: str(v).strip(),
        "default_empty": lambda v: str(v) if v else "",
    }

    def trial_mapping(self, mapping_id: int, sample_value: str) -> dict[str, Any]:
        """对单条映射做试算，返回转换结果供比对。"""
        entry = self.get_entry(mapping_id)
        if entry is None:
            return {"ok": False, "reason": "映射规则不存在"}
        transform = entry.get("转换函数", "strip")
        null_policy = entry.get("空值策略", "skip")
        if not sample_value and null_policy == "skip":
            return {"ok": False, "reason": "空值且策略为skip，跳过"}
        fn = self._TRANSFORM_FUNCTIONS.get(transform, self._TRANSFORM_FUNCTIONS["strip"])
        try:
            result = fn(sample_value)
            return {"ok": True, "mapping_id": mapping_id, "input": sample_value, "output": result}
        except (ValueError, TypeError) as e:
            return {"ok": False, "reason": f"转换失败: {e}"}

    def activate_mappings(self, mapping_ids: list[int]) -> dict[str, Any]:
        """批量生效映射规则。"""
        ok, fail = 0, 0
        for mid in mapping_ids:
            entry = self.get_entry(mid)
            if entry and entry.get("status") in ("草稿", "试算中"):
                self.run_action(mid, "提交试算")
                self.run_action(mid, "生效映射")
                ok += 1
            else:
                fail += 1
        return {"ok": True, "activated": ok, "failed": fail}

    # ── 批量转换规则 ─────────────────────────────────────────

    @staticmethod
    def _is_null(raw_value: Any) -> bool:
        """空值判定口径：None 或去空白后为空串。"""
        return raw_value is None or (isinstance(raw_value, str) and raw_value.strip() == "")

    def _convert_one(self, entry: dict[str, Any], raw_value: Any) -> dict[str, Any]:
        """按映射条目当前的转换口径转换单个值。"""
        transform = entry.get("转换函数", "strip")
        null_policy = entry.get("空值策略", "skip")
        if self._is_null(raw_value) and null_policy == "skip":
            return {"status": "skipped", "output": None, "reason": "空值按策略skip跳过"}
        fn = self._TRANSFORM_FUNCTIONS.get(transform, self._TRANSFORM_FUNCTIONS["strip"])
        try:
            if self._is_null(raw_value):
                # 非 skip 策略：空值交给转换函数兜底（如 str_to_int→0）
                output = fn("")
            else:
                output = fn(raw_value if isinstance(raw_value, str) else str(raw_value))
            return {"status": "converted", "output": output, "reason": ""}
        except (ValueError, TypeError, ArithmeticError) as e:
            return {"status": "failed", "output": None, "reason": f"转换失败: {e}"}

    @staticmethod
    def _normalize_records(
        records: Iterable[Any], source_field: str,
    ) -> list[tuple[str, Any]]:
        """把入参规整为 (来源标识, 原始值)：支持 dict 记录或裸值。"""
        normalized: list[tuple[str, Any]] = []
        for idx, rec in enumerate(records):
            if isinstance(rec, dict):
                source_id = str(
                    rec.get("source_id") or rec.get("来源编号") or f"ROW-{idx + 1}"
                )
                if source_field in rec:
                    value = rec[source_field]
                else:
                    value = rec.get("value")
            else:
                source_id, value = f"ROW-{idx + 1}", rec
            normalized.append((source_id, value))
        return normalized

    def convert_batch(
        self,
        mapping_id: int,
        records: Iterable[Any],
        *,
        source_field: str | None = None,
    ) -> dict[str, Any]:
        """批量转换：整批采集记录按映射当前版本过同一套转换口径。

        每条记录产出转换明细（converted/skipped/failed），失败行带来源标识、
        来源字段与映射编号+版本，可按来源和规则定位。本方法无副作用，
        试算页与落库链路共用同一引擎，保证口径一致。
        """
        entry = self.get_entry(mapping_id)
        if entry is None:
            return {"ok": False, "reason": "映射规则不存在"}
        field = source_field or entry.get("来源字段") or "value"
        version = str(entry.get("映射版本") or "")
        rule_no = str(entry.get("映射编号") or "")
        items: list[dict[str, Any]] = []
        counts = {"converted": 0, "skipped": 0, "failed": 0}
        for source_id, raw_value in self._normalize_records(records, field):
            outcome = self._convert_one(entry, raw_value)
            counts[outcome["status"]] += 1
            items.append({
                "source_id": source_id,
                "input": raw_value,
                "status": outcome["status"],
                "output": outcome["output"],
                "reason": outcome["reason"],
                "来源字段": field,
                "映射编号": rule_no,
                "映射版本": version,
            })
        return {
            "ok": True,
            "mapping_id": mapping_id,
            "映射编号": rule_no,
            "映射版本": version,
            "来源字段": field,
            "total": len(items),
            "converted": counts["converted"],
            "skipped": counts["skipped"],
            "failed": counts["failed"],
            "items": items,
        }

    def revise_mapping(
        self,
        mapping_id: int,
        *,
        transform: str | None = None,
        null_policy: str | None = None,
    ) -> dict[str, Any]:
        """修改转换口径（转换函数/空值策略）并递增映射版本。

        口径一旦改变即产生新版本，落库恢复时按新版本完整复算，
        不允许同一交接明细混用两个版本。
        """
        entry = self.get_entry(mapping_id)
        if entry is None:
            return {"ok": False, "reason": "映射规则不存在"}
        if transform is None and null_policy is None:
            return {"ok": False, "reason": "未提供新的转换口径"}
        if transform is not None:
            if transform not in self._TRANSFORM_FUNCTIONS:
                return {"ok": False, "reason": f"未知转换函数: {transform}"}
            entry["转换函数"] = transform
        if null_policy is not None:
            entry["空值策略"] = null_policy
        previous = str(entry.get("映射版本") or "")
        entry["映射版本"] = _next_version(previous)
        entry["最近生效"] = now_iso()
        return {
            "ok": True,
            "mapping_id": mapping_id,
            "previous_version": previous,
            "映射版本": entry["映射版本"],
            "转换函数": entry.get("转换函数"),
            "空值策略": entry.get("空值策略"),
        }
