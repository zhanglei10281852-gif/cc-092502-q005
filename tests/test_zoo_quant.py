"""量化引擎(纯函数)测试:配对、年龄不相容、唯一部位、分组边界、低置信与确定性。"""
from __future__ import annotations

from app.zoo_quant import input_hash, quantize, result_hash

TAXON = "Mammalia/Artiodactyla/Bovidae/Ovis aries"


def rec(no, **overrides):
    base = {
        "record_no": no,
        "taxon_path": TAXON,
        "element": "humerus",
        "side": "left",
        "age_stage": "adult",
        "portion": 1.0,
        "confidence": 1.0,
        "fragment_count": 1,
        "context_unit": "H1",
        "layer": "L1",
        "grid": "T1",
        "bag": "B1",
    }
    base.update(overrides)
    return base


def totals(result):
    return result["totals"]


def test_left_right_pairing_bounds_mni():
    # 2 件左侧 + 1 件右侧肱骨:左右配对后下界为 2,而不是碎片数 3
    result = quantize([rec("L1"), rec("L2"), rec("R1", side="right")], [])
    assert totals(result)["nisp"] == 3
    assert totals(result)["mne"] == 3
    assert totals(result)["mni"] == 2
    element = result["groups"][0]["taxa"][0]["elements"][0]
    assert "paired" in element["mni_basis"]


def test_age_incompatible_raises_lower_bound():
    # 同侧 2 件肱骨年龄不相容(幼年 vs 成年),至少 2 个个体
    result = quantize([rec("J1", age_stage="juvenile"), rec("A1", age_stage="adult")], [])
    assert totals(result)["mni"] == 2
    element = result["groups"][0]["taxa"][0]["elements"][0]
    assert element["sides"]["left"]["age_slot_count"] == 2
    # 年龄相容(同为成年)且保存比例互补时可拼为一件
    merged = quantize(
        [rec("P1", portion=0.4), rec("P2", portion=0.5)],
        [],
    )
    assert totals(merged)["mne"] == 1
    assert totals(merged)["mni"] == 1


def test_age_incompatible_blocks_portion_merge():
    # 保存比例互补但年龄不相容,不得并入同一单元
    result = quantize(
        [rec("J1", age_stage="juvenile", portion=0.4), rec("A1", age_stage="adult", portion=0.5)],
        [],
    )
    assert totals(result)["mne"] == 2
    assert totals(result)["mni"] == 2


def test_unique_element_counts_individuals_directly():
    # 3 块骶骨(中轴、每个个体仅一件)→ 至少 3 个个体
    records = [rec(f"S{i}", element="sacrum", side="axial") for i in range(3)]
    result = quantize(records, [], {"unique_elements": ["sacrum"]})
    assert totals(result)["mni"] == 3
    element = result["groups"][0]["taxa"][0]["elements"][0]
    assert element["unique_element"] is True


def test_portion_bin_packing():
    # 0.6 + 0.5 超过 1,必须计为 2 个单元
    result = quantize([rec("A", portion=0.6), rec("B", portion=0.5)], [])
    assert totals(result)["mne"] == 2
    # 关闭装箱后,互补碎片也不再合并
    no_merge = quantize([rec("A", portion=0.4), rec("B", portion=0.5)], [], {"portion_merge": False})
    assert totals(no_merge)["mne"] == 2


def test_grouping_boundary_changes_mni():
    # 同一件左肱骨与一件右肱骨:不分组可配对(MNI=1);按层分组后跨层不配对(各组 MNI=1,合计 2)
    records = [rec("L1", layer="L1"), rec("R1", side="right", layer="L2")]
    merged = quantize(records, [])
    assert totals(merged)["mni"] == 1
    grouped = quantize(records, ["layer"])
    assert len(grouped["groups"]) == 2
    assert totals(grouped)["mni"] == 2
    assert [group["key"] for group in grouped["groups"]] == [{"layer": "L1"}, {"layer": "L2"}]


def test_low_confidence_records_excluded_with_details():
    records = [rec("OK", confidence=0.9), rec("LOW", confidence=0.3, side="right")]
    result = quantize(records, [], {"min_confidence": 0.5})
    assert totals(result)["included_records"] == 1
    assert totals(result)["excluded_records"] == 1
    assert totals(result)["nisp"] == 1
    assert result["excluded"] == [
        {"record_no": "LOW", "reason": "low_confidence", "confidence": 0.3, "min_confidence": 0.5}
    ]


def test_fragment_count_drives_nisp_not_mne():
    # 一条记录代表 5 块已拼合碎片:NISP 计 5,MNE 仍为 1
    result = quantize([rec("F1", fragment_count=5)], [])
    assert totals(result)["nisp"] == 5
    assert totals(result)["mne"] == 1
    assert totals(result)["mni"] == 1


def test_unknown_side_separate_vs_assign_minority():
    records = [rec("U1", side="unknown"), rec("U2", side="unknown"), rec("U3", side="unknown")]
    separate = quantize(records, [])
    assert totals(separate)["mni"] == 2  # ceil(3/2)
    assigned = quantize(records, [], {"unknown_side": "assign_minority"})
    assert totals(assigned)["mni"] == 2  # 2 归入一侧、1 归入另一侧后取较大侧


def test_deterministic_output_and_hashes():
    records = [
        rec("B", portion=0.4, layer="L2"),
        rec("A", side="right", layer="L1"),
        rec("C", age_stage="juvenile", layer="L1"),
    ]
    first = quantize(records, ["layer"], {"min_confidence": 0.5})
    second = quantize(list(reversed(records)), ["layer"], {"min_confidence": 0.5})
    assert first == second
    assert result_hash(first) == result_hash(second)
    assert input_hash(records, ["layer"], {"min_confidence": 0.5}) == input_hash(
        records, ["layer"], {"min_confidence": 0.5}
    )


def test_contribution_details_are_explainable():
    result = quantize([rec("L1"), rec("L2"), rec("R1", side="right")], [])
    element = result["groups"][0]["taxa"][0]["elements"][0]
    left_units = element["sides"]["left"]["units"]
    assert [unit["members"] for unit in left_units] == [["L1"], ["L2"]]
    assert element["sides"]["left"]["age_slots"] == [["L1", "L2"]]
    taxon = result["groups"][0]["taxa"][0]
    assert taxon["deciding_elements"] == ["humerus"]
