"""
Script to capture screenshots with accordion expansion for ACAT and BATCH_DT rows.
"""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

async def expand_and_capture():
    """Expand accordion rows and capture screenshots."""
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
        browser = await p.chromium.launch(headless=False, slow_mo=100)
        context = await browser.new_context(viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        
        try:
            # Navigate and upload
            print("Opening http://127.0.0.1:8550/mappings")
            await page.goto("http://127.0.0.1:8550/mappings", wait_until="networkidle")
            await page.wait_for_timeout(1000)
            
            print(f"Uploading files...")
            await page.locator('input[type="file"]').first.set_input_files(str(drd_file))
            await page.wait_for_timeout(500)
            await page.locator('input[type="file"]').nth(1).set_input_files(str(odi_file))
            await page.wait_for_timeout(500)
            
            print("Clicking compare v16.6 button")
            await page.locator('button#odi-v16-btn').click()
            await page.wait_for_timeout(5000)
            
            # Look for ACAT_CNTRA_FIRM in page
            print("Searching for ACAT_CNTRA_FIRM rows...")
            
            # Find all folder rows that might contain ACAT
            folder_rows = page.locator('.folder-row-folder_4')
            count = await folder_rows.count()
            print(f"Found {count} folder rows")
            
            for i in range(count):
                row = folder_rows.nth(i)
                text = await row.text_content()
                
                if 'ACAT_CNTRA_FIRM' in text:
                    print(f"Found ACAT_CNTRA_FIRM in row {i}: {text[:100]}")
                    results["acat_found"] = True
                    
                    # Try to click to expand
                    try:
                        await row.click()
                        await page.wait_for_timeout(1000)
                        
                        # Find the expanded content
                        # Look for the next sibling or child elements
                        expanded_content = page.locator(f'tr.folder-row-folder_4:nth-child({i+1}) ~ tr').first
                        
                        # Take a wider screenshot of the expanded area
                        await page.screenshot(path=str(acat_screenshot))
                        print(f"Screenshot saved: {acat_screenshot}")
                        break
                    except Exception as e:
                        print(f"Error expanding/capturing ACAT row: {e}")
                        # Fallback: just capture viewport
                        await page.screenshot(path=str(acat_screenshot))
                        print(f"Fallback screenshot saved: {acat_screenshot}")
                        break
            
            # Look for BATCH_DT
            print("Searching for BATCH_DT rows...")
            
            for i in range(count):
                row = folder_rows.nth(i)
                text = await row.text_content()
                
                if 'BATCH_DT' in text:
                    print(f"Found BATCH_DT in row {i}: {text[:100]}")
                    results["batchdt_found"] = True
                    
                    try:
                        await row.click()
                        await page.wait_for_timeout(1000)
                        
                        await page.screenshot(path=str(batchdt_screenshot))
                        print(f"Screenshot saved: {batchdt_screenshot}")
                        break
                    except Exception as e:
                        print(f"Error expanding/capturing BATCH_DT row: {e}")
                        await page.screenshot(path=str(batchdt_screenshot))
                        print(f"Fallback screenshot saved: {batchdt_screenshot}")
                        break
            
            await page.wait_for_timeout(2000)
            
        finally:
            await browser.close()
    
    return results

if __name__ == "__main__":
    results = asyncio.run(expand_and_capture())
    print("\n=== Results ===")
    print(f"ACAT_CNTRA_FIRM_* found: {results['acat_found']}")
    print(f"ACAT screenshot path: {results['acat_path']}")
    print(f"BATCH_DT found: {results['batchdt_found']}")
    print(f"BATCH_DT screenshot path: {results['batchdt_path']}")
