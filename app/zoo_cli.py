"""动物遗存量化与报告校验的命令行入口。

`quant` 子命令对固定输入文件(记录快照 JSON + 规则 JSON)执行与 HTTP 接口
完全相同的量化计算,输出结果及输入/结果摘要,用于离线复现;
`verify-report` 子命令按数据库中报告冻结的记录版本重算并校验一致性。

用法:
    python -m app.zoo_cli quant --records records.json [--rules rules.json] \
        [--grouping layer,context_unit] [--filters filters.json]
    python -m app.zoo_cli verify-report --project-id 1 --report-id 1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.zoo_quant import apply_filters, input_hash, quantize, result_hash


def _load_json(path: str) -> object:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def cmd_quant(args: argparse.Namespace) -> int:
    records = _load_json(args.records)
    if not isinstance(records, list):
        print(json.dumps({"error": "records 文件必须是记录对象数组"}, ensure_ascii=False))
        return 2
    rules = _load_json(args.rules) if args.rules else None
    filters = _load_json(args.filters) if args.filters else {}
    grouping = [field for field in args.grouping.split(",") if field]
    try:
        filtered = apply_filters(records, filters)
        result = quantize(filtered, grouping, rules)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    output = {
        "input_hash": input_hash(filtered, grouping, rules or {}, filters),
        "result_hash": result_hash(result),
        "result": result,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


def cmd_verify_report(args: argparse.Namespace) -> int:
    from app.database import init_db
    from app.zoo_schema import init_zoo_db
    from app.zoo_service import ZooService

    init_db()
    init_zoo_db()
    try:
        outcome = ZooService().verify_report(args.project_id, args.report_id)
    except Exception as exc:  # ServiceError 等,统一以 JSON 报告
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return 0 if outcome["consistent"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zoo_cli", description="动物遗存量化与报告校验")
    sub = parser.add_subparsers(dest="command", required=True)

    quant = sub.add_parser("quant", help="对固定输入文件复现量化计算")
    quant.add_argument("--records", required=True, help="记录快照 JSON 文件(数组)")
    quant.add_argument("--rules", default=None, help="规则参数 JSON 文件(可选)")
    quant.add_argument("--grouping", default="", help="分组字段,逗号分隔,如 layer,context_unit")
    quant.add_argument("--filters", default=None, help="过滤条件 JSON 文件(可选)")
    quant.set_defaults(handler=cmd_quant)

    verify = sub.add_parser("verify-report", help="按报告冻结的记录版本重算并校验")
    verify.add_argument("--project-id", type=int, required=True)
    verify.add_argument("--report-id", type=int, required=True)
    verify.set_defaults(handler=cmd_verify_report)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
