// Run on snail after verify_alert_grounding_release.py; all API calls are reads.
const {chromium} = require('/home/ubuntu/AgenticSRE-releases/skill-owned-release-20261013/browserdeps/node_modules/playwright');
const fs = require('fs'), assert = require('assert');
const root = '/home/ubuntu/AgenticSRE-releases/alert-grounding-release-20261016';
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
    // Exercise the deployed renderer with an explicitly synthetic result.
    // This checks browser behavior, not a live model or RCA workflow.
    await page.evaluate(() => {
      switchView('rca');
      renderRCAFinalResult({status:'completed',result:{
        diagnosis_status:'requires_review',confidence:.5,fault_type:'unknown',
        root_cause:'根因机制尚未证实；需补充机制证据并人工复核。',
        automation_gate:{allowed:false},
        citation_validation:{status:'passed',cited:['a-release-fixture'],unresolved:[]},
        evidence_references:[{id:'a-release-fixture',namespace:'release-verification',service:'fixture',
          observation:'synthetic latency_ms=350 <img src=x onerror="window.__unsafe=true">'}],
        unverified_analysis:{root_cause:'UNVERIFIED_RELEASE_FIXTURE'}
      }});
    });
    const diagnosis = page.locator('#rca-result-content');
    assert((await diagnosis.innerText()).includes('待人工复核'));
    assert((await diagnosis.innerText()).includes('自动学习与自动恢复已暂停'));
    assert((await diagnosis.innerText()).includes('逐字引用已核对'));
    assert(!(await diagnosis.innerText()).includes('UNVERIFIED_RELEASE_FIXTURE'));
    await diagnosis.getByText('原始观测 1 项',{exact:true}).click();
    assert((await diagnosis.innerText()).includes('synthetic latency_ms=350'));
    assert(await diagnosis.locator('img').count() === 0);
    assert(await page.evaluate(() => window.__unsafe) === undefined);
    await diagnosis.getByText('模型分析草稿（未经确认，仅供复核）',{exact:true}).click();
    assert((await diagnosis.innerText()).includes('UNVERIFIED_RELEASE_FIXTURE'));
    await diagnosis.getByText('模型分析草稿（未经确认，仅供复核）',{exact:true}).click();
    await page.screenshot({path:root+'/production-grounding-fixture.png'});
    result.grounding_renderer_fixture = {passed:true,review_visible:true,
      automation_pause_visible:true,quotes_visible:true,draft_collapsed:true,html_escaped:true};
    assert.deepStrictEqual(result.page_errors, []);
    assert.deepStrictEqual(result.console_errors, []);
    result.passed = true;
    result.scope = 'production page and backend, synthetic zero-budget job; deployed renderer fixture; no live RCA or model call';
  } finally {
    fs.writeFileSync(root+'/production-browser.json', JSON.stringify(result,null,2));
    await browser.close();
  }
  console.log(JSON.stringify(result));
})().catch(error => {console.error(error);process.exitCode=1;});
