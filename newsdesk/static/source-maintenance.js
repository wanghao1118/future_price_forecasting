/* Uses the same modal, keyboard focus trap and same-origin API as settings. */
(() => {
  const labels = {available:'可获取信息',requires_login:'需要登录会话',access_blocked:'访问受限',requires_access:'需要登录 / 访问受限',network_error:'请求失败',discovery_failed:'目录待修复',content_unavailable:'未取得正文',time_unverified:'时间未通过验证',no_qualified_items:'本轮无合格内容',unchecked:'尚未检测'};
  let data = null, timer = null, version = 0;
  const stamp = value => value ? value.replace('T',' ').replace(/(?:\.\d+)?(?:Z|\+00:00)$/,' UTC+0') : '—';
  function render() {
    if (state.modal !== 'source-maintenance' || !data) return;
    const query = $('#source-search').value.trim().toLowerCase(), filter = $('#source-state-filter').value, group = $('#source-group-filter').value;
    const scoped = data.sources.filter(s => !group || (group === 'new' ? Boolean(s.authority_row) : !s.authority_row));
    const rows = scoped.filter(s => (!query || `${s.name} ${s.source_id}`.toLowerCase().includes(query)) && (!filter || (filter === 'issues' ? s.status !== 'available' : s.status === filter)));
    const available = scoped.filter(s => s.status === 'available').length, unchecked = scoped.filter(s => s.status === 'unchecked').length;
    $('#source-health-counts').textContent = `${scoped.length} 个${group === 'new' ? '新增' : group === 'existing' ? '原有' : '已启用'}信源 · ${available} 个可获取信息 · ${scoped.length-available-unchecked} 个需要处理 · ${unchecked} 个尚未检测`;
    const running = data.job.running;
    $('#check-all-sources').disabled = running;
    $('#source-check-progress').textContent = running ? `正在检测 ${data.job.completed} / ${data.job.total}，结果会逐项更新` : data.job.error ? `检测未完成：${data.job.error}` : data.job.finished_at ? `本轮检测完成 ${data.job.completed} / ${data.job.total} · ${stamp(data.job.finished_at)}` : '自动更新新闻时也会刷新连通状态';
    $('#source-health-rows').innerHTML = rows.length ? rows.map(s => `<tr><td><a href="${escapeHTML(s.url)}" target="_blank" rel="noopener noreferrer">${escapeHTML(s.name)}</a><small>${s.authority_row ? '本次新增 · 榜单第 '+s.authority_row+' 行' : '原有信源'}</small></td><td><span class="source-health-tag ${s.status === 'available' ? 'is-available' : s.status === 'unchecked' ? '' : 'has-issue'}">${escapeHTML(labels[s.status] || s.status)}</span><small>${escapeHTML(s.message || '')}</small>${s.sample_url ? `<a class="source-sample" href="${escapeHTML(s.sample_url)}" target="_blank" rel="noopener noreferrer">查看已验证文章</a><small>发布：${escapeHTML(stamp(s.sample_published_at))}</small>` : ''}</td><td class="source-health-time">${escapeHTML(stamp(s.checked_at))}<small>${s.origin === 'manual' ? '手动检测' : s.origin === 'scheduled' ? '新闻自动更新' : '接入验证'}${s.candidate_count ? ' · 验证 '+s.candidate_count+' 条' : ''}</small>${s.last_success_at && s.status !== 'available' ? `<small>最近成功：${escapeHTML(stamp(s.last_success_at))}</small>` : ''}</td><td><button class="button" data-check-source="${escapeHTML(s.source_id)}" ${running ? 'disabled' : ''}>重测</button></td></tr>`).join('') : '<tr><td colspan="4" class="empty-source-health">没有符合条件的信源</td></tr>';
    $$('[data-check-source]').forEach(button => button.addEventListener('click', () => check([button.dataset.checkSource])));
  }
  async function refresh(current = version) {
    try {
      const value = await api('/api/sources/health');
      if (current !== version || state.modal !== 'source-maintenance') return;
      data = value; render();
      clearTimeout(timer);
      timer = setTimeout(() => refresh(current), data.job.running ? 2500 : 15000);
    } catch (error) {
      if (current === version && state.modal === 'source-maintenance') $('#source-check-progress').textContent = `读取失败：${error.message}`;
    }
  }
  async function check(ids) {
    $('#check-all-sources').disabled = true;
    $$('[data-check-source]').forEach(b => b.disabled = true);
    try { await api('/api/sources/check',{method:'POST',body:JSON.stringify(ids ? {source_ids:ids} : {})}); }
    catch (error) { toast(error.message,true); }
    await refresh();
  }
  $('#source-maintenance').addEventListener('click', () => {
    version++; clearTimeout(timer); closeSettingsMenu(); closeMenu();
    mountOverlay(`<div class="overlay modal-overlay"><section class="modal source-health-modal" role="dialog" aria-modal="true" aria-labelledby="source-health-title"><header class="modal-header"><h2 id="source-health-title">信源维护</h2><button type="button" class="icon-button" data-close aria-label="关闭信源维护">${icon('close')}</button></header><div class="source-health-body"><div class="source-health-summary" id="source-health-counts">正在读取信源状态…</div><p class="source-health-note">检测最新目录、可读正文与具体发布时间。检测通过的内容仍需经过原油相关性审核后才会入库。</p><div class="source-health-tools"><input type="search" id="source-search" placeholder="搜索信源" aria-label="搜索信源"><select id="source-group-filter" aria-label="筛选信源范围"><option value="">全部信源</option><option value="new">本次新增</option><option value="existing">原有信源</option></select><select id="source-state-filter" aria-label="筛选连通状态"><option value="">全部状态</option><option value="available">可获取信息</option><option value="issues">需要处理 / 尚未检测</option>${Object.entries(labels).filter(([k])=>k!=='available').map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select><button class="button primary" id="check-all-sources">检测全部信源</button></div><p id="source-check-progress" class="source-health-note" role="status" aria-live="polite"></p><div class="source-health-table-wrap"><table class="source-health-table"><thead><tr><th>信源</th><th>连通情况</th><th>最近检测</th><th>操作</th></tr></thead><tbody id="source-health-rows"></tbody></table></div></div></section></div>`, 'source-maintenance');
    returnFocus = $('#settings-button');
    $('#source-search').addEventListener('input',render);
    $('#source-state-filter').addEventListener('change',render);
    $('#source-group-filter').addEventListener('change',render);
    $('#check-all-sources').addEventListener('click',()=>check());
    refresh();
  });
})();
