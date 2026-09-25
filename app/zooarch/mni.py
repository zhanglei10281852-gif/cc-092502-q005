"""动物遗存量化纯函数引擎。

本模块不依赖数据库与 Web 框架，HTTP 接口与命令行都以同一份固定输入调用
:func:`quantify`，保证结果可复现。输入记录字段见 ``RECORD_FIELDS``。

量化口径（可解释下界）：

- NISP（碎片数）：纳入统计记录的 ``fragment_count`` 之和，拼合不改变碎片计数。
- MNE（最小单元数）：按 （部位, 侧, 年龄阶段） 统计骨骼单元数。未经审核确认的
  碎片各自计为一个单元（候选拼合不自动合并）；经确认的拼合组件合并为一件虚拟
  标本（保存比例相加、封顶 1.0）计为一个单元。保存比例同时以 ``portion_sum``
  形式保留在明细中，并用于拼合候选校验（比例互补之和不超过 1）。
- MNI（最小个体数）：按 分组 × 分类单元 × 年龄类 计算。左右配对只在同一年龄类
  内进行（年龄不相容的个体不得合并）；未知侧单元先与已知侧配对，剩余两两配对；
  唯一部位（如头骨、骶骨）不参与配对，每个单元即一个个体。分类单元的 MNI 为
  各年龄类内"取各部位最大值"后再跨年龄类求和，并给出驱动部位明细。
"""
from __future__ import annotations

import hashlib
from typing import Any, Iterable

GROUP_FIELDS = ("unit", "layer", "square", "bag")

RECORD_FIELDS = (
    "id", "version", "taxon_path", "element", "side", "age_stage",
    "portion", "fragment_count", "burned", "cut_marks", "confidence",
    "status", "unit", "layer", "square", "bag",
)

SIDES = ("left", "right", "axial", "unknown")

DEFAULT_AGE_CLASSES = {
    "juvenile": ["juvenile"],
    "adult": ["subadult", "adult"],
    "senile": ["senile"],
    "unknown": ["unknown"],
}

DEFAULT_UNIQUE_ELEMENTS = ["skull", "mandible", "sacrum", "sternum", "atlas", "axis"]

DEFAULT_RULESET = {
    "min_confidence": 0.4,
    "unique_elements": DEFAULT_UNIQUE_ELEMENTS,
    "age_classes": DEFAULT_AGE_CLASSES,
    "refit_same_context": True,
}


class QuantifyError(ValueError):
    """量化输入不合法（分组字段、年龄阶段映射、规则集等）。"""


def _stable_json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode()).hexdigest()


def normalize_ruleset(rules: dict[str, Any] | None = None) -> dict[str, Any]:
    """合并默认规则并校验，返回规范化规则集（新 dict，不改入参）。"""
    merged = {
        "min_confidence": DEFAULT_RULESET["min_confidence"],
        "unique_elements": list(DEFAULT_RULESET["unique_elements"]),
        "age_classes": {k: list(v) for k, v in DEFAULT_RULESET["age_classes"].items()},
        "refit_same_context": DEFAULT_RULESET["refit_same_context"],
    }
    if rules:
        for key in merged:
            if key in rules and rules[key] is not None:
                merged[key] = rules[key]
    unknown = set(rules or {}) - set(merged)
    if unknown:
        raise QuantifyError(f"规则集包含未知字段: {sorted(unknown)}")
    try:
        merged["min_confidence"] = float(merged["min_confidence"])
    except (TypeError, ValueError) as exc:
        raise QuantifyError("min_confidence 必须是数值") from exc
    if not 0.0 <= merged["min_confidence"] <= 1.0:
        raise QuantifyError("min_confidence 必须位于 [0, 1]")
    merged["unique_elements"] = sorted({str(e).strip().lower() for e in merged["unique_elements"] if str(e).strip()})
    classes = merged["age_classes"]
    if not isinstance(classes, dict) or not classes:
        raise QuantifyError("age_classes 必须是非空映射")
    seen: dict[str, str] = {}
    normalized_classes: dict[str, list[str]] = {}
    for cls, stages in classes.items():
        if not cls or not isinstance(stages, (list, tuple)) or not stages:
            raise QuantifyError(f"年龄类 {cls!r} 必须包含至少一个年龄阶段")
        normalized = []
        for stage in stages:
            stage = str(stage).strip()
            if not stage:
                raise QuantifyError(f"年龄类 {cls!r} 包含空阶段名")
            if stage in seen:
                raise QuantifyError(f"年龄阶段 {stage!r} 同时出现在 {seen[stage]!r} 与 {cls!r}")
            seen[stage] = str(cls)
            normalized.append(stage)
        normalized_classes[str(cls)] = normalized
    merged["age_classes"] = normalized_classes
    merged["refit_same_context"] = bool(merged["refit_same_context"])
    return merged


def stage_class_map(rules: dict[str, Any]) -> dict[str, str]:
    return {stage: cls for cls, stages in rules["age_classes"].items() for stage in stages}


def canonical_record(record: dict[str, Any]) -> dict[str, Any]:
    """抽取量化相关字段并规范化类型，保证哈希稳定。"""
    missing = [key for key in RECORD_FIELDS if key not in record]
    if missing:
        raise QuantifyError(f"记录缺少字段: {missing}")
    out = {
        "id": int(record["id"]),
        "version": int(record["version"]),
        "taxon_path": str(record["taxon_path"]).strip(),
        "element": str(record["element"]).strip().lower(),
        "side": str(record["side"]).strip().lower(),
        "age_stage": str(record["age_stage"]).strip(),
        "portion": float(record["portion"]),
        "fragment_count": int(record["fragment_count"]),
        "burned": bool(record["burned"]),
        "cut_marks": bool(record["cut_marks"]),
        "confidence": float(record["confidence"]),
        "status": str(record["status"]),
        "unit": str(record["unit"]).strip(),
        "layer": str(record["layer"]).strip(),
        "square": str(record["square"]).strip(),
        "bag": str(record["bag"]).strip(),
    }
    if out["side"] not in SIDES:
        raise QuantifyError(f"记录 {out['id']} 的侧别非法: {out['side']!r}")
    if not 0.0 < out["portion"] <= 1.0:
        raise QuantifyError(f"记录 {out['id']} 的保存比例必须位于 (0, 1]")
    if out["fragment_count"] < 1:
        raise QuantifyError(f"记录 {out['id']} 的碎片数必须 >= 1")
    if not 0.0 <= out["confidence"] <= 1.0:
        raise QuantifyError(f"记录 {out['id']} 的置信度必须位于 [0, 1]")
    if out["status"] not in ("staged", "published"):
        raise QuantifyError(f"记录 {out['id']} 的状态非法: {out['status']!r}")
    if not out["taxon_path"] or not out["element"]:
        raise QuantifyError(f"记录 {out['id']} 的分类层级与骨骼部位不能为空")
    return out


def canonical_refit_pairs(refit_groups: Iterable[Iterable[int]] | None) -> list[list[int]]:
    pairs = set()
    for group in refit_groups or ():
        ids = sorted({int(x) for x in group})
        if len(ids) < 2:
            continue
        for i in range(len(ids)):
            for j in range(i + 1, len(ids)):
                pairs.add((ids[i], ids[j]))
    return [list(pair) for pair in sorted(pairs)]


def canonical_input(records: list[dict[str, Any]], ruleset: dict[str, Any] | None,
                    group_by: Iterable[str], refit_groups: Iterable[Iterable[int]] | None) -> dict[str, Any]:
    return {
        "records": sorted((canonical_record(r) for r in records), key=lambda r: r["id"]),
        "ruleset": normalize_ruleset(ruleset),
        "group_by": list(group_by),
        "refit_groups": canonical_refit_pairs(refit_groups),
    }


def input_hash(records: list[dict[str, Any]], ruleset: dict[str, Any] | None,
               group_by: Iterable[str], refit_groups: Iterable[Iterable[int]] | None) -> str:
    return _hash(canonical_input(records, ruleset, group_by, refit_groups))


def _validate_group_by(group_by: list[str]) -> None:
    if len(set(group_by)) != len(group_by):
        raise QuantifyError(f"分组字段重复: {group_by}")
    bad = [f for f in group_by if f not in GROUP_FIELDS]
    if bad:
        raise QuantifyError(f"分组字段非法: {bad}，可选 {list(GROUP_FIELDS)}")


def _bucket(side: str) -> str:
    return side if side in ("left", "right") else "other"


def _components(ids: list[int], pairs: list[list[int]]) -> list[list[int]]:
    parent = {i: i for i in ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in pairs:
        if a in parent and b in parent:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)
    comps: dict[int, list[int]] = {}
    for i in ids:
        comps.setdefault(find(i), []).append(i)
    return [sorted(members) for _, members in sorted(comps.items())]


def _merge(members: list[dict[str, Any]]) -> dict[str, Any]:
    """把一组已确认拼合的记录合并为一件虚拟标本（成员按 id 升序）。"""
    base = dict(members[0])
    base["members"] = [m["id"] for m in members]
    base["portion"] = min(1.0, sum(m["portion"] for m in members))
    base["fragment_count"] = sum(m["fragment_count"] for m in members)
    base["confidence"] = max(m["confidence"] for m in members)
    base["burned"] = any(m["burned"] for m in members)
    base["cut_marks"] = any(m["cut_marks"] for m in members)
    known_sides = {m["side"] for m in members} - {"unknown", "axial"}
    if len(known_sides) == 1:
        base["side"] = known_sides.pop()
    best = sorted(members, key=lambda m: (-m["confidence"], m["id"]))[0]
    base["age_stage"] = best["age_stage"]
    return base


def _pair_counts(left: int, right: int, other: int) -> tuple[int, int]:
    """返回 (配对数, 未配对数)。未知侧先与已知侧配对，剩余两两配对。"""
    pairs = min(left, right)
    remainder = abs(left - right)
    absorb = min(other, remainder)
    pairs += absorb
    other_left = other - absorb
    pairs += other_left // 2
    total = left + right + other
    return pairs, total - 2 * pairs


def quantify(records: list[dict[str, Any]], ruleset: dict[str, Any] | None = None,
             group_by: Iterable[str] = ("unit", "layer"),
             refit_groups: Iterable[Iterable[int]] | None = None) -> dict[str, Any]:
    """对固定输入计算 NISP/MNE/MNI 及贡献明细，返回可 JSON 序列化的结果。"""
    group_by = list(group_by)
    _validate_group_by(group_by)
    canonical = canonical_input(records, ruleset, group_by, refit_groups)
    rules = canonical["ruleset"]
    stage_class = stage_class_map(rules)
    unique_elements = set(rules["unique_elements"])

    excluded: dict[tuple, dict[str, int]] = {}
    eligible: list[dict[str, Any]] = []
    for record in canonical["records"]:
        key = tuple(record[f] for f in group_by)
        if record["status"] != "published":
            excluded.setdefault(key, {"staged": 0, "low_confidence": 0})["staged"] += 1
        elif record["confidence"] < rules["min_confidence"]:
            excluded.setdefault(key, {"staged": 0, "low_confidence": 0})["low_confidence"] += 1
        else:
            eligible.append(record)

    pairs = canonical["refit_groups"]
    by_id = {r["id"]: r for r in eligible}
    merged = [_merge([by_id[i] for i in comp]) for comp in _components([r["id"] for r in eligible], pairs)]

    for record in merged:
        if record["age_stage"] not in stage_class:
            raise QuantifyError(
                f"记录 {record['id']} 的年龄阶段 {record['age_stage']!r} 未在规则集 age_classes 中映射")

    grouped: dict[tuple, list[dict[str, Any]]] = {}
    for record in merged:
        grouped.setdefault(tuple(record[f] for f in group_by), []).append(record)

    out_groups = []
    totals = {"nisp": 0, "mne": 0, "mni": 0, "excluded": {"staged": 0, "low_confidence": 0}}
    for key in sorted(set(grouped) | set(excluded)):
        taxa_map: dict[str, list[dict[str, Any]]] = {}
        for record in grouped.get(key, []):
            taxa_map.setdefault(record["taxon_path"], []).append(record)
        taxa_entries = []
        group_totals = {"nisp": 0, "mne": 0, "mni": 0}
        for taxon in sorted(taxa_map):
            entry = _quantify_taxon(taxa_map[taxon], rules, stage_class, unique_elements)
            taxa_entries.append(entry)
            group_totals["nisp"] += entry["nisp"]
            group_totals["mne"] += entry["mne"]
            group_totals["mni"] += entry["mni"]
        exc = excluded.get(key, {"staged": 0, "low_confidence": 0})
        out_groups.append({
            "key": dict(zip(group_by, key)),
            "nisp": group_totals["nisp"],
            "mne": group_totals["mne"],
            "mni": group_totals["mni"],
            "excluded": exc,
            "taxa": taxa_entries,
        })
        totals["nisp"] += group_totals["nisp"]
        totals["mne"] += group_totals["mne"]
        totals["mni"] += group_totals["mni"]
        totals["excluded"]["staged"] += exc["staged"]
        totals["excluded"]["low_confidence"] += exc["low_confidence"]

    result: dict[str, Any] = {
        "group_by": group_by,
        "input_hash": _hash(canonical),
        "groups": out_groups,
        "totals": totals,
    }
    result["result_hash"] = _hash(result)
    return result


def _quantify_taxon(records: list[dict[str, Any]], rules: dict[str, Any],
                    stage_class: dict[str, str], unique_elements: set[str]) -> dict[str, Any]:
    elements_map: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        elements_map.setdefault(record["element"], []).append(record)

    element_entries = []
    class_mni: dict[str, int] = {}
    class_drivers: dict[str, list[str]] = {}
    mne_taxon = 0
    for element in sorted(elements_map):
        element_records = elements_map[element]
        unique = element in unique_elements
        counts: dict[tuple[str, str], int] = {}
        portion_sums: dict[tuple[str, str], float] = {}
        for record in element_records:
            key = (_bucket(record["side"]), record["age_stage"])
            counts[key] = counts.get(key, 0) + 1
            portion_sums[key] = portion_sums.get(key, 0.0) + record["portion"]
        by_side_stage = [
            {"side": bucket, "age_stage": stage, "portion_sum": round(portion_sums[(bucket, stage)], 6),
             "units": count}
            for (bucket, stage), count in sorted(counts.items())
        ]
        mne_element = sum(counts.values())
        mne_taxon += mne_element

        classes_detail: dict[str, Any] = {}
        for cls in sorted(rules["age_classes"]):
            per = {"left": 0, "right": 0, "other": 0}
            for (bucket, stage), count in counts.items():
                if stage_class[stage] == cls:
                    per[bucket] += count
            total_units = sum(per.values())
            if total_units == 0:
                continue
            if unique:
                pairs, unpaired = 0, total_units
            else:
                pairs, unpaired = _pair_counts(per["left"], per["right"], per["other"])
            classes_detail[cls] = {
                "units": per,
                "pairs": pairs,
                "unpaired": unpaired,
                "mni": pairs + unpaired,
            }
            class_mni[cls] = max(class_mni.get(cls, 0), classes_detail[cls]["mni"])

        element_entries.append({
            "element": element,
            "unique": unique,
            "mne": mne_element,
            "mni": sum(c["mni"] for c in classes_detail.values()),
            "by_side_stage": by_side_stage,
            "classes": classes_detail,
            "records": sorted(i for r in element_records for i in r.get("members", [r["id"]])),
        })

    for cls, mni in class_mni.items():
        class_drivers[cls] = sorted(
            e["element"] for e in element_entries if e["classes"].get(cls, {}).get("mni", 0) == mni and mni > 0)

    return {
        "taxon_path": records[0]["taxon_path"],
        "nisp": sum(r["fragment_count"] for r in records),
        "mne": mne_taxon,
        "mni": sum(class_mni.values()),
        "burned_count": sum(1 for r in records if r["burned"]),
        "cut_mark_count": sum(1 for r in records if r["cut_marks"]),
        "age_classes": {cls: class_mni[cls] for cls in sorted(class_mni)},
        "driving_elements": {cls: class_drivers[cls] for cls in sorted(class_drivers)},
        "elements": element_entries,
    }
