// Remote browser against the first candidate diagnosis, including a failed one.
const {chromium} = require('/home/ubuntu/AgenticSRE-releases/skill-owned-release-20261013/browserdeps/node_modules/playwright');
const fs = require('fs'), assert = require('assert'), path = require('path');
const root=process.argv[2];
if (!root) throw new Error('explicit campaign directory required');
(async()=>{
  const result={passed:false,page_errors:[],console_errors:[],scope:'actual isolated Web backend, actual model result, read-only browser'};
  let browser;
  try {
    let entry;
    for(let i=0;i<600;i++) {
      try {entry=JSON.parse(fs.readFileSync(path.join(root,'report.json'),'utf8')).cases.find(row=>row.arm==='candidate');} catch {}
      if(entry)break;
      await new Promise(resolve=>setTimeout(resolve,1000));
    }
    assert(entry,'first candidate diagnosis unavailable');
    result.case=entry.case;result.run_id=entry.run_id;
    browser=await chromium.launch({headless:true,args:['--no-sandbox']});
    const page=await browser.newPage({viewport:{width:1440,height:1100}});
    page.on('pageerror',error=>result.page_errors.push(error.message));
    page.on('console',message=>{if(message.type()==='error')result.console_errors.push(message.text());});
    await page.route('**/api/**',route=>route.request().method()==='GET'?route.continue():route.abort());
    const base='http://127.0.0.1:'+entry.port;
    const response=await page.goto(base+'/',{waitUntil:'domcontentloaded'});
    assert(response.status()===200);
    const run=await (await page.request.get(base+'/api/rca/'+entry.run_id)).json();
    assert(run.status==='completed');
    await page.evaluate(value=>{switchView('rca');renderRCAFinalResult(value.result);},run);
    const final=run.result.result.result;
    const body=page.locator('#rca-result-content');
    if(final.automation_gate.allowed===false)assert((await body.innerText()).includes('待人工复核'));
    if(final.citation_validation.status==='passed')assert((await body.innerText()).includes('逐字引用已核对'));
    const details=body.locator('details');
    assert(await details.count()>=2);
    assert(await details.last().getAttribute('open')===null);
    await details.first().locator('summary').click();
    assert((await body.innerText()).includes(final.evidence_references[0].id));
    await body.scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(root,'online-diagnosis.png')});
    assert.deepStrictEqual(result.page_errors,[]);
    assert.deepStrictEqual(result.console_errors,[]);
    result.passed=true;result.diagnosis_status=final.diagnosis_status;
  } finally {
    fs.writeFileSync(path.join(root,'browser.json'),JSON.stringify(result,null,2));
    if(browser)await browser.close();
  }
  console.log(JSON.stringify(result));
})().catch(error=>{console.error(error);process.exitCode=1;});
