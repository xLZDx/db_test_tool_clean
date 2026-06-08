#!/usr/bin/env python3
"""
Canonical v16.6.5 entrypoint.

Runs:
1. compare_drd_odi_v16_6_fast_generic_rule_proof.py
2. generate_independent_reports.py

Every successful run creates:
- independent_reports/independent_reports.xlsx
- independent_reports/summary.json
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def find_out_arg(argv):
    for i, arg in enumerate(argv):
        if arg == "--out" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--out="):
            return arg.split("=", 1)[1]
    return "v16_6_delta_output"


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "-h" in args or "--help" in args:
        print("""v16.6.5 Excel workbook reports

Usage:
  python compare_drd_odi_v16_6_generic_rule_proof.py --xlsx DRD.xlsx --original-xml odi1.xml --fixed-xml odi2.xml --out out_dir [options]

Always generates:
  out_dir/independent_reports/independent_reports.xlsx
  out_dir/independent_reports/summary.json

Workbook tabs:
  Summary
  DRD vs ODI File 1
  DRD vs ODI File 2
  DRD vs ODI1 vs ODI2
  ODI1 vs ODI2 Columns
  ODI1 vs ODI2 SQL Blocks
""")
        return 0

    here = Path(__file__).resolve().parent
    canonical = here / "compare_drd_odi_v16_6_fast_generic_rule_proof.py"
    reports = here / "generate_independent_reports.py"

    if not canonical.exists():
        print(f"ERROR: canonical engine not found: {canonical}", file=sys.stderr)
        return 2
    if not reports.exists():
        print(f"ERROR: report generator not found: {reports}", file=sys.stderr)
        return 2

    rc = subprocess.call([sys.executable, "-B", str(canonical)] + args, cwd=str(here))
    if rc != 0:
        return rc

    out_dir = Path(find_out_arg(args)).expanduser().resolve()
    return subprocess.call([sys.executable, "-B", str(reports), "--out-dir", str(out_dir)], cwd=str(here))


if __name__ == "__main__":
    raise SystemExit(main())

