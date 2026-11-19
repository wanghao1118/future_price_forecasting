const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let report;
function render(){
 const query=document.querySelector('#query').value.trim().toLocaleLowerCase();
 const status=document.querySelector('#coverage').value,level=document.querySelector('#level').value;
 const rows=report.entries.filter(row=>(!query||(row.name+' '+row.category+' '+row.aliases.join(' ')).toLocaleLowerCase().includes(query))&&(!status||row.coverage_status===status)&&(!level||row.level===level));
 document.querySelector('#count').textContent=`显示 ${rows.length} / ${report.source_count} 个来源`;
 document.querySelector('#rows').innerHTML=rows.length?rows.map(row=>{
  const collection=row.collection_enabled?'已接入':row.issuer_news_count?'未接入，已有转载':'未接入';
  const notes=row.configured_sources.map(s=>`${s.name}：${s.enabled?(s.last_collection_status==='partial'?'最近采集部分完成':s.last_collection_status==='failed'?'最近采集失败':'已启用采集'):'未启用采集'}${s.last_time_skipped?`；最近 ${s.last_time_skipped} 条因缺少合格发布时间未纳入`:''}。`).join(' ');
  return `<tr><td class="source">${escapeHTML(row.name)}<small>${escapeHTML(row.category)} · ${escapeHTML(row.scope)}</small></td><td>${escapeHTML(row.level)}</td><td>${row.issuer_news_count.toLocaleString()}</td><td>${row.feed_news_count.toLocaleString()}</td><td>${escapeHTML(collection)}</td><td>${escapeHTML(row.coverage_status)}</td><td><details><summary>查看依据</summary><p>${escapeHTML(row.basis)}</p>${row.notes?`<p>${escapeHTML(row.notes)}</p>`:''}${notes?`<p>${escapeHTML(notes)}</p>`:''}<p>${escapeHTML(row.sheet)}!${escapeHTML(row.level_cell)}</p></details></td></tr>`;
 }).join(''):'<tr><td colspan="7" class="empty">没有符合筛选条件的来源</td></tr>';
}
fetch('/api/source-coverage').then(r=>{if(!r.ok)throw new Error('覆盖清单暂时无法读取');return r.json()}).then(data=>{
 report=data;
 const checked=new Date(data.checked_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
 document.querySelector('#intro').textContent=`依据 ${data.workbook} · ${data.source_sheet} ${data.source_range}。当前 ${data.visible_news_count.toLocaleString()} 条可见新闻中，${data.rated_news_count.toLocaleString()} 条匹配到原始发布机构档位。核对时间 ${checked}（北京时间）。`;
 document.querySelector('#summary').innerHTML=['已有新闻','已接入，暂无有效入库','未覆盖'].map(label=>`<div><strong>${data.counts[label]||0}</strong>${escapeHTML(label)}</div>`).join('');render();
 for(const id of ['query','coverage','level'])document.getElementById(id).addEventListener('input',render);
}).catch(error=>{document.querySelector('#intro').className='error';document.querySelector('#intro').textContent=error.message;});
