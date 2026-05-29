"""AI service – uses OpenAI GPT to assist with rule extraction, test generation, and triage."""
from typing import Any, Dict, List, Optional, Tuple
from app.config import settings
from app.services.copilot_auth_service import get_runtime_copilot_token
from app.services.schema_kb_service import load_schema_kb_payload
from app.models.agent_contracts import TfsContext, EtlMappingSpec, TestCaseDesign, AgentPhaseReport
from app.models.db_dialects import get_dialect_prompt
from app.services.sql_pattern_validation import validate_test_definition_sql
from app.services.tfs_service import fetch_work_item_full_context
from app.database import async_session
from app.models.datasource import DataSource
from app.connectors.factory import get_connector_from_model
from sqlalchemy import select, func
import asyncio
import json, logging, re
import httpx

logger = logging.getLogger(__name__)


def _strip_code_fences(text: str) -> str:
    cleaned = (text or "").strip()
    if not cleaned.startswith("```"):
        return cleaned

    lines = cleaned.splitlines()
    if not lines:
        return cleaned
    if lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _extract_balanced_json_fragment(text: str) -> Optional[str]:
    source = text or ""
    opener_to_closer = {"{": "}", "[": "]"}

    for start_idx, ch in enumerate(source):
        if ch not in opener_to_closer:
            continue

        stack = [opener_to_closer[ch]]
        in_string = False
        escaped = False

        for idx in range(start_idx + 1, len(source)):
            cur = source[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif cur == "\\":
                    escaped = True
                elif cur == '"':
                    in_string = False
                continue

            if cur == '"':
                in_string = True
                continue
            if cur in opener_to_closer:
                stack.append(opener_to_closer[cur])
                continue
            if stack and cur == stack[-1]:
                stack.pop()
                if not stack:
                    return source[start_idx:idx + 1]

    return None


def _parse_json_response_text(text: str, expected: Optional[str] = None) -> Any:
    cleaned = _strip_code_fences((text or "").strip())
    candidates: List[str] = []
    if cleaned:
        candidates.append(cleaned)
    fragment = _extract_balanced_json_fragment(cleaned)
    if fragment and fragment not in candidates:
        candidates.append(fragment)

    last_error: Optional[Exception] = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if expected == "object" and not isinstance(parsed, dict):
                raise ValueError("Expected JSON object")
            if expected == "array" and not isinstance(parsed, list):
                raise ValueError("Expected JSON array")
            return parsed
        except Exception as exc:
            last_error = exc

    raise ValueError(f"Unable to parse AI response as JSON: {last_error or 'unknown error'}")


def _repair_json_via_ai(client, provider: str, model: Optional[str], raw_text: str, schema_text: str, expected: str) -> Any:
    target_desc = "a JSON object" if expected == "object" else "a JSON array"
    prompt = f"""You are a JSON repair assistant.
Convert the following AI response into STRICT JSON only.
Return ONLY {target_desc} and no markdown, no explanation, no code fences.

Target schema:
{schema_text}

Original AI response:
{raw_text}
"""
    call_args = _build_chat_call_args([{"role": "user", "content": prompt}], 0.0, 4000, provider, model)
    resp = _chat_completion_with_fallback(client, call_args, provider)
    repaired_text = (resp.choices[0].message.content or "").strip()
    return _parse_json_response_text(repaired_text, expected=expected)


def _parse_or_repair_json_response(client, provider: str, model: Optional[str], raw_text: str, schema_text: str, expected: str) -> Any:
    try:
        return _parse_json_response_text(raw_text, expected=expected)
    except Exception as first_exc:
        logger.warning("AI returned non-JSON payload; attempting repair. Error=%s", first_exc)
        return _repair_json_via_ai(client, provider, model, raw_text, schema_text, expected)


def _canonicalize_key(key: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(key or "").strip().lower())


def _as_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    parts = re.split(r"\s*,\s*|\s*;\s*|\n+", text)
    return [part.strip() for part in parts if part.strip()]


def _normalize_mapping_rule_payload(item: Any) -> dict:
    payload = item if isinstance(item, dict) else {}
    by_key = {_canonicalize_key(k): v for k, v in payload.items()}
    return {
        "source_column": by_key.get("sourcecolumn") or by_key.get("sourcefield") or "",
        "target_column": by_key.get("targetcolumn") or by_key.get("targetfield") or "",
        "transformation": by_key.get("transformation") or by_key.get("logic") or "Direct",
        "rule_type": by_key.get("ruletype") or by_key.get("mappingtype") or "direct",
    }


def _normalize_etl_mapping_spec_payload(payload: Any) -> dict:
    current = payload
    if isinstance(current, dict) and len(current) == 1:
        only_key = next(iter(current.keys()))
        if _canonicalize_key(only_key) in {"etlmappingspec", "mappingspec", "spec"}:
            current = current[only_key]

    by_key = {_canonicalize_key(k): v for k, v in (current.items() if isinstance(current, dict) else [])}
    mappings = by_key.get("mappings") or by_key.get("mappingrules") or []
    if isinstance(mappings, dict):
        mappings = [mappings]

    return {
        "source_tables": _as_string_list(by_key.get("sourcetables") or by_key.get("sources")),
        "target_tables": _as_string_list(by_key.get("targettables") or by_key.get("targets")),
        "business_keys": _as_string_list(by_key.get("businesskeys") or by_key.get("keys")),
        "join_conditions": str(by_key.get("joinconditions") or by_key.get("joincondition") or "").strip(),
        "mappings": [_normalize_mapping_rule_payload(item) for item in mappings if item],
        "filters": str(by_key.get("filters") or by_key.get("filter") or by_key.get("whereclause") or "").strip(),
    }


def _normalize_test_case_design_payload(item: Any) -> dict:
    payload = item if isinstance(item, dict) else {}
    by_key = {_canonicalize_key(k): v for k, v in payload.items()}
    return {
        "name": str(by_key.get("name") or by_key.get("testname") or "Generated Test").strip(),
        "test_type": str(by_key.get("testtype") or by_key.get("type") or "value_match").strip(),
        "severity": str(by_key.get("severity") or "medium").strip(),
        "description": str(by_key.get("description") or by_key.get("purpose") or "").strip(),
        "db_dialect": str(by_key.get("dbdialect") or by_key.get("dialect") or "oracle").strip(),
        "source_query": str(by_key.get("sourcequery") or by_key.get("sourcesql") or "").strip(),
        "target_query": str(by_key.get("targetquery") or by_key.get("targetsql") or "").strip(),
        "expected_result": str(by_key.get("expectedresult") or by_key.get("expected") or "0").strip(),
    }


def _normalize_test_case_design_array_payload(payload: Any) -> List[dict]:
    current = payload
    if isinstance(current, dict):
        by_key = {_canonicalize_key(k): v for k, v in current.items()}
        current = (
            by_key.get("tests")
            or by_key.get("testcases")
            or by_key.get("testcasedesign")
            or by_key.get("items")
            or current
        )
    if isinstance(current, dict):
        current = [current]
    if not isinstance(current, list):
        return []
    return [_normalize_test_case_design_payload(item) for item in current if item]


def _is_trailer_population_request(user_prompt: str) -> bool:
    text = (user_prompt or "").lower()
    # Deterministic trailer SQL is now opt-in only.
    # Default behavior should run the full 4-phase agentic pipeline.
    return (
        "force_deterministic_trailer=true" in text
        or "[force_deterministic_trailer]" in text
    )


def _extract_since_date_literal(user_prompt: str) -> str:
    text = (user_prompt or "")

    iso = re.search(r"(20\d{2})[-/](\d{1,2})[-/](\d{1,2})", text)
    if iso:
        year = int(iso.group(1))
        month = int(iso.group(2))
        day = int(iso.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}"

    mdy = re.search(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", text)
    if mdy:
        month = int(mdy.group(1))
        day = int(mdy.group(2))
        year = int(mdy.group(3))
        if year < 100:
            year += 2000
        return f"{year:04d}-{month:02d}-{day:02d}"

    return "2026-03-30"


def _trailer_expected_cte(date_literal: str) -> str:
    return f"""WITH src AS (
  SELECT
     src.TXN_SRC_KEY,
         src.EFFECTIVE_DATE AS TD,
     src.FROM_LOCATION,
     src.TO_LOCATION,
         NVL(src.SECURITY_TYPE_X, TO_CHAR(src.SECURITY_TYPE_N)) AS SECURITY_TYPE,
         src.WS_PRODUCT_CATEGORY AS PRODUCT_CATEGORY,
     src.SHARES,
     src.OPTION_CLS,
     src.TERM_NAME,
         src.WS_MARKET_MAKER_5 AS WS_MKT_MKR_5,
         src.TRAILER AS TRLR
  FROM CDS_STG_OWNER.STCCCALQ_GG_VW src
    WHERE TRUNC(NVL(src.EFFECTIVE_DATE, SYSDATE)) >= DATE '{date_literal}'
),
parsed AS (
  SELECT
     src.TXN_SRC_KEY,
     map.TXN_SBTP_CD,
     map.EXEC_SBTP_CD,
     map.CNX_IND AS SRC_PCS_TP_CD,
     map.CDSM_RULE_MAP_ID,
     map.TRLR_RULE_TXT,
     map.REC_TP_SEQ,
     CASE
       WHEN map.HAS_TOKENS = 0 THEN 1
       WHEN map.HAS_VARPLUS = 1 THEN 3
       ELSE 2
     END AS PRTY
  FROM src
  JOIN CCAL_OWNER.CDSM_RULE_MAP map
    ON TRUNC(NVL(map.EFF_DT, src.TD)) <= TRUNC(src.TD)
   AND TRUNC(NVL(map.END_DT, SYSDATE)) > TRUNC(src.TD)
   AND map.CALLING_PRGM = 'STRCCDSB'
   AND map.REC_TP = 7
   AND (map.FM_LOC IS NULL OR map.FM_LOC = src.FROM_LOCATION)
   AND (map.TO_LOC IS NULL OR map.TO_LOC = src.TO_LOCATION)
   AND (map.SEC_TP IS NULL OR map.SEC_TP = src.SECURITY_TYPE)
   AND (map.PD_CGY IS NULL OR map.PD_CGY = src.PRODUCT_CATEGORY)
   AND (
        map.TRLR_SHS_TP_CD IS NULL
        OR (map.TRLR_SHS_TP_CD = 'Y' AND src.SHARES <> 0)
        OR (map.TRLR_SHS_TP_CD = 'P' AND src.SHARES > 0)
        OR (map.TRLR_SHS_TP_CD = 'N' AND src.SHARES < 0)
   )
   AND (map.OPT_CLS IS NULL OR map.OPT_CLS = src.OPTION_CLS)
   AND (map.SRC_PRGM IS NULL OR map.SRC_PRGM = src.TERM_NAME)
   AND (map.MKT_MKR_5 IS NULL OR map.MKT_MKR_5 = src.WS_MKT_MKR_5)
  WHERE REGEXP_LIKE(src.TRLR, map.REGEX_PATT, 'c')
),
ranked AS (
  SELECT
     parsed.TXN_SRC_KEY,
     parsed.TXN_SBTP_CD,
     parsed.EXEC_SBTP_CD,
     parsed.SRC_PCS_TP_CD,
     parsed.CDSM_RULE_MAP_ID,
     ROW_NUMBER() OVER (
       PARTITION BY parsed.TXN_SRC_KEY
       ORDER BY parsed.PRTY, parsed.TRLR_RULE_TXT, parsed.REC_TP_SEQ
     ) AS RN
  FROM parsed
),
expected AS (
  SELECT
     txn_src_key,
     txn_sbtp_cd,
     exec_sbtp_cd,
     src_pcs_tp_cd,
     (SELECT CL_VAL_ID FROM CCAL_OWNER.CL_VAL_VW WHERE CL_VAL_CODE = TRIM(txn_sbtp_cd) AND CL_SCM_CODE = 'TXNSBTP' AND ROWNUM = 1) AS txn_sbtp_id,
     (SELECT CL_VAL_ID FROM CCAL_OWNER.CL_VAL_VW WHERE CL_VAL_CODE = TRIM(exec_sbtp_cd) AND CL_SCM_CODE = 'EXECSBTP' AND ROWNUM = 1) AS exec_sbtp_id,
     (SELECT CL_VAL_ID FROM CCAL_OWNER.CL_VAL_VW WHERE CL_VAL_CODE = TRIM(src_pcs_tp_cd) AND CL_SCM_CODE = 'SRCPCSTP' AND ROWNUM = 1) AS src_pcs_tp_id,
     CDSM_RULE_MAP_ID
  FROM ranked
  WHERE RN = 1
)"""


def _build_trailer_population_tests(user_prompt: str, db_dialect: str = "oracle") -> List[TestCaseDesign]:
    date_literal = _extract_since_date_literal(user_prompt)
    cte = _trailer_expected_cte(date_literal)
    zero_target = "SELECT 0 AS mismatch_count FROM dual"

    tests: List[TestCaseDesign] = [
        TestCaseDesign(
            name="TXN TXN_SBTP_ID mismatch from CDSM_RULE_MAP",
            test_type="custom_sql",
            severity="high",
            description=f"Since {date_literal}, validate CCAL_OWNER.TXN.TXN_SBTP_ID equals derived expected TXNSBTP ID from trailer pattern rule ranking.",
            db_dialect=db_dialect,
            source_query=cte + "\nSELECT COUNT(*) AS mismatch_count FROM expected e JOIN CCAL_OWNER.TXN t ON t.TXN_SRC_KEY = e.TXN_SRC_KEY WHERE NVL(t.TXN_SBTP_ID, -1) <> NVL(e.TXN_SBTP_ID, -1)",
            target_query=zero_target,
            expected_result="0",
        ),
        TestCaseDesign(
            name="TXN EXEC_SBTP_ID mismatch from CDSM_RULE_MAP",
            test_type="custom_sql",
            severity="high",
            description=f"Since {date_literal}, validate CCAL_OWNER.TXN.EXEC_SBTP_ID equals derived expected EXECSBTP ID from trailer pattern rule ranking.",
            db_dialect=db_dialect,
            source_query=cte + "\nSELECT COUNT(*) AS mismatch_count FROM expected e JOIN CCAL_OWNER.TXN t ON t.TXN_SRC_KEY = e.TXN_SRC_KEY WHERE NVL(t.EXEC_SBTP_ID, -1) <> NVL(e.EXEC_SBTP_ID, -1)",
            target_query=zero_target,
            expected_result="0",
        ),
        TestCaseDesign(
            name="TXN SRC_PCS_TP_ID mismatch from CDSM_RULE_MAP",
            test_type="custom_sql",
            severity="high",
            description=f"Since {date_literal}, validate CCAL_OWNER.TXN.SRC_PCS_TP_ID equals derived expected SRCPCSTP ID from trailer pattern rule ranking.",
            db_dialect=db_dialect,
            source_query=cte + "\nSELECT COUNT(*) AS mismatch_count FROM expected e JOIN CCAL_OWNER.TXN t ON t.TXN_SRC_KEY = e.TXN_SRC_KEY WHERE NVL(t.SRC_PCS_TP_ID, -1) <> NVL(e.SRC_PCS_TP_ID, -1)",
            target_query=zero_target,
            expected_result="0",
        ),
        TestCaseDesign(
            name="TXN coverage for all expected TXN_SRC_KEY",
            test_type="custom_sql",
            severity="high",
            description=f"Since {date_literal}, validate every expected TXN_SRC_KEY has a corresponding row in CCAL_OWNER.TXN.",
            db_dialect=db_dialect,
            source_query=cte + "\nSELECT COUNT(*) AS mismatch_count FROM expected e LEFT JOIN CCAL_OWNER.TXN t ON t.TXN_SRC_KEY = e.TXN_SRC_KEY WHERE t.TXN_ID IS NULL",
            target_query=zero_target,
            expected_result="0",
        ),
        TestCaseDesign(
            name="APA coverage by TXN_SRC_KEY",
            test_type="custom_sql",
            severity="high",
            description=f"Since {date_literal}, validate each expected TXN_SRC_KEY has a corresponding APA row via TXN.EXEC_ID path.",
            db_dialect=db_dialect,
            source_query=cte + "\nSELECT COUNT(*) AS mismatch_count FROM expected e JOIN CCAL_OWNER.TXN t ON t.TXN_SRC_KEY = e.TXN_SRC_KEY LEFT JOIN CCAL_OWNER.APA ap ON ap.EXEC_ID = t.TXN_ID WHERE ap.APA_ID IS NULL",
            target_query=zero_target,
            expected_result="0",
        ),
        TestCaseDesign(
            name="APA_TP_CODE alignment vs expected TXN_SBTP_CD",
            test_type="custom_sql",
            severity="high",
            description=f"Since {date_literal}, validate APA_TP_CODE in CCAL_OWNER.APA is consistent with trailer-derived expected TXN_SBTP_CD by TXN_SRC_KEY.",
            db_dialect=db_dialect,
            source_query=cte + "\nSELECT COUNT(*) AS mismatch_count FROM expected e JOIN CCAL_OWNER.TXN t ON t.TXN_SRC_KEY = e.TXN_SRC_KEY JOIN CCAL_OWNER.APA ap ON ap.EXEC_ID = t.TXN_ID LEFT JOIN CCAL_OWNER.CL_VAL_VW apcv ON apcv.CL_VAL_ID = ap.APA_TP_ID WHERE NVL(TRIM(apcv.CL_VAL_CODE), '##') <> NVL(TRIM(e.TXN_SBTP_CD), '##')",
            target_query=zero_target,
            expected_result="0",
        ),
        TestCaseDesign(
            name="APA iAPASEC (APA_SEC_IND) alignment vs expected SRC_PCS_TP_CD",
            test_type="custom_sql",
            severity="high",
            description=f"Since {date_literal}, validate iAPASEC-equivalent source subtype code represented by CCAL_OWNER.APA.SRC_DTL_TXN_SBTP_CODE aligns with trailer-derived expected SRC_PCS_TP_CD by TXN_SRC_KEY.",
            db_dialect=db_dialect,
            source_query=cte + "\nSELECT COUNT(*) AS mismatch_count FROM expected e JOIN CCAL_OWNER.TXN t ON t.TXN_SRC_KEY = e.TXN_SRC_KEY JOIN CCAL_OWNER.APA ap ON ap.EXEC_ID = t.TXN_ID WHERE NVL(TRIM(ap.SRC_DTL_TXN_SBTP_CODE), '##') <> NVL(TRIM(e.SRC_PCS_TP_CD), '##')",
            target_query=zero_target,
            expected_result="0",
        ),
        TestCaseDesign(
            name="FIP_TP_CODE alignment vs expected EXEC_SBTP_CD",
            test_type="custom_sql",
            severity="high",
            description=f"Since {date_literal}, validate FIP_TP_CODE in CCAL_OWNER.FIP is consistent with trailer-derived expected EXEC_SBTP_CD by TXN_SRC_KEY.",
            db_dialect=db_dialect,
            source_query=cte + "\nSELECT COUNT(*) AS mismatch_count FROM expected e JOIN CCAL_OWNER.TXN t ON t.TXN_SRC_KEY = e.TXN_SRC_KEY JOIN CCAL_OWNER.APA ap ON ap.EXEC_ID = t.TXN_ID JOIN CCAL_OWNER.FIP f ON f.APA_ID = ap.APA_ID LEFT JOIN CCAL_OWNER.CL_VAL_VW fcv ON fcv.CL_VAL_ID = f.FIP_TP_ID WHERE NVL(TRIM(fcv.CL_VAL_CODE), '##') <> NVL(TRIM(e.EXEC_SBTP_CD), '##')",
            target_query=zero_target,
            expected_result="0",
        ),
        TestCaseDesign(
            name="TXN_RLTNP EXEC_TP_CODE alignment vs expected EXEC_SBTP_CD",
            test_type="custom_sql",
            severity="high",
            description=f"Since {date_literal}, validate EXEC_TP_CODE in CCAL_OWNER.TXN_RLTNP is consistent with trailer-derived expected EXEC_SBTP_CD by TXN_SRC_KEY.",
            db_dialect=db_dialect,
            source_query=cte + "\nSELECT COUNT(*) AS mismatch_count FROM expected e JOIN CCAL_OWNER.TXN t ON t.TXN_SRC_KEY = e.TXN_SRC_KEY JOIN CCAL_OWNER.TXN_RLTNP tr ON tr.SRC_TXN_ID = t.TXN_ID LEFT JOIN CCAL_OWNER.CL_VAL_VW trcv ON trcv.CL_VAL_ID = t.EXEC_TP_ID WHERE NVL(TRIM(trcv.CL_VAL_CODE), '##') <> NVL(TRIM(e.EXEC_SBTP_CD), '##')",
            target_query=zero_target,
            expected_result="0",
        ),
    ]

    return tests


def _validate_deterministic_sql_tests_or_raise(tests: List[TestCaseDesign]) -> None:
    """Light gate - only reject if source_query is empty, let Oracle validation handle syntax."""
    violations: List[str] = []
    for idx, test in enumerate(tests, start=1):
        if not (test.source_query or "").strip():
            violations.append(f"test[{idx}] {test.name}: source_query is empty")

    if violations:
        raise RuntimeError("Deterministic SQL validation failed: " + " | ".join(violations))


async def _resolve_oracle_validation_connector(datasource_id: Optional[int] = None):
    ds = None
    async with async_session() as db:
        if datasource_id:
            candidate = await db.get(DataSource, int(datasource_id))
            if candidate and (candidate.db_type or "").strip().lower() == "oracle" and bool(candidate.is_active):
                ds = candidate
        if ds is None:
            stmt = (
                select(DataSource)
                .where(func.lower(DataSource.db_type) == "oracle")
                .where(DataSource.is_active.is_(True))
                .order_by(DataSource.id.asc())
            )
            ds = (await db.execute(stmt)).scalars().first()

    if ds is None:
        return None

    connector = get_connector_from_model(ds)
    if not hasattr(connector, "validate_sql_batch"):
        return None
    await asyncio.to_thread(connector.connect)
    return connector


async def _collect_sql_validation_errors(test: TestCaseDesign, connector) -> List[str]:
    all_errors: List[str] = []
    errs = validate_test_definition_sql(test.model_dump())
    all_errors.extend(errs.get("source", []))
    all_errors.extend(errs.get("target", []))

    if not (test.source_query or "").strip():
        all_errors.append("Source query is empty")
    if not (test.target_query or "").strip():
        all_errors.append("Target query is empty")

    if test.source_query:
        all_errors.extend(_validate_tables_against_pdm(test.source_query))
    if test.target_query:
        all_errors.extend(_validate_tables_against_pdm(test.target_query))

    if connector:
        if test.source_query:
            source_err = (await asyncio.to_thread(connector.validate_sql_batch, [test.source_query]))[0]
            if source_err:
                all_errors.append(f"Oracle source dry-run error: {source_err}")
        if test.target_query:
            target_err = (await asyncio.to_thread(connector.validate_sql_batch, [test.target_query]))[0]
            if target_err:
                all_errors.append(f"Oracle target dry-run error: {target_err}")

    return all_errors


def _local_schema_kb_context(max_tables: int = 80) -> str:
    payload = load_schema_kb_payload()
    sources = payload.get("sources", []) if isinstance(payload, dict) else []
    if not sources:
        return "No local schema KB loaded."

    lines = ["Local DB Knowledge Base (PDM/LDM) summary:"]
    added = 0
    for src in sources:
        pdm = (src or {}).get("pdm", {})
        ds = pdm.get("datasource", {})
        lines.append(f"- Data source: {ds.get('name')} ({ds.get('db_type')})")
        for s in pdm.get("schemas", []) or []:
            schema_name = s.get("schema")
            for t in s.get("tables", []) or []:
                cols = t.get("columns", []) or []
                pk = t.get("primary_keys", []) or []
                lines.append(
                    f"  - {schema_name}.{t.get('name')} [{t.get('type')}] "
                    f"cols={len(cols)} pk={','.join(pk) if pk else '-'} fk={len(t.get('foreign_keys', []) or [])}"
                )
                added += 1
                if added >= max_tables:
                    lines.append("  - ...truncated...")
                    return "\n".join(lines)
    return "\n".join(lines)

GENERATOR_JOIN_KB = """
Generator KB – Join and table-role rules:
1) HARD RULE: source table and target table must be business S/T tables only.
2) HARD RULE: lookup/dimension tables (aliases like LK, D, C) must NEVER be used as source_table or target_table.
3) HARD RULE: NEVER join LK/D/C aliases to the same staging/fact business table just to fetch lookup values.
4) Prefer LEFT OUTER JOIN for lookup joins so missing lookup values are visible as mismatches.
5) Use canonical lookup tables by domain:
    - coded values: CCAL_REPL_OWNER.CL_VAL
    - source stream dimension: COMMON_OWNER.SRC_STM_DIM
    - cird period mapping: CCAL_REPL_OWNER.CCAL_CIRD_PD_MAP
6) Canonical join patterns:
    - JOIN CCAL_REPL_OWNER.CL_VAL C
         ON S.<code_id_column> = C.CL_VAL_ID
        AND C.CL_SCM_ID = <expected_schema_id>
    - LEFT JOIN COMMON_OWNER.SRC_STM_DIM D
         ON T.SRC_STM_ID = D.SRC_STM_ID
    - LEFT OUTER JOIN CCAL_REPL_OWNER.CCAL_CIRD_PD_MAP LK
         ON LK.CCAL_PD_ID = S.CCAL_PD_ID
        AND LK.ACTV_F = 'Y'
7) Comparison rule: compare lookup-derived value to target/fact value (for example LK.CIRD_PD_ID <> T.CIRD_PD_ID),
    not lookup-to-lookup and not S/T self-lookup substitutions.
8) Query-shape preference for mismatch tests:
    - FROM <FACT/TARGET> T
    - LEFT JOIN <STAGE/SOURCE> S ON business-key join
    - LEFT OUTER JOIN <LOOKUP TABLE> LK/D/C ON lookup key + active/SCM filters
    - WHERE mismatch predicate
9) Keep generated SQL strictly Oracle-compliant. Use NVL, TO_CHAR, SYSDATE, and FETCH FIRST n ROWS ONLY. Do not use ISNULL, GETDATE(), or LIMIT.
"""


def _filtered_schema_kb_context(text_corpus: str, max_tables: int = 60) -> str:
    """RAG Filter: Only pulls tables from the KB that are mentioned in the prompt/artifacts."""
    payload = load_schema_kb_payload()
    sources = payload.get("sources", []) if isinstance(payload, dict) else []
    if not sources:
        return "No local schema KB loaded."

    # Extract table-like names (ALL_CAPS_WITH_UNDERSCORES)
    tokens = set(re.findall(r'\b[A-Z][A-Z0-9_]{2,}\b', text_corpus.upper()))
    
    lines = ["Local DB Knowledge Base (Filtered for current context):"]
    added = 0
    for src in sources:
        pdm = (src or {}).get("pdm", {})
        for s in pdm.get("schemas", []) or []:
            schema_name = s.get("schema")
            for t in s.get("tables", []) or []:
                tbl_name = t.get("name", "")
                # Check if the table name or SCHEMA.TABLE is in the corpus
                if tbl_name.upper() in tokens or f"{schema_name}.{tbl_name}".upper() in text_corpus.upper():
                    cols = t.get("columns", []) or []
                    col_names = [c.get("name") for c in cols[:20]] # Provide top 20 columns for AI logic mapping
                    lines.append(f"  - {schema_name}.{tbl_name} [{t.get('type')}] columns: {', '.join(col_names)}")
                    added += 1
                    if added >= max_tables:
                        return "\n".join(lines)
    return "\n".join(lines)

def _validate_tables_against_pdm(sql: str) -> List[str]:
    """Extracts tables from SQL and verifies they exist in the local PDM Knowledge Base."""
    missing = []
    tables_in_sql = _extract_schema_tables(sql)
    if not tables_in_sql:
        return missing
        
    payload = load_schema_kb_payload()
    valid_tables = set(["DUAL", "SYS"]) # Built-in Oracle tables
    
    for src in payload.get("sources", []) if isinstance(payload, dict) else []:
        for s in (src.get("pdm", {}).get("schemas", []) or []):
            schema_name = (s.get("schema") or "").upper()
            for t in (s.get("tables", []) or []):
                tbl_name = (t.get("name") or "").upper()
                valid_tables.add(tbl_name)
                if schema_name:
                    valid_tables.add(f"{schema_name}.{tbl_name}")
                    
    for tbl in tables_in_sql:
        tbl_clean = tbl.replace('"', '').upper()
        if tbl_clean not in valid_tables:
            missing.append(f"Table or view '{tbl_clean}' not found in the Database Schema Knowledge Base.")
    return missing

def _format_ai_error(e: Exception) -> str:
    raw = f"{type(e).__name__}: {str(e)}"
    lower = raw.lower()
    if "blocked request: application" in lower or "web proxy" in lower or "openai-api" in lower:
        return (
            "OpenAI API traffic is blocked by your corporate web proxy/policy. "
            "Request access via your IT web access process or use an approved internal AI endpoint."
        )
    if "certificate_verify_failed" in lower or "self-signed certificate" in lower:
        return (
            "OpenAI TLS certificate validation failed. "
            "Set OPENAI_CA_BUNDLE to your corporate root CA file path, "
            "or set OPENAI_VERIFY_SSL=false as a temporary workaround."
        )
    if "connection error" in lower:
        provider = (settings.AI_PROVIDER or "openai").lower().strip()
        if provider == "openai":
            return (
                "Could not reach OpenAI from this network. "
                "If your company blocks OpenAI, configure AI_PROVIDER=azure or AI_PROVIDER=compatible "
                "with internal endpoint values in .env."
            )
        return (
            "Could not reach AI endpoint. Check internet/proxy settings, "
            "or configure OPENAI_CA_BUNDLE / OPENAI_VERIFY_SSL in .env."
        )
    if "timed out" in lower or "timeout" in lower:
        provider = (settings.AI_PROVIDER or "openai").lower().strip()
        if provider == "openai":
            return (
                "AI request timed out reaching OpenAI. This is commonly caused by proxy/network restrictions. "
                "Use AI_PROVIDER=azure or AI_PROVIDER=compatible with your internal endpoint."
            )
        return "AI request timed out while contacting the configured endpoint. Check endpoint URL, proxy, and firewall."
    return str(e)


def _build_http_client():
    verify: bool | str = settings.OPENAI_VERIFY_SSL
    if settings.OPENAI_CA_BUNDLE:
        verify = settings.OPENAI_CA_BUNDLE
    return httpx.Client(verify=verify, timeout=float(settings.AI_HTTP_TIMEOUT_SECONDS))


def _resolve_model(provider_override: Optional[str] = None) -> str:
    provider = (provider_override or settings.AI_PROVIDER or "openai").lower().strip()
    if provider == "githubcopilot":
        # Use explicit Copilot model to satisfy OpenAI SDK required argument.
        return (settings.GITHUBCOPILOT_MODEL or "gpt-5mini").strip()
    return settings.AI_MODEL or settings.OPENAI_MODEL


def _normalize_provider(provider_override: Optional[str] = None) -> str:
    provider = (provider_override or settings.AI_PROVIDER or "openai").lower().strip()
    if provider in {"local", "internal", "localagent", "local_agent"}:
        return "local"
    return provider


def _local_assistant_reply(messages: list, context: str = "", agent_system_prompt: str = "") -> str:
    """Deterministic local fallback responder for offline mode.

    Provides actionable SQL diagnostics when Copilot is unavailable.
    """
    user_text = ""
    for msg in reversed(messages or []):
        if (msg or {}).get("role") == "user":
            user_text = (msg or {}).get("content") or ""
            break
    corpus = f"{context or ''}\n{user_text}"
    upper = corpus.upper()

    tips: list[str] = []
    if "ORA-00942" in upper:
        tips.append("ORA-00942 indicates a missing table/view or missing privileges. Verify schema.table spelling and grants.")
        tips.append("Quick check: run SELECT 1 FROM <SCHEMA>.<TABLE> WHERE ROWNUM <= 1 to confirm object visibility.")
    if "ORA-00904" in upper:
        tips.append("ORA-00904 indicates an invalid identifier (column/alias). Verify alias scope and exact column names.")
        tips.append("Quick check: DESCRIBE <SCHEMA>.<TABLE> or query ALL_TAB_COLUMNS for the referenced table.")
    if "ORA-01722" in upper:
        tips.append("ORA-01722 invalid number usually means implicit string-to-number conversion. CAST explicitly or fix predicates.")
    if "ORA-00911" in upper:
        tips.append("ORA-00911 invalid character is commonly a trailing semicolon or hidden character in API-submitted SQL.")

    if not tips:
        tips.append("Local assistant is active. I can provide rule-based SQL diagnostics and rewrite guidance.")
        tips.append("Include the exact Oracle error text and the failing SQL snippet for targeted fixes.")

    if agent_system_prompt:
        tips.append("Applied local agent profile guidance from your selected internal agent.")

    return "\n".join(f"- {t}" for t in tips)


def _build_chat_call_args(messages: list, temperature: float, max_tokens: int, provider: str, model: Optional[str]) -> dict:
    call_args = {"messages": messages, "temperature": temperature, "max_tokens": max_tokens}
    if provider == "githubcopilot":
        call_args["model"] = (model or settings.GITHUBCOPILOT_MODEL or "gpt-5mini").strip()
    elif model:
        call_args["model"] = model
    return call_args


async def build_tfs_and_schema_context(tfs_item_id: Optional[str] = None, artifact_contents: Optional[List[str]] = None, user_prompt: str = "") -> TfsContext:
    """
    Context Builder Agent (Phase 2):
    Gathers TFS requirements, attached artifacts, and relevant schema into a strict data contract.
    """
    title = "Ad-hoc Mapping"
    description = "No specific TFS requirement provided."
    comments = []
    # Always copy so we don't mutate the caller's list when appending TFS documents
    artifacts = list(artifact_contents or [])

    tfs_download_log: List[str] = []

    if tfs_item_id and tfs_item_id.strip():
        try:
            # Fetch full context including attachment texts via existing TFS logic
            tfs_data = await fetch_work_item_full_context(int(tfs_item_id))
            title = tfs_data.get("title", title)
            description = tfs_data.get("description_text", "")

            # Combine acceptance criteria into description if present
            ac = tfs_data.get("acceptance_criteria", "")
            if ac:
                description += f"\n\nAcceptance Criteria:\n{ac}"

            # Append TFS attachment texts — report success/failure for each
            for att in tfs_data.get("attachments", []):
                name = att.get("name", "unnamed")
                text = att.get("content_text", "")
                if text and not text.startswith("[Download failed") and not text.startswith("[Could not"):
                    artifacts.append(f"TFS Attachment ({name}):\n{text}")
                    tfs_download_log.append(f"✅ Attachment '{name}' ({len(text):,} chars downloaded)")
                else:
                    err = text[:200] if text else "no content"
                    tfs_download_log.append(f"❌ Attachment '{name}' failed: {err}")

            # Append hyperlinked texts
            for link in tfs_data.get("hyperlinks", []):
                url = link.get("url", "")
                text = link.get("content_text", "")
                if text and not text.startswith("[Failed") and not text.startswith("[HTTP"):
                    artifacts.append(f"TFS Linked Page ({url}):\n{text}")
                    tfs_download_log.append(f"✅ Hyperlink '{url[:80]}' ({len(text):,} chars downloaded)")
                elif text:
                    tfs_download_log.append(f"❌ Hyperlink '{url[:80]}' failed: {text[:120]}")
                else:
                    tfs_download_log.append(f"⏭️ Hyperlink '{url[:80]}' — no content returned")

        except Exception as e:
            tfs_download_log.append(f"❌ TFS fetch failed: {e}")
            logger.warning(f"Failed to fetch TFS context for {tfs_item_id}: {e}")

    # RAG filtering: Merge all text to search for database table tokens
    full_corpus = f"{title}\n{description}\n{user_prompt}\n" + "\n".join(artifacts)
    
    schema_ddl = _filtered_schema_kb_context(full_corpus, max_tables=60)

    ctx = TfsContext(
        work_item_id=tfs_item_id or "N/A",
        title=title,
        description=description,
        comments=comments,
        artifact_contents=artifacts,
        schema_ddl=schema_ddl,
    )
    # Attach download log so orchestrators can present it in Phase 1 reports
    ctx.__dict__["_tfs_download_log"] = tfs_download_log
    return ctx

async def analyze_etl_requirements(
    context: TfsContext,
    agent_system_prompt: str = "",
    reports: Optional[List[AgentPhaseReport]] = None,
    correction: str = "",
) -> EtlMappingSpec:
    """Analysis Agent (Phase 2): Parses the TFS requirement and mappings without writing SQL.

    Passes the FULL artifact text (up to 12 KB per artifact) and enforces strict table anchoring.
    If `reports` list is provided the agent appends an AgentPhaseReport entry to it.
    """
    provider = _normalize_provider()
    client, model, cfg_error = _get_client_and_model(provider)
    if not client:
        raise RuntimeError(f"AI Client init failed: {cfg_error}")

    # Build document list as labelled sections (not truncated to 12 KB per doc)
    doc_sections: List[str] = []
    doc_names: List[str] = []
    for i, art in enumerate(context.artifact_contents, start=1):
        # Extract real filename from [File: name] prefix or TFS labels
        first_line = art.splitlines()[0] if art else ""
        if first_line.startswith("[File:"):
            file_label = first_line[len("[File:"):].rstrip("]").strip()
        elif first_line.startswith("TFS Attachment ("):
            file_label = "TFS: " + first_line.split("(", 1)[-1].rstrip(")").rstrip(":")
        elif first_line.startswith("TFS Linked Page ("):
            file_label = "TFS link: " + first_line.split("(", 1)[-1].rstrip(")").rstrip(":")
        else:
            file_label = f"Document {i}"
        label = f"[{file_label}]"
        doc_names.append(f"{file_label} ({len(art):,} chars)")
        doc_sections.append(f"{label}\n{art[:12000]}")

    artifacts_text = "\n\n".join(doc_sections) or "(no attached documents)"
    correction_note = f"\n\nUser Correction: {correction}" if correction.strip() else ""

    prompt = f"""You are an ETL Business Analyst. Your task is to extract the source/target table mapping from the documents below.

CRITICAL RULES:
1. ONLY list tables that you found LITERALLY in the documents below or in the Database Schema.  
   Do NOT invent or guess table names. If a staging table is referenced as a variable like
   #CDS.CCAL_TXN_TRLR_SRC_STG_TBL, find its assigned value in the ODI package steps.
2. For each source_table and target_table, cite which document it came from (e.g. "found in Document 3").
3. Extract ALL mapping rules from the documents (column-to-column assignments in INSERT/SELECT statements).
4. Do NOT write SQL. Produce only structured ETL analysis.
5. Look for variable assignments in the ODI scripts: tables like TXN_TRLR_STCCCALQ_STG,
   STCCCALQ_STG, TXN_STCCCALQ_STG, APA_STCCCALQ_STG etc. are staging tables in CDS_STG_OWNER.
   Target tables like TXN, APA, FIP, TXN_RLTNP live in CCAL_OWNER.

TFS Title: {context.title}
TFS Description: {context.description}{correction_note}

--- DOCUMENTS REVIEWED ---
{artifacts_text}

--- DATABASE SCHEMA (use these names for any table you reference) ---
{context.schema_ddl}
---

Return a JSON object matching EtlMappingSpec schema.  
In the `join_conditions` field include the full SQL join logic you found in the INSERT statements.
"""
    messages = [{"role": "system", "content": agent_system_prompt}, {"role": "user", "content": prompt}]
    call_args = _build_chat_call_args(messages, 0.1, 6000, provider, model)
    resp = _chat_completion_with_fallback(client, call_args, provider)
    text = (resp.choices[0].message.content or "").strip()
    parsed = _parse_or_repair_json_response(
        client,
        provider,
        model,
        text,
        json.dumps(EtlMappingSpec.model_json_schema(), indent=2),
        "object",
    )
    spec = EtlMappingSpec.model_validate(_normalize_etl_mapping_spec_payload(parsed))

    if reports is not None:
        # Detect potential hallucinated tables
        schema_text = (context.schema_ddl + " " + artifacts_text).lower()
        warnings: List[str] = []
        all_tables = spec.source_tables + spec.target_tables
        for tbl in all_tables:
            bare = tbl.split(".")[-1].lower()
            if bare not in schema_text and tbl.lower() not in schema_text:
                warnings.append(f"Table '{tbl}' NOT found in documents or schema KB — may be hallucinated")
        reports.append(AgentPhaseReport(
            phase="analysis",
            documents_reviewed=doc_names,
            tables_identified=all_tables,
            decisions=[
                f"Source tables: {', '.join(spec.source_tables) or 'none'}",
                f"Target tables: {', '.join(spec.target_tables) or 'none'}",
                f"Business keys: {', '.join(spec.business_keys) or 'none'}",
                f"Field mappings: {len(spec.mappings)}",
            ],
            warnings=warnings,
            result_summary=(
                f"Analyzed {len(doc_names)} document(s). "
                f"Identified {len(spec.source_tables)} source table(s) and "
                f"{len(spec.target_tables)} target table(s) with {len(spec.mappings)} mapping rules."
            ),
            result_payload=spec.model_dump(),
        ))
    return spec

async def design_sql_tests(
    spec: EtlMappingSpec,
    db_dialect: str = "oracle",
    agent_system_prompt: str = "",
    artifact_contents: Optional[List[str]] = None,
    schema_ddl: str = "",
    reports: Optional[List[AgentPhaseReport]] = None,
    correction: str = "",
) -> List[TestCaseDesign]:
    """Design Agent (Phase 3): Translates structured ETL logic into strictly typed SQL.

    Passes raw artifacts alongside EtlMappingSpec so the AI can reference exact column names.
    Strictly forbids inventing table names not grounded in the spec or schema.
    """
    provider = _normalize_provider()
    client, model, cfg_error = _get_client_and_model(provider)
    if not client:
        raise RuntimeError(f"AI Client init failed: {cfg_error}")

    dialect_rules = get_dialect_prompt(db_dialect)
    correction_note = f"\n\nUser Correction: {correction}" if correction.strip() else ""

    # Pass the first 6K of each raw artifact so AI can see real column names from ODI
    orig_docs = "\n\n".join(
        f"[Artifact {i+1}]\n{a[:6000]}"
        for i, a in enumerate(artifact_contents or [])
    ) or "(no raw artifacts)"

    prompt = f"""You are a Data Quality Automation Engineer.
Your task: Generate Oracle SQL validation tests from the ETL Mapping Specification below.

{dialect_rules}

CRITICAL TABLE-NAME RULES:
1. You MUST ONLY use table names that appear in `ETL Specification.source_tables`,
   `ETL Specification.target_tables`, or exactly in the Database Schema section.
2. DO NOT invent new table names. If a table you need is not listed, raise it as a warning
   in the test description, but still produce a best-effort test.
3. Source staging tables are in CDS_STG_OWNER schema (e.g. CDS_STG_OWNER.TXN_TRLR_STCCCALQ_STG).
   Target tables are in CCAL_OWNER (e.g. CCAL_OWNER.TXN, CCAL_OWNER.APA, CCAL_OWNER.FIP).
4. The business key for all joins is TXN_SRC_KEY.
5. For trailer-rule tests: derive expected values via CDSM_RULE_MAP + REGEXP_LIKE and join to
   CCAL_OWNER.CL_VAL_VW to resolve IDs, then compare to staged/loaded values.
{correction_note}

--- ETL SPECIFICATION ---
{spec.model_dump_json(indent=2)}

--- DATABASE SCHEMA (table names to use) ---
{schema_ddl[:4000] if schema_ddl else '(not provided)'}

--- ORIGINAL ODI/MAPPING ARTIFACTS (for column name reference only) ---
{orig_docs[:4000]}
---

Return a STRICT JSON array of test cases matching the TestCaseDesign schema. No markdown.
Each test must validate a specific assertion: row count, value match, referential integrity, or
custom_sql. Use complete SQL statements with all required JOINs and WHERE clauses.
"""
    messages = [{"role": "system", "content": agent_system_prompt}, {"role": "user", "content": prompt}]
    call_args = _build_chat_call_args(messages, 0.1, 6000, provider, model)
    resp = _chat_completion_with_fallback(client, call_args, provider)
    text = (resp.choices[0].message.content or "").strip()
    parsed = _parse_or_repair_json_response(
        client,
        provider,
        model,
        text,
        json.dumps([TestCaseDesign.model_json_schema()], indent=2),
        "array",
    )
    normalized = _normalize_test_case_design_array_payload(parsed)
    tests = [TestCaseDesign.model_validate(item) for item in normalized]

    if reports is not None:
        schema_and_spec = (spec.model_dump_json() + " " + schema_ddl + " " + orig_docs).lower()
        warnings: List[str] = []
        import re as _re
        for t in tests:
            src_tables = _re.findall(r"FROM\s+([\w\.]+)|JOIN\s+([\w\.]+)", (t.source_query or ""), _re.IGNORECASE)
            for match in src_tables:
                tbl = (match[0] or match[1]).strip().split("(")[0]
                bare = tbl.split(".")[-1].lower()
                if bare and bare not in schema_and_spec and tbl.lower() not in schema_and_spec:
                    warnings.append(f"Possibly hallucinated table '{tbl}' in test '{t.name}'")
        # Build real filenames for design report
        _design_doc_names = ["EtlMappingSpec"]
        for _a in (artifact_contents or []):
            _fl = _a.splitlines()[0] if _a else ""
            if _fl.startswith("[File:"):
                _design_doc_names.append(_fl[len("[File:"):].rstrip("]").strip())
            elif _fl.startswith("TFS Attachment (") or _fl.startswith("TFS Linked"):
                _design_doc_names.append(_fl.split("(", 1)[-1].rstrip(")").rstrip(":"))
            else:
                _design_doc_names.append(_fl[:80] if _fl else "artifact")
        reports.append(AgentPhaseReport(
            phase="design",
            documents_reviewed=_design_doc_names,
            tables_identified=spec.source_tables + spec.target_tables,
            decisions=[f"Generated {len(tests)} test case(s)",
                       f"Test types: {', '.join(set(t.test_type for t in tests))}"],
            warnings=list(set(warnings)),
            result_summary=(
                f"Generated {len(tests)} test case(s) for dialect '{db_dialect}'. "
                + (f"{len(set(warnings))} potential table-name warnings." if warnings else "No table warnings.")
            ),
            result_payload={"tests": [t.model_dump() for t in tests]},
        ))
    return tests

async def validate_and_fix_sql_tests(
    draft_tests: List[TestCaseDesign],
    spec: EtlMappingSpec,
    db_dialect: str = "oracle",
    validation_datasource_id: Optional[int] = None,
) -> List[TestCaseDesign]:
    """Validation Agent (Phase 4): Gatekeeper that checks SQL syntax and forces AI corrections."""
    provider = _normalize_provider()
    client, model, cfg_error = _get_client_and_model(provider)
    if not client:
        return draft_tests  # fallback if AI not available

    connector = None
    validated = []
    try:
        if db_dialect.lower().strip() == "oracle":
            connector = await _resolve_oracle_validation_connector(validation_datasource_id)

        for test in draft_tests:
            candidate = test
            last_errors: List[str] = []

            for _attempt in range(3):
                all_errors = await _collect_sql_validation_errors(candidate, connector)
                if not all_errors:
                    validated.append(candidate)
                    break

                last_errors = all_errors
                prompt = f"""You are a SQL Validation Gatekeeper.
The following test case generated for {db_dialect} failed validation.
Errors: {', '.join(all_errors)}

Original Test:
{candidate.model_dump_json(indent=2)}

ETL Spec Context:
{spec.model_dump_json(indent=2)}

Fix the SQL queries to resolve all errors and return ONLY a corrected object matching the TestCaseDesign JSON schema. No markdown."""

                call_args = _build_chat_call_args([{"role": "user", "content": prompt}], 0.1, 2200, provider, model)
                resp = _chat_completion_with_fallback(client, call_args, provider)
                text = (resp.choices[0].message.content or "").strip()
                parsed = _parse_or_repair_json_response(
                    client,
                    provider,
                    model,
                    text,
                    json.dumps(TestCaseDesign.model_json_schema(), indent=2),
                    "object",
                )
                candidate = TestCaseDesign.model_validate(_normalize_test_case_design_payload(parsed))
            else:
                logger.warning("Validation Agent could not repair test '%s': %s", test.name, "; ".join(last_errors))

        return validated
    finally:
        if connector is not None:
            try:
                await asyncio.to_thread(connector.disconnect)
            except Exception:
                pass

async def orchestrate_test_generation(
    tfs_item_id: Optional[str],
    artifact_contents: List[str],
    user_prompt: str,
    db_dialect: str = "oracle",
    validation_datasource_id: Optional[int] = None,
) -> List[TestCaseDesign]:
    """The Master Orchestrator linking Context, Analysis, Design, and Validation Agents."""
    if _is_trailer_population_request(user_prompt):
        deterministic_tests = _build_trailer_population_tests(user_prompt, db_dialect)
        _validate_deterministic_sql_tests_or_raise(deterministic_tests)
        deterministic_spec = EtlMappingSpec(
            source_tables=["CDS_STG_OWNER.STCCCALQ_GG_VW", "CCAL_OWNER.CDSM_RULE_MAP"],
            target_tables=["CCAL_OWNER.TXN", "CCAL_OWNER.APA", "CCAL_OWNER.FIP", "CCAL_OWNER.TXN_RLTNP"],
            business_keys=["TXN_SRC_KEY"],
            join_conditions="TXN_SRC_KEY-driven validation with ranked trailer rule mapping",
            mappings=[],
            filters="TRUNC(NVL(EFFECTIVE_DATE, SYSDATE)) >= DATE '2026-03-30'",
        )
        return await validate_and_fix_sql_tests(
            deterministic_tests,
            deterministic_spec,
            db_dialect,
            validation_datasource_id,
        )

    context = await build_tfs_and_schema_context(tfs_item_id, artifact_contents, user_prompt)
    _reports: List[AgentPhaseReport] = []
    spec = await analyze_etl_requirements(context, reports=_reports)
    draft_tests = await design_sql_tests(
        spec, db_dialect,
        artifact_contents=context.artifact_contents,
        schema_ddl=context.schema_ddl,
        reports=_reports,
    )
    final_tests = await validate_and_fix_sql_tests(draft_tests, spec, db_dialect, validation_datasource_id)
    return final_tests


async def run_orchestration_phase(
    phase: str,
    state: Dict[str, Any],
    correction: str = "",
) -> Tuple[Dict[str, Any], AgentPhaseReport]:
    """Run exactly one agent phase and return updated state + phase report.

    Used by semi-manual chat mode so each phase result can be reviewed/corrected before
    the next phase runs.

    Args:
        phase: One of "context" | "analysis" | "design" | "validation"
        state: Accumulated pipeline state dict (persisted in pending_orchestration).
        correction: Optional user-supplied correction text to incorporate.

    Returns:
        (updated_state, AgentPhaseReport)
    """
    reports: List[AgentPhaseReport] = []

    if phase == "context":
        context = await build_tfs_and_schema_context(
            tfs_item_id=state.get("tfs_item_id"),
            artifact_contents=state.get("artifact_contents", []),
            user_prompt=state.get("user_prompt", ""),
        )
        # Label documents with their source (TFS vs user upload)
        raw_arts = context.artifact_contents
        doc_names = []
        for i, a in enumerate(raw_arts, start=1):
            # first line has source hint e.g. "TFS Attachment (foo.xlsx):"
            first_line = a.splitlines()[0] if a else ""
            label = first_line[:80] if first_line.startswith("TFS ") else f"User Upload {i}"
            doc_names.append(f"{label} ({len(a):,} chars)")

        # Count tables from _filtered_schema_kb_context output (format: "  - SCHEMA.TABLE [type]")
        schema_tables = re.findall(r"- ([A-Z][\w]+\.[A-Z][\w]+)", context.schema_ddl)

        # Pull download log attached by build_tfs_and_schema_context
        download_log: List[str] = context.__dict__.get("_tfs_download_log", [])

        decisions = [
            f"TFS work item: {context.work_item_id}",
            f"Title: {context.title[:120]}",
            f"Artifacts collected: {len(doc_names)}",
            f"Schema KB tables loaded: {len(schema_tables)}",
        ] + download_log

        warnings = [d for d in download_log if d.startswith("❌")]

        report = AgentPhaseReport(
            phase="context_builder",
            documents_reviewed=doc_names,
            tables_identified=schema_tables,
            decisions=decisions,
            warnings=warnings,
            result_summary=(
                f"Collected {len(doc_names)} document(s) for work item '{context.work_item_id}'. "
                f"Schema KB filtered to {len(schema_tables)} relevant table(s). "
                + (f"{len(download_log)} TFS download event(s)." if download_log else "")
            ),
            result_payload=context.model_dump(),
        )
        state = dict(state)
        state["context_json"] = context.model_dump()
        state["phase"] = "context_done"
        return state, report

    if phase == "analysis":
        from app.models.agent_contracts import TfsContext as _TfsContext
        context = _TfsContext.model_validate(state["context_json"])
        spec = await analyze_etl_requirements(context, reports=reports, correction=correction)
        state = dict(state)
        state["spec_json"] = spec.model_dump()
        state["phase"] = "analysis_done"
        return state, reports[0] if reports else AgentPhaseReport(
            phase="analysis", result_summary="Analysis complete.", tables_identified=[],
        )

    if phase == "design":
        from app.models.agent_contracts import EtlMappingSpec as _EtlMappingSpec, TfsContext as _TfsContext
        spec = _EtlMappingSpec.model_validate(state["spec_json"])
        context = _TfsContext.model_validate(state["context_json"])
        db_dialect = state.get("db_dialect", "oracle")
        draft_tests = await design_sql_tests(
            spec,
            db_dialect,
            artifact_contents=context.artifact_contents,
            schema_ddl=context.schema_ddl,
            reports=reports,
            correction=correction,
        )
        state = dict(state)
        state["draft_tests_json"] = [t.model_dump() for t in draft_tests]
        state["phase"] = "design_done"
        return state, reports[0] if reports else AgentPhaseReport(
            phase="design", result_summary=f"Design complete. {len(draft_tests)} tests generated.", tables_identified=[],
        )

    if phase == "validation":
        from app.models.agent_contracts import EtlMappingSpec as _EtlMappingSpec
        spec = _EtlMappingSpec.model_validate(state["spec_json"])
        db_dialect = state.get("db_dialect", "oracle")
        draft_tests = [TestCaseDesign.model_validate(t) for t in state.get("draft_tests_json", [])]
        final_tests = await validate_and_fix_sql_tests(
            draft_tests, spec, db_dialect, state.get("validation_datasource_id")
        )
        dropped = len(draft_tests) - len(final_tests)
        state = dict(state)
        state["final_tests_json"] = [t.model_dump() for t in final_tests]
        state["phase"] = "complete"
        report = AgentPhaseReport(
            phase="validation",
            documents_reviewed=[f"Oracle datasource #{state.get('validation_datasource_id')}"],
            tables_identified=[],
            decisions=[
                f"Draft tests: {len(draft_tests)}",
                f"Passed validation: {len(final_tests)}",
                f"Dropped (unresolvable errors): {dropped}",
            ],
            warnings=[f"Dropped {dropped} test(s) whose SQL errors could not be auto-repaired"] if dropped else [],
            result_summary=(
                f"Validation complete. {len(final_tests)}/{len(draft_tests)} tests passed. "
                + (f"{dropped} dropped." if dropped else "No tests dropped.")
            ),
            result_payload={"tests": [t.model_dump() for t in final_tests]},
        )
        return state, report

    raise ValueError(f"Unknown phase: {phase!r}. Must be one of: context, analysis, design, validation")

def _is_model_not_supported_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "model_not_supported" in text or "requested model is not supported" in text


def _chat_completion_with_fallback(client, call_args: dict, provider: str):
    try:
        if provider == "githubcopilot":
            logger.info(
                "Copilot chat request keys=%s model=%s",
                sorted(call_args.keys()),
                call_args.get("model"),
            )
        return client.chat.completions.create(**call_args)
    except Exception as e:
        if provider == "githubcopilot" and _is_model_not_supported_error(e):
            fallback_model = "gpt-4o"
            current_model = (call_args.get("model") or "").strip().lower()
            if current_model != fallback_model.lower():
                retry_args = dict(call_args)
                retry_args["model"] = fallback_model
                logger.warning("Copilot model '%s' not supported; retrying with '%s'", call_args.get("model"), fallback_model)
                return client.chat.completions.create(**retry_args)
        raise


def _merge_ai_suggested_tests_with_heuristic(ai_payload: dict, heuristic_payload: dict) -> dict:
    ai_tests = (ai_payload or {}).get("suggested_tests") if isinstance(ai_payload, dict) else None
    heuristic_tests = (heuristic_payload or {}).get("suggested_tests") if isinstance(heuristic_payload, dict) else None

    if not isinstance(ai_tests, list) or not ai_tests:
        if isinstance(heuristic_tests, list):
            ai_payload["suggested_tests"] = heuristic_tests
        return ai_payload

    if not isinstance(heuristic_tests, list):
        heuristic_tests = []

    enriched = []
    for idx, test in enumerate(ai_tests):
        test_obj = dict(test) if isinstance(test, dict) else {"name": f"AI Test {idx + 1}"}
        fallback = heuristic_tests[idx] if idx < len(heuristic_tests) and isinstance(heuristic_tests[idx], dict) else {}
        if not test_obj.get("source_query"):
            test_obj["source_query"] = fallback.get("source_query") or ""
        if not test_obj.get("target_query"):
            test_obj["target_query"] = fallback.get("target_query") or ""
        if not test_obj.get("test_type"):
            test_obj["test_type"] = fallback.get("test_type") or "value_match"
        if not test_obj.get("severity"):
            test_obj["severity"] = fallback.get("severity") or "medium"
        if not test_obj.get("description"):
            test_obj["description"] = fallback.get("description") or ""
        enriched.append(test_obj)

    ai_payload["suggested_tests"] = enriched
    return ai_payload


def _copilot_default_headers() -> dict:
    return {
        "Editor-Version": settings.GITHUBCOPILOT_EDITOR_VERSION or "vscode/1.98.0",
        "Editor-Plugin-Version": settings.GITHUBCOPILOT_EDITOR_PLUGIN_VERSION or "copilot-chat/0.26.7",
        "Copilot-Integration-Id": settings.GITHUBCOPILOT_INTEGRATION_ID or "vscode-chat",
        "User-Agent": "GitHubCopilotChat/0.26.7",
    }


def _get_client_and_model(provider_override: Optional[str] = None):
    provider = (provider_override or settings.AI_PROVIDER or "openai").lower().strip()
    model = _resolve_model(provider)
    if provider != "githubcopilot":
        return None, None, (
            "Only GitHub Copilot provider is enabled in this deployment. "
            "Connect Copilot on the AI page or set AI_PROVIDER=githubcopilot."
        )

    # Backward-compatible TLS behavior for Copilot endpoint calls.
    github_verify: bool | str = settings.GITHUB_VERIFY_SSL
    if settings.GITHUB_CA_BUNDLE:
        github_verify = settings.GITHUB_CA_BUNDLE
    elif settings.OPENAI_CA_BUNDLE:
        github_verify = settings.OPENAI_CA_BUNDLE
    elif not settings.OPENAI_VERIFY_SSL:
        github_verify = False
    http_client = httpx.Client(verify=github_verify, timeout=float(settings.AI_HTTP_TIMEOUT_SECONDS), trust_env=True)

    if provider == "githubcopilot":
        from openai import OpenAI
        base_url = settings.GITHUBCOPILOT_BASE_URL or settings.AI_BASE_URL or "https://api.githubcopilot.com"
        runtime_token = get_runtime_copilot_token()
        api_key = runtime_token or settings.GITHUBCOPILOT_API_KEY
        if not base_url:
            return None, None, (
                "AI_PROVIDER=githubcopilot requires GITHUBCOPILOT_BASE_URL "
                "(or AI_BASE_URL)."
            )
        if not api_key:
            return None, None, (
                "GitHub Copilot is not connected. Use 'Connect with GitHub' on the AI page "
                "or set GITHUBCOPILOT_API_KEY in .env."
            )
        client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=http_client,
            default_headers=_copilot_default_headers(),
        )
        return client, model, None

    return None, None, "GitHub Copilot client was not initialized."


async def ai_extract_rules(sql_text: str, agent_system_prompt: str = "") -> dict:
    """Parse SQL/ETL text and extract mapping rules as structured JSON."""
    provider = _normalize_provider()
    client, model, cfg_error = _get_client_and_model(provider)
    if not client:
        return {"error": cfg_error}

    prompt = f"""You are a data engineering expert. Analyze the following SQL/ETL code and extract 
all source-to-target mapping rules. For each rule, return:
- source_table, source_schema, source_columns (array)
- target_table, target_schema, target_columns (array)
- transformation (SQL expression if any)
- join_condition (if any)
- filter_condition (if any)
- rule_type: one of [direct, aggregation, lookup, scd, custom]

Return ONLY valid JSON array. No markdown, no explanation.

SQL Code:
```
{sql_text}
```"""

    try:
        messages = []
        if agent_system_prompt:
            messages.append({"role": "system", "content": agent_system_prompt})
        messages.append({"role": "user", "content": prompt})
        call_args = _build_chat_call_args(messages, 0.1, 4000, provider, model)
        resp = _chat_completion_with_fallback(client, call_args, provider)
        text = resp.choices[0].message.content.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        rules = json.loads(text)
        return {"rules": rules, "confidence": 0.85}
    except Exception as e:
        logger.exception("AI rule extraction failed")
        return {"error": _format_ai_error(e)}


async def ai_suggest_tests(mapping_rule: dict, schema_info: dict, agent_system_prompt: str = "") -> dict:
    """Given a mapping rule and schema info, suggest additional test cases."""
    provider = _normalize_provider()
    client, model, cfg_error = _get_client_and_model(provider)
    if not client:
        return {"error": cfg_error}

    prompt = f"""You are a data quality testing expert. Given this mapping rule and schema info,
suggest test cases that would catch common ETL issues. For each test, return:
- name, test_type, severity, source_query, target_query, description

Test types: row_count, null_check, uniqueness, referential_integrity, value_match, 
aggregation, freshness, custom_sql, schema_drift

Return ONLY valid JSON array.

Mapping Rule: {json.dumps(mapping_rule)}
Schema Info: {json.dumps(schema_info)}"""

    try:
        messages = []
        if agent_system_prompt:
            messages.append({"role": "system", "content": agent_system_prompt})
        messages.append({"role": "user", "content": prompt})
        call_args = _build_chat_call_args(messages, 0.2, 4000, provider, model)
        resp = _chat_completion_with_fallback(client, call_args, provider)
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        tests = json.loads(text)
        return {"tests": tests, "confidence": 0.80}
    except Exception as e:
        logger.exception("AI test suggestion failed")
        return {"error": _format_ai_error(e)}


async def ai_triage_failures(failures: List[dict]) -> dict:
    """Cluster and root-cause analyze a batch of test failures."""
    provider = _normalize_provider()
    client, model, cfg_error = _get_client_and_model(provider)
    if not client:
        return {"error": cfg_error}

    prompt = f"""You are a data quality triage expert. Analyze these test failures and:
1. Group them into clusters by likely root cause
2. For each cluster, provide:
   - cluster_name
   - likely_root_cause
   - affected_tests (list of test names)
   - suggested_investigation_steps (list)
   - severity: critical/high/medium/low

Return ONLY valid JSON array of clusters.

Failures: {json.dumps(failures)}"""

    try:
        call_args = _build_chat_call_args([{"role": "user", "content": prompt}], 0.1, 4000, provider, model)
        resp = _chat_completion_with_fallback(client, call_args, provider)
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        clusters = json.loads(text)
        return {"clusters": clusters}
    except Exception as e:
        logger.exception("AI triage failed")
        return {"error": _format_ai_error(e)}


async def ai_analyze_sql(sql_text: str, agent_system_prompt: str = "") -> dict:
    """General-purpose SQL analysis – explain what the SQL does, find issues, suggest improvements."""
    provider = _normalize_provider()
    client, model, cfg_error = _get_client_and_model(provider)
    if not client:
        return {"error": cfg_error}

    prompt = f"""Analyze this SQL code and provide:
1. summary: what does this SQL do (1-2 sentences)
2. source_tables: list of source tables referenced
3. target_tables: list of target/insert tables
4. transformations: key transformations applied
5. potential_issues: list of potential data quality issues
6. test_suggestions: list of recommended tests

Return ONLY valid JSON object.

SQL:
```
{sql_text}
```"""

    try:
        messages = []
        if agent_system_prompt:
            messages.append({"role": "system", "content": agent_system_prompt})
        messages.append({"role": "user", "content": prompt})
        call_args = _build_chat_call_args(messages, 0.1, 4000, provider, model)
        resp = _chat_completion_with_fallback(client, call_args, provider)
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        return json.loads(text)
    except Exception as e:
        logger.exception("AI SQL analysis failed")
        return {"error": _format_ai_error(e)}


async def ai_chat(
    messages: list,
    context: str = "",
    provider_override: str = "",
    agent_system_prompt: str = "",
    attachments: Optional[List[dict]] = None,
) -> dict:
    """Multi-turn conversational chat using the configured AI provider.
    messages: list of {"role": "user"|"assistant", "content": "..."}
    context: optional system-level context about the DB testing workspace.
    """
    provider = _normalize_provider(provider_override or None)
    if provider == "local":
        return {"reply": _local_assistant_reply(messages, context, agent_system_prompt), "provider": "local"}

    client, model, cfg_error = _get_client_and_model(provider)
    if not client:
        # Graceful fallback: keep local/internal agents usable even if Copilot is disconnected.
        if agent_system_prompt:
            return {
                "reply": _local_assistant_reply(messages, context, agent_system_prompt)
                        + "\n\n(Copilot unavailable, served by local fallback engine)",
                "provider": "local-fallback",
            }
        return {"error": cfg_error}

    system_msg = (
        "You are Copilot, an expert AI assistant integrated into the DB Testing Tool. "
        "You help data engineers with: SQL queries, ETL mapping rules, data quality tests, "
        "TFS/Azure DevOps work items, Oracle/Redshift migration issues, and general data engineering tasks. "
        "Be concise and practical. When providing SQL, ALWAYS use strictly Oracle SQL syntax unless explicitly asked otherwise. "
        "When suggesting tests, include the test type, severity, and SQL queries. "
        "If attached file contents are provided in context or attachments, treat them as user-provided text. "
        "Do not say you cannot access files/uploads; use the provided content directly."
    )
    if context:
        system_msg += f"\n\nWorkspace context:\n{context}"
    if agent_system_prompt:
        system_msg += f"\n\nMulti-agent guidance:\n{agent_system_prompt}"

    chat_messages = [{"role": "system", "content": system_msg}] + messages
    if attachments:
        attachment_lines = []
        for a in attachments:
            name = (a or {}).get("name") or "attachment"
            ftype = (a or {}).get("type") or "unknown"
            note = (a or {}).get("note") or ""
            content = (a or {}).get("content") or ""
            block = f"Attachment: {name} (type={ftype})"
            if note:
                block += f"\nNote: {note}"
            if content:
                block += f"\nContent:\n{content}"
            attachment_lines.append(block)
        chat_messages.append({
            "role": "user",
            "content": "Use these attached file contents in your answer:\n\n" + "\n\n".join(attachment_lines),
        })

    try:
        call_args = _build_chat_call_args(chat_messages, 0.3, 4000, provider, model)
        resp = _chat_completion_with_fallback(client, call_args, provider)
        reply = resp.choices[0].message.content.strip()
        return {"reply": reply}
    except Exception as e:
        logger.exception("AI chat failed")
        return {"error": _format_ai_error(e)}


async def ai_generate_mapping_rules_from_rows(
    mapping_rows: List[dict],
    target_schema: str,
    target_table: str,
    source_datasource_id: int,
    target_datasource_id: int,
    agent_system_prompt: str = "",
    provider_override: Optional[str] = None,
) -> dict:
    """Generate mapping rules directly from parsed mapping rows using AI."""
    provider = _normalize_provider(provider_override)
    client, model, cfg_error = _get_client_and_model(provider)
    if not client:
        return {"error": cfg_error}

    prompt = f"""You are a senior ETL architect.
Generate mapping rules in STRICT JSON array format from mapping row data.

Return objects with exactly these keys:
- name
- source_datasource_id
- source_schema
- source_table
- source_columns (JSON array string)
- target_datasource_id
- target_schema
- target_table
- target_columns (JSON array string)
- transformation_sql
- join_condition
- filter_condition
- rule_type (direct|lookup|aggregation|scd|custom)
- description

Rules:
1) Group rows by source_schema + source_table.
2) Keep Oracle-friendly SQL identifiers (no forced quoted identifiers).
3) Infer rule_type=lookup when lookup/join logic exists, otherwise direct unless clearly custom.
4) Include all mapped target columns in target_columns.
5) Keep source_datasource_id={source_datasource_id} and target_datasource_id={target_datasource_id}.
6) target_schema={target_schema}, target_table={target_table}.
7) No markdown.

Mapping rows JSON:
{json.dumps(mapping_rows[:3000])}
"""

    try:
        messages = []
        if agent_system_prompt:
            messages.append({"role": "system", "content": agent_system_prompt})
        messages.append({"role": "user", "content": prompt})
        call_args = _build_chat_call_args(messages, 0.1, 4000, provider, model)
        resp = _chat_completion_with_fallback(client, call_args, provider)
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        rules = json.loads(text)
        if not isinstance(rules, list):
            return {"error": "AI returned invalid rules payload"}

        sanitized: List[dict[str, Any]] = []
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            sanitized.append({
                "name": (rule.get("name") or f"AI Rule: {rule.get('source_table') or 'SOURCE'} → {target_table}")[:255],
                "source_datasource_id": int(rule.get("source_datasource_id") or source_datasource_id),
                "source_schema": rule.get("source_schema") or "",
                "source_table": rule.get("source_table") or "",
                "source_columns": rule.get("source_columns") or None,
                "target_datasource_id": int(rule.get("target_datasource_id") or target_datasource_id),
                "target_schema": rule.get("target_schema") or target_schema,
                "target_table": rule.get("target_table") or target_table,
                "target_columns": rule.get("target_columns") or None,
                "transformation_sql": rule.get("transformation_sql") or None,
                "join_condition": rule.get("join_condition") or None,
                "filter_condition": rule.get("filter_condition") or None,
                "rule_type": (rule.get("rule_type") or "direct"),
                "description": rule.get("description") or None,
            })

        if not sanitized:
            return {"error": "AI returned no usable mapping rules"}
        return {"rules": sanitized}
    except Exception as e:
        logger.exception("AI mapping rule generation failed")
        return {"error": _format_ai_error(e)}


async def ai_generate_tests_from_mapping_with_kb(
    mapping_rows: List[dict],
    kb_tests: List[dict],
    target_table: str = "",
    agent_system_prompt: str = "",
    provider_override: Optional[str] = None,
) -> dict:
    """Generate/refine mapping tests with AI using generator KB baseline tests as grounding."""
    provider = _normalize_provider(provider_override)
    client, model, cfg_error = _get_client_and_model(provider)
    if not client:
        return {"error": cfg_error}

    prompt = f"""You are a senior ETL test generation assistant.
Use the provided mapping rows and baseline generator tests (KB) to produce final test definitions.

Requirements:
1) Keep test definitions practical and execution-ready.
2) Preserve useful baseline tests; improve names/descriptions/queries where needed.
3) Include row-count, direct-mapping, and transformation/lookup tests when applicable.
4) Return STRICT JSON array only, no markdown.
5) Each test object should include keys:
   name, test_type, severity, source_query, target_query, description,
   source_schema, source_table, source_field, source_filter,
   target_schema, target_table, target_field, transformation_rule,
   mapping_type, expected_value, tolerance_percent.
6) Apply the Generator KB join/table-role rules exactly.

{GENERATOR_JOIN_KB}

Target table hint: {target_table}

Mapping rows (sample):
{json.dumps((mapping_rows or [])[:800])}

Baseline generator tests (KB):
{json.dumps((kb_tests or [])[:300])}

Local schema/LDM knowledge:
{_local_schema_kb_context()}
"""

    try:
        messages = []
        if agent_system_prompt:
            messages.append({"role": "system", "content": agent_system_prompt})
        messages.append({"role": "user", "content": prompt})
        call_args = _build_chat_call_args(messages, 0.1, 4000, provider, model)
        resp = _chat_completion_with_fallback(client, call_args, provider)
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        tests = json.loads(text)
        if not isinstance(tests, list):
            return {"error": "AI returned invalid tests payload"}
        return {"tests": tests}
    except Exception as e:
        logger.exception("AI mapping test generation failed")
        return {"error": _format_ai_error(e)}


def _is_complex_transformation(transformation: str) -> bool:
    t = (transformation or "").strip().lower()
    if not t:
        return False
    complex_markers = [
        "case ", "decode(", "join", "lookup", "substr", "trim(", "replace(",
        "coalesce(", "nvl(", "cast(", "when ", " then ", "else ", "*", "/", "+", "-",
    ]
    return any(marker in t for marker in complex_markers)


def _extract_schema_tables(sql_text: str) -> List[str]:
    text = sql_text or ""
    tables = set()
    cte_names = set()
    with_match = re.search(r'^\s*WITH\s+([\s\S]+?)\bSELECT\b', text, flags=re.IGNORECASE)
    if with_match:
        with_body = with_match.group(1)
        for name in re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\s+AS\s*\(', with_body, flags=re.IGNORECASE):
            cte_names.add(name.upper())

    pattern = re.compile(r'\b(?:from|join|into|update)\s+([A-Za-z0-9_\.\"]+)', re.IGNORECASE)
    for match in pattern.finditer(text):
        token = match.group(1).strip().strip('"')
        if token and token.upper() not in cte_names:
            tables.add(token.upper())
    return sorted(tables)


def _extract_main_joins(sql_text: str) -> List[dict]:
    text = sql_text or ""
    joins = []
    pattern = re.compile(
        r'\b(?:(left|right|inner|full)\s+)?join\s+([A-Za-z0-9_\.\"]+)\s*(?:\w+)?\s+on\s+(.+?)(?=\b(?:left|right|inner|full)\s+join\b|\bwhere\b|\bgroup\b|\border\b|\bhaving\b|\bfetch\b|$)',
        re.IGNORECASE | re.DOTALL,
    )
    for m in pattern.finditer(text):
        joins.append({
            "join_type": (m.group(1) or "INNER").upper(),
            "table": m.group(2).strip().strip('"').upper(),
            "condition": re.sub(r'\s+', ' ', (m.group(3) or '').strip()),
        })
        if len(joins) >= 25:
            break
    return joins


def _normalize_mapping_rows(mapping_rows: Optional[List[dict]], mapping_text: str = "") -> List[dict]:
    if mapping_rows:
        return mapping_rows

    text = (mapping_text or "").strip()
    if not text:
        return []

    rows = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    for ln in lines:
        parts = [p.strip() for p in re.split(r'\t+|\s{2,}|,(?=(?:[^"]*"[^"]*")*[^"]*$)', ln)]
        if len(parts) < 3:
            continue
        rows.append({
            "source_schema": parts[0] if len(parts) > 0 else "",
            "source_table": parts[1] if len(parts) > 1 else "",
            "source_attribute": parts[2] if len(parts) > 2 else "",
            "physical_name": parts[3] if len(parts) > 3 else "",
            "transformation": parts[4] if len(parts) > 4 else "",
        })
    return rows


def _heuristic_mapping_sql_compare(
    mapping_rows: List[dict],
    sql_text: str,
    source_table: str = "",
    target_table: str = "",
    single_db_testing: bool = True,
    cross_db_optional: bool = True,
) -> dict:
    sql_upper = (sql_text or "").upper()
    direct = []
    complex_rows = []
    comparisons = []

    for row in mapping_rows:
        src_attr = (row.get("source_attribute") or "").strip()
        tgt_attr = (row.get("physical_name") or row.get("logical_name") or "").strip()
        transformation = (row.get("transformation") or "").strip()
        mapping_type = "complex" if _is_complex_transformation(transformation) else "direct"

        field_found = False
        if src_attr and re.search(rf'\b{re.escape(src_attr.upper())}\b', sql_upper):
            field_found = True
        if tgt_attr and re.search(rf'\b{re.escape(tgt_attr.upper())}\b', sql_upper):
            field_found = True

        status = "matched" if field_found else "missing_in_sql"
        record = {
            "source_field": src_attr,
            "target_field": tgt_attr,
            "mapping_type": mapping_type,
            "transformation": transformation,
            "status": status,
        }
        comparisons.append(record)
        if mapping_type == "direct":
            direct.append(record)
        else:
            complex_rows.append(record)

    mismatches = [c for c in comparisons if c["status"] != "matched"]

    suggested_tests = []
    for row in comparisons[:100]:
        src = row.get("source_field") or "<source_field>"
        tgt = row.get("target_field") or "<target_field>"
        mapping_type = row.get("mapping_type")
        transformation = row.get("transformation") or "Direct"
        src_tbl = source_table or "<SOURCE_TABLE>"
        tgt_tbl = target_table or "<TARGET_TABLE>"
        source_query = (
            f'SELECT COUNT(*) AS total_cnt, COUNT(DISTINCT {src}) AS distinct_cnt, '
            f'SUM(CASE WHEN {src} IS NULL THEN 1 ELSE 0 END) AS null_cnt '
            f'FROM {src_tbl}'
        )
        target_query = (
            f'SELECT COUNT(*) AS total_cnt, COUNT(DISTINCT {tgt}) AS distinct_cnt, '
            f'SUM(CASE WHEN {tgt} IS NULL THEN 1 ELSE 0 END) AS null_cnt '
            f'FROM {tgt_tbl}'
        )
        suggested_tests.append({
            "name": f"Validate {src} → {tgt}",
            "test_type": "value_match",
            "field": tgt,
            "source_field": src,
            "mapping_type": mapping_type,
            "transformation": transformation,
            "validation_pattern": "source-lookup/transformation-target",
            "severity": "high" if mapping_type == "complex" else "medium",
            "source_query": source_query,
            "target_query": target_query,
            "description": f"Validate mapping field {src} to {tgt}. Transformation: {transformation}",
        })

    src_tbl = source_table or (mapping_rows[0].get("source_table") if mapping_rows else "") or "<SOURCE_TABLE>"
    tgt_tbl = target_table or "<TARGET_TABLE>"
    option_a = (
        f"SELECT s.*, t.* FROM {src_tbl} s "
        f"LEFT JOIN {tgt_tbl} t ON /* add business key join */ 1=1 FETCH FIRST 100 ROWS ONLY"
    )
    option_b = (
        f"SELECT /* projected mapping columns */ FROM {src_tbl} s "
        f"LEFT JOIN {tgt_tbl} t ON /* add business key join */ 1=1 WHERE 1=1 FETCH FIRST 100 ROWS ONLY"
    )

    return {
        "summary": (
            f"Compared {len(comparisons)} mapping row(s) against SQL. "
            f"Matched: {len(comparisons) - len(mismatches)}, Missing in SQL: {len(mismatches)}."
        ),
        "testing_strategy": {
            "single_db_default": bool(single_db_testing),
            "cross_db_optional": bool(cross_db_optional),
            "recommended_mode": "single_db" if single_db_testing else "cross_db",
        },
        "schema_tables": _extract_schema_tables(sql_text),
        "main_joins": _extract_main_joins(sql_text),
        "direct_mappings": direct,
        "complex_mappings": complex_rows,
        "field_comparison": comparisons,
        "mismatch_highlights": mismatches[:200],
        "suggested_tests": suggested_tests,
        "validation_sql_preview": {
            "option_a_same_joins_select_all": option_a,
            "option_b_projection_validation": option_b,
        },
    }


async def ai_compare_mapping_with_sql(
    mapping_rows: Optional[List[dict]],
    sql_text: str,
    source_table: str = "",
    target_table: str = "",
    single_db_testing: bool = True,
    cross_db_optional: bool = True,
    mapping_text: str = "",
    agent_system_prompt: str = "",
    provider_override: Optional[str] = None,
) -> dict:
    normalized_rows = _normalize_mapping_rows(mapping_rows, mapping_text)
    if not normalized_rows:
        return {"error": "No mapping rows provided. Upload/paste a mapping first."}

    heuristic = _heuristic_mapping_sql_compare(
        normalized_rows,
        sql_text,
        source_table=source_table,
        target_table=target_table,
        single_db_testing=single_db_testing,
        cross_db_optional=cross_db_optional,
    )

    provider = _normalize_provider(provider_override)
    client, model, cfg_error = _get_client_and_model(provider)
    if not client:
        heuristic["ai_note"] = f"AI unavailable, returned heuristic comparison: {cfg_error}"
        return heuristic

    prompt = f"""You are a senior ETL mapping QA analyst.
Compare mapping rows against SQL/ODI code and return STRICT JSON object with keys:
- summary
- testing_strategy (single_db_default, cross_db_optional, recommended_mode)
- main_joins (array of {{join_type, table, condition}})
- direct_mappings (array)
- complex_mappings (array)
- field_comparison (array of {{source_field,target_field,mapping_type,transformation,status,notes}})
- mismatch_highlights (array)
- validation_sql_preview (object with option_a_same_joins_select_all, option_b_projection_validation)

1) Use single DB testing as default; cross DB should be optional.
2) Categorize mapping rows into direct vs complex from transformation logic.
3) Pattern for each suggested test: Source-Lookup/Transformation-Target validation.
4) Highlight main joins and field-by-field mismatches between mapping and SQL.
5) Include field and transformation used for each suggested test.
    6) suggested_tests MUST include both source_query and target_query SQL text using valid Oracle SQL syntax.
    7) Apply the Generator KB join/table-role rules exactly.
    8) No markdown.
{GENERATOR_JOIN_KB}

Inputs:
source_table_hint={source_table}
target_table_hint={target_table}
single_db_testing={single_db_testing}
cross_db_optional={cross_db_optional}

Mapping rows (JSON):
{json.dumps(normalized_rows[:1500])}

SQL code:
{sql_text}

Local schema/LDM knowledge:
{_local_schema_kb_context()}
"""

    try:
        messages = []
        if agent_system_prompt:
            messages.append({"role": "system", "content": agent_system_prompt})
        messages.append({"role": "user", "content": prompt})
        call_args = _build_chat_call_args(messages, 0.1, 4000, provider, model)
        resp = _chat_completion_with_fallback(client, call_args, provider)
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        parsed = json.loads(text)
        parsed = _merge_ai_suggested_tests_with_heuristic(parsed, heuristic)
        parsed.setdefault("fallback_summary", heuristic.get("summary"))
        parsed.setdefault("ai_provider", provider)
        parsed.setdefault("baseline_total", len(heuristic.get("suggested_tests") or []))
        return parsed
    except Exception as e:
        logger.exception("AI mapping/SQL comparison failed; returning heuristic result")
        heuristic["ai_note"] = _format_ai_error(e)
        return heuristic
