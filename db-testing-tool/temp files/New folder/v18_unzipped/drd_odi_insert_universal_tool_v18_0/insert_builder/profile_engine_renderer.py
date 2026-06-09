#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Dict


def load_profile(path: str | Path) -> dict:
    p = Path(path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(p)
    return json.loads(p.read_text(encoding="utf-8"))


def render_template_text(template_text: str, token_map: Dict[str, str]) -> str:
    out = template_text
    # Longest placeholders first is defensive; placeholder lengths are equal now.
    for placeholder in sorted(token_map, key=len, reverse=True):
        out = out.replace(placeholder, token_map[placeholder])
    return out


def materialize_engine(package_dir: str | Path, out_dir: str | Path, profile_path: str | Path) -> Path:
    package_dir = Path(package_dir).resolve()
    out_dir = Path(out_dir).expanduser().resolve()
    profile = load_profile(profile_path)
    token_map = profile.get("token_map", {})
    if not isinstance(token_map, dict) or not token_map:
        raise RuntimeError("Profile missing token_map; cannot materialize config-driven engine.")

    generated_dir = out_dir / profile.get("generated_engine_policy", {}).get("generated_dir_name", ".generated_profile_engine")
    generated_dir.mkdir(parents=True, exist_ok=True)

    template_dir = package_dir / profile.get("generated_engine_policy", {}).get("template_dir", "engine_templates")
    if not template_dir.exists():
        raise FileNotFoundError(template_dir)

    for template in template_dir.glob("*.py.tpl"):
        rendered = render_template_text(template.read_text(encoding="utf-8"), token_map)
        target = generated_dir / template.name.replace(".py.tpl", ".py")
        target.write_text(rendered, encoding="utf-8")

    # Copy generic support modules into generated runtime dir so imports are isolated and deterministic.
    for fname in ["schema_kb_sql_gate.py", "field_compare_sql_generator.py"]:
        src = package_dir / fname
        if src.exists():
            shutil.copy2(src, generated_dir / fname)

    # Keep a copy of the profile used for forensic reproducibility.
    shutil.copy2(Path(profile_path).expanduser().resolve(), generated_dir / "active_resolution_profile.json")
    return generated_dir
