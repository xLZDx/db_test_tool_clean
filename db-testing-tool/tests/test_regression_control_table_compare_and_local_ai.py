from app.routers.tests_control_table import _compare_doc_pair, _extract_doc_mappings
from app.services.ai_service import _local_assistant_reply, _normalize_provider


def test_normalize_provider_maps_local_aliases():
    assert _normalize_provider("local") == "local"
    assert _normalize_provider("internal") == "local"
    assert _normalize_provider("local_agent") == "local"


def test_local_assistant_reply_returns_oracle_guidance():
    reply = _local_assistant_reply(
        [{"role": "user", "content": "ORA-00904 invalid identifier in query"}],
        context="",
        agent_system_prompt="local helper",
    )
    upper = reply.upper()
    assert "ORA-00904" in upper
    assert "INVALID IDENTIFIER" in upper


def test_extract_doc_mappings_supports_sql_and_odi_patterns():
    sql_text = "SELECT SRC.COL_A AS TARGET_A, TRIM(SRC.COL_B) AS TARGET_B FROM X"
    xml_text = '<map target="TARGET_A" source="SRC.COL_A" />\n<targetColumn>TARGET_C</targetColumn><sourceExpression>SRC.COL_C</sourceExpression>'

    sql_map = _extract_doc_mappings(sql_text)
    xml_map = _extract_doc_mappings(xml_text)

    assert "TARGET_A" in sql_map["by_target"]
    assert "TARGET_B" in sql_map["by_target"]
    assert "TARGET_A" in xml_map["by_target"]
    assert "TARGET_C" in xml_map["by_target"]


def test_compare_doc_pair_detects_conflicts():
    left = {"TARGET_A": ["SRC.COL_A"], "TARGET_B": ["SRC.COL_B"]}
    right = {"TARGET_A": ["SRC.COL_A"], "TARGET_B": ["TRIM(SRC.COL_B)"]}

    result = _compare_doc_pair("DRD", left, "ODI", right)

    assert result["common_count"] == 2
    assert result["conflict_count"] == 1
    assert result["expression_conflicts"][0]["target"] == "TARGET_B"
