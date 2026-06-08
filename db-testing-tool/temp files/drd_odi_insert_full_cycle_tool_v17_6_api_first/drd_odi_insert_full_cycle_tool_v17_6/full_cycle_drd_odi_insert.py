#!/usr/bin/env python3
"""
full_cycle_drd_odi_insert.py

v17.6 API-first full-cycle flow.

Important:
- API JSON reports are built directly from raw compare/insert outputs.
- Excel reports are optional renderings from the API JSON model.
- --report-mode api does not create or parse Excel workbooks.

Flow:
1. Run or reuse DRD/ODI comparison.
2. Generate Step 1 report via api_first_report_builder.py.
3. Run or reuse DRD-based generated INSERT.
4. Generate Step 4 report via api_first_report_builder.py.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def run(cmd, cwd: Path):
    print("RUN:", " ".join(str(x) for x in cmd))
    rc = subprocess.call(cmd, cwd=str(cwd))
    if rc != 0:
        raise SystemExit(rc)


def copy_dir(src: Path, dst: Path):
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def read_json_if_exists(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"_error": str(exc), "_path": str(path)}


def main(argv=None) -> int:
    here = Path(__file__).resolve().parent
    p = argparse.ArgumentParser(description="Full DRD/ODI/Generated INSERT comparison flow v17.6 API-first")
    p.add_argument("--xlsx", default="", help="DRD Excel file; required unless --existing-compare-out and --existing-insert-out are both provided")
    p.add_argument("--original-xml", default="", help="ODI File 1 / old XML")
    p.add_argument("--fixed-xml", default="", help="ODI File 2 / current XML")
    p.add_argument("--out", required=True)
    p.add_argument("--existing-compare-out", default="", help="Reuse existing v16.6 compare output directory")
    p.add_argument("--existing-insert-out", default="", help="Reuse existing insert-builder output directory")
    p.add_argument("--profile", default="auto", choices=["auto", "generic", "avy", "taxlot"])
    p.add_argument("--target-table", default="")
    p.add_argument("--target-schema", default="")
    p.add_argument("--primary-source", default="")
    p.add_argument("--mapping-sheet", default="")
    p.add_argument("--target-col", default="")
    p.add_argument("--source-cols", default="")
    p.add_argument("--rule-col", default="")
    p.add_argument("--header-row", default="")
    p.add_argument("--schema-kb", default="")
    p.add_argument("--resolution-profile", default="auto", help="auto uses bundled insert_builder/profiles/lh_ds3_resolution_profile.json if present; empty disables")
    p.add_argument("--insert-xml", default="", help="XML evidence for insert builder; default = --fixed-xml")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--report-mode", default="both", choices=["excel", "api", "both"], help="Report output mode: excel, api, or both")
    args = p.parse_args(argv)

    out = Path(args.out).expanduser().resolve()
    compare_out = out / "odi_compare"
    insert_out = out / "insert_builder"
    final_dir = out / "final_reports"
    final_dir.mkdir(parents=True, exist_ok=True)

    compare_script = here / "compare_engine" / "compare_drd_odi_v16_6_generic_rule_proof.py"
    insert_script = here / "insert_builder" / "universal_insert_builder.py"
    api_first_script = here / "api_first_report_builder.py"

    # STEP 1: Compare
    if args.existing_compare_out:
        copy_dir(Path(args.existing_compare_out).expanduser().resolve(), compare_out)
    else:
        if not args.xlsx or not args.original_xml or not args.fixed_xml:
            raise SystemExit("Missing --xlsx/--original-xml/--fixed-xml for compare run. Or use --existing-compare-out.")
        compare_cmd = [
            sys.executable, "-B", str(compare_script),
            "--xlsx", args.xlsx,
            "--original-xml", args.original_xml,
            "--fixed-xml", args.fixed_xml,
            "--out", str(compare_out),
            "--profile", args.profile,
        ]
        for opt, val in [
            ("--target-table", args.target_table),
            ("--mapping-sheet", args.mapping_sheet),
            ("--target-col", args.target_col),
            ("--source-cols", args.source_cols),
            ("--rule-col", args.rule_col),
            ("--header-row", args.header_row),
        ]:
            if val:
                compare_cmd += [opt, val]
        if args.quiet:
            compare_cmd += ["--quiet"]
        run(compare_cmd, here)

    # STEP 1 REPORT: native API first, optional Excel render.
    step1_cmd = [
        sys.executable, "-B", str(api_first_script),
        "--compare-out", str(compare_out),
        "--final-dir", str(final_dir),
        "--report-mode", args.report_mode,
        "--phase", "step1",
    ]
    run(step1_cmd, here)

    # STEP 2/3: Insert builder
    if args.existing_insert_out:
        copy_dir(Path(args.existing_insert_out).expanduser().resolve(), insert_out)
    else:
        if not args.xlsx:
            raise SystemExit("Missing --xlsx for insert builder run. Or use --existing-insert-out.")
        insert_xml = args.insert_xml or args.fixed_xml
        insert_cmd = [
            sys.executable, "-B", str(insert_script),
            "--xlsx", args.xlsx,
            "--out", str(insert_out),
            "--profile", args.profile,
        ]
        if insert_xml:
            insert_cmd += ["--xml", insert_xml]
        for opt, val in [
            ("--target-schema", args.target_schema),
            ("--target-table", args.target_table),
            ("--primary-source", args.primary_source),
            ("--mapping-sheet", args.mapping_sheet),
            ("--target-col", args.target_col),
            ("--source-cols", args.source_cols),
            ("--rule-col", args.rule_col),
            ("--header-row", args.header_row),
            ("--schema-kb", args.schema_kb),
        ]:
            if val:
                insert_cmd += [opt, val]
        if args.resolution_profile:
            rp = args.resolution_profile
            if rp == "auto":
                bundled = here / "insert_builder" / "profiles" / "lh_ds3_resolution_profile.json"
                rp = str(bundled) if bundled.exists() else ""
            if rp:
                insert_cmd += ["--resolution-profile", rp]
        if args.quiet:
            insert_cmd += ["--quiet"]
        run(insert_cmd, here)

    # STEP 4 REPORT: native API first, optional Excel render.
    step4_cmd = [
        sys.executable, "-B", str(api_first_script),
        "--compare-out", str(compare_out),
        "--insert-out", str(insert_out),
        "--final-dir", str(final_dir),
        "--report-mode", args.report_mode,
        "--phase", "step4",
    ]
    run(step4_cmd, here)

    api_manifest = final_dir / "api" / "manifest.json"
    manifest = read_json_if_exists(api_manifest) or {}
    full_cycle_summary = read_json_if_exists(final_dir / "full_cycle_summary.json") or {}

    summary = {
        "version": "17.6",
        "pipeline_mode": "api_first",
        "process_status": "ARTIFACTS_GENERATED",
        "business_status": manifest.get("business_status"),
        "out": str(out),
        "compare_out": str(compare_out),
        "insert_out": str(insert_out),
        "final_dir": str(final_dir),
        "report_mode": args.report_mode,
        "api_manifest": str(api_manifest) if api_manifest.exists() else "",
        "step1_compare_report_xlsx": str(final_dir / "step1_compare_report.xlsx") if (final_dir / "step1_compare_report.xlsx").exists() else "",
        "step4_full_cycle_report_xlsx": str(final_dir / "step4_full_cycle_report.xlsx") if (final_dir / "step4_full_cycle_report.xlsx").exists() else "",
        "final_full_cycle_report_xlsx": str(final_dir / "final_full_cycle_report.xlsx") if (final_dir / "final_full_cycle_report.xlsx").exists() else "",
        "generated_insert": str(insert_out / "generated_insert_select_candidate.sql"),
        "hardcode_gate_report": str(insert_out / "hardcode_gate_report.json") if (insert_out / "hardcode_gate_report.json").exists() else "",
        "full_cycle_summary": str(final_dir / "full_cycle_summary.json") if (final_dir / "full_cycle_summary.json").exists() else "",
        "full_cycle_summary_payload": full_cycle_summary,
        "used_existing_compare_out": bool(args.existing_compare_out),
        "used_existing_insert_out": bool(args.existing_insert_out),
        "compare_engine": "v16.6.5",
        "insert_builder": "v6.2 config-driven profile",
        "returncode_semantics": "0 means artifacts were generated. Check business_status to know whether review/blockers remain.",
    }
    (out / "full_cycle_run_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
