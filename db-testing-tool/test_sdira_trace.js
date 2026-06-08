const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();
  
  try {
    console.log('Opening http://127.0.0.1:8550...');
    await page.goto('http://127.0.0.1:8550', { waitUntil: 'networkidle', timeout: 10000 });
    
    // Wait for page to be ready
    await page.waitForTimeout(1000);
    
    // Capture initial state for debugging
    console.log('Page title:', await page.title());
    
    // Look for ODI link or section
    console.log('Looking for ODI section...');
    try {
      const odiLink = await page.getByText('ODI', { exact: false }).or(page.locator('[href*="odi"]')).first();
      if (await odiLink.isVisible({ timeout: 2000 })) {
        console.log('Clicking ODI link...');
        await odiLink.click();
        await page.waitForLoadState('networkidle');
        await page.waitForTimeout(1000);
      }
    } catch (e) {
      console.log('Could not find ODI link, checking if already on ODI page...');
    }
    
    // Configure ODI root path to point to local sample files
    console.log('Configuring ODI root path...');
    const rootPathInput = await page.locator('#odi-root-path, input[placeholder*="AppData"]').first();
    if (await rootPathInput.isVisible({ timeout: 2000 })) {
      console.log('Setting root path to data/taxlot...');
      await rootPathInput.fill('D:/test 2/db-test-tool-analysis/db-testing-tool/data/taxlot');
      await page.waitForTimeout(500);
    }
    
    // Try clicking "Connect To Repo" or "Reload Config Files"
    try {
      const connectBtn = await page.getByRole('button', { name: /connect|reload config/i }).first();
      if (await connectBtn.isVisible({ timeout: 2000 })) {
        console.log('Clicking Connect/Reload button...');
        await connectBtn.click();
        await page.waitForLoadState('networkidle');
        await page.waitForTimeout(2000);
      }
    } catch (e) {
      console.log('Could not find Connect button:', e.message);
    }
    
    // Check for scenario/compare page elements
    const selects = await page.locator('select').count();
    console.log('Found', selects, 'select elements');
    
    // First, try clicking "Analyze Files" to load scenarios
    console.log('Looking for Analyze Files button...');
    try {
      const analyzeBtn = await page.getByRole('button', { name: 'Analyze Files' });
      if (await analyzeBtn.isVisible({ timeout: 2000 })) {
        console.log('Clicking Analyze Files...');
        await analyzeBtn.click();
        await page.waitForLoadState('networkidle');
        await page.waitForTimeout(2000);
      }
    } catch (e) {
      console.log('Could not find Analyze Files button:', e.message);
    }
    
    // Look for file list or scenario tree
    console.log('Looking for scenario list...');
    const bodyText1 = await page.textContent('body');
    console.log('Page now contains AVY:', bodyText1.includes('AVY'));
    
    // Save HTML for debugging
    const html = await page.content();
    require('fs').writeFileSync('D:/test 2/db-test-tool-analysis/db-testing-tool/debug_page.html', html);
    console.log('Saved page HTML to debug_page.html');
    
    // Try to find any scenario/package elements
    const treeItems = await page.locator('[role="treeitem"], .scenario-item, .package-item, li').allTextContents();
    console.log('Tree/list items (first 20):', treeItems.slice(0, 20));
    
    // Try to find and click AVY in the scenario/file list
    try {
      const avyElement = await page.getByText('AVY', { exact: false }).first();
      if (await avyElement.isVisible({ timeout: 2000 })) {
        console.log('Found AVY element, clicking...');
        await avyElement.click();
        await page.waitForTimeout(500);
      }
    } catch (e) {
      console.log('Could not find AVY element:', e.message);
    }
    
    // Also try looking for scenario files in a table
    try {
      const rows = await page.locator('tr, .row').allTextContents();
      console.log('Table rows (first 10):', rows.slice(0, 10).map(r => r.substring(0, 50)));
    } catch (e) {
      console.log('No table rows found');
    }
    
    
    // Look for compare or run button (Run Package in this case)
    console.log('Looking for Run Package button...');
    try {
      const buttons = await page.locator('button, input[type="submit"]').allTextContents();
      console.log('Available buttons:', buttons);
      
      const runBtn = await page.getByRole('button', { name: /run package|compare/i }).first();
      if (await runBtn.isVisible({ timeout: 2000 })) {
        console.log('Clicking Run Package button...');
        await runBtn.click();
        await page.waitForLoadState('networkidle');
        await page.waitForTimeout(3000);
      }
    } catch (e) {
      console.log('Could not find Run Package button:', e.message);
    }
    
    // Look for v16 compare-all mode or tab
    console.log('Looking for v16 compare-all mode...');
    try {
      const currentText = await page.textContent('body');
      console.log('Page contains v16:', currentText.includes('v16'));
      console.log('Page contains compare-all:', currentText.includes('compare-all'));
      
      const v16Tab = await page.getByText(/v16|compare-all/i).or(page.locator('[href*="compare-all"]')).first();
      if (await v16Tab.isVisible({ timeout: 2000 })) {
        console.log('Clicking v16/compare-all tab...');
        await v16Tab.click();
        await page.waitForLoadState('networkidle');
        await page.waitForTimeout(1500);
      }
    } catch (e) {
      console.log('Could not find v16 tab:', e.message);
    }
    
    // Search for SDIRA_TXN_TP_CD in the page
    console.log('Searching for SDIRA_TXN_TP_CD...');
    const bodyText = await page.textContent('body');
    const containsSDIRA = bodyText.includes('SDIRA_TXN_TP_CD');
    console.log('Page contains SDIRA_TXN_TP_CD:', containsSDIRA);
    
    const sdiraElement = await page.getByText('SDIRA_TXN_TP_CD', { exact: false }).first();
    let found = false;
    
    if (await sdiraElement.isVisible()) {
      found = true;
      console.log('Found SDIRA_TXN_TP_CD! Scrolling into view...');
      await sdiraElement.scrollIntoViewIfNeeded();
      await page.waitForTimeout(500);
      
      // Take screenshot
      console.log('Taking screenshot...');
      await page.screenshot({ 
        path: 'D:/test 2/db-test-tool-analysis/db-testing-tool/screenshots/after_sdira_trace_fix.png',
        fullPage: false
      });
      console.log('Screenshot saved!');
    } else {
      console.log('SDIRA_TXN_TP_CD not found in current view');
      // Take screenshot anyway for debugging
      await page.screenshot({ 
        path: 'D:/test 2/db-test-tool-analysis/db-testing-tool/screenshots/after_sdira_trace_fix.png',
        fullPage: false
      });
    }
    
    console.log(`FOUND: ${found}`);
    
  } catch (error) {
    console.error('Error:', error.message);
    // Take screenshot on error
    await page.screenshot({ 
      path: 'D:/test 2/db-test-tool-analysis/db-testing-tool/screenshots/after_sdira_trace_fix.png',
      fullPage: false
    });
  } finally {
    await browser.close();
  }
})();
