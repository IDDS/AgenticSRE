const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');

function explorer() {
    const elements = new Map(), requests = [];
    function element() {
        return {
            options: [], children: [], listeners: {},
            add(option) { this.options.push(option); },
            append(...nodes) { this.children.push(...nodes); },
            replaceChildren(...nodes) { this.children = nodes; },
            addEventListener(type, handler) { this.listeners[type] = handler; },
        };
    }
    function get(id) {
        if (!elements.has(id)) elements.set(id, element());
        return elements.get(id);
    }
    const context = vm.createContext({
        document: {getElementById: get, createElement: element},
        Option: class { constructor(text, value) { this.text = text; this.value = value; } },
        URLSearchParams,
        api(url) {
            return new Promise((resolve, reject) => {
                if (url.includes('/ontology-graph?')) requests.push({url: new URL(url, 'http://localhost'), reject});
            });
        },
        switchView(view, options) {
            assert.equal(view, 'ontology');
            graph.open(options?.ontologyLocation);
        },
    });
    const graph = vm.runInContext(readFileSync('web_app/static/js/ontology.js', 'utf8') + '\nontologyExplorer', context);
    return {graph, get, requests};
}

test('RCA carries the investigation revision and exact object through a refresh and a missing-object response', async () => {
    const {graph, get, requests} = explorer();
    graph.fromRun('run/a', 'old-id', 'rev-1');
    assert.equal(requests[0].url.pathname, '/api/rca/run%2Fa/ontology-graph');
    assert.equal(requests[0].url.searchParams.get('focus_id'), 'old-id');
    assert.equal(requests[0].url.searchParams.get('revision'), 'rev-1');
    requests[0].reject(Object.assign(new Error('调查已更新'), {status: 409}));
    await new Promise(setImmediate);
    const status = get('og-status');
    assert.equal(get('og-model').hidden, true);
    status.children.find(b => b.textContent === '读取最新调查版本').listeners.click();
    assert.equal(requests[1].url.searchParams.get('focus_id'), 'old-id');
    assert.equal(requests[1].url.searchParams.get('revision'), '');
    requests[1].reject(Object.assign(new Error('调查中找不到该对象'), {status: 404}));
    await new Promise(setImmediate);
    assert.equal(get('og-model').hidden, true);
    assert.equal(requests.length, 2);
    status.children.find(b => b.textContent === '浏览本次调查的对象').listeners.click();
    assert.equal(requests[2].url.searchParams.get('focus_id'), '');
});

test('a late error from the previous investigation cannot replace the current object selection', async () => {
    const {graph, get, requests} = explorer();
    graph.fromRun('first', 'first-object', 'r1');
    graph.fromRun('second', 'second-object', 'r2');
    requests[0].reject(Object.assign(new Error('旧调查已更新'), {status: 409}));
    await new Promise(setImmediate);
    assert.equal(get('og-status').children.length, 0);
    assert.equal(get('og-run').value, 'second');
    assert.equal(requests[1].url.searchParams.get('focus_id'), 'second-object');
    get('og-run').listeners.change({target: {value: 'third'}});
    assert.equal(requests[2].url.searchParams.get('focus_id'), '');
    assert.equal(requests[2].url.searchParams.get('revision'), '');
});
