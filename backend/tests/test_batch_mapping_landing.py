"""整批转换落库链路测试：批量转换规则、落库回执、中断恢复单版本复算。

验证场景：
1. 数量为 007、空值、7x 的三条采集记录 —— 回执只确认一条值为 7 的记录，
   空值跳过，7x 能按来源和规则定位；
2. 两条合法数字在写入中断时切换转换规则 —— 恢复后交接明细由单个映射
   版本完整复算，两条记录不各用一版，已交接的历史依据保留。
"""
from __future__ import annotations

import pytest

from app.pipeline import land_records_with_mapping, resume_interrupted_landing
from app.services import service_for


def _create_int_mapping(version: str = "v1") -> int:
    """登记一条 str_to_int + 空值 skip 的映射规则并生效，返回映射 id。"""
    mapping_svc = service_for("field_mapping")
    entry, missing = mapping_svc.create_entry({
        "映射编号": "FIEL-QTY",
        "来源字段": "quantity",
        "目标字段": "qty_int",
        "转换函数": "str_to_int",
        "空值策略": "skip",
        "类型声明": "int",
        "映射版本": version,
        "最近生效": "",
    })
    assert not missing
    mapping_svc.run_action(entry["id"], "提交试算")
    mapping_svc.run_action(entry["id"], "生效映射")
    return entry["id"]


class TestBatchConversion:
    """批量转换规则：整数与空值判定口径逐条落明细。"""

    def test_integer_null_and_dirty_records(self):
        mapping_id = _create_int_mapping()
        mapping_svc = service_for("field_mapping")
        result = mapping_svc.convert_batch(mapping_id, [
            {"source_id": "R1", "quantity": "007"},
            {"source_id": "R2", "quantity": None},
            {"source_id": "R3", "quantity": "7x"},
        ], source_field="quantity")
        assert result["ok"] is True
        assert result["total"] == 3
        assert result["converted"] == 1
        assert result["skipped"] == 1
        assert result["failed"] == 1
        by_src = {item["source_id"]: item for item in result["items"]}
        # 007 按整数口径转为 7
        assert by_src["R1"]["status"] == "converted"
        assert by_src["R1"]["output"] == 7
        # 空值按 skip 策略跳过
        assert by_src["R2"]["status"] == "skipped"
        # 7x 转换失败，可按来源和规则定位
        assert by_src["R3"]["status"] == "failed"
        assert by_src["R3"]["来源字段"] == "quantity"
        assert by_src["R3"]["映射编号"] == "FIEL-QTY"
        assert by_src["R3"]["映射版本"] == "v1"
        assert "7x" in by_src["R3"]["reason"]

    def test_batch_items_share_single_mapping_version(self):
        mapping_id = _create_int_mapping()
        mapping_svc = service_for("field_mapping")
        result = mapping_svc.convert_batch(
            mapping_id, ["1", "2", "3"], source_field="quantity")
        versions = {item["映射版本"] for item in result["items"]}
        assert versions == {"v1"}

    def test_revise_mapping_bumps_version(self):
        mapping_id = _create_int_mapping()
        mapping_svc = service_for("field_mapping")
        revised = mapping_svc.revise_mapping(mapping_id, null_policy="zero")
        assert revised["ok"] is True
        assert revised["previous_version"] == "v1"
        assert revised["映射版本"] == "v2"
        entry = mapping_svc.get_entry(mapping_id)
        assert entry["空值策略"] == "zero"
        assert entry["映射版本"] == "v2"

    def test_revise_rejects_unknown_transform(self):
        mapping_id = _create_int_mapping()
        mapping_svc = service_for("field_mapping")
        result = mapping_svc.revise_mapping(mapping_id, transform="not_a_fn")
        assert result["ok"] is False


class TestLandingReceiptsAndHandoff:
    """落库回执与交接清单回写：007 / 空值 / 7x 场景。"""

    def test_receipt_only_confirms_converted_row(self):
        mapping_id = _create_int_mapping()
        result = land_records_with_mapping(mapping_id, [
            {"source_id": "R1", "quantity": "007"},
            {"source_id": "R2", "quantity": None},
            {"source_id": "R3", "quantity": "7x"},
        ], source_field="quantity")
        assert result["status"] == "landed"
        # 回执只确认一条值为 7 的记录
        assert len(result["receipts"]) == 1
        receipt = result["receipts"][0]
        assert receipt["source_id"] == "R1"
        assert receipt["output"] == 7
        assert receipt["映射版本"] == "v1"
        # 空值跳过、7x 失败，均无回执
        by_src = {d["source_id"]: d for d in result["details"]}
        assert by_src["R2"]["status"] == "skipped"
        assert by_src["R3"]["status"] == "failed"
        assert by_src["R3"]["映射编号"] == "FIEL-QTY"

    def test_landing_records_mapping_version(self):
        mapping_id = _create_int_mapping()
        result = land_records_with_mapping(mapping_id, [
            {"source_id": "R1", "quantity": "007"},
            {"source_id": "R2", "quantity": None},
            {"source_id": "R3", "quantity": "7x"},
        ], source_field="quantity")
        landing_svc = service_for("data_landing")
        landing = landing_svc.get_entry(result["landing_id"])
        assert landing["映射版本"] == "v1"
        assert landing["写入行数"] == "1"
        assert landing["status"] == "已写入"

    def test_confirmation_written_back_to_handoff(self):
        mapping_id = _create_int_mapping()
        result = land_records_with_mapping(mapping_id, [
            {"source_id": "R1", "quantity": "007"},
            {"source_id": "R2", "quantity": None},
            {"source_id": "R3", "quantity": "7x"},
        ], source_field="quantity")
        handoff_svc = service_for("handoff_pack")
        pack = handoff_svc.get_entry(result["handoff_id"])
        assert pack["status"] == "已交接"
        # 交接清单回写：确认行数、映射版本、回执明细
        assert pack["记录行数"] == "1"
        assert pack["映射版本"] == "v1"
        manifest = pack["_manifest"]
        assert manifest["确认行数"] == 1
        assert manifest["映射版本"] == "v1"
        assert len(manifest["receipts"]) == 1
        assert manifest["receipts"][0]["output"] == 7
        # 交接明细落判定口径：转换 7 / 空值跳过 / 7x 失败可定位
        details = {d["source_id"]: d for d in pack["_details"]}
        assert details["R1"]["output"] == 7
        assert details["R2"]["status"] == "skipped"
        assert details["R3"]["status"] == "failed"
        assert details["R3"]["映射编号"] == "FIEL-QTY"
        # 交接方核对通过
        verify = handoff_svc.verify_handoff(result["handoff_id"])
        assert verify["ok"] is True
        assert verify["single_version"] is True
        assert verify["confirmed_rows"] == 1


class TestInterruptedResume:
    """写入中断切换转换规则后恢复：单版本复算，历史依据保留。"""

    def _interrupted_landing(self) -> dict:
        mapping_id = _create_int_mapping()
        result = land_records_with_mapping(mapping_id, [
            {"source_id": "A", "quantity": "5"},
            {"source_id": "B", "quantity": "8"},
        ], source_field="quantity", fail_after=1)
        assert result["status"] == "interrupted"
        result["mapping_id"] = mapping_id
        return result

    def test_interruption_preserves_partial_receipts_as_history(self):
        result = self._interrupted_landing()
        landing_svc = service_for("data_landing")
        landing = landing_svc.get_entry(result["landing_id"])
        assert landing["status"] == "写入异常"
        assert landing["映射版本"] == "v1"
        # 已落库的一行（值 5）作为历史依据保留
        history = landing["_write_history"]
        assert len(history) == 1
        assert history[0]["映射版本"] == "v1"
        assert len(history[0]["receipts"]) == 1
        assert history[0]["receipts"][0]["output"] == 5
        # 交接包尚未确认
        handoff_svc = service_for("handoff_pack")
        pack = handoff_svc.get_entry(result["handoff_id"])
        assert pack["status"] == "待打包"

    def test_resume_recomputes_all_records_under_single_version(self):
        result = self._interrupted_landing()
        mapping_svc = service_for("field_mapping")
        # 写入中断后切换转换口径 → 映射版本升到 v2
        revised = mapping_svc.revise_mapping(result["mapping_id"], null_policy="zero")
        assert revised["映射版本"] == "v2"

        resumed = resume_interrupted_landing(result["landing_id"])
        assert resumed["status"] == "resumed"
        assert resumed["映射版本"] == "v2"
        # 两条记录完整复算，交接明细不各用一版
        assert len(resumed["details"]) == 2
        detail_versions = {d["映射版本"] for d in resumed["details"]}
        assert detail_versions == {"v2"}
        receipt_versions = {r["映射版本"] for r in resumed["receipts"]}
        assert receipt_versions == {"v2"}
        outputs = {r["source_id"]: r["output"] for r in resumed["receipts"]}
        assert outputs == {"A": 5, "B": 8}
        # 历史依据保留：v1 的中断尝试仍在
        assert resumed["history_preserved"] == 1
        landing_svc = service_for("data_landing")
        landing = landing_svc.get_entry(result["landing_id"])
        assert landing["_write_history"][0]["映射版本"] == "v1"
        assert landing["status"] == "已写入"

    def test_handoff_after_resume_uses_single_version(self):
        result = self._interrupted_landing()
        mapping_svc = service_for("field_mapping")
        mapping_svc.revise_mapping(result["mapping_id"], null_policy="zero")
        resumed = resume_interrupted_landing(result["landing_id"])

        handoff_svc = service_for("handoff_pack")
        pack = handoff_svc.get_entry(resumed["handoff_id"])
        assert pack["status"] == "已交接"
        assert pack["映射版本"] == "v2"
        assert pack["记录行数"] == "2"
        versions = {d["映射版本"] for d in pack["_details"]}
        assert versions == {"v2"}
        verify = handoff_svc.verify_handoff(resumed["handoff_id"])
        assert verify["ok"] is True
        assert verify["single_version"] is True
        assert verify["confirmed_rows"] == 2

    def test_resume_without_records_is_rejected(self):
        landing_svc = service_for("data_landing")
        landing, _ = landing_svc.create_entry({
            "落库编号": "DATA-NOOP",
            "关联批次": "CAPT-NOOP",
            "目标存储": "datalake_rpa_collect",
        })
        result = landing_svc.resume_write(landing["id"])
        assert result["ok"] is False
