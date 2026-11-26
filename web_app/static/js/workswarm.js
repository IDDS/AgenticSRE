const workSwarm = (() => {
    const byId = id => document.getElementById(id);
    const form = byId('ws-form');
    const history = byId('ws-history');
    const key = 'agenticsre.workswarm.sessions';
    const labels = {read_service: 'Service 配置', read_endpoints: 'EndpointSlice 就绪状态',
        read_pods: '后端 Pod', list_diagnostic_skills: '诊断 Skill 目录', run_diagnostic_skill: '诊断 Skill'};
    let recent = [];
    try { recent = JSON.parse(sessionStorage.getItem(key) || '[]'); } catch {}
    let selected = recent[0]?.id || '';
    let ready = false;
    let busy = false;
    let timer;
    function controls() {
        byId('ws-send').disabled = busy || !ready;
        form.elements.namespace.readOnly = form.elements.service.readOnly = Boolean(selected) || busy;
        byId('ws-new').disabled = history.disabled = busy;
    }
    function options() {
        history.replaceChildren(new Option('新会话', ''));
        for (const item of recent) history.add(new Option(item.target, item.id));
        history.value = selected;
    }
    function node(tag, value) {
        const el = document.createElement(tag);
        el.textContent = value;
        return el;
    }
    async function request(path, init) {
        const response = await fetch('/api/workswarm/' + path, init);
        if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            throw new Error(typeof body.detail === 'string' ? body.detail : '请求失败（' + response.status + '）');
        }
        return response.json();
    }
    function render(session) {
        form.elements.namespace.value = session.namespace;
        form.elements.service.value = session.service;
        const turns = byId('ws-turns');
        const expanded = new Set([...turns.querySelectorAll('details[open]')].map(el => el.dataset.call));
        turns.replaceChildren();
        for (const turn of session.turns) {
            const section = node('section', '');
            section.append(node('h4', turn.question));
            for (const item of turn.evidence) {
                const row = node('details', '');
                row.dataset.call = item.call_id;
                row.open = expanded.has(item.call_id);
                const status = {reading: '读取中', ok: '已读取', error: '读取失败'};
                row.append(node('summary', (labels[item.tool] || item.tool) + ' · ' + status[item.status]));
                row.append(node('small', item.tool + (item.skill_id ? ' · ' + item.skill_id : '')));
                const obs = item.observation;
                if (item.tool === 'read_endpoints' && obs?.readiness) {
                    const counts = obs.readiness;
                    section.append(node('p', `端点记录 ${obs.endpoint_count} 条 · ready=true ${counts.true} · ready=false ${counts.false} · 未声明 ${counts.unspecified}`));
                    if (obs.truncated) section.append(node('p', `明细展示 ${obs.slices.length}/${obs.count} 个 EndpointSlice、${obs.shown_endpoint_count}/${obs.endpoint_count} 条端点记录。计数包含全部记录。`));
                }
                if (item.tool === 'read_pods' && obs?.truncated) {
                    section.append(node('p', `Pod 明细展示 ${obs.pods.length}/${obs.count} 个匹配对象。`));
                }
                if (item.status === 'error' && obs?.error) section.append(node('p', obs.error));
                if (item.observation) row.append(node('pre', JSON.stringify(item.observation, null, 2)));
                section.append(row);
            }
            if (turn.missing_tools?.length) {
                section.append(node('p', '本轮缺少证据 · ' + turn.missing_tools.map(name => labels[name] || name).join('、')));
            }
            if (turn.answer) {
                const answer = node('div', '');
                answer.innerHTML = renderSimpleMarkdown(turn.answer);
                section.append(answer);
            }
            if (turn.error) section.append(node('p', turn.error));
            turns.append(section);
        }
        byId('ws-status').textContent = {running: '诊断中', completed: '已完成', partial: '证据不全', error: '诊断失败'}[session.status];
    }
    async function load(id) {
        clearTimeout(timer);
        if (!id) return;
        try {
            const data = await request('sessions/' + encodeURIComponent(id));
            if (selected !== id) return;
            render(data);
            busy = data.status === 'running';
            if (busy) timer = setTimeout(() => load(id), 1500);
        } catch (error) {
            if (selected !== id) return;
            byId('ws-status').textContent = error.message;
            busy = false;
        }
        controls();
    }
    function select(id) {
        selected = id;
        clearTimeout(timer);
        byId('ws-turns').replaceChildren();
        form.reset();
        history.value = id;
        byId('ws-status').textContent = id ? '加载中' : '待开始';
        busy = Boolean(id);
        controls();
        load(id);
    }
    byId('ws-new').onclick = () => select('');
    history.onchange = () => select(history.value);
    form.onsubmit = async event => {
        event.preventDefault();
        if (busy || !ready) return;
        const body = Object.fromEntries(new FormData(form));
        if (selected) body.session_id = selected;
        busy = true;
        controls();
        byId('ws-status').textContent = '正在提交';
        try {
            const data = await request('turn', {method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)});
            selected = data.session_id;
            recent = [{id: selected, target: body.namespace + '/' + body.service},
                ...recent.filter(item => item.id !== selected)].slice(0, 20);
            try { sessionStorage.setItem(key, JSON.stringify(recent)); } catch {}
            options();
            form.elements.question.value = '';
            await load(selected);
        } catch (error) {
            busy = false;
            byId('ws-status').textContent = error.message;
            controls();
        }
    };
    return {async open() {
        options();
        try {
            const data = await request('capabilities');
            ready = data.available;
            byId('ws-runtime').textContent = ready ? '运行时已连接' : data.error;
            byId('ws-model').textContent = '模型 · ' + (data.model || '未配置');
            byId('ws-tools').textContent = (data.tools || []).map(name => labels[name] || name).join('、');
        } catch (error) {
            ready = false;
            byId('ws-runtime').textContent = error.message;
        }
        controls();
        if (selected) await load(selected);
    }};
})();
