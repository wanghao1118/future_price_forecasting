(async()=>{
'use strict';
const response=await fetch('/source-feasibility.json',{cache:'no-store'});
if(!response.ok)throw new Error('最新核查清单暂时无法读取，请刷新重试');
const report=await response.json();
const escapeHTML=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function safeLink(value){return /^https?:\/\//.test(value)?escapeHTML(value):'#';}
const passed=report.entries.filter(e=>e.sample_meets_accepted_time_policy).length,pending=report.entries.length-passed;
const countStatus=key=>report.entries.filter(e=>e.status===key).length;
document.getElementById('scope').textContent=`榜单共 95 个来源。本轮检查其中尚未配置采集适配器的 ${report.source_count} 个来源，另有 23 个已配置来源不在本轮测试范围内。`;
document.getElementById('accepted-policy').textContent='找到至少一篇正文可读、具体发布时间与时区可核实的内容，即视为来源测试通过。通过测试后，仍需开发接入并逐条审核新闻。';
document.getElementById('timezone-policy').textContent='时区规则：自媒体账号按用户确认采用中国标准时间 UTC+8，再换算为 UTC+0。源站已有明确偏移或 Unix 时间戳的，保留原绝对时刻。';
document.getElementById('updated').textContent=`报告更新于 ${new Date(report.checked_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})}（北京时间）`;
document.getElementById('cards').innerHTML=[['本轮测试',report.source_count,'','来源总数'],['已通过',passed,'passed','已找到正文和时间合格的样本'],['尚未通过',pending,'pending','可按下方原因筛选查看']].map(([label,count,cls,note])=>`<div class="card ${cls}"><span>${label}</span><strong>${count}</strong><small>${note}</small></div>`).join('');
const reasons=[['time_pending','时间或时区待核实'],['identity_pending','来源身份或入口待确认'],['unavailable','尚未取得可用正文']];
document.getElementById('breakdown').innerHTML=reasons.map(([key,label])=>`<button type="button" data-status="${key}" aria-pressed="false">${label} · ${countStatus(key)}</button>`).join('');
for(const [value,label,count] of [['pending','尚未通过',pending],['passed','已通过',passed],['','全部来源',report.source_count]])document.querySelector(`#status option[value="${value}"]`).textContent=`${label}（${count}）`;
document.getElementById('limits').innerHTML=report.limits.map(v=>`<li>${escapeHTML(v)}</li>`).join('');
for(const [value,label] of Object.entries(report.status_labels)){const o=document.createElement('option');o.value=value;o.textContent=label;document.getElementById('status').append(o);}
function render(){
 const query=document.getElementById('query').value.trim().toLocaleLowerCase(),status=document.getElementById('status').value,level=document.getElementById('level').value;
 const entries=report.entries.filter(e=>(!status||(status==='passed'?e.sample_meets_accepted_time_policy:status==='pending'?!e.sample_meets_accepted_time_policy:e.status===status))&&(!level||e.level===level)&&(!query||[e.name,e.content_result,e.conclusion,e.policy_note,e.publication_field,e.timezone_evidence,e.access_mode].join(' ').toLocaleLowerCase().includes(query)));
 document.getElementById('count').textContent=`显示 ${entries.length} / ${report.source_count} 个来源 · 已通过 ${passed} 个 · 尚未通过 ${pending} 个`;
 for(const button of document.querySelectorAll('[data-status]'))button.setAttribute('aria-pressed',String(button.dataset.status===status));
 document.getElementById('rows').innerHTML=entries.length?entries.map(e=>{
  const utc=e.published_utc?e.published_utc.replace('T',' ').replace('Z',' UTC+0'):'未转换：没有充分证据';
  const scope=e.status==='ready'?'所测来源渠道发布时间':e.published_utc?'所测平台发布时间；不是原机构最早首发':'时间待确认';
  return `<tr><td><div class="name">${escapeHTML(e.name)}</div><div class="muted">${escapeHTML(e.level)} · ${escapeHTML(e.category)}<br>榜单第 ${e.row} 行</div></td><td><span class="badge ${e.status}">${e.sample_meets_accepted_time_policy?'已通过 · ':''}${escapeHTML(e.status_label)}</span><div class="note">${escapeHTML(e.content_result)}</div>${e.access_mode?`<div class="access">${escapeHTML(e.access_mode)}</div>`:''}</td><td><div class="muted">${escapeHTML(scope)}</div><div class="stamp">${escapeHTML(utc)}</div>${e.publication_raw?`<div class="muted">原始值</div><div class="stamp">${escapeHTML(e.publication_raw)}</div>`:''}${e.timezone_evidence?`<p class="note">${escapeHTML(e.timezone_evidence)}</p>`:''}</td><td><div class="note">${escapeHTML(e.conclusion)}</div><p><a href="${safeLink(e.sample_url)}" target="_blank" rel="noopener noreferrer">查看所测页面</a></p><details><summary>查看时间字段与核查依据</summary><p>样本：${escapeHTML(e.sample_title||e.sample_url)}</p><p>时间字段：${escapeHTML(e.publication_field||'未确认')}</p><p>抓取时刻（不是发布时间）：${escapeHTML(e.sample_fetched_at)}</p><p>本地证据：${escapeHTML(e.sample_evidence_file)}</p>${e.policy_note?`<p class="detail-policy">${escapeHTML(e.policy_note)}</p>`:''}<p>本来源共 ${e.tested_urls.length} 个 HTTP 请求记录；完整 URL、状态与错误详见 JSON。</p></details></td></tr>`;
 }).join(''):'<tr><td colspan="4" class="empty">没有符合筛选条件的来源</td></tr>';
}
for(const id of ['query','status','level'])document.getElementById(id).addEventListener('input',render);
for(const button of document.querySelectorAll('[data-status]'))button.addEventListener('click',()=>{document.getElementById('status').value=button.dataset.status;render();});
document.getElementById('footer').textContent=`数据依据：Forecasting数据库权威榜单.xlsx · ${report.workbook_range}。置信等级沿用榜单，不代表单条新闻真实性判断。汇总生成时间：${report.checked_at}。`;
render();

})().catch(error=>{document.getElementById('count').textContent=error.message;});
