"""
GUI validation for ODI XML Logic display fix
Uses Selenium to interact with the web UI and capture screenshots
"""
import time
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

# Setup
base_url = "http://127.0.0.1:8550/mappings"  # Navigate to the mappings page
data_dir = Path(r"D:\test 2\db-test-tool-analysis\db-testing-tool\data\taxlot")
screenshot_dir = Path(r"D:\test 2\db-test-tool-analysis\db-testing-tool\screenshots")
screenshot_dir.mkdir(exist_ok=True)

drd_file = data_dir / "DRD_Activity_Fact.xlsx"
odi_file = data_dir / "1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml"

# Setup Chrome options
chrome_options = Options()
# chrome_options.add_argument("--headless")  # Comment out to see the browser
chrome_options.add_argument("--window-size=1920,1080")

print("Starting browser...")
driver = webdriver.Chrome(options=chrome_options)

try:
    print(f"Opening {base_url}...")
    driver.get(base_url)
    
    # Wait for page to load
    time.sleep(2)
    
    # Take initial screenshot
    screenshot_path = screenshot_dir / "01_initial_page.png"
    driver.save_screenshot(str(screenshot_path))
    print(f"Screenshot saved: {screenshot_path}")
    
    # Check page title and URL
    print(f"Page title: {driver.title}")
    print(f"Current URL: {driver.current_url}")
    
    # Try to find the ODI validation card
    print("\nLooking for ODI vs DRD Validation panel...")
    
    # Method 1: Try by ID
    try:
        odi_card = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.ID, "odi-val-card"))
        )
        print("✓ Found ODI validation card by ID")
        driver.execute_script("arguments[0].scrollIntoView(true);", odi_card)
        time.sleep(1)
    except Exception as e:
        print(f"✗ Could not find card by ID: {e}")
        
        # Method 2: Try by text
        try:
            odi_heading = driver.find_element(By.XPATH, "//*[contains(text(), 'ODI vs DRD')]")
            print("✓ Found ODI heading by text")
            driver.execute_script("arguments[0].scrollIntoView(true);", odi_heading)
            time.sleep(1)
        except Exception as e2:
            print(f"✗ Could not find by text either: {e2}")
            
            # Just scroll down to see what's on the page
            print("Scrolling down to explore page...")
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight/2);")
            time.sleep(1)
    
    # Take screenshot after scroll
    screenshot_path = screenshot_dir / "02_after_scroll.png"
    driver.save_screenshot(str(screenshot_path))
    print(f"Screenshot saved: {screenshot_path}")
    
    # Try to find file upload inputs
    print("\nLooking for ODI vs DRD Validation file inputs...")
    
    try:
        # Upload DRD file to odi-drd-file
        drd_input = driver.find_element(By.ID, "odi-drd-file")
        drd_input.send_keys(str(drd_file.absolute()))
        print(f"✓ Uploaded DRD file: {drd_file.name}")
        time.sleep(1)
        
        # Upload ODI XML file to odi-xml-file  
        odi_input = driver.find_element(By.ID, "odi-xml-file")
        odi_input.send_keys(str(odi_file.absolute()))
        print(f"✓ Uploaded ODI file: {odi_file.name}")
        time.sleep(1)
        
        # Take screenshot after file upload
        screenshot_path = screenshot_dir / "03_files_uploaded.png"
        driver.save_screenshot(str(screenshot_path))
        print(f"Screenshot saved: {screenshot_path}")
        
        # Click the "Compare v16.6" button
        print("\nClicking 'Compare v16.6' button...")
        compare_btn = driver.find_element(By.ID, "odi-v16-btn")
        
        # Scroll to button
        driver.execute_script("arguments[0].scrollIntoView(true);", compare_btn)
        time.sleep(1)
        
        # Click using JavaScript to bypass interactability issues
        driver.execute_script("arguments[0].click();", compare_btn)
        print("✓ Clicked Compare v16.6 button")
        
        # Wait for results to load (increase timeout for processing)
        print("Waiting for results to load...")
        time.sleep(10)  # Increased wait time for processing
        
        # Take screenshot of results
        screenshot_path = screenshot_dir / "04_results.png"
        driver.save_screenshot(str(screenshot_path))
        print(f"Screenshot saved: {screenshot_path}")
        
        # Look for the v16 results div - it might be in a different location
        print("\nLooking for v16 results...")
        
        # Try to find v16 results display elements
        possible_result_ids = ['odi-v16-results', 'odi-results', 'odi-val-body']
        results_element = None
        
        for result_id in possible_result_ids:
            try:
                elem = driver.find_element(By.ID, result_id)
                print(f"✓ Found results element with ID: {result_id}")
                results_element = elem
                break
            except:
                print(f"✗ No element with ID: {result_id}")
        
        if not results_element:
            print("Trying to find results by text content...")
            # Just get the whole page body
            results_element = driver.find_element(By.TAG_NAME, "body")
        
        results_html = results_element.get_attribute('innerHTML')
        
        print(f"\nResults element found, content length: {len(results_html)} chars")
        print(f"Results HTML preview (first 1000 chars):\n{results_html[:1000]}\n")
        
        
        # Save full HTML to file for inspection
        with open(screenshot_dir / "results_html.txt", "w", encoding="utf-8") as f:
            f.write(results_html)
        print(f"Full results HTML saved to: {screenshot_dir / 'results_html.txt'}")
        
        # Try to find SDIRA row
        print("\nLooking for SDIRA row...")
        time.sleep(3)
        
        # Look for cells containing SDIRA text (case insensitive)  
        sdira_cells = driver.find_elements(By.XPATH, "//*[contains(translate(text(), 'SDIRA', 'sdira'), 'sdira')]")
        
        # Also try looking in the entire page
        if not sdira_cells:
            print("Trying broader search in entire page...")
            all_text = driver.find_element(By.TAG_NAME, "body").text
            print(f"Page text length: {len(all_text)} chars")
            
            # Check if SDIRA appears anywhere
            if 'SDIRA' in all_text or 'sdira' in all_text.lower():
                print("✓ SDIRA text found on page!")
                
                # Save page text to file
                with open(screenshot_dir / "page_text.txt", "w", encoding="utf-8") as f:
                    f.write(all_text)
                print(f"Full page text saved to: {screenshot_dir / 'page_text.txt'}")
                
                # Try to find it in any element  
                elements_with_sdira = driver.find_elements(By.XPATH, "//*[contains(., 'SDIRA')]")
                print(f"Found {len(elements_with_sdira)} elements containing SDIRA")
                
                if elements_with_sdira:
                    sdira_cells = elements_with_sdira[:5]  # Take first 5
            else:
                print("✗ SDIRA text not found anywhere on page")
                print(f"\nPage text preview (first 3000 chars):\n{all_text[:3000]}")
        
        if sdira_cells:
            print(f"✓ Found {len(sdira_cells)} cell(s) containing 'SDIRA'")
            
            # Scroll to first SDIRA cell
            first_sdira = sdira_cells[0]
            driver.execute_script("arguments[0].scrollIntoView(true);", first_sdira)
            time.sleep(1)
            
            # Take screenshot of SDIRA area
            screenshot_path = screenshot_dir / "05_sdira_row.png"
            driver.save_screenshot(str(screenshot_path))
            print(f"Screenshot saved: {screenshot_path}")
            
            # Get the row
            row = first_sdira.find_element(By.XPATH, "./ancestor::tr")
            cells = row.find_elements(By.TAG_NAME, "td")
            
            print(f"\nSDIRA Row Details:")
            print(f"Number of cells in row: {len(cells)}")
            
            for i, cell in enumerate(cells):
                text = cell.text.strip()
                if text:
                    preview = text[:200] if len(text) > 200 else text
                    print(f"  Cell {i}: {preview}")
                    
                    # Check if this looks like the ODI Logic cell
                    if 'insert' in text.lower() or 'select' in text.lower():
                        print(f"\n=== FOUND POTENTIAL ODI XML LOGIC IN CELL {i} ===")
                        print(f"Content (first 800 chars):")
                        print(text[:800])
                        
                        # Analysis
                        has_insert = 'insert' in text.lower()
                        has_columns = '(' in text and ',' in text
                        is_bare_alias = len(text) < 50 and '.' in text
                        
                        print(f"\n=== ANALYSIS ===")
                        print(f"Has INSERT statement: {has_insert}")
                        print(f"Has column lists: {has_columns}")
                        print(f"Appears to be bare alias: {is_bare_alias}")
                        
                        if has_insert and has_columns and not is_bare_alias:
                            print("✓✓✓ BUG FIX VERIFIED: ODI XML Logic shows detailed INSERT statement")
                        elif is_bare_alias:
                            print("✗✗✗ BUG STILL PRESENT: ODI XML Logic is still a bare alias")
                        else:
                            print("? UNCLEAR: ODI XML Logic has content but unclear if fix is complete")
        else:
            print("✗ No SDIRA cells found in results")
            
        # Final full page screenshot
        screenshot_path = screenshot_dir / "06_final.png"
        driver.save_screenshot(str(screenshot_path))
        print(f"Screenshot saved: {screenshot_path}")
        
    except Exception as e:
        print(f"✗ Error during file upload/compare: {e}")
        import traceback
        traceback.print_exc()
        
        # Take error screenshot
        screenshot_path = screenshot_dir / "99_error.png"
        driver.save_screenshot(str(screenshot_path))
        print(f"Error screenshot saved: {screenshot_path}")

finally:
    print("\nClosing browser...")
    driver.quit()
    
    print(f"\n{'='*60}")
    print("GUI VALIDATION COMPLETE")
    print(f"Screenshots saved to: {screenshot_dir}")
    print(f"{'='*60}")
