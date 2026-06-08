"""
Script to capture screenshots of ODI XML Logic trace for ACAT_CNTRA_FIRM_* and BATCH_DT columns.
Run v16 compare workflow and capture specific row screenshots.
"""
import asyncio
import time
from pathlib import Path
from playwright.async_api import async_playwright

async def run_v16_compare_and_capture():
    """Run v16 compare workflow and capture screenshots."""
    # File paths
    drd_file = Path("D:/test 2/db-test-tool-analysis/db-testing-tool/DRD_Activity_Fact.xlsx")
    odi_file = Path("D:/test 2/db-test-tool-analysis/db-testing-tool/1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml")
    
    screenshot_dir = Path("D:/test 2/db-test-tool-analysis/db-testing-tool/screenshots")
    screenshot_dir.mkdir(exist_ok=True)
    
    acat_screenshot = screenshot_dir / "after_acat_trace_fix.png"
    batchdt_screenshot = screenshot_dir / "after_batchdt_trace_fix.png"
    
    results = {
        "acat_found": False,
        "batchdt_found": False,
        "acat_path": str(acat_screenshot),
        "batchdt_path": str(batchdt_screenshot)
    }
    
    async with async_playwright() as p:
        # Launch browser
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        
        try:
            # Navigate to mappings page
            print("Opening http://127.0.0.1:8550/mappings")
            await page.goto("http://127.0.0.1:8550/mappings", wait_until="networkidle")
            await page.wait_for_timeout(1000)
            
            # Upload DRD file
            print(f"Uploading DRD file: {drd_file}")
            drd_input = page.locator('input[type="file"]').first
            await drd_input.set_input_files(str(drd_file))
            await page.wait_for_timeout(500)
            
            # Upload ODI XML file
            print(f"Uploading ODI file: {odi_file}")
            odi_input = page.locator('input[type="file"]').nth(1)
            await odi_input.set_input_files(str(odi_file))
            await page.wait_for_timeout(500)
            
            # Click compare button
            print("Clicking compare v16.6 button")
            compare_button = page.locator('button#odi-v16-btn')
            await compare_button.click()
            
            # Wait for results to load
            print("Waiting for results...")
            await page.wait_for_timeout(5000)
            
            # Take a full page screenshot to debug
            debug_screenshot = screenshot_dir / "debug_full_page.png"
            await page.screenshot(path=str(debug_screenshot), full_page=True)
            print(f"Debug screenshot saved: {debug_screenshot}")
            
            # Look for ACAT_CNTRA_FIRM_* rows in the entire page content
            print("Searching for ACAT_CNTRA_FIRM_* text in page...")
            
            # Try different selectors
            acat_rows = page.locator('text=/ACAT_CNTRA_FIRM/i')
            acat_count = await acat_rows.count()
            
            if acat_count > 0:
                print(f"Found {acat_count} ACAT_CNTRA_FIRM_* elements")
                results["acat_found"] = True
                
                # Get the first matching element and find its containing row
                first_acat = acat_rows.first
                
                # Get the closest ancestor tr element
                containing_row = page.locator('tr').filter(has_text='ACAT_CNTRA_FIRM').first
                
                # Take screenshot of the row
                try:
                    await containing_row.screenshot(path=str(acat_screenshot))
                    print(f"Screenshot saved: {acat_screenshot}")
                except Exception as e:
                    print(f"Error with row screenshot: {e}")
                    # Fallback: get bounding box and capture that area
                    box = await first_acat.bounding_box()
                    if box:
                        await page.screenshot(
                            path=str(acat_screenshot),
                            clip={
                                "x": 0,
                                "y": max(0, box["y"] - 50),
                                "width": 1920,
                                "height": min(200, box["height"] + 100)
                            }
                        )
                        print(f"Screenshot saved (bounding box): {acat_screenshot}")
            else:
                print("WARNING: No ACAT_CNTRA_FIRM_* text found")
            
            # Look for BATCH_DT
            print("Searching for BATCH_DT text in page...")
            batchdt_rows = page.locator('text=/BATCH_DT/i')
            batchdt_count = await batchdt_rows.count()
            
            if batchdt_count > 0:
                print(f"Found {batchdt_count} BATCH_DT elements")
                results["batchdt_found"] = True
                
                first_batchdt = batchdt_rows.first
                containing_row = page.locator('tr').filter(has_text='BATCH_DT').first
                
                try:
                    await containing_row.screenshot(path=str(batchdt_screenshot))
                    print(f"Screenshot saved: {batchdt_screenshot}")
                except Exception as e:
                    print(f"Error with row screenshot: {e}")
                    box = await first_batchdt.bounding_box()
                    if box:
                        await page.screenshot(
                            path=str(batchdt_screenshot),
                            clip={
                                "x": 0,
                                "y": max(0, box["y"] - 50),
                                "width": 1920,
                                "height": min(200, box["height"] + 100)
                            }
                        )
                        print(f"Screenshot saved (bounding box): {batchdt_screenshot}")
            else:
                print("WARNING: No BATCH_DT text found")
            
            # Keep browser open for a moment
            await page.wait_for_timeout(2000)
            
        finally:
            await browser.close()
    
    return results

if __name__ == "__main__":
    results = asyncio.run(run_v16_compare_and_capture())
    print("\n=== Results ===")
    print(f"ACAT_CNTRA_FIRM_* found: {results['acat_found']}")
    print(f"ACAT screenshot path: {results['acat_path']}")
    print(f"BATCH_DT found: {results['batchdt_found']}")
    print(f"BATCH_DT screenshot path: {results['batchdt_path']}")
