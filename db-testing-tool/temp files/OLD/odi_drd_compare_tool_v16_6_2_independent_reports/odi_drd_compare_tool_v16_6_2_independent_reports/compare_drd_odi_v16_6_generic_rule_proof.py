#!/usr/bin/env python3
"""
Canonical v16.6.2 entrypoint.

Runs:
1. compare_drd_odi_v16_6_fast_generic_rule_proof.py
2. generate_independent_reports.py

So every successful compare now always produces:
- independent_reports/01_DRD_vs_ODI_File_1.csv
- independent_reports/02_DRD_vs_ODI_File_2.csv
- independent_reports/03_ODI1_vs_ODI2_Columns_with_DRD_logic.csv
- independent_reports/04_ODI1_vs_ODI2_SQL_Blocks_with_DRD_logic.csv
- independent_reports/independent_reports.xlsx
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
