"""命令行复现:quant 子命令与 HTTP 报告结果一致,verify-report 校验通过。"""
from __future__ import annotations

import json

from app.zoo_cli import main as cli_main
from app.zoo_quant import input_hash, quantize, result_hash

RECORDS = [
    {"record_no": "K1", "taxon_path": "Mammalia/Artiodactyla/Bovidae/Ovis aries", "element": "humerus",
     "side": "left", "age_stage": "adult", "portion": 1.0, "confidence": 0.9, "fragment_count": 2,
     "context_unit": "H1", "layer": "L1", "grid": "T1", "bag": "B1"},
    {"record_no": "K2", "taxon_path": "Mammalia/Artiodactyla/Bovidae/Ovis aries", "element": "humerus",
     "side": "right", "age_stage": "adult", "portion": 0.6, "confidence": 0.9, "fragment_count": 1,
     "context_unit": "H1", "layer": "L1", "grid": "T1", "bag": "B2"},
    {"record_no": "K3", "taxon_path": "Mammalia/Artiodactyla/Bovidae/Ovis aries", "element": "humerus",
     "side": "left", "age_stage": "juvenile", "portion": 0.8, "confidence": 0.3, "fragment_count": 1,
     "context_unit": "H1", "layer": "L2", "grid": "T1", "bag": "B3"},
]

RULES = {"min_confidence": 0.5}


def test_cli_quant_matches_engine(tmp_path, capsys):
    records_file = tmp_path / "records.json"
    rules_file = tmp_path / "rules.json"
    records_file.write_text(json.dumps(RECORDS), encoding="utf-8")
    rules_file.write_text(json.dumps(RULES), encoding="utf-8")

    exit_code = cli_main([
        "quant", "--records", str(records_file), "--rules", str(rules_file), "--grouping", "layer",
    ])
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)

    expected = quantize(RECORDS, ["layer"], RULES)
    assert output["result"] == expected
    assert output["result_hash"] == result_hash(expected)
    assert output["input_hash"] == input_hash(RECORDS, ["layer"], RULES)
    # 低置信记录 K3 被排除,两组各 1 个个体
    assert output["result"]["totals"]["excluded_records"] == 1
    assert output["result"]["totals"]["mni"] == 1


def test_cli_quant_is_reproducible(tmp_path, capsys):
    records_file = tmp_path / "records.json"
    records_file.write_text(json.dumps(RECORDS), encoding="utf-8")
    args = ["quant", "--records", str(records_file), "--grouping", "layer,context_unit"]
    assert cli_main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert cli_main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert first == second


def test_cli_verify_report(tmp_path, capsys, client, owner, project, zoo):
    zoo.create_record(record_no="V1", side="left")
    zoo.create_record(record_no="V2", side="right")
    rule = zoo.create_rule(params={}).json()
    report = zoo.create_report(rule["id"]).json()

    exit_code = cli_main(["verify-report", "--project-id", str(project["id"]), "--report-id", str(report["id"])])
    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["consistent"] is True
    assert output["record_count"] == 2
