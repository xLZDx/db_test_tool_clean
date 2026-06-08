const { chromium } = require('playwright');

async function manualCapture() {
  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext({ viewport: { width: 1920, height: 1080 } });
  const page = await context.newPage();
  
  try {
    console.log('Opening http://127.0.0.1:8550...');
    await page.goto('http://127.0.0.1:8550');
    
    console.log('\n===========================================');
    console.log('MANUAL INTERACTION MODE');
    console.log('===========================================');
    console.log('Please:');
    console.log('1. Navigate to ODI Scenario Compare');
    console.log('2. Upload AVY scenario file(s)');
    console.log('3. Run the comparison');
    console.log('4. Navigate to v16 results');
    console.log('5. Locate SDIRA_TXN_TP_CD row');
    console.log('6. Expand/view ODI XML Logic content');
    console.log('\nWhen ready for screenshot, press ENTER in this terminal...');
    console.log('===========================================\n');
    
    // Wait for user input
    await new Promise(resolve => {
      process.stdin.once('data', () => resolve());
    });
    
    console.log('Capturing screenshot...');
    const screenshotPath = 'D:/test 2/db-test-tool-analysis/db-testing-tool/screenshots/before_sdira_trace_fix.png';
    await page.screenshot({ path: screenshotPath, fullPage: false });
    
    console.log(`\n✓ Screenshot saved to: ${screenshotPath}`);
    console.log('Press ENTER again to close browser...');
    
    await new Promise(resolve => {
      process.stdin.once('data', () => resolve());
    });
    
  } finally {
    await browser.close();
  }
}

manualCapture().catch(console.error);
