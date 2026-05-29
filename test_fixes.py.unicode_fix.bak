"""Quick test to verify template download and auto-detect fixes."""
import requests
import json
from pathlib import Path

BASE_URL = "http://127.0.0.1:8550"

def test_download_templates():
    """Test template download endpoint."""
    print("Testing template downloads...")
    
    template_types = ["basic", "enterprise", "data_warehouse", "etl_pipeline"]
    
    for template_type in template_types:
        url = f"{BASE_URL}/download/template/{template_type}"
        print(f"  Downloading {template_type}...")
        
        try:
            response = requests.get(url, timeout=10)
            if response.status_code == 200:
                print(f"    ✓ {template_type} download successful ({len(response.content)} bytes)")
            else:
                print(f"    ✗ {template_type} download failed: {response.status_code}")
                print(f"      Response: {response.text}")
        except Exception as e:
            print(f"    ✗ {template_type} download error: {e}")
    
    print()


def test_auto_detect():
    """Test auto-detect with basic template headers."""
    print("Testing auto-detect with basic template headers...")
    
    # Simulate basic template headers
    test_cases = [
        {
            "name": "Basic template - all required columns",
            "headers": ["Rule Name", "Source Table", "Target Table", "Transformation SQL Query", "Description"],
            "expected": "basic"
        },
        {
            "name": "Basic template - with aliases",
            "headers": ["name", "source table", "target table", "sql query", "desc"],
            "expected": "basic"
        },
        {
            "name": "Enterprise template - key columns",
            "headers": ["Rule Name", "Logical Name", "Physical Name", "Source Datasource ID", "Source Table", "Target Datasource ID", "Target Table"],
            "expected": "enterprise"
        }
    ]
    
    # Test import directly
    from app.services.template_manager import template_manager
    
    for test_case in test_cases:
        print(f"\n  Test: {test_case['name']}")
        print(f"    Headers: {', '.join(test_case['headers'])}")
        
        headers_lower = [h.lower().strip() for h in test_case['headers']]
        detected = template_manager.detect_template_type(test_case['headers'])
        
        if detected:
            print(f"    ✓ Detected: {detected.value}")
            if detected.value == test_case['expected']:
                print(f"      ✓ Matches expected: {test_case['expected']}")
            else:
                print(f"      ✗ Expected {test_case['expected']}, got {detected.value}")
        else:
            print(f"    ✗ Failed to detect template")
        
        # Show scores for debugging
        scores = {}
        for template_type, template in template_manager._templates.items():
            score = 0
            total_required = 0
            required_matches = 0
            
            for col in template.columns:
                if col.required:
                    total_required += 1
                
                col_matches = [col.name.lower()]
                col_matches.extend([alias.lower() for alias in col.aliases])
                
                if any(match in headers_lower for match in col_matches):
                    if col.required:
                        required_matches += 1
                        score += 3
                    else:
                        score += 1
            
            if total_required > 0:
                missing_required = total_required - required_matches
                final_score = score - (missing_required * 2)
                scores[template_type.value] = max(0, final_score)
            else:
                scores[template_type.value] = score
        
        print(f"    Scores: {json.dumps(scores, indent=6)}")


if __name__ == "__main__":
    print("=" * 60)
    print("Testing Template Download and Auto-Detect Fixes")
    print("=" * 60)
    print()
    
    # Test downloads
    test_download_templates()
    
    # Test auto-detect
    test_auto_detect()
    
    print()
    print("=" * 60)
    print("Test complete")
    print("=" * 60)
