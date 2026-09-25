"""动物遗存量化引擎:NISP(碎片数)、MNE(最小单元数)、MNI(最小个体数)。

引擎为纯函数实现,不访问数据库;HTTP 接口与命令行共用同一入口,
保证相同输入产生相同输出(可复现)。MNI 下界由三条可解释规则合成:

1. 左右配对: 成对部位分别对左右两侧做年龄分层,取两侧较大者;
2. 年龄不相容: 同侧同部位中年龄阶段不相容的单元必须属于不同个体;
3. 唯一部位: 每个个体仅一件的部位(如寰椎、骶骨)按单元数直接计数。

每一步都输出贡献明细(单元成员、年龄分层、取值依据),便于人工核对。
"""
from __future__ import annotations

import hashlib
import math
from typing import Any

from app.security import stable_json

UNKNOWN = "unknown"
EPSILON = 1e-9

#: 允许作为分组范围的空间来源字段
PROVENANCE_FIELDS = ("context_unit", "layer", "grid", "bag")

SIDES = ("left", "right", "axial", "unknown")

#: 量化规则的默认参数;规则版本保存的是与本字典合并后的完整参数
DEFAULT_RULES: dict[str, Any] = {
    "min_confidence": 0.0,  # 纳入统计的最低鉴定置信度
    "portion_merge": True,  # 同侧同部位按保存比例装箱合并为骨骼单元
    "portion_tolerance": 0.0,  # 装箱容量容差(比例和允许超出 1 的幅度)
    "respect_age_in_merge": True,  # 装箱时年龄不相容的记录不得并入同一单元
    "age_tolerance": 0,  # 年龄阶段秩次差不超过该值视为相容
    "age_ranks": {"juvenile": 0, "subadult": 1, "adult": 2, "senile": 3},
    "unknown_side": "separate",  # 侧别未知单元的处理: separate | assign_minority
    "element_counts": {},  # 每个个体该部位的数量,默认 2(成对)
    "unique_elements": [],  # 每个个体仅一件的部位(寰椎、骶骨等)
}


def normalize_rules(overrides: dict[str, Any] | None) -> dict[str, Any]:
    """合并默认规则参数,拒绝未知键,保证规则版本语义明确。"""
    merged: dict[str, Any] = {
        key: (dict(value) if isinstance(value, dict) else (list(value) if isinstance(value, list) else value))
        for key, value in DEFAULT_RULES.items()
    }
    for key, value in (overrides or {}).items():
        if key not in merged:
            raise ValueError(f"未知规则参数: {key}")
        merged[key] = value
    if merged["unknown_side"] not in ("separate", "assign_minority"):
        raise ValueError("unknown_side 仅支持 separate 或 assign_minority")
    if not isinstance(merged["age_ranks"], dict):
        raise ValueError("age_ranks 必须是 阶段名->秩次 的映射")
    return merged


def apply_filters(records: list[dict[str, Any]], filters: dict[str, Any] | None) -> list[dict[str, Any]]:
    """按来源字段等值列表、部位列表或分类前缀过滤记录。"""
    if not filters:
        return list(records)
    output = []
    for rec in records:
        keep = True
        for key, value in filters.items():
            if key == "taxon_prefix":
                keep = str(rec.get("taxon_path", "")).startswith(str(value))
            elif key == "element" or key in PROVENANCE_FIELDS:
                allowed = value if isinstance(value, list) else [value]
                keep = str(rec.get(key) or "") in {str(item) for item in allowed}
            else:
                raise ValueError(f"不支持的过滤字段: {key}")
            if not keep:
                break
        if keep:
            output.append(rec)
    return output


def _age_rank(age: str, rules: dict[str, Any]) -> int | None:
    return rules["age_ranks"].get(age)


def ages_compatible(age_a: str, age_b: str, rules: dict[str, Any]) -> bool:
    """未知或未登记秩次的年龄阶段与任何阶段相容(保守,不抬高下界)。"""
    rank_a, rank_b = _age_rank(age_a, rules), _age_rank(age_b, rules)
    if rank_a is None or rank_b is None:
        return True
    return abs(rank_a - rank_b) <= int(rules["age_tolerance"])


def _record_no(rec: dict[str, Any], fallback: str) -> str:
    return str(rec.get("record_no") or fallback)


def _new_unit(rec: dict[str, Any]) -> dict[str, Any]:
    return {"members": [rec], "portion": float(rec.get("portion", 1.0))}


def _pack_units(records: list[dict[str, Any]], rules: dict[str, Any]) -> list[dict[str, Any]]:
    """把同(分类,部位,侧别)的记录按保存比例装箱为最小骨骼单元(MNE)。

    采用按保存比例降序的首次适配装箱:比例互补(和不超过 1+容差)且
    年龄相容的记录可拼为同一骨骼单元;否则各自独立。结果按成员编号排序,
    保证确定性。
    """
    ordered = sorted(
        records,
        key=lambda rec: (-float(rec.get("portion", 1.0)), _record_no(rec, "")),
    )
    units: list[dict[str, Any]] = []
    for rec in ordered:
        if not rules["portion_merge"]:
            units.append(_new_unit(rec))
            continue
        placed = False
        for unit in units:
            if unit["portion"] + float(rec.get("portion", 1.0)) > 1.0 + float(rules["portion_tolerance"]) + EPSILON:
                continue
            if rules["respect_age_in_merge"] and not all(
                ages_compatible(str(member.get("age_stage", UNKNOWN)), str(rec.get("age_stage", UNKNOWN)), rules)
                for member in unit["members"]
            ):
                continue
            unit["members"].append(rec)
            unit["portion"] += float(rec.get("portion", 1.0))
            placed = True
            break
        if not placed:
            units.append(_new_unit(rec))
    for unit in units:
        unit["members"].sort(key=lambda rec: _record_no(rec, ""))
    units.sort(key=lambda unit: _record_no(unit["members"][0], ""))
    return units


def _unit_age(unit: dict[str, Any], rules: dict[str, Any]) -> str:
    """单元年龄取成员中秩次最小的已知阶段;全未知则为 unknown。"""
    known = [str(member.get("age_stage", UNKNOWN)) for member in unit["members"]]
    known = [age for age in known if _age_rank(age, rules) is not None]
    if not known:
        return UNKNOWN
    return min(known, key=lambda age: _age_rank(age, rules))


def _age_slots(records: list[dict[str, Any]], rules: dict[str, Any]) -> list[list[dict[str, Any]]]:
    """把记录按年龄相容性做首次适配分层:同层记录两两相容,
    层数即该侧该部位由年龄不相容约束给出的最少个体数。"""
    def sort_key(rec: dict[str, Any]) -> tuple[int, int, str]:
        age = str(rec.get("age_stage", UNKNOWN))
        rank = _age_rank(age, rules)
        return (1 if rank is None else 0, rank if rank is not None else 0, _record_no(rec, ""))

    slots: list[list[dict[str, Any]]] = []
    for rec in sorted(records, key=sort_key):
        age = str(rec.get("age_stage", UNKNOWN))
        for slot in slots:
            if all(ages_compatible(age, str(member.get("age_stage", UNKNOWN)), rules) for member in slot):
                slot.append(rec)
                break
        else:
            slots.append([rec])
    return slots


def _quant_element(element: str, records: list[dict[str, Any]], rules: dict[str, Any]) -> dict[str, Any]:
    """计算单个(分类单元,部位)的 MNE 与 MNI,并给出可解释明细。

    每侧个体下界取两条独立证据的较大者:
    - 保存比例装箱得到的骨骼单元数(同侧两件骨骼必属两个个体);
    - 记录级年龄分层数(年龄不相容的记录必属不同个体)。
    """
    by_side: dict[str, list[dict[str, Any]]] = {side: [] for side in SIDES}
    for index, rec in enumerate(records):
        side = str(rec.get("side", UNKNOWN))
        by_side[side if side in by_side else UNKNOWN].append({**rec, "record_no": _record_no(rec, f"row-{index}")})

    unique = element in set(rules["unique_elements"])
    count = int(rules["element_counts"].get(element, 1 if unique else 2))

    side_detail: dict[str, Any] = {}
    total_mne = 0
    for side in SIDES:
        units = _pack_units(by_side[side], rules)
        slots = _age_slots(by_side[side], rules)
        total_mne += len(units)
        side_detail[side] = {
            "record_count": len(by_side[side]),
            "mne": len(units),
            "age_slot_count": len(slots),
            "individual_bound": max(len(units), len(slots)),
            "units": [
                {
                    "members": [_record_no(member, "") for member in unit["members"]],
                    "portion": round(unit["portion"], 6),
                    "age_stage": _unit_age(unit, rules),
                }
                for unit in units
            ],
            "age_slots": [[_record_no(member, "") for member in slot] for slot in slots],
        }

    if count == 2 and not unique:
        left = side_detail["left"]["individual_bound"]
        right = side_detail["right"]["individual_bound"]
        unknown = side_detail["unknown"]["individual_bound"]
        if rules["unknown_side"] == "assign_minority" and unknown:
            for _ in range(unknown):
                if left <= right:
                    left += 1
                else:
                    right += 1
            mni = max(left, right)
            basis = f"assign_minority: 侧别未知单元逐件归入较少侧后 left={left}, right={right}"
        else:
            unknown_bound = math.ceil(unknown / 2)
            mni = max(left, right, unknown_bound)
            basis = f"paired: max(left={left}, right={right}, ceil(unknown/2)={unknown_bound})"
    else:
        total_bound = sum(side_detail[side]["individual_bound"] for side in SIDES)
        mni = math.ceil(total_bound / count)
        basis = f"per-individual-count={count}: ceil({total_bound}/{count})"

    return {
        "element": element,
        "count_per_individual": count,
        "unique_element": unique,
        "nisp": sum(int(rec.get("fragment_count", 1)) for rec in records),
        "mne": total_mne,
        "mni": mni,
        "mni_basis": basis,
        "sides": side_detail,
    }


def _quant_taxon(taxon_path: str, records: list[dict[str, Any]], rules: dict[str, Any]) -> dict[str, Any]:
    by_element: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        by_element.setdefault(str(rec.get("element", UNKNOWN)), []).append(rec)
    elements = [_quant_element(element, by_element[element], rules) for element in sorted(by_element)]
    mni = max((element["mni"] for element in elements), default=0)
    return {
        "taxon_path": taxon_path,
        "nisp": sum(element["nisp"] for element in elements),
        "mne": sum(element["mne"] for element in elements),
        "mni": mni,
        "deciding_elements": [element["element"] for element in elements if element["mni"] == mni],
        "elements": elements,
    }


def quantize(
    records: list[dict[str, Any]],
    grouping: list[str] | tuple[str, ...] = (),
    rules_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """对记录快照按分组范围计算 NISP/MNE/MNI 及贡献明细。

    records 为已发布记录的版本快照(服务层负责过滤);此处仅按规则中的
    min_confidence 排除低置信记录,并在 excluded 中说明原因。
    """
    rules = normalize_rules(rules_override)
    grouping = list(grouping)
    for field in grouping:
        if field not in PROVENANCE_FIELDS:
            raise ValueError(f"不支持的分组字段: {field}")

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for index, rec in enumerate(records):
        confidence = float(rec.get("confidence", 1.0))
        if confidence < float(rules["min_confidence"]):
            excluded.append({
                "record_no": _record_no(rec, f"row-{index}"),
                "reason": "low_confidence",
                "confidence": confidence,
                "min_confidence": float(rules["min_confidence"]),
            })
        else:
            included.append(rec)
    excluded.sort(key=lambda item: item["record_no"])

    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for rec in included:
        key = tuple(str(rec.get(field) or "") for field in grouping)
        groups.setdefault(key, []).append(rec)

    group_results = []
    for key in sorted(groups):
        by_taxon: dict[str, list[dict[str, Any]]] = {}
        for rec in groups[key]:
            by_taxon.setdefault(str(rec.get("taxon_path", UNKNOWN)), []).append(rec)
        taxa = [_quant_taxon(taxon, by_taxon[taxon], rules) for taxon in sorted(by_taxon)]
        group_results.append({
            "key": dict(zip(grouping, key)),
            "nisp": sum(taxon["nisp"] for taxon in taxa),
            "mne": sum(taxon["mne"] for taxon in taxa),
            "mni": sum(taxon["mni"] for taxon in taxa),
            "taxa": taxa,
        })

    return {
        "grouping": grouping,
        "rules": rules,
        "groups": group_results,
        "excluded": excluded,
        "totals": {
            "nisp": sum(group["nisp"] for group in group_results),
            "mne": sum(group["mne"] for group in group_results),
            "mni": sum(group["mni"] for group in group_results),
            "included_records": len(included),
            "excluded_records": len(excluded),
        },
    }


def input_hash(records: list[dict[str, Any]], grouping: list[str], rules: dict[str, Any], filters: dict[str, Any] | None = None) -> str:
    """固定输入(记录快照+分组+规则+过滤)的摘要,用于复现性校验。"""
    payload = {
        "records": list(records),
        "grouping": list(grouping),
        "rules": normalize_rules(rules),
        "filters": filters or {},
    }
    return hashlib.sha256(stable_json(payload).encode()).hexdigest()


def result_hash(result: dict[str, Any]) -> str:
    return hashlib.sha256(stable_json(result).encode()).hexdigest()


def find_refit_pairs(records: list[dict[str, Any]], rules_override: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """识别可能跨袋拼合的候选对(只提出候选,绝不修改记录)。

    判定依据:同分类同部位、侧别一致或一侧未知、分属不同袋号、
    保存比例互补、年龄相容、置信度达到规则阈值。
    """
    rules = normalize_rules(rules_override)
    eligible = [rec for rec in records if float(rec.get("confidence", 1.0)) >= float(rules["min_confidence"])]
    pairs: list[dict[str, Any]] = []
    for index, first in enumerate(eligible):
        for second in eligible[index + 1:]:
            if str(first.get("taxon_path")) != str(second.get("taxon_path")):
                continue
            if str(first.get("element")) != str(second.get("element")):
                continue
            side_a, side_b = str(first.get("side", UNKNOWN)), str(second.get("side", UNKNOWN))
            if side_a != side_b and UNKNOWN not in (side_a, side_b):
                continue
            bag_a, bag_b = str(first.get("bag") or ""), str(second.get("bag") or "")
            if not bag_a or not bag_b or bag_a == bag_b:
                continue
            portion_a, portion_b = float(first.get("portion", 1.0)), float(second.get("portion", 1.0))
            if portion_a + portion_b > 1.0 + float(rules["portion_tolerance"]) + EPSILON:
                continue
            if not ages_compatible(str(first.get("age_stage", UNKNOWN)), str(second.get("age_stage", UNKNOWN)), rules):
                continue
            record_a, record_b = sorted((first, second), key=lambda rec: int(rec.get("record_id", 0)))
            pairs.append({
                "record_a": record_a,
                "record_b": record_b,
                "reason": (
                    f"同部位({first.get('element')})同侧/侧别未知,保存比例互补"
                    f"({portion_a}+{portion_b}<=1),跨袋 {bag_a}<->{bag_b}"
                ),
            })
    pairs.sort(key=lambda pair: (int(pair["record_a"].get("record_id", 0)), int(pair["record_b"].get("record_id", 0))))
    return pairs
