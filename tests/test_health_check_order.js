const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('web_app/static/js/app.js', 'utf8');
const marker = source.indexOf('let _healthCheckGeneration = 0;');
const start = marker >= 0 ? marker : source.indexOf('async function healthCheck()');
assert(start >= 0);
const end = source.indexOf('\n}', source.indexOf('async function healthCheck()', start)) + 2;
assert(end > start);
const nodes = {
  dot: {}, text: {}, badge: {}, warning: {style: {}},
};
const pending = [];
const context = vm.createContext({
  api: () => new Promise(resolve => pending.push(resolve)),
  document: {
    querySelector: selector => selector.includes('last-child') ? nodes.text : nodes.dot,
    getElementById: id => id === 'cluster-badge' ? nodes.badge : nodes.warning,
  },
});
vm.runInContext(source.slice(start, end), context);

(async () => {
  const older = context.healthCheck();
  const newer = context.healthCheck();
  pending[1]({status: 'ok', llm_configured: true});
  await newer;
  pending[0](null);
  await older;
  assert.equal(nodes.badge.textContent, '系统在线');
  assert.match(nodes.badge.title, /集群状态/);
  assert.equal(nodes.warning.style.display, 'none');

  const oldSuccess = context.healthCheck();
  const newFailure = context.healthCheck();
  pending[3](null);
  await newFailure;
  pending[2]({status: 'ok'});
  await oldSuccess;
  assert.equal(nodes.badge.textContent, '系统连接异常');
  console.log('health check ordering: 2 scenarios passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
