"""Integration tests for /api/odi/scenario/* endpoints.

Uses the real ODI XML fixture when present; skipped in CI without it.
These tests verify the full stack: parse -> emit -> response shape.
"""
from __future__ import annotations

import io
import pathlib
import pytest

_XML_PATH = pathlib.Path(__file__).parent.parent / (
    "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"
)

pytestmark = pytest.mark.skipif(
    not _XML_PATH.exists(),
    reason="ODI XML fixture not present",
)

import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest.fixture(scope="module")
def xml_bytes() -> bytes:
    return _XML_PATH.read_bytes()


@pytest.fixture(scope="module")
def app():
    from app.main import app as _app
    return _app


@pytest.mark.asyncio
async def test_parse_endpoint_returns_sql(app, xml_bytes):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/odi/scenario/parse",
            files={"xml_file": ("scenario.xml", xml_bytes, "application/xml")},
            params={"strict": "false"},
        )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "sql" in data
    assert data["sql"].strip().startswith("--") or "WITH" in data["sql"] or "INSERT" in data["sql"]
    assert data["model_summary"]["step_count"] == 5
    assert data["model_summary"]["final_column_count"] > 50


@pytest.mark.asyncio
async def test_parse_endpoint_steps_have_source_tables(app, xml_bytes):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/odi/scenario/parse",
            files={"xml_file": ("scenario.xml", xml_bytes, "application/xml")},
        )
    assert resp.status_code == 200
    data = resp.json()
    step1 = next((s for s in data["steps"] if s["step_id"] == 1), None)
    assert step1 is not None
    assert step1["column_count"] > 10
    assert len(step1["source_tables"]) > 5


@pytest.mark.asyncio
async def test_emit_sql_endpoint_returns_plain_sql(app, xml_bytes):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/odi/scenario/emit-sql",
            files={"xml_file": ("scenario.xml", xml_bytes, "application/xml")},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "sql" in data
    assert "unresolved_count" in data
    assert "status" in data
    assert data["status"] in ("OK", "PARTIAL")


@pytest.mark.asyncio
async def test_compare_endpoint_without_drd_returns_sql_only(app, xml_bytes):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/odi/scenario/compare",
            files={"xml_file": ("scenario.xml", xml_bytes, "application/xml")},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["comparison"] is None
    assert "sql" in data
    assert "No DRD file" in (data.get("note") or "")


@pytest.mark.asyncio
async def test_parse_endpoint_invalid_xml_returns_422(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/odi/scenario/parse",
            files={"xml_file": ("bad.xml", b"NOT_XML_AT_ALL", "application/xml")},
        )
    # Either 422 (parse error) or 500; must not be 200 with fake SQL
    assert resp.status_code in (422, 500)


@pytest.mark.asyncio
async def test_parse_endpoint_upload_too_large(app):
    large = b"x" * (21 * 1024 * 1024)  # 21 MB
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post(
            "/api/odi/scenario/parse",
            files={"xml_file": ("big.xml", large, "application/xml")},
        )
    assert resp.status_code == 413
