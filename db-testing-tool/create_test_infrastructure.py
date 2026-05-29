#!/usr/bin/env python3
"""Create TFS test plan and generate local test suite for AVY_FACT_SIDE."""
import requests
import json
from pathlib import Path

# Read the generated SQL scripts
ddl_sql = Path('avyfactside_ddl.sql').read_text()
insert_sql = Path('avyfactside_insert.sql').read_text()
validation_sql = Path('avyfactside_validation.sql').read_text()

base_url = "http://127.0.0.1:8550/api/tests"

print("=" * 70)
print("STEP 1: Generate Test Suite with SQL Scripts")
print("=" * 70)

# Call the test suite generation endpoint
test_suite_url = f"{base_url}/test-suites/generate"
payload = {
    "target_table": "AVY_FACT_SIDE",
    "target_schema": "IKOROSTELEV",
    "ddl_sql": ddl_sql,
    "insert_sql": insert_sql,
    "validation_sql": validation_sql,
}

try:
    resp = requests.post(test_suite_url, data=payload, timeout=30)
    if resp.status_code == 200:
        result = resp.json()
        print(f"OK: Test suite created successfully")
        print(f"  Suite ID: {result.get('suite_id')}")
        print(f"  Suite Name: {result.get('suite_name')}")
        print(f"  Tests Created: {result.get('tests_created')}")
        suite_id = result.get('suite_id')
    else:
        print(f"ERROR: {resp.status_code}")
        print(f"  Response: {resp.text[:500]}")
        suite_id = None
except Exception as e:
    print(f"ERROR: {e}")
    suite_id = None

print("\n" + "=" * 70)
print("STEP 2: Create TFS Test Plan 'Test123' in Lighthouse")
print("=" * 70)

tfs_url = "http://127.0.0.1:8550/api/tfs/test-plans"
tfs_payload = {
    "name": "Test123",
    "project": "Lighthouse",
    "description": "AVY_FACT_SIDE control table test plan with 326-column DDL/INSERT/validation suite"
}

try:
    resp = requests.post(tfs_url, json=tfs_payload, timeout=30)
    if resp.status_code == 200:
        result = resp.json()
        if result.get('plan_id'):
            print(f"OK: TFS Test Plan created successfully")
            print(f"  Plan ID: {result.get('plan_id')}")
            print(f"  Plan Name: {result.get('plan_name')}")
            plan_id = result.get('plan_id')
        else:
            print(f"ERROR: Plan creation returned status but no plan_id")
            print(f"  Response: {json.dumps(result, indent=2)}")
            plan_id = None
    else:
        print(f"ERROR: {resp.status_code}")
        print(f"  Response: {resp.text[:500]}")
        plan_id = None
except Exception as e:
    print(f"ERROR: {e}")
    plan_id = None

if plan_id:
    print("\n" + "=" * 70)
    print("STEP 3: Create Test Suites (static + PBI requirement-based)")
    print("=" * 70)
    
    suite_url = "http://127.0.0.1:8550/api/tfs/test-suites"
    
    # Create static suite
    static_payload = {
        "project": "Lighthouse",
        "plan_id": plan_id,
        "name": "test statick",
        "suite_type": "StaticTestSuite"
    }
    
    static_suite_id = None
    try:
        resp = requests.post(suite_url, json=static_payload, timeout=30)
        if resp.status_code == 200:
            result = resp.json()
            static_suite_id = result.get('suite_id')
            print(f"OK: Static suite created")
            print(f"  Suite ID: {result.get('suite_id')}")
            print(f"  Suite Name: {result.get('name')}")
        else:
            print(f"ERROR creating static suite: {resp.status_code}")
            print(f"  Response: {resp.text[:500]}")
    except Exception as e:
        print(f"ERROR creating static suite: {e}")
    
    # Create PBI requirement-based suite
    pbi_payload = {
        "project": "Lighthouse",
        "plan_id": plan_id,
        "name": "PBI2674782_AVY_Coverage",
        "suite_type": "RequirementTestSuite",
        "requirement_id": 2674782
    }
    
    pbi_suite_id = None
    try:
        resp = requests.post(suite_url, json=pbi_payload, timeout=30)
        if resp.status_code == 200:
            result = resp.json()
            pbi_suite_id = result.get('suite_id')
            print(f"OK: PBI suite created")
            print(f"  Suite ID: {result.get('suite_id')}")
            print(f"  Suite Name: {result.get('name')}")
        else:
            print(f"ERROR creating PBI suite: {resp.status_code}")
            print(f"  Response: {resp.text[:500]}")
    except Exception as e:
        print(f"ERROR creating PBI suite: {e}")

print("\n" + "=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"Local Test Suite: {'OK' if suite_id else 'FAILED'}")
print(f"TFS Test Plan: {'OK' if plan_id else 'FAILED'}")
print(f"\nNext: Run pytest to validate, then commit")
