// Run on the remote host: node tests/test_skill_followup_render.js
const assert = require('assert');
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync('web_app/static/js/app.js', 'utf8');
const start = source.indexOf('function renderStrategyAudit(data) {');
const end = source.indexOf('\nasync function openStrategyEvidence(', start);
assert(start >= 0 && end > start);
const htmlEscape = value => String(value ?? '').replace(/[&<>"']/g, c => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
})[c]);
const context = {escapeHtml: htmlEscape, escapeHtmlAttr: htmlEscape,
  formatConfidence: String, rcaAgentLabel: String, STRATEGY_REASONS: {}};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const data = {recording_status:'recorded', generation:{items:[]}, rounds:[{iteration:1, decision:{},
  readiness:{blockers:['unverified_candidate_rejections'], unverified_rejection_ids:['H2']}}],
  adaptive_skill_replanning_enabled:false,
  followup:{attempted:true, candidate_ids:['H2'], stop_reason:'followup_collected_review_required',
    reassessment_received:false, commands:[{command:'get events <script>', executed:true, success:true}]},
  pod_termination_evidence:{items:[{pod:'pod-a',container:'app',state:'previous_state',
    reason:'OOMKilled <tag>',exit_code:137,finished_at:'2026-10-28T00:59:00Z',
    collected_at:'2026-10-28T01:00:00Z',time_status:'after_collection',observation_id:'obs-1'}],omitted:0},
  independent_reviews:[{stage:'agent_skill_initial_review',items:[],skill_review:{independent_role_call:true,
    provider_receipt:{schema_version:1,source:'provider_response',completion_status:'complete',finish_reason:'stop',
      refusal_present:false,tool_call_present:false}}},
    {stage:'agent_skill_final_review',items:[],skill_review:{independent_role_call:false}}]};
const output = context.renderStrategyAudit(data);
assert(output.includes('受限补证 · 已尝试'));
assert(output.includes('自适应 Skill 重规划尚未启用'));
assert(output.includes('复核 1 · 初次质量复核'));
assert(output.includes('复核 2 · 补证后复核'));
assert(output.includes('独立模型：已调用；已记录完整结束凭据'));
assert(output.includes('独立模型：未调用（输入或预算未满足）；没有完整结束凭据'));
assert(output.includes('未收到有效的补证后评估'));
assert(output.includes('排除结论尚未核实的候选：H2'));
assert(output.includes('上一次终止') && output.includes('OOMKilled &lt;tag&gt;'));
assert(output.includes('报告时间尚未绑定事故时间窗'));
assert(output.includes('报告结束时间晚于采集时间，需核对时钟'));
assert(output.includes('&lt;script&gt;') && !output.includes('<script>'));
const old = context.renderStrategyAudit({recording_status:'recorded', generation:{items:[]}, rounds:[],
  independent_reviews:[{stage:'agent_skill_final_review',items:[]}]});
assert(old.includes('最终质量复核') && !old.includes('补证后复核'));
console.log('Skill follow-up rendering checks passed');
