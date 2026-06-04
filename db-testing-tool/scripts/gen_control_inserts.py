"""Generic control-table INSERT generator + ODI grader.

Applies the SAME pipeline to ANY (DRD, ODI-scenario, PDM-target) triple:
  1. analyze_control_table(DRD + PDM target)  -> generated control INSERT
  2. emit_insert(ODI scenario)                -> faithful ODI INSERT (the oracle)
  3. per-column compare, alias-drift-tolerant -> match / real-diff report
  4. write the generated INSERT to data/<tag>_CONTROL_INSERT.sql

Close taxlot is just one example; the operator's point is the approach is
file-agnostic.  Run:  python scripts/gen_control_inserts.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.sql_model.odi_parser import OdiXmlParser
from app.sql_model.sql_emitter import emit_insert
from app.services.control_table_service import analyze_control_table

ROOT = Path(__file__).resolve().parents[1]

# (tag, DRD path, ODI xml path, target schema, target table)
FIXTURES = [
    ("CLOSE", "data/taxlot/DRD_Closed_Tax_Lots_non_bkr_Fact (3).xlsx",
     "data/taxlot/SCEN_SSDS_CLOSED_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
     "TAXLOT_OWNER", "CLS_TAX_LOTS_NON_BKR_FACT"),
    ("OPEN", "data/taxlot/DRD_Open_Tax_Lots_non_bkr_Fact (2).xlsx",
     "data/taxlot/SCEN_SSDS_OPEN_TAXLOT_NONBKR_RJTRUST_FACT_Version_001.xml",
     "TAXLOT_OWNER", "OPN_TAX_LOTS_NON_BKR_FACT"),
    ("AVY", "DRD_Activity_Fact.xlsx",
     "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml",
     "TRANSACTIONS_OWNER", "AVY_FACT_SIDE"),
]
DS = 2
AUDIT = {"SESN_NUM", "CRT_DTM", "CRT_USR_NM", "LAST_UDT_DTM", "LAST_UDT_USR_NM"}


def _read_text(p: Path) -> str:
    for enc in ("ISO-8859-1", "utf-8"):
        try:
            return p.read_text(encoding=enc)
        except Exception:
            continue
    return p.read_bytes().decode("latin-1", "replace")


def parse_insert(sql: str) -> dict:
    ins = sql.upper().find("INSERT INTO")
    op = sql.find("(", ins)
    d = 0
    end = op
    for i in range(op, len(sql)):
        if sql[i] == "(":
            d += 1
        elif sql[i] == ")":
            d -= 1
            if d == 0:
                end = i
                break
    cols = [c.strip().upper() for c in sql[op + 1:end].split(",") if c.strip()]
    ss = sql.upper().find("SELECT", end)
    fi = -1
    d = 0
    for mm in re.finditer(r"\(|\)|\bFROM\b", sql[ss:], re.I):
        t = mm.group(0)
        if t == "(":
            d += 1
        elif t == ")":
            d -= 1
        elif d == 0:
            fi = ss + mm.start()
            break
    body = sql[ss + 6:fi]
    exprs = []
    buf = []
    d = 0
    for ch in body:
        if ch == "(":
            d += 1
            buf.append(ch)
        elif ch == ")":
            d -= 1
            buf.append(ch)
        elif ch == "," and d == 0:
            exprs.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        exprs.append("".join(buf).strip())
    out = {}
    for e in exprs:
        m = re.match(r"^(.*)\bAS\b\s+([A-Z0-9_$#]+)\s*$", e, re.I | re.S)
        if m:
            out[m.group(2).upper()] = re.sub(r"\s+", " ", m.group(1).strip())
    if not out:
        for c, e in zip(cols, exprs):
            out[c] = re.sub(r"\s+AS\s+[A-Z0-9_$#]+\s*$", "", e, flags=re.I).strip()
    return out


def _strip_comments(s: str) -> str:
    return re.sub(r"/\*.*?\*/", "", s or "", flags=re.S)


def _norm(expr: str) -> str:
    """Whitespace-stripped, comment-stripped + alias-drift-tolerant.

    Collapses lookup-alias drift so ODI's ``CL_VAL3_1`` and GEN's ``CL_VAL_2``
    (same dim, different alias number) compare equal -- strip any trailing
    digits/underscores from the alias prefix (CL_VAL3_1 -> CL_VAL, ACG_TP_DIM_1
    -> ACG_TP_DIM)."""
    e = re.sub(r"\s+", "", _strip_comments(expr)).upper()

    def repl(m):
        al = re.sub(r"[_0-9]+$", "", m.group(1))  # CL_VAL3_1 / CL_VAL_2 / ACG_TP_DIM_1 -> base
        return f"{al}.{m.group(2)}"
    return re.sub(r"\b([A-Z0-9_]+)\.([A-Z0-9_#$]+)", repl, e)


def _write_compare(tag: str, ttbl: str, cmp_rows: list) -> None:
    """Write a per-column ODI-vs-control-INSERT comparison as .md + .csv twin."""
    import csv as _csv
    md = [
        f"# {tag} -- control-table INSERT vs ODI ({ttbl})",
        "",
        f"Columns: {len(cmp_rows)} | "
        f"MATCH {sum(1 for r in cmp_rows if r[3]=='MATCH')} / "
        f"ALIAS_DRIFT {sum(1 for r in cmp_rows if r[3]=='ALIAS_DRIFT')} / "
        f"REAL_DIFF {sum(1 for r in cmp_rows if r[3]=='REAL_DIFF')} / "
        f"AUDIT {sum(1 for r in cmp_rows if r[3]=='AUDIT')}",
        "",
        "| Column | Verdict | ODI expression | GEN (control-table) expression |",
        "|---|---|---|---|",
    ]
    for c, o, g, v in cmp_rows:
        mo = (o or "").replace("|", "\\|").replace("\n", " ")
        mg = (g or "").replace("|", "\\|").replace("\n", " ")
        md.append(f"| {c} | {v} | {mo} | {mg} |")
    (ROOT / "data" / f"{tag}_COMPARE.md").write_text("\n".join(md), encoding="utf-8")
    with open(ROOT / "data" / f"{tag}_COMPARE.csv", "w", newline="", encoding="utf-8") as fh:
        w = _csv.writer(fh)
        w.writerow(["column", "verdict", "odi_expression", "gen_expression"])
        for c, o, g, v in cmp_rows:
            w.writerow([c, v, (o or "").replace("\n", " "), (g or "").replace("\n", " ")])


def grade(tag, drd, odi_xml, tsch, ttbl):
    print(f"\n{'='*70}\n{tag}: {ttbl}\n{'='*70}")
    drd_p, odi_p = ROOT / drd, ROOT / odi_xml
    if not drd_p.exists():
        print(f"  DRD MISSING: {drd}")
        return
    res = analyze_control_table(
        file_bytes=drd_p.read_bytes(), filename=drd_p.name,
        target_schema=tsch, target_table=ttbl,
        source_datasource_id=DS, target_datasource_id=DS, control_schema="ikorostelev",
    )
    gen_sql = res.get("generated_insert_sql", "") or ""
    out_path = ROOT / "data" / f"{tag}_CONTROL_INSERT.sql"
    out_path.write_text(gen_sql, encoding="utf-8")
    gen = parse_insert(gen_sql)
    print(f"  generated INSERT: {len(gen)} cols -> {out_path.relative_to(ROOT)}")

    if not odi_p.exists():
        print(f"  ODI XML MISSING ({odi_xml}) -- generated only, no grade")
        return
    try:
        odi_full = emit_insert(OdiXmlParser(target_schema="", target_table="").parse_text(_read_text(odi_p)), strict=False).sql
        odi = parse_insert(odi_full)
    except Exception as e:
        print(f"  ODI parse error: {type(e).__name__}: {str(e)[:60]}")
        return
    odi_out = ROOT / "data" / f"{tag}_ODI_INSERT.sql"
    odi_out.write_text(odi_full, encoding="utf-8")
    print(f"  ODI INSERT:       {len(odi)} cols -> {odi_out.relative_to(ROOT)}")
    if len(odi) <= 1:
        print(f"  ODI parsed degenerately ({len(odi)} col) -- saved but cannot grade (parser gap for this IKM style)")
        return
    exact = aliasdrift = real = 0
    cmp_rows = []
    for c, o in odi.items():
        g = gen.get(c, "<MISSING>")
        if c in AUDIT:
            verdict = "AUDIT"
        elif re.sub(r"\s+", "", _strip_comments(o)).upper() == re.sub(r"\s+", "", _strip_comments(g)).upper():
            verdict = "MATCH"; exact += 1
        elif _norm(o) == _norm(g):
            verdict = "ALIAS_DRIFT"; aliasdrift += 1
        else:
            verdict = "REAL_DIFF"; real += 1
        cmp_rows.append((c, o, g, verdict))
    _write_compare(tag, ttbl, cmp_rows)
    print(f"  vs ODI: exact={exact} alias-drift={aliasdrift} REAL={real} (audit excluded)"
          f" -> data/{tag}_COMPARE.md + .csv")
    for c, o, g, v in cmp_rows:
        if v == "REAL_DIFF":
            print(f"    [{c}] ODI={o[:48]!r} GEN={g[:48]!r}")


if __name__ == "__main__":
    for fx in FIXTURES:
        grade(*fx)
