#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def should_skip(path: Path, allowed_parts: set[str]) -> bool:
    parts = set(path.parts)
    return bool(parts & allowed_parts)


def scan(root: Path, config: dict):
    tokens = config.get("forbidden_tokens", [])
    exts = set(config.get("scan_extensions", [".py"]))
    allowed_parts = set(config.get("allowed_path_parts", []))
    findings = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix not in exts:
            continue
        rel = p.relative_to(root)
        if should_skip(rel, allowed_parts):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for token in tokens:
            if token and token in text:
                for i, line in enumerate(text.splitlines(), start=1):
                    if token in line:
                        findings.append({
                            "file": str(rel),
                            "line": i,
                            "token": token,
                            "excerpt": line.strip()[:240],
                        })
    return findings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Fail if executable Python/templates contain profile-specific business tokens.")
    parser.add_argument("--root", default=".", help="Package/source root to scan")
    parser.add_argument("--config", default="profiles/no_hardcoded_business_tokens.json")
    parser.add_argument("--out", default="")
    args = parser.parse_args(argv)

    root = Path(args.root).expanduser().resolve()
    cfg_path = (root / args.config).resolve() if not Path(args.config).is_absolute() else Path(args.config).resolve()
    cfg = load_config(cfg_path)
    findings = scan(root, cfg)
    report = {
        "root": str(root),
        "config": str(cfg_path),
        "finding_count": len(findings),
        "status": "PASS" if not findings else "FAIL",
        "findings": findings,
    }
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
