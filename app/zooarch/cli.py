"""动物遗存模块命令行。

``quantify`` 从 JSON 文件读取固定输入（records/ruleset/group_by/refit_groups），
调用与 HTTP 接口完全相同的纯函数引擎，输出相同的结果与哈希，用于离线复现。
``report`` 与 ``recompute`` 直接操作 SQLite 数据库（路径同服务配置）。
"""
from __future__ import annotations

import argparse
import json
import sys

from app.zooarch import mni


def _cmd_quantify(args: argparse.Namespace) -> int:
    with open(args.input, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    try:
        result = mni.quantify(
            payload.get("records", []),
            payload.get("ruleset"),
            payload.get("group_by", ["unit", "layer"]),
            payload.get("refit_groups", []),
        )
    except mni.QuantifyError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    indent = 2 if args.pretty else None
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=indent))
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from app.database import close_connection, init_db
    from app.zooarch.service import ZooarchService

    init_db()
    try:
        group_by = [item.strip() for item in args.group_by.split(",") if item.strip()]
        report = ZooarchService().create_report(
            args.project_id, args.name, group_by, args.ruleset_version, args.actor_id)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    finally:
        close_connection()


def _cmd_recompute(args: argparse.Namespace) -> int:
    from app.database import close_connection, init_db
    from app.zooarch.service import ZooarchService

    init_db()
    try:
        outcome = ZooarchService().recompute_report(args.report_id)
        print(json.dumps(outcome, ensure_ascii=False, sort_keys=True))
        return 0 if outcome["matches"] else 1
    finally:
        close_connection()


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.zooarch.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    quantify = sub.add_parser("quantify", help="以固定 JSON 输入复现量化计算")
    quantify.add_argument("--input", required=True, help="输入 JSON 文件路径")
    quantify.add_argument("--pretty", action="store_true")
    quantify.set_defaults(func=_cmd_quantify)

    report = sub.add_parser("report", help="在数据库中创建固定版本的量化报告")
    report.add_argument("--project-id", type=int, required=True)
    report.add_argument("--name", required=True)
    report.add_argument("--group-by", default="unit,layer")
    report.add_argument("--ruleset-version", type=int, default=None)
    report.add_argument("--actor-id", type=int, default=None)
    report.set_defaults(func=_cmd_report)

    recompute = sub.add_parser("recompute", help="按报告固定的记录版本重算并校验")
    recompute.add_argument("--report-id", type=int, required=True)
    recompute.set_defaults(func=_cmd_recompute)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
