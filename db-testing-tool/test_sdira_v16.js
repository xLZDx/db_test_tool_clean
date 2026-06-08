const { chromium } = require('playwright');
const path = require('path');

(async () => {
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();
  
  const BASE_URL = 'http://127.0.0.1:8550/mappings';
  const DATA_DIR = 'D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot';
  const DRD_FILE = path.join(DATA_DIR, 'DRD_Activity_Fact.xlsx');
  const ODI_FILE = path.join(DATA_DIR, '1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml');
  const SCREENSHOT_PATH = 'D:/test 2/db-test-tool-analysis/db-testing-tool/screenshots/after_sdira_trace_fix.png';
  
  try {
    console.log(`Opening ${BASE_URL}...`);
    await page.goto(BASE_URL, { waitUntil: 'networkidle', timeout: 15000 });
    await page.waitForTimeout(2000);
    
    console.log('Page title:', await page.title());
    
    // Upload DRD file
    console.log('Uploading DRD file...');
    const drdInput = await page.locator('#odi-drd-file');
    await drdInput.setInputFiles(DRD_FILE);
    console.log('✓ DRD file uploaded');
    await page.waitForTimeout(1000);
    
    // Upload ODI XML file
    console.log('Uploading ODI XML file...');
    const odiInput = await page.locator('#odi-xml-file');
    await odiInput.setInputFiles(ODI_FILE);
    console.log('✓ ODI XML file uploaded');
    await page.waitForTimeout(1000);
    
    // Click Compare v16.6 button
    console.log('Looking for Compare v16 button...');
    const compareBtn = await page.locator('#odi-v16-btn');
    if (await compareBtn.isVisible({ timeout: 5000 })) {
      console.log('Scrolling to and clicking Compare v16 button...');
      await compareBtn.scrollIntoViewIfNeeded();
      await page.waitForTimeout(500);
      await compareBtn.click({ force: true });
      console.log('✓ Clicked Compare v16 button');
      
      // Wait for results to load
      console.log('Waiting for results to load...');
      await page.waitForTimeout(8000);
      
      // Scroll down to see more of the results table
      console.log('Scrolling down to view results table...');
      await page.evaluate(() => window.scrollBy(0, 500));
      await page.waitForTimeout(1000);
      
      // Look for SDIRA_TXN_TP_CD in the page
      console.log('Searching for SDIRA_TXN_TP_CD...');
      const bodyText = await page.textContent('body');
      const found = bodyText.includes('SDIRA_TXN_TP_CD');
      
      console.log(`SDIRA_TXN_TP_CD found: ${found}`);
      
      if (found) {
        // Try to find and scroll to the results table containing SDIRA_TXN_TP_CD
        try {
          console.log('Looking for results table...');
          // Look for any table that might contain the results
          const tables = await page.locator('table').all();
          console.log(`Found ${tables.length} tables on the page`);
          
          for (let i = 0; i < tables.length; i++) {
            const tableText = await tables[i].textContent();
            if (tableText.includes('SDIRA_TXN_TP_CD')) {
              console.log(`Found SDIRA_TXN_TP_CD in table ${i}`);
              await tables[i].scrollIntoViewIfNeeded({ timeout: 5000 });
              await page.waitForTimeout(500);
              
              // Try to find the specific row
              const sdiraRow = await tables[i].locator('tr:has-text("SDIRA_TXN_TP_CD")').first();
              if (await sdiraRow.isVisible()) {
                await sdiraRow.scrollIntoViewIfNeeded();
                console.log('✓ Scrolled to SDIRA_TXN_TP_CD row');
                await page.waitForTimeout(1000);
              }
              break;
            }
          }
        } catch (e) {
          console.log(`Could not scroll to SDIRA_TXN_TP_CD table (${e.message})`);
          // Scroll down more to try to bring it into view
          console.log('Scrolling down more...');
          await page.evaluate(() => window.scrollBy(0, 800));
          await page.waitForTimeout(1000);
        }
        
        console.log('Taking screenshot with results visible...');
      } else {
        console.log('SDIRA_TXN_TP_CD not found, taking screenshot anyway...');
      }
      
      // Take screenshot
      await page.screenshot({ 
        path: SCREENSHOT_PATH,
        fullPage: false
      });
      console.log(`Screenshot saved to: ${SCREENSHOT_PATH}`);
      
      // Log results
      console.log('\n=== RESULTS ===');
      console.log(`SDIRA_TXN_TP_CD Found: ${found}`);
      console.log(`Screenshot Path: ${SCREENSHOT_PATH}`);
      
    } else {
      console.log('✗ Compare v16 button not found');
      await page.screenshot({ path: SCREENSHOT_PATH });
    }
    
  } catch (error) {
    console.error('Error:', error.message);
    await page.screenshot({ path: SCREENSHOT_PATH });
  } finally {
    await browser.close();
  }
})();
