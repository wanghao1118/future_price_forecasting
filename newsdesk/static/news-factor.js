(function (global) {
  'use strict';

  const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
  const today = () => new Date().toISOString().slice(0, 10);
  const storageKey = `news-factor-experiments:${location.host}`;
  const ranges = [
    {id:'train', label:'训练集', bmk:'BMK-TRAIN', start:'2026-01-01', end:'2026-05-31', note:'1–5 月日频价格'},
    {id:'valid', label:'验证集', bmk:'BMK-VALID', start:'2026-06-01', end:'2026-07-31', note:'6–7 月日频价格'},
    {id:'backtest', label:'回测集', bmk:'BMK-BACKTEST', start:'2026-08-01', end:today, note:'8 月至当前日期，动态更新'},
    {id:'live', label:'实盘', bmk:'BMK-LIVE', start:null, end:today, note:'按实验开始时间起算，日频更新'},
  ];
  let options = null;
  let root = null;
  let active = false;
  let view = null;
  let loading = false;
  let loadVersion = 0;
  let market = [];

  function contracts() {
    return (global.OilContracts?.families || []).flatMap(family => (family.contracts || []).map(contract => ({...contract, family:family.id, familyLabel:family.label || family.name || family.id})));
  }

  function experiments() {
    try {
      const value = JSON.parse(localStorage.getItem(storageKey) || '[]');
      return Array.isArray(value) ? value : [];
    } catch {
      return [];
    }
  }

  function saveExperiments(items) {
    try { localStorage.setItem(storageKey, JSON.stringify(items)); } catch { /* The page remains usable without persistence. */ }
  }

  function formatPeriod(start, end) {
    if (!start) return `实验开始时间 → ${end}`;
    return `${start} → ${end}`;
  }

  function rangeCount(start, end) {
    return market.reduce((total, item) => total + item.bars.filter(bar => {
      const date = String(bar.trading_date || '').slice(0, 10);
      return date && date >= start && date <= end;
    }).length, 0);
  }

  function latestBar(item) {
    return item.bars[item.bars.length - 1] || null;
  }

  function bmkCards() {
    const current = today();
    return ranges.map(range => {
      const end = range.id === 'backtest' || range.id === 'live' ? current : range.end;
      const count = range.start ? rangeCount(range.start, end) : null;
      return `<article class="news-factor-bmk-card"><div class="news-factor-card-top"><span>${escapeHTML(range.label)}</span><strong>${escapeHTML(range.bmk)}</strong></div><h3>${escapeHTML(range.start ? formatPeriod(range.start, end) : '实验开始时间 → 当前日期')}</h3><p>${escapeHTML(range.note)}</p><div class="news-factor-card-stat">${count === null ? '按实验卡片计算' : `${count.toLocaleString('zh-CN')} 根日K`}</div></article>`;
    }).join('');
  }

  function marketTable() {
    if (!market.length) return '<div class="news-factor-empty">当前没有读取到日频价格数据。</div>';
    return `<div class="news-factor-table-wrap"><table class="news-factor-table"><thead><tr><th>合约</th><th>品种</th><th>最新交易日</th><th>收盘</th><th>日K数量</th></tr></thead><tbody>${market.map(item => {
      const bar = latestBar(item);
      return `<tr><td>${escapeHTML(item.code || item.contract.id)}</td><td>${escapeHTML(item.contract.familyLabel)}</td><td>${escapeHTML(bar?.trading_date || '—')}</td><td>${bar?.close == null ? '—' : Number(bar.close).toFixed(2)}</td><td>${item.bars.length.toLocaleString('zh-CN')}</td></tr>`;
    }).join('')}</tbody></table></div>`;
  }

  function renderBMK() {
    const current = today();
    root.innerHTML = `<div class="news-factor-page"><header class="news-factor-page-header"><div><span class="news-factor-eyebrow">BMK · DAILY FUTURES DATA</span><h2>BMK 日频价格数据</h2><p>训练集、验证集、回测集和实盘统一使用 BMK 标注，回测截止日随当前日期更新。</p></div><span class="news-factor-updated">数据日期 ${escapeHTML(current)}</span></header><section class="news-factor-bmk-grid" aria-label="BMK 数据分段">${bmkCards()}</section><section class="news-factor-section"><div class="news-factor-section-heading"><h3>当前采集的期货价格</h3><span>${market.length ? `${market.length} 个合约 · 日频` : '读取中'}</span></div>${loading ? '<div class="news-factor-loading">正在读取日频行情…</div>' : marketTable()}</section></div>`;
  }

  function experimentDatasets(startAt) {
    const start = String(startAt || '').slice(0, 10) || today();
    return ranges.map(range => ({...range, end:range.id === 'backtest' || range.id === 'live' ? today() : range.end, start:range.id === 'live' ? start : range.start}));
  }

  function renderExperiments() {
    const items = experiments();
    root.innerHTML = `<div class="news-factor-page"><header class="news-factor-page-header"><div><span class="news-factor-eyebrow">EXPERIMENTS · BMK TRACKING</span><h2>experiments</h2><p>每个实验使用一张卡片记录，实盘区间从该实验的开始时间起算。</p></div><span class="news-factor-updated">${items.length} 个实验</span></header><section class="news-factor-experiment-form"><form id="news-factor-experiment-form"><div><label for="news-factor-experiment-name">实验名称</label><input id="news-factor-experiment-name" name="name" required maxlength="120" placeholder="例如：因子组合 v1"></div><div><label for="news-factor-experiment-start">实验开始时间</label><input id="news-factor-experiment-start" name="startAt" type="datetime-local" required></div><div class="news-factor-form-wide"><label for="news-factor-experiment-notes">实验说明</label><textarea id="news-factor-experiment-notes" name="notes" rows="3" placeholder="记录因子、模型、参数或观察目标"></textarea></div><button class="button primary" type="submit">新增实验卡片</button></form></section><section class="news-factor-experiment-list">${items.length ? items.map(renderExperimentCard).join('') : '<div class="news-factor-empty">还没有实验卡片。创建第一张卡片后，BMK 会自动显示该实验的实盘起算时间。</div>'}</section></div>`;
  }

  function renderExperimentCard(item) {
    const datasets = experimentDatasets(item.startAt);
    return `<article class="news-factor-experiment-card"><header><div><span class="news-factor-eyebrow">${escapeHTML(item.createdAt?.slice(0, 10) || '')}</span><h3>${escapeHTML(item.name)}</h3></div><button type="button" class="text-button" data-delete-experiment="${escapeHTML(item.id)}">删除</button></header><p>${escapeHTML(item.notes || '未填写实验说明')}</p><div class="news-factor-experiment-datasets">${datasets.map(dataset => `<div><strong>${escapeHTML(dataset.bmk)}</strong><span>${escapeHTML(dataset.label)}</span><small>${escapeHTML(formatPeriod(dataset.start, dataset.end))}</small></div>`).join('')}</div></article>`;
  }

  async function loadBMK() {
    const version = ++loadVersion;
    loading = true;
    renderBMK();
    const rows = await Promise.allSettled(contracts().map(async contract => {
      const response = await options.api(`/api/market/${encodeURIComponent(contract.id)}?period=1d`);
      return {contract, code:contract.code, bars:Array.isArray(response.bars) ? response.bars : []};
    }));
    if (!active || version !== loadVersion) return;
    market = rows.filter(row => row.status === 'fulfilled').map(row => row.value).filter(row => row.bars.length);
    loading = false;
    renderBMK();
  }

  function bindEvents() {
    root.addEventListener('submit', event => {
      if (event.target.id !== 'news-factor-experiment-form') return;
      event.preventDefault();
      const data = new FormData(event.target);
      const items = experiments();
      items.unshift({id:`experiment-${Date.now()}`, name:String(data.get('name') || '').trim(), startAt:String(data.get('startAt') || ''), notes:String(data.get('notes') || '').trim(), createdAt:new Date().toISOString()});
      saveExperiments(items);
      renderExperiments();
    });
    root.addEventListener('click', event => {
      const button = event.target.closest('[data-delete-experiment]');
      if (!button) return;
      saveExperiments(experiments().filter(item => item.id !== button.dataset.deleteExperiment));
      renderExperiments();
    });
  }

  function initialize(config) {
    if (options) return;
    options = config;
    root = document.querySelector('#research-workspace');
    if (root) bindEvents();
  }

  function show(next) {
    if (!root) return;
    active = true;
    view = next || {};
    if (view.subsection === 'bmk') loadBMK();
    else if (view.subsection === 'experiments') renderExperiments();
    else root.innerHTML = '';
  }

  function hide() {
    active = false;
    loadVersion += 1;
  }

  global.NewsFactor = {initialize,show,hide};
})(window);
