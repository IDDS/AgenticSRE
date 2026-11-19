// All identities and relation directions come from the selected investigation revision.
const ontologyExplorer = (() => {
    const byId = id => document.getElementById('og-' + id);
    const read = url => api(url, {cache: 'no-store', throwOnError: true, suppressErrorLog: true});
    const relations = {
        owned_by: '归属于', scheduled_on: '调度至', controlled_by: '受控于',
        selects_pod: '选择 Pod', matches_workload_template: '匹配工作负载模板',
        has_endpoint_slice: '拥有 EndpointSlice', endpoint_slice_label_association: '通过标签关联 EndpointSlice',
        endpoint_targets_pod: '端点指向 Pod'
    };
    const identities = {uid_bound: '已绑定 UID', name_bound: '按名称识别', scope_incomplete: '范围信息不全'};
    const relationLabel = type => relations[type] || type;
    let run = '', revision = '', cursor = '', historyVersion = 0, graphVersion = 0, evidenceVersion = 0;
    let initialized = false, model = null, query = '', offset = 0, focus = '', edgeOffset = 0;

    function el(tag, value, className) {
        const node = document.createElement(tag);
        if (value != null) node.textContent = value;
        if (className) node.className = className;
        return node;
    }
    function button(text, click, cls = 'btn btn-sm') {
        const b = el('button', text, cls);
        b.type = 'button';
        b.addEventListener('click', click);
        return b;
    }
    function pages(host, at, total, size, move) {
        host.replaceChildren();
        if (total <= size) return;
        const prev = button('上页', () => move(at - size));
        const next = button('下页', () => move(at + size));
        prev.disabled = at <= 0;
        next.disabled = at + size >= total;
        host.append(prev, el('small', (at + 1) + '–' + Math.min(at + size, total) + ' / ' + total), next);
    }
    function failure(host, error, retry) {
        host.replaceChildren(el('p', error.message || '请求失败'), button('重新读取', retry));
    }
    function errorView(host, error, retry) {
        if (error.status === 409) {
            ++graphVersion;
            model = null;
            byId('model').hidden = true;
            byId('status').replaceChildren(el('p', error.message), button('读取最新调查版本', refresh));
        } else failure(host, error, retry);
    }
    function refresh() {
        revision = '';
        edgeOffset = offset = 0;
        loadGraph(true);
        history();
    }
    function selectRun(id, objectId = '', sourceRevision = '') {
        run = id;
        revision = sourceRevision;
        focus = objectId;
        query = '';
        offset = edgeOffset = 0;
        byId('query').value = '';
        byId('rca').disabled = !id;
        if (id && !Array.from(byId('run').options).some(o => o.value === id)) {
            byId('run').add(new Option(id, id));
        }
        byId('run').value = id;
        return loadGraph(Boolean(objectId));
    }
    async function history(append = false) {
        const version = ++historyVersion;
        byId('more-runs').disabled = true;
        byId('history-status').textContent = '';
        const params = new URLSearchParams({limit: '25'});
        if (append) params.set('cursor', cursor);
        try {
            const data = await read('/api/rca/history?' + params);
            if (version !== historyVersion) return;
            const menu = byId('run');
            if (!append) menu.replaceChildren();
            for (const row of data.runs) {
                if (Array.from(menu.options).some(o => o.value === row.id)) continue;
                const scope = [row.namespace, row.id].filter(Boolean).join(' · ');
                menu.add(new Option(scope, row.id));
            }
            cursor = data.next_cursor || '';
            byId('more-runs').hidden = !cursor;
            if (run) {
                if (!Array.from(menu.options).some(o => o.value === run)) menu.add(new Option(run, run));
                menu.value = run;
            } else if (menu.value) selectRun(menu.value);
            else byId('status').textContent = '当前没有已保存的诊断调查。';
        } catch (err) {
            if (version === historyVersion) failure(byId('history-status'), err, () => history(append));
        } finally {
            if (version === historyVersion) byId('more-runs').disabled = false;
        }
    }
    async function loadGraph(focusHeading = false) {
        const version = ++graphVersion;
        ++evidenceVersion;
        byId('model').hidden = true;
        byId('status').textContent = run ? '正在读取调查图谱…' : '';
        model = null;
        if (!run) return;
        const params = new URLSearchParams({revision, q: query, offset, focus_id: focus, edge_offset: edgeOffset});
        try {
            const data = await read('/api/rca/' + encodeURIComponent(run) + '/ontology-graph?' + params);
            if (version !== graphVersion) return;
            model = data;
            revision = data.revision;
            focus = data.focus?.id || '';
            render();
            byId('status').textContent = '';
            byId('model').hidden = false;
            if (focusHeading) (byId('map').querySelector('h3') || byId('query')).focus();
        } catch (err) {
            if (version !== graphVersion) return;
            errorView(byId('status'), err, () => loadGraph());
            if (err.status === 404 && focus) {
                byId('status').append(button('浏览本次调查的对象', () => selectRun(run, '', revision)));
            }
        }
    }
    function pick(id) {
        focus = id;
        edgeOffset = 0;
        loadGraph(true);
    }
    function resourceButton(o, cls) {
        const b = button('', () => pick(o.id), cls);
        b.append(el('small', o.kind), el('strong', o.name));
        b.title = [o.cluster_id, o.namespace, o.uid || o.id].filter(Boolean).join(' / ');
        return b;
    }
    function render() {
        const m = model, o = m.focus;
        byId('counts').textContent = m.counts.objects + ' 个资源对象 · ' + m.counts.edges + ' 条结构关系 · ' + m.counts.observations + ' 条观测';
        const coverage = byId('coverage');
        coverage.replaceChildren();
        if (!m.counts.observations) {
            coverage.append(el('p', '这次调查未保留可构图的结构化观测。图谱可读取 ' + m.coverage.tool_executions +
                ' 条工具执行记录，详情见「查看诊断过程」。'));
        } else if (m.coverage.unbound_observations) {
            const unbound = m.coverage.unbound_observations;
            const list = el('details'), body = el('div');
            list.append(el('summary', (m.counts.observations - unbound) + ' 条观测已关联资源 · 查看 ' + unbound + ' 条未绑定观测'), body);
            list.addEventListener('toggle', () => {
                if (list.open && !body.childNodes.length) loadObservations(body, 0, {unbound_only: true});
            });
            coverage.append(list);
        }
        byId('scope').textContent = [m.engine + ' ' + m.engine_version, m.scope.cluster_id, m.scope.namespace, m.revision].filter(Boolean).join(' / ');
        byId('export').disabled = false;
        byId('export-status').replaceChildren();
        byId('hypotheses').open = false;
        byId('hypotheses').querySelector('div').replaceChildren();
        const catalog = byId('objects');
        catalog.replaceChildren();
        for (const item of m.objects) {
            const b = resourceButton(item, 'og-resource');
            b.append(el('small', [item.namespace || item.cluster_id, item.uid || identities[item.identity_status]].filter(Boolean).join(' · ')));
            b.setAttribute('aria-pressed', String(item.id === o?.id));
            catalog.append(b);
        }
        if (!m.objects.length) catalog.append(el('p', query ? '没有匹配的对象。' : '尚无已识别的对象。'));
        pages(byId('object-pages'), offset, m.total_objects, m.limit, at => {
            offset = at; pick('');
        });
        byId('gaps').querySelector('div').replaceChildren(...(m.gaps.length ? m.gaps.map(g =>
            el('p', ONTOLOGY_GAP_LABELS[g] || g)) : [el('p', '构建过程未记录数据缺口。')]));
        const map = byId('map');
        map.replaceChildren();
        byId('evidence').replaceChildren();
        byId('observations').replaceChildren();
        byId('edge-pages').replaceChildren();
        if (!o) {
            map.append(el('p', query ? '可修改搜索条件重新查看。' : '这次调查尚无可绘制的资源对象。'));
            return;
        }
        const center = el('header', null, 'og-center');
        const title = el('h3', o.name);
        title.tabIndex = -1;
        center.append(el('small', o.kind), title, el('p', [o.cluster_id, o.namespace, identities[o.identity_status]].filter(Boolean).join(' / ')));
        if (o.uid) center.append(el('code', o.uid));
        map.append(center);
        if (o.topology_conflict) {
            const conflict = o.topology_conflict;
            const open = button('核对冲突观测', () => inspectAssertions('拓扑观测冲突',
                o.kind + '/' + o.name + ' · 取值不同的字段：' + conflict.changed_fields.join('、'),
                conflict.variants.map(ids => ({attributes: {}, evidence_ids: ids})), '字段取值组合'));
            byId('observations').append(el('p', '该 UID 记录了 ' + conflict.variants.length + ' 组不同的拓扑取值。'), open);
        }
        const branches = el('div', null, 'og-branches');
        for (const edge of m.edges) {
            const same = edge.source_id === edge.target_id;
            const outgoing = edge.source_id === o.id;
            const row = el('div', null, 'og-branch');
            const direction = same ? '↺ 自关联' : outgoing ? '→ 出向' : '← 入向';
            const label = relationLabel(edge.type);
            const line = el('span', direction + ' · ' + label, 'og-predicate');
            line.title = edge.type;
            const node = resourceButton(edge.neighbor, 'og-neighbor');
            node.setAttribute('aria-label', '查看 ' + edge.neighbor.kind + ' / ' + edge.neighbor.name);
            row.append(line, node, button('查看依据（' + edge.evidence_ids.length + '）', () => showEdge(edge), 'og-link'));
            branches.append(row);
        }
        if (m.edges.length) map.append(branches);
        else map.append(el('p', '该对象暂无已记录的结构关系。', 'og-empty'));
        pages(byId('edge-pages'), edgeOffset, m.total_edges, m.limit, at => {
            edgeOffset = at; loadGraph(true);
        });
        const details = el('details');
        details.append(el('summary', '查看该对象的观测'), el('div'));
        details.addEventListener('toggle', () => {
            if (details.open && !details.lastChild.childNodes.length) loadObservations(details.lastChild, 0);
        });
        byId('observations').append(details);
        details.open = m.total_edges === 0;
    }
    function collection(params) {
        return read('/api/rca/' + encodeURIComponent(run) + '/ontology/observations?' +
            new URLSearchParams({revision, ...params}));
    }
    function observation(o) {
        const item = el('article', null, 'og-record');
        const source = [o.source_agent, o.source, o.signal_type].filter(Boolean).join(' · ');
        item.append(el('strong', source || '来源未记录'), el('p', o.observation), el('small', [o.id,
            o.observed_at || o.event_time || o.sampled_at || o.timestamp || '时间未记录',
            o.identity_basis === 'verified_legacy_metric_labels' ? '对象名称来自已核对样本的历史指标标签' : '',
            o.excerpted ? '记录为节选' : ''].filter(Boolean).join(' · ')));
        const raw = o.raw_ref || {}, meta = raw.metadata || {}, spec = raw.spec || {};
        const fields = Object.fromEntries(Object.entries({
            kind: raw.kind, name: meta.name || raw.name, namespace: meta.namespace || raw.namespace,
            uid: meta.uid || raw.pod_uid || raw.uid, resourceVersion: o.resource_version,
            eventResourceVersion: o.event_resource_version, referencedResourceVersion: o.referenced_resource_version,
            labels: meta.labels, ownerReferences: meta.ownerReferences, selector: spec.selector,
            templateLabels: spec.template?.metadata?.labels, nodeName: spec.nodeName || raw.node_name,
            replicas: spec.replicas, nodeSelector: spec.nodeSelector, serviceType: spec.type,
            publishNotReadyAddresses: spec.publishNotReadyAddresses, addressType: raw.addressType,
            endpoints: Array.isArray(raw.endpoints) ? raw.endpoints.map(e => ({
                targetRef: e?.targetRef, addresses: e?.addresses, conditions: e?.conditions
            })) : null
        }).filter(([, value]) => value != null));
        if (Object.keys(fields).length) {
            const details = el('details');
            details.append(el('summary', '查看采集的资源字段'), el('pre', JSON.stringify(fields, null, 2)));
            item.append(details);
        }
        return item;
    }
    async function loadObservations(host, at, filter = {entity_id: focus}) {
        const version = graphVersion;
        host.textContent = '正在读取观测…';
        try {
            const data = await collection({...filter, offset: at, limit: 8});
            if (version !== graphVersion) return;
            const records = el('div');
            if (filter.unbound_only) {
                records.className = 'og-unbound-records';
                records.tabIndex = 0;
                records.setAttribute('role', 'region');
                records.setAttribute('aria-label', '本页未绑定观测');
            }
            records.append(...data.items.map(observation));
            host.replaceChildren(records);
            if (!data.total) host.append(el('p', filter.unbound_only ? '当前没有未绑定的观测。' : '该对象只出现在关系端点，尚无直接观测。'));
            const nav = el('div', null, 'og-pager');
            pages(nav, at, data.total, data.limit, n => loadObservations(host, n, filter));
            host.append(nav);
        } catch (err) {
            if (version === graphVersion) errorView(host, err, () => loadObservations(host, at, filter));
        }
    }
    function showEdge(edge) {
        const outgoing = model.focus.id === edge.source_id;
        const a = outgoing ? model.focus : edge.neighbor, b = outgoing ? edge.neighbor : model.focus;
        inspectAssertions('关系依据', a.kind + '/' + a.name + ' → ' + relationLabel(edge.type) +
            ' → ' + b.kind + '/' + b.name,
            edge.assertions?.length ? edge.assertions : [{attributes: {}, evidence_ids: edge.evidence_ids}],
            '记录的条件组合');
    }
    function inspectAssertions(heading, description, assertions, choiceLabel) {
        const version = graphVersion, host = byId('evidence');
        ++evidenceVersion;
        const title = el('h4', heading);
        title.tabIndex = -1;
        host.replaceChildren(title, el('p', description));
        const body = el('div');
        const show = async id => {
            const ticket = ++evidenceVersion;
            body.textContent = '正在读取来源记录…';
            try {
                const data = await collection({record_id: id, limit: 1});
                if (version !== graphVersion || ticket !== evidenceVersion) return;
                body.replaceChildren(data.items.length ? observation(data.items[0]) : el('p', '该引用未匹配到观测。'));
            } catch (err) {
                if (version === graphVersion && ticket === evidenceVersion) errorView(body, err, () => show(id));
            }
        };
        const fields = el('div', null, 'og-assertion');
        const sources = el('div');
        const toggle = value => value === true ? '是' : value === false ? '否' : '未记录';
        const renderAssertion = assertion => {
            ++evidenceVersion;
            fields.replaceChildren();
            sources.replaceChildren();
            body.replaceChildren();
            const attr = assertion.attributes;
            if (attr.selector) {
                fields.append(el('p', '匹配的选择器 · ' + Object.entries(attr.selector).map(([k, v]) => k + '=' + v).join('，')));
            }
            if (attr.binding === 'service_label_and_owner_uid') fields.append(el('p', '关联依据 · Service 名称标签和所有者 UID 均匹配'));
            if (attr.binding === 'service_name_label_only') fields.append(el('p', '关联依据 · Service 名称标签匹配，实例 UID 尚未验证'));
            if (attr.addresses) fields.append(el('p', '端点地址 · ' + attr.addresses.join('，')));
            if (attr.endpoint_conditions) {
                const c = attr.endpoint_conditions;
                fields.append(el('p', '端点原始条件 · ready ' + toggle(c.ready) + ' / serving ' + toggle(c.serving) +
                    ' / terminating ' + toggle(c.terminating)));
                fields.append(el('p', 'Service 发布未就绪地址 · ' + (typeof attr.publish_not_ready_addresses === 'boolean'
                    ? toggle(attr.publish_not_ready_addresses) : '未确认')));
            }
            if (attr.snapshot_consistency === 'not_atomic') fields.append(el('small', '涉及的资源分别采集，采集时间见各条观测。'));
            const ids = assertion.evidence_ids;
            if (!ids.length) {
                sources.textContent = '这组关系条件没有观测引用。';
                return;
            }
            const menu = el('select');
            menu.setAttribute('aria-label', '关系引用的观测');
            ids.forEach(id => menu.add(new Option([model.evidence_objects?.[id], id].filter(Boolean).join(' · '), id)));
            menu.addEventListener('change', () => show(menu.value));
            sources.append(menu);
            show(menu.value);
        };
        if (assertions.length > 1) {
            const label = el('label', choiceLabel + ' ');
            const choice = el('select');
            assertions.forEach((assertion, index) => {
                const address = assertion.attributes.addresses?.join('，');
                choice.add(new Option((index + 1) + ' / ' + assertions.length + (address ? ' · ' + address : ''), index));
            });
            choice.addEventListener('change', () => renderAssertion(assertions[Number(choice.value)]));
            label.append(choice);
            host.append(label);
        }
        host.append(fields, sources, body);
        renderAssertion(assertions[0]);
        title.focus();
    }
    async function download() {
        const ticket = graphVersion;
        const host = byId('export-status');
        byId('export').disabled = true;
        host.textContent = '正在生成 Semantica 图文件…';
        try {
            const res = await fetch('/api/rca/' + encodeURIComponent(run) + '/semantica-export?' +
                new URLSearchParams({revision}), {cache: 'no-store'});
            if (!res.ok) {
                const detail = await res.json().catch(() => ({}));
                const err = new Error(detail.detail || '导出失败（HTTP ' + res.status + '）');
                err.status = res.status;
                throw err;
            }
            const blob = await res.blob();
            if (ticket !== graphVersion) return;
            const url = URL.createObjectURL(blob);
            const link = el('a');
            link.href = url;
            link.download = 'semantica-' + revision + '.json';
            link.click();
            setTimeout(() => URL.revokeObjectURL(url), 60000);
            host.textContent = '图文件已生成，可在 Semantica Knowledge Explorer 中打开。';
        } catch (err) {
            if (ticket === graphVersion) errorView(host, err, download);
        } finally {
            if (ticket === graphVersion) byId('export').disabled = false;
        }
    }
    async function hypotheses() {
        const ticket = graphVersion, host = byId('hypotheses').querySelector('div');
        host.textContent = '正在读取诊断策略…';
        try {
            const data = await read('/api/rca/' + encodeURIComponent(run) + '/strategy');
            if (ticket !== graphVersion) return;
            const items = data.generation?.items || [];
            host.replaceChildren(el('small', data.revision));
            if (!items.length) host.append(el('p', '这份策略记录中没有初始候选假设。'));
            for (const item of items) host.append(el('p', [item.id, item.description].filter(Boolean).join(' · ')));
        } catch (err) {
            if (ticket === graphVersion) failure(host, err, hypotheses);
        }
    }
    async function schema() {
        const host = byId('schema-body');
        host.textContent = '正在读取模式…';
        try {
            const data = await read('/api/ontology/schema');
            host.replaceChildren(el('h4', '支持的对象类型'), el('p', data.object_types.join(' · ')),
                el('h4', '关系与构建依据'));
            const list = el('dl');
            for (const [type, spec] of Object.entries(data.relation_types)) {
                list.append(el('dt', relationLabel(type)), el('dd', type + ' · ' + spec.source));
            }
            host.append(list, el('h4', '对象动作'), el('p', data.action_types.map(a =>
                a.object_types.join('/') + ' · ' + a.id).join('，')));
        } catch (err) { failure(host, err, schema); }
    }
    function open(location) {
        if (!initialized) {
            initialized = true;
            byId('run').addEventListener('change', e => selectRun(e.target.value));
            byId('more-runs').addEventListener('click', () => history(true));
            byId('refresh').addEventListener('click', refresh);
            byId('export').addEventListener('click', download);
            byId('hypotheses').addEventListener('toggle', () => {
                if (byId('hypotheses').open && !byId('hypotheses').querySelector('div').childNodes.length) hypotheses();
            });
            byId('rca').addEventListener('click', () => openRCAHistoryRun(run));
            byId('find').addEventListener('submit', e => {
                e.preventDefault();
                query = byId('query').value.trim(); offset = 0; pick('');
            });
            byId('schema').addEventListener('toggle', () => {
                if (byId('schema').open && !byId('schema-body').childNodes.length) schema();
            });
        }
        if (location) selectRun(location.runId, location.objectId, location.revision);
        else loadGraph();
        history();
    }
    function fromRun(runId, objectId = '', revision = '') {
        switchView('ontology', {ontologyLocation: {runId, objectId, revision}});
    }
    return {open, fromRun, relationLabel};
})();
