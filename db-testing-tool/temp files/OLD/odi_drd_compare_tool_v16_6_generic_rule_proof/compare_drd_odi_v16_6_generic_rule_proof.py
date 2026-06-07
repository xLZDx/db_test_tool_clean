#!/usr/bin/env python3
"""One-command v16.6 generic rule-proof comparator."""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="v16.6 generic DRD/ODI delta comparator with generic rule-proof overlay")
    p.add_argument("--xlsx", required=True)
    p.add_argument("--original-xml", required=True)
    p.add_argument("--fixed-xml", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--profile", default="auto", choices=["auto", "generic", "avy", "taxlot"])
    p.add_argument("--target-table", default="")
    p.add_argument("--mapping-sheet", default="")
    p.add_argument("--target-col", default="")
    p.add_argument("--source-cols", default="")
    p.add_argument("--rule-col", default="")
    p.add_argument("--header-row", type=int, default=None)
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    here = Path(__file__).resolve().parent
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="v16_6_base_") as td:
        base_out = Path(td) / "base_v16"
        base_cmd = [
            sys.executable, "-B", str(here / "compare_drd_odi_v16_2_delta_safe.py"),
            "--xlsx", args.xlsx,
            "--original-xml", args.original_xml,
            "--fixed-xml", args.fixed_xml,
            "--out", str(base_out),
            "--profile", args.profile,
            "--quiet",
        ]
        if args.target_table:
            base_cmd += ["--target-table", args.target_table]
        if args.mapping_sheet:
            base_cmd += ["--mapping-sheet", args.mapping_sheet]
        if args.target_col:
            base_cmd += ["--target-col", args.target_col]
        if args.source_cols:
            base_cmd += ["--source-cols", args.source_cols]
        if args.rule_col:
            base_cmd += ["--rule-col", args.rule_col]
        if args.header_row is not None:
            base_cmd += ["--header-row", str(args.header_row)]
        subprocess.run(base_cmd, check=True)

        proof_cmd = [
            sys.executable, "-B", str(here / "reclassify_v16_6_generic_rule_proof.py"),
            "--v16-output", str(base_out),
            "--xlsx", args.xlsx,
            "--original-xml", args.original_xml,
            "--fixed-xml", args.fixed_xml,
            "--out", str(out),
        ]
        if args.target_table:
            proof_cmd += ["--target-table", args.target_table]
        if args.mapping_sheet:
            proof_cmd += ["--mapping-sheet", args.mapping_sheet]
        if args.target_col:
            proof_cmd += ["--target-col", args.target_col]
        if args.source_cols:
            proof_cmd += ["--source-cols", args.source_cols]
        if args.rule_col:
            proof_cmd += ["--rule-col", args.rule_col]
        if args.header_row is not None:
            proof_cmd += ["--header-row", str(args.header_row)]
        subprocess.run(proof_cmd, check=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
