// Run on snail after verify_alert_review_release.py; all API calls are reads.
const {chromium} = require('/home/ubuntu/AgenticSRE-releases/skill-owned-release-20261013/browserdeps/node_modules/playwright');
const fs = require('fs'), assert = require('assert');
const root = '/home/ubuntu/AgenticSRE-releases/alert-review-budget-20261016';
(async () => {
  const identity = JSON.parse(fs.readFileSync(root+'/production-api.json', 'utf8'));
  const browser = await chromium.launch({headless:true, args:['--no-sandbox']});
  const result = {passed:false, page_errors:[], console_errors:[]};
  try {
    const page = await browser.newPage({viewport:{width:1440,height:1000}});
    page.on('pageerror', error => result.page_errors.push(String(error)));
    page.on('console', message => {
      if (message.type() === 'error') result.console_errors.push(message.text());
    });
    // Prevent accidental diagnosis/healing or any other browser mutation.
    await page.route('**/api/**', route => route.request().method() === 'GET' ? route.continue() : route.abort());
    const started = Date.now();
    const response = await page.goto('http://127.0.0.1:8080/', {waitUntil:'domcontentloaded',timeout:30000});
    result.home_status = response.status();
    result.home_load_ms = Date.now()-started;
    assert(result.home_status === 200 && result.home_load_ms < 10000);
    await page.evaluate(() => switchView('healcenter'));
    await page.locator('#heal-run-filter').selectOption('recovered');
    await page.waitForFunction(() => document.querySelector('#heal-runs-pagination').textContent.includes('匹配'));
    result.heal_records_visible = await page.locator('#heal-runs .fault-exp-run').count();
    assert(result.heal_records_visible > 0);
    const caps = await (await page.request.get('http://127.0.0.1:8080/api/hermes/capabilities')).json();
    result.hermes_ready = caps.status === 'ready';
    assert(result.hermes_ready);
    await page.evaluate(async identity => {
      switchView('alerts');
      await loadAlertList();
      clearTimeout(_alertScanTimer);
      _alertScanCluster = identity.cluster_id;
      _alertScanJob = {job_id:identity.job_id,cluster_id:identity.cluster_id,status:'partial'};
      await pollAlertScan(_alertScanJob, ++_alertScanGeneration);
    }, identity);
    const target = page.locator('#alert-scan-result');
    const text = await target.innerText();
    assert(text.includes('事实证据仍可查看和诊断'));
    assert(text.includes(`待审 ${identity.deferred_pairs} 对`));
    assert(await target.getByRole('button',{name:'重试未完成审核',exact:true}).count() === 0);
    result.diagnosis_buttons = await target.getByRole('button',{name:'证据包诊断',exact:true}).count();
    assert(result.diagnosis_buttons === 6);
    await target.getByText('关系审核清单',{exact:true}).click();
    await target.getByRole('button',{name:'加载审核状态',exact:true}).click();
    await page.waitForFunction(count => document.querySelector('#alert-review-pairs').textContent.includes(`共 ${count} 对`), identity.deferred_pairs);
    result.deferred_pairs = identity.deferred_pairs;
    result.partial_result_visible = true;
    await target.scrollIntoViewIfNeeded();
    await page.screenshot({path:root+'/production-partial.png'});
    assert.deepStrictEqual(result.page_errors, []);
    assert.deepStrictEqual(result.console_errors, []);
    result.passed = true;
    result.scope = 'production page and backend; synthetic zero-budget job; no diagnosis or model call';
  } finally {
    fs.writeFileSync(root+'/production-browser.json', JSON.stringify(result,null,2));
    await browser.close();
  }
  console.log(JSON.stringify(result));
})().catch(error => {console.error(error);process.exitCode=1;});
