"""
Full end-to-end test of 4-phase agentic orchestration:
1. Context Builder → TfsContext + schema KB
2. Analysis Agent → EtlMappingSpec
3. Design Agent → TestCaseDesign array with SQL
4. Validation Agent → Oracle dry-run + auto-repair
"""
import asyncio
import sys
from pathlib import Path

# Add app to path
sys.path.insert(0, str(Path(__file__).parent / "app"))

from models.agent_contracts import TfsContext, EtlMappingSpec, TestCaseDesign
from services.ai_service import orchestrate_test_generation
from models.db_dialects import DbDialect, DIALECTS

async def test_full_orchestration():
    """Test the complete 4-agent pipeline with Oracle validation."""
    print("\n" + "="*80)
    print("FULL E2E ORCHESTRATION TEST (4-PHASE PIPELINE)")
    print("="*80)
    
    # Test the deterministic trailer pattern request
    user_prompt = """
    PBI 1736268: Generate test cases for trailer-pattern subtype assignment.
    Ensure that the ETL correctly assigns trailer patterns to subtypes based on:
    1. Trailer type (flat, drop-deck, etc.)
    2. Product category (equity, fixed-income, etc.)
    3. Market maker status
    4. Location routing
    """
    
    print(f"\n[PHASE 1] Context Builder Input:")
    print(f"  Work Item: 1736268")
    print(f"  Dialect: Oracle")
    print(f"  Request: {user_prompt[:80]}...")
    
    try:
        # Phase 1-4: Run the full orchestrator
        print(f"\n[PHASE 2] Analysis Agent → EtlMappingSpec")
        print(f"  Status: Analyzing ETL requirements...")
        
        print(f"\n[PHASE 3] Design Agent → TestCaseDesign[] with Oracle SQL")
        print(f"  Status: Generating deterministic tests with dialect rules...")
        
        print(f"\n[PHASE 4] Validation Agent → Oracle EXPLAIN PLAN Dry-Run + Auto-Repair")
        print(f"  Status: Running orchestrator with validation_datasource_id=2...")
        
        # Run the full orchestrator
        tests = await orchestrate_test_generation(
            tfs_item_id="1736268",
            artifact_contents=[],
            user_prompt=user_prompt,
            db_dialect="oracle",
            validation_datasource_id=2  # Oracle datasource
        )
        
        print(f"\n✅ ORCHESTRATION COMPLETE")
        print(f"\n📊 Generated Test Suite:")
        print(f"  Test Count: {len(tests)}")
        print(f"  Dialect: Oracle")
        
        for idx, test in enumerate(tests, start=1):
            print(f"\n  Test {idx}: {test.name}")
            print(f"    Type: {test.test_type}")
            print(f"    Severity: {test.severity}")
            print(f"    Description: {test.description[:80]}...")
            
            # Check source query is valid
            if test.source_query:
                # Ensure no hardcoded invalid column refs
                if "src.TD" in test.source_query and "AS TD" not in test.source_query:
                    print(f"    ❌ Invalid column reference: src.TD (not aliased correctly)")
                    return False
                print(f"    ✓ Source query: {len(test.source_query)} chars")
            
            if test.target_query:
                print(f"    ✓ Target query: {len(test.target_query)} chars")
        
        print(f"\n✅ ALL PHASES COMPLETED SUCCESSFULLY")
        return True
        
    except Exception as e:
        print(f"\n❌ ORCHESTRATION FAILED")
        print(f"Error: {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    result = asyncio.run(test_full_orchestration())
    sys.exit(0 if result else 1)
