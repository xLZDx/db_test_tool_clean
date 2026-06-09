#!/usr/bin/env python3
"""
Config-driven public entrypoint for universal_insert_builder v6.2.

This wrapper:
1. Loads profile config (default profiles/lh_ds3_resolution_profile.json).
2. Materializes the runtime engine from templates + profile token_map.
3. Runs the generated engine.
4. Runs hardcode gate against the checked-in package sources and writes a gate report.

The checked-in executable Python source must not contain profile-specific business column/table tokens.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from profile_engine_renderer import materialize_engine


def _find_arg(args: list[str], name: str, default: str = "") -> str:
    for i, arg in enumerate(args):
        if arg == name and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
    return default


def _has_arg(args: list[str], name: str) -> bool:
    return any(a == name or a.startswith(name + "=") for a in args)


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    here = Path(__file__).resolve().parent

    if "-h" in args or "--help" in args:
        print("""universal_insert_builder v6.2 config-driven wrapper

Usage:
  python universal_insert_builder.py --xlsx DRD.xlsx --xml helper.xml --out out_dir [options]

Additional behavior:
  - Uses profiles/lh_ds3_resolution_profile.json by default when --resolution-profile is not provided.
  - Materializes runtime engine under <out>/.generated_profile_engine.
  - Writes <out>/hardcode_gate_report.json.
""")
        return 0

    out_dir = Path(_find_arg(args, "--out", "insert_builder_output")).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_profile_path = _find_arg(args, "--resolution-profile", "")
    if raw_profile_path:
        profile_path = Path(raw_profile_path).expanduser()
    else:
        profile_path = here / "profiles" / "lh_ds3_resolution_profile.json"
        args.extend(["--resolution-profile", str(profile_path)])

    generated_dir = materialize_engine(here, out_dir, profile_path)
    engine = generated_dir / "universal_insert_builder.py"

    cmd = [sys.executable, "-B", str(engine)] + args
    rc = subprocess.call(cmd, cwd=str(generated_dir))
    if rc != 0:
        return rc

    gate_report = out_dir / "hardcode_gate_report.json"
    gate_cmd = [sys.executable, "-B", str(here / "hardcode_gate.py"), "--root", str(here), "--out", str(gate_report)]
    gate_rc = subprocess.call(gate_cmd, cwd=str(here))
    return gate_rc


if __name__ == "__main__":
    raise SystemExit(main())
