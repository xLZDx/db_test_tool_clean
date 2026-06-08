"""
Final validation - extract and display SDIRA row ODI XML Logic content
"""
import time
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options

# Setup
base_url = "http://127.0.0.1:8550/mappings"
data_dir = Path(r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot")
screenshot_dir = Path(r"D:\test 2\db-test-tool-analysis\db-testing-tool\screenshots")

drd_file = data_dir / "DRD_Activity_Fact.xlsx"
odi_file = data_dir / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"

chrome_options = Options()
chrome_options.add_argument("--window-size=1920,1080")

driver = webdriver.Chrome(options=chrome_options)

try:
    driver.get(base_url)
    time.sleep(2)
    
    # Scroll to ODI card
    odi_card = driver.find_element(By.ID, "odi-val-card")
    driver.execute_script("arguments[0].scrollIntoView(true);", odi_card)
    time.sleep(1)
    
    # Upload files
    drd_input = driver.find_element(By.ID, "odi-drd-file")
    drd_input.send_keys(str(drd_file.absolute()))
    
    odi_input = driver.find_element(By.ID, "odi-xml-file")
    odi_input.send_keys(str(odi_file.absolute()))
    time.sleep(1)
    
    # Click Compare v16.6
    compare_btn = driver.find_element(By.ID, "odi-v16-btn")
    driver.execute_script("arguments[0].scrollIntoView(true);", compare_btn)
    time.sleep(1)
    driver.execute_script("arguments[0].click();", compare_btn)
    
    # Wait for results - look for the "Analyzing" message to disappear
    print("Waiting for analysis to complete...")
    max_wait = 60  # Wait up to 60 seconds
    waited = 0
    while waited < max_wait:
        time.sleep(2)
        waited += 2
        
        # Check if still analyzing
        page_text = driver.find_element(By.TAG_NAME, "body").text
        if 'Analyzing ODI XML' not in page_text:
            print(f"Analysis complete after {waited} seconds")
            break
        else:
            print(f"Still analyzing... ({waited}s elapsed)")
    
    # Give it a bit more time to render results
    time.sleep(5)
    
    # Save full page source to file
    page_source = driver.page_source
    with open(screenshot_dir / "full_page_source.html", "w", encoding="utf-8") as f:
        f.write(page_source)
    print(f"Page source saved (length: {len(page_source)} chars)")
    
    # Get all visible text
    body = driver.find_element(By.TAG_NAME, "body")
    all_text = body.text
    with open(screenshot_dir / "all_page_text.txt", "w", encoding="utf-8") as f:
        f.write(all_text)
    print(f"All page text saved (length: {len(all_text)} chars)")
    
    # Check if SDIRA appears anywhere
    sdira_count = all_text.count('SDIRA')
    print(f"\n'SDIRA' appears {sdira_count} times in page text")
    
    if sdira_count > 0:
        # Find where it appears
        lines = all_text.split('\n')
        for i, line in enumerate(lines):
            if 'SDIRA' in line:
                print(f"\nLine {i}: {line[:200]}")
                if i < len(lines) - 1:
                    print(f"Next line: {lines[i+1][:200]}")
    
    # Try to find table rows
    print("\nSearching for SDIRA in table rows...")
    all_rows = driver.find_elements(By.TAG_NAME, "tr")
    print(f"Found {len(all_rows)} total rows on page")
    
    sdira_rows = []
    for row in all_rows:
        row_text = row.text
        if 'SDIRA' in row_text:
            sdira_rows.append(row)
            print(f"\nFound SDIRA row:")
            print(f"Row text: {row_text[:500]}")
    
    print(f"\n{len(sdira_rows)} rows containing SDIRA")
    
    if sdira_rows:
        print("\n" + "="*80)
        print("DETAILED ANALYSIS OF FIRST SDIRA ROW")
        print("="*80)
        
        first_row = sdira_rows[0]
        cells = first_row.find_elements(By.TAG_NAME, "td")
        
        print(f"\nRow has {len(cells)} cells\n")
        
        for i, cell in enumerate(cells):
            text = cell.text.strip()
            print(f"\n--- Cell {i} ---")
            if len(text) > 0:
                if len(text) > 1000:
                    print(f"Length: {len(text)} chars")
                    print(f"Preview (first 1000 chars):")
                    print(text[:1000])
                    print(f"\n... (truncated, total {len(text)} chars)")
                else:
                    print(text)
                
                # Check for key indicators
                if i >= 3:  # ODI Logic column is typically later
                    has_insert = 'insert' in text.lower() or 'INSERT' in text
                    has_select = 'select' in text.lower() or 'SELECT' in text
                    has_columns = '(' in text and ',' in text
                    is_short = len(text) < 100
                    has_dot = '.' in text
                    
                    if has_insert or has_select:
                        print(f"\n>>> POTENTIAL ODI XML LOGIC CELL <<<")
                        print(f"Has INSERT: {has_insert}")
                        print(f"Has SELECT: {has_select}")
                        print(f"Has column lists: {has_columns}")
                        print(f"Is short/bare alias: {is_short}")
                        
                        if (has_insert or has_select) and has_columns and not is_short:
                            print("\n✓✓✓ BUG FIX CONFIRMED: Informative SQL logic with column details!")
                        elif is_short and has_dot:
                            print("\n✗✗✗ BUG STILL PRESENT: Bare alias pattern detected")
            else:
                print("(empty)")
        
        # Take final screenshot
        driver.execute_script("arguments[0].scrollIntoView(true);", first_row)
        time.sleep(1)
        screenshot_path = screenshot_dir / "final_sdira_detail.png"
        driver.save_screenshot(str(screenshot_path))
        print(f"\nFinal screenshot saved: {screenshot_path}")
    else:
        print("\n✗ No SDIRA rows found")
        screenshot_path = screenshot_dir / "no_sdira_found.png"
        driver.save_screenshot(str(screenshot_path))

finally:
    driver.quit()
    print("\n" + "="*80)
    print("VALIDATION COMPLETE")
    print("="*80)
