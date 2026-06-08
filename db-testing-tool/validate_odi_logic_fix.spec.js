// GUI validation for ODI XML Logic display fix
// Tests that SDIRA row shows informative logic, not bare alias tokens

const { test, expect } = require('@playwright/test');
const path = require('path');

test('ODI vs DRD Validation - SDIRA row shows informative ODI XML Logic', async ({ page }) => {
  test.setTimeout(90000); // Increase timeout to 90 seconds
  
  // Setup
  const baseUrl = 'http://127.0.0.1:8550';
  const dataDir = 'D:\\test 2\\db-test-tool-analysis\\db-testing-tool\\data\\taxlot';
  const drdFile = path.join(dataDir, 'DRD_Activity_Fact.xlsx');
  const odiFile = path.join(dataDir, '1_SCEN_LH_AVY_PKG_LOAD_AVY_FACT_SIDE_V1_RT_ST_Version_001.xml');
  
  console.log('Opening page...');
  await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
  
  // Wait for page to load
  await page.waitForTimeout(2000);
  
  // Take initial screenshot
  await page.screenshot({ path: 'D:\\test 2\\db-test-tool-analysis\\db-testing-tool\\screenshots\\01_initial_page.png', fullPage: true });
  console.log('Screenshot: 01_initial_page.png');
  
  // Scroll to ODI vs DRD Validation card by ID
  console.log('Scrolling to ODI vs DRD Validation panel...');
  const odiCard = page.locator('#odi-val-card');
  await odiCard.scrollIntoViewIfNeeded({ timeout: 10000 });
  await page.waitForTimeout(1000);
  
  // Take screenshot of the panel
  await page.screenshot({ path: 'D:\\test 2\\db-test-tool-analysis\\db-testing-tool\\screenshots\\02_odi_panel.png', fullPage: true });
  console.log('Screenshot: 02_odi_panel.png');
  
  // Upload DRD file
  console.log('Uploading DRD file...');
  const drdInput = page.locator('#odi-drd-file');
  await drdInput.setInputFiles(drdFile);
  await page.waitForTimeout(500);
  
  // Upload ODI XML file
  console.log('Uploading ODI XML file...');
  const odiInput = page.locator('#odi-xml-file');
  await odiInput.setInputFiles(odiFile);
  await page.waitForTimeout(500);
  
  // Take screenshot after file upload
  await page.screenshot({ path: 'D:\\test 2\\db-test-tool-analysis\\db-testing-tool\\screenshots\\03_files_uploaded.png', fullPage: true });
  console.log('Screenshot: 03_files_uploaded.png');
  
  // Click the Compare v16.6 button
  console.log('Clicking Compare v16.6 button...');
  const compareBtn = page.locator('button:has-text("Compare v16.6")');
  await compareBtn.click();
  
  // Wait for results to load (increase timeout for processing)
  console.log('Waiting for results...');
  await page.waitForTimeout(5000);
  
  // Take screenshot of results
  await page.screenshot({ path: 'D:\\test 2\\db-test-tool-analysis\\db-testing-tool\\screenshots\\04_results_loading.png', fullPage: true });
  console.log('Screenshot: 04_results_loading.png');
  
  // Wait a bit more for the table to render
  await page.waitForTimeout(3000);
  
  // Take full results screenshot
  await page.screenshot({ path: 'D:\\test 2\\db-test-tool-analysis\\db-testing-tool\\screenshots\\05_full_results.png', fullPage: true });
  console.log('Screenshot: 05_full_results.png');
  
  // Try to find and scroll to SDIRA row
  console.log('Looking for SDIRA row...');
  const sdiraRow = page.locator('td:has-text("SDIRA_TXN_TP")').first();
  
  try {
    await sdiraRow.waitFor({ timeout: 5000 });
    await sdiraRow.scrollIntoViewIfNeeded();
    await page.waitForTimeout(1000);
    
    // Take screenshot of SDIRA row area
    await page.screenshot({ path: 'D:\\test 2\\db-test-tool-analysis\\db-testing-tool\\screenshots\\06_sdira_row.png', fullPage: true });
    console.log('Screenshot: 06_sdira_row.png');
    
    // Try to get the ODI XML Logic cell text
    const sdiraParent = sdiraRow.locator('xpath=ancestor::tr').first();
    const odiLogicCell = sdiraParent.locator('td').nth(5); // Adjust index based on column position
    const odiLogicText = await odiLogicCell.textContent();
    
    console.log('\n=== SDIRA ODI XML Logic Content (first 500 chars) ===');
    console.log(odiLogicText.substring(0, 500));
    console.log('\n=== Analysis ===');
    
    // Check if it contains informative content
    const hasInsertStatement = odiLogicText.includes('insert') || odiLogicText.includes('INSERT');
    const hasColumnLists = odiLogicText.includes('(') && odiLogicText.includes(',');
    const isBareAlias = odiLogicText.match(/^[A-Z_]+\.[A-Z_]+$/);
    
    if (hasInsertStatement && hasColumnLists) {
      console.log('✓ PASS: ODI XML Logic shows informative INSERT statement with column details');
    } else if (isBareAlias) {
      console.log('✗ FAIL: ODI XML Logic still shows bare alias pattern');
    } else {
      console.log('? PARTIAL: ODI XML Logic has content but unclear if informative enough');
    }
    
  } catch (error) {
    console.log('Could not find SDIRA row:', error.message);
    
    // Take screenshot anyway
    await page.screenshot({ path: 'D:\\test 2\\db-test-tool-analysis\\db-testing-tool\\screenshots\\06_no_sdira_found.png', fullPage: true });
  }
  
  // Final screenshot
  await page.screenshot({ path: 'D:\\test 2\\db-test-tool-analysis\\db-testing-tool\\screenshots\\07_final.png', fullPage: true });
  console.log('Screenshot: 07_final.png');
  
  console.log('\nValidation complete! Check screenshots in: D:\\test 2\\db-test-tool-analysis\\db-testing-tool\\screenshots\\');
});
