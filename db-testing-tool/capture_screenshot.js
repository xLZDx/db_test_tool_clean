const { chromium } = require('playwright');
const path = require('path');

async function captureScreenshot() {
  const browser = await chromium.launch({ headless: false, slowMo: 500 });
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();
  
  try {
    console.log('Step 1: Navigating to http://127.0.0.1:8550...');
    await page.goto('http://127.0.0.1:8550', { waitUntil: 'domcontentloaded', timeout: 10000 });
    await page.waitForTimeout(2000);
    await page.screenshot({ path: 'D:/test 2/db-test-tool-analysis/db-testing-tool/screenshots/step1_homepage.png' });
    console.log('Homepage loaded, screenshot saved');
    
    console.log('Step 2: Looking for ODI Scenario tab/link...');
    // Check for various possible navigation elements
    const possibleNavs = [
      'a:has-text("ODI")', 
      'a:has-text("Scenario")',
      'a:has-text("Compare")',
      'button:has-text("ODI")',
      '[href*="odi"]',
      '[href*="scenario"]'
    ];
    
    for (const selector of possibleNavs) {
      const elements = await page.locator(selector).all();
      if (elements.length > 0) {
        console.log(`Found element with selector: ${selector}`);
        await elements[0].click();
        await page.waitForTimeout(2000);
        await page.screenshot({ path: 'D:/test 2/db-test-tool-analysis/db-testing-tool/screenshots/step2_after_nav.png' });
        break;
      }
    }
    
    console.log('Step 3: Looking for AVY scenario file inputs...');
    // Look for file inputs or dropdowns for AVY scenario
    const fileInputs = await page.locator('input[type="file"]').all();
    console.log(`Found ${fileInputs.length} file input elements`);
    
    // Upload the AVY XML file to the first file input
    if (fileInputs.length > 0) {
      const avyXmlPath = 'D:/test 2/db-test-tool-analysis/db-testing-tool/1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml';
      console.log(`Uploading file: ${avyXmlPath}`);
      await fileInputs[0].setInputFiles(avyXmlPath);
      await page.waitForTimeout(1000);
    }
    
    // Take screenshot of current state
    await page.screenshot({ path: 'D:/test 2/db-test-tool-analysis/db-testing-tool/screenshots/step3_file_inputs.png' });
    
    console.log('Step 4: Looking for submit/compare button...');
    const submitButtons = await page.locator('button[type="submit"], input[type="submit"], button:has-text("Compare"), button:has-text("Run")').all();
    if (submitButtons.length > 0) {
      console.log('Clicking submit button...');
      await submitButtons[0].click();
      await page.waitForTimeout(8000); // Wait longer for comparison
      await page.screenshot({ path: 'D:/test 2/db-test-tool-analysis/db-testing-tool/screenshots/step4_after_submit.png' });
    }
    
    console.log('Step 5: Searching for SDIRA_TXN_TP_CD in page content...');
    const pageContent = await page.content();
    const hasSDIRA = pageContent.includes('SDIRA_TXN_TP_CD');
    console.log(`Page contains SDIRA_TXN_TP_CD: ${hasSDIRA}`);
    
    if (hasSDIRA) {
      console.log('Found SDIRA_TXN_TP_CD! Locating element...');
      const sdiraRow = page.locator('text=SDIRA_TXN_TP_CD').first();
      await sdiraRow.scrollIntoViewIfNeeded({ timeout: 5000 });
      await page.waitForTimeout(1000);
      
      // Look for ODI XML Logic content near this row
      console.log('Looking for ODI XML Logic expand/view controls...');
      await page.screenshot({ path: 'D:/test 2/db-test-tool-analysis/db-testing-tool/screenshots/step5_sdira_located.png' });
      
      // Try clicking any expand buttons near SDIRA row
      const expandButtons = await page.locator('button:near(text=SDIRA_TXN_TP_CD), details:near(text=SDIRA_TXN_TP_CD), .expand:near(text=SDIRA_TXN_TP_CD)').all();
      if (expandButtons.length > 0) {
        await expandButtons[0].click();
        await page.waitForTimeout(1000);
      }
    }
    
    // Final screenshot
    const screenshotPath = 'D:/test 2/db-test-tool-analysis/db-testing-tool/screenshots/before_sdira_trace_fix.png';
    console.log(`Step 6: Capturing final screenshot to ${screenshotPath}...`);
    await page.screenshot({ path: screenshotPath, fullPage: true });
    
    console.log('✓ Screenshot captured successfully!');
    console.log(`✓ Path: ${screenshotPath}`);
    
    // Keep browser open for 5 seconds to allow manual inspection
    console.log('Keeping browser open for 5 seconds for inspection...');
    await page.waitForTimeout(5000);
    
  } catch (error) {
    console.error('✗ Error during automation:', error.message);
    await page.screenshot({ path: 'D:/test 2/db-test-tool-analysis/db-testing-tool/screenshots/error_state.png' });
    throw error;
  } finally {
    await browser.close();
  }
}

captureScreenshot().catch(err => {
  console.error('✗ Failed:', err.message);
  process.exit(1);
});
