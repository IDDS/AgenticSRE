const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('web_app/static/js/app.js', 'utf8');
const calls = [];
const elements = {'node-grid': {}, 'alert-preview': {}, 'overview-snapshot-status': {}, 'overview-events-status': {}};
const charts = [];
const totals = {};
const context = vm.createContext({
    api(path) {
        return new Promise(resolve => calls.push({path, resolve}));
    },
    document: {getElementById: id => elements[id]},
    escapeHtml: String,
    animateValue: (id, value) => { totals[id] = value; },
    renderOverviewTopology: data => { totals.nodes = data.nodes.length; },
    renderPodChart: phases => charts.push({...phases}),
});
vm.runInContext(source.slice(source.indexOf('let overviewSnapshotRequest'), source.indexOf('function animateValue(')), context);
const snapshot = {summary: {}, deployments: [], services: [], namespaces: []};
const topology = '/api/cluster/topology';
const events = '/api/cluster/events?limit=5';
const node = {name: 'n1', ready: true, roles: ['worker'], version: 'v1', cpu: '4', memory: '8Gi'};
const tick = () => new Promise(resolve => setImmediate(resolve));

(async () => {
    const first = context.loadOverview();
    const overlap = context.loadOverview();
    assert.deepEqual(calls.map(c => c.path), [topology, events]);
    calls[0].resolve({...snapshot, nodes: [node], pods: [{phase: 'Running'}, {phase: 'Running'}, {}]});
    await tick();
    assert.deepEqual(charts, [{Running: 2, Unknown: 1}]);
    assert.match(elements['node-grid'].innerHTML, /badge-success/);
    assert.match(elements['node-grid'].innerHTML, /v1/);
    assert.equal(elements['alert-preview'].innerHTML, undefined);

    const next = context.loadOverview();
    assert.deepEqual(calls.map(c => c.path), [topology, events, topology]);
    calls[1].resolve({events: [{type: 'Warning', reason: 'BackOff', object: 'pod/p1'}]});
    calls[2].resolve(null);
    await Promise.all([first, overlap]);
    assert.match(elements['alert-preview'].innerHTML, /BackOff/);
    assert.deepEqual(calls.slice(3).map(c => c.path), ['/api/cluster/overview', '/api/cluster/nodes']);
    calls[3].resolve({nodes: 1, pods_total: 0, namespaces: 0, pod_phases: {}});
    calls[4].resolve({nodes: [{...node, ready: 'False'}]});
    await next;
    assert.match(elements['node-grid'].innerHTML, /NotReady/);
    assert.equal(totals['stat-pods'], 0);
    assert.deepEqual(charts.at(-1), {});

    const recovered = context.loadOverview();
    calls[5].resolve({...snapshot, nodes: [], pods: []});
    calls[6].resolve({events: []});
    await recovered;
    assert.equal(elements['node-grid'].innerHTML, '');
    assert.match(elements['alert-preview'].innerHTML, /暂无告警/);
    console.log('Overview refresh concurrency, fallback, and recovery passed');
})().catch(err => { console.error(err); process.exitCode = 1; });
