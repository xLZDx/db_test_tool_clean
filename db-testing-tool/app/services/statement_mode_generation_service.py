"""Statement mode generator.

Generates all SQL shapes needed by the tool:
- source_select: review/debug SQL
- insert_select: preferred DRD generator output
- cte: preferred control-table output
- merge: target load simulation

This service expects DRD rows to be PDM-enriched before generation.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List


def typed_null(dtype: str) -> str:
    dtype = str(dtype or "").upper()
    if dtype.startswith("NUMBER"):
        return "CAST(NULL AS NUMBER)"
    if dtype.startswith("DATE"):
        return "CAST(NULL AS DATE)"
    if dtype.startswith("TIMESTAMP"):
        return "CAST(NULL AS TIMESTAMP)"
    m = re.match(r"VARCHAR2\((\d+)\)", dtype)
    if m:
        return f"CAST(NULL AS VARCHAR2({m.group(1)}))"
    return "NULL"


def parse_join_from_transformation(text: str, primary_alias: str, join_alias: str) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    m = re.search(r"(?i)(?:LOOK\s+UP\s+)?USING\s+([A-Z0-9_$.]+)\s*=\s*([A-Z0-9_$.]+)", flat)
    if m:
        return f"{m.group(1)} = {m.group(2)}"
    m = re.search(r"(?i)\bON\b\s+(.+)", flat)
    if m:
        return m.group(1).strip().rstrip(";")
    m = re.search(r"(?i)\b(?:BY|USING)\s+([A-Z][A-Z0-9_#$]*)\b", flat)
    if m:
        col = m.group(1).upper()
        return f"{primary_alias}.{col} = {join_alias}.{col}"
    return ""


class StatementModeGenerationService:
    def __init__(self, config: Dict[str, Any] | None = None):
        self.config = config or {}
        self.table = self.config.get("table", {}).get("name", "TARGET_TABLE")
        self.business_keys = [str(x).upper() for x in self.config.get("sql_generation", {}).get("business_keys", ["TXN_ID"])]

    def build_plan(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        pairs = [(r.get("source_schema", ""), r.get("source_table", "")) for r in rows if r.get("source_schema") and r.get("source_table")]
        primary_pair = Counter(pairs).most_common(1)[0][0] if pairs else ("", "")
        alias_by_pair = {}
        for idx, (pair, _) in enumerate(Counter(pairs).most_common(), start=1):
            alias_by_pair[pair] = "B" if pair == primary_pair else f"J{idx}"

        expr_by_col: Dict[str, str] = {}
        origin_by_col: Dict[str, str] = {}
        joins: List[Dict[str, Any]] = []
        unresolved: List[Dict[str, Any]] = []
        seen = set()

        for r in rows:
            col = r.get("column", "")
            pair = (r.get("source_schema", ""), r.get("source_table", ""))
            alias = alias_by_pair.get(pair, "B")
            attr = r.get("source_attribute", "")
            trans = r.get("transformation", "")
            if attr and re.match(r"^[A-Z][A-Z0-9_#$]*$", attr, re.I):
                expr_by_col[col] = f"{alias}.{attr}"
                origin_by_col[col] = "step1_base" if pair == primary_pair else "step2_lookup"
                if pair != primary_pair:
                    cond = parse_join_from_transformation(trans, "B", alias)
                    if cond:
                        key = (pair[0], pair[1], cond)
                        if key not in seen:
                            seen.add(key)
                            joins.append({"join_type": "LEFT JOIN", "schema": pair[0], "table": pair[1], "alias": alias, "condition": cond, "target_column": col})
                    else:
                        unresolved.append({"column": col, "reason": "Non-primary source has no join condition", "source_schema": pair[0], "source_table": pair[1], "source_attribute": attr})
            else:
                expr_by_col[col] = typed_null(r.get("dtype", ""))
                origin_by_col[col] = "unresolved"
                unresolved.append({"column": col, "reason": "No source attribute after PDM resolution", "source_schema": pair[0], "source_table": pair[1], "source_attribute": attr})
        return {"rows": rows, "columns": [r["column"] for r in rows], "primary_pair": primary_pair, "alias_by_pair": alias_by_pair, "expr_by_col": expr_by_col, "origin_by_col": origin_by_col, "joins": joins, "unresolved": unresolved}

    def source_select(self, plan: Dict[str, Any]) -> str:
        ps_schema, ps_table = plan["primary_pair"]
        lines = ["SELECT"]
        lines.append("    " + ",\n    ".join([f"{plan['expr_by_col'][c]} AS {c}" for c in plan["columns"]]))
        lines.append(f"FROM {ps_schema}.{ps_table} B" if ps_schema and ps_table else "FROM DUAL")
        for j in plan["joins"]:
            lines.append(f"{j['join_type']} {j['schema']}.{j['table']} {j['alias']}")
            lines.append(f"    ON {j['condition']}")
        return "\n".join(lines)

    def insert_select(self, plan: Dict[str, Any]) -> str:
        return f"INSERT INTO {self.table} (\n    " + ",\n    ".join(plan["columns"]) + "\n)\n" + self.source_select(plan) + ";\n"

    def cte(self, plan: Dict[str, Any]) -> str:
        base = self.source_select(plan)
        return "WITH DRD_STEP1_TO_STEP5_FINAL AS (\n" + "\n".join("    " + line for line in base.splitlines()) + "\n)\nSELECT * FROM DRD_STEP1_TO_STEP5_FINAL;\n"

    def merge(self, plan: Dict[str, Any]) -> str:
        cte_sql = self.cte(plan).rstrip().rstrip(";")
        cols = plan["columns"]
        keys = [k for k in self.business_keys if k in cols]
        on_clause = " AND ".join([f"T.{k} = S.{k}" for k in keys]) if keys else "1 = 0 /* REVIEW: configure business key */"
        nonkeys = [c for c in cols if c not in set(keys)]
        return cte_sql + f"\nMERGE INTO {self.table} T\nUSING (SELECT * FROM DRD_STEP1_TO_STEP5_FINAL) S\nON ({on_clause})\nWHEN MATCHED THEN UPDATE SET\n    " + ",\n    ".join([f"T.{c} = S.{c}" for c in nonkeys]) + "\nWHEN NOT MATCHED THEN INSERT (\n    " + ",\n    ".join([f"T.{c}" for c in cols]) + "\n) VALUES (\n    " + ",\n    ".join([f"S.{c}" for c in cols]) + "\n);\n"

    def generate_all(self, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        plan = self.build_plan(rows)
        return {
            "plan": plan,
            "source_select": self.source_select(plan),
            "insert_select": self.insert_select(plan),
            "cte": self.cte(plan),
            "merge": self.merge(plan),
            "unresolved": plan["unresolved"]
        }
