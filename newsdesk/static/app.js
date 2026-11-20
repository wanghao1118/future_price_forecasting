if (window.location.protocol === 'file:') {
  window.location.replace('http://127.0.0.1:8765/');
  throw new Error('原油工作台需要通过 http://127.0.0.1:8765/ 打开');
}

'use strict';

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const paths = {
  archive:'M3 4h18v4H3z M5 8v12h14V8 M9 12h6', layers:'m12 3 9 5-9 5-9-5 9-5Z M3 12l9 5 9-5 M3 16l9 5 9-5',
  database:'M20 6c0 2-3.6 3.5-8 3.5S4 8 4 6s3.6-3.5 8-3.5S20 4 20 6Z M4 6v12c0 2 3.6 3.5 8 3.5s8-1.5 8-3.5V6 M4 12c0 2 3.6 3.5 8 3.5s8-1.5 8-3.5',
  'arrow-up-right':'M7 17 17 7 M6 7h11v11', plus:'M12 5v14 M5 12h14', search:'M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z',
  upload:'M12 16V3 M7 8l5-5 5 5 M4 15v5h16v-5', download:'M12 3v13 M7 11l5 5 5-5 M4 17v4h16v-4',
  globe:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z M3 12h18 M12 3c5 6 5 12 0 18-5-6-5-12 0-18Z',
  signal:'M4 19v-5 M9 19V9 M14 19V5 M19 19V2', filter:'M4 6h16 M7 12h10 M10 18h4', list:'M9 5h12 M9 12h12 M9 19h12 M3 5h1 M3 12h1 M3 19h1',
  grid:'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z', refresh:'M20 7V3l-4 4 M20 7a8 8 0 1 0 1 6',
  info:'M12 11v6 M12 7h.01 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z', check:'m5 12 4 4L19 6', clock:'M12 8v5l3 2 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
  link:'m10 14 4-4 M8 16l-1 1a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0 M16 8l1-1a4 4 0 0 1 6 6l-5 5a4 4 0 0 1-6 0',
  'chevron-right':'m9 5 7 7-7 7', 'chevron-left':'m15 5-7 7 7 7', 'chevron-up':'m6 15 6-6 6 6', close:'m6 6 12 12 M6 18 18 6', edit:'m16 3 5 5 M3 21l5-1L21 7a2 2 0 0 0-5-5L3 15v6Z',
  trash:'M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7', file:'M13 3H5v18h14V9l-6-6Z M13 3v6h6 M8 13h8 M8 17h5',
  menu:'M4 6h16 M4 12h16 M4 18h16', target:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z M17 12a5 5 0 1 1-10 0 5 5 0 0 1 10 0Z M12 10v4 M10 12h4',
  bookmark:'M5 3h14v18l-7-5-7 5V3Z', alert:'m12 3 10 18H2L12 3Z M12 9v5 M12 17h.01', copy:'M8 8h13v13H8z M16 8V3H3v13h5',
};
function icon(name) { return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${paths[name] || paths.file}"/></svg>`; }
function hydrateIcons(root = document) { $$('[data-icon]', root).forEach(element => { element.innerHTML = icon(element.dataset.icon); }); }
const shortNames = {supply:'生产与供应', demand:'需求与经济', inventory:'库存与储备', refining:'炼厂与成品油', geopolitics:'地缘与制裁', logistics:'运输与交付', macro:'宏观与流动性', policy:'能源与政策', positioning:'资金与交易', weather:'天气与灾害'};
let state = {items:[], total:0, realTotal:0, categoryTotals:{}, config:null, category:'', researchSection:'', researchSubsection:'', researchMode:'backtest', researchFamily:'all', researchQuery:'', contractId:'wti-202612', query:'', source:'', asset:'', direction:'', verification:'', demo:'real', time:'', sort:'published', view:'list', page:1, perPage:10, modal:null, detailId:null, loading:true};
let returnFocus = null;
let currentFormItem = null;
let formDirty = false;
let formSaving = false;
let moreFiltersOpen = false;
let contractChart = null;
let updateSettings = null;
let settingsSaving = false;
let settingsRequestVersion = 0;
let updatePollRunning = false;
let observedUpdateFinishes = null;
const sidebarStorageKey = 'oil-signal.sidebar.v1';
const sidebarSectionIds = ['leaderboard','environment','benchmark','news-factor','trading-return','contracts','news'];
// The sidebar is intentionally seeded from the AIHub smoke-test allowlist.  The
// evaluation API can add more strategy records later, but a model is visible
// before its first run so that the leaderboard is easy to discover.
const leaderboardModels = {
  GPT: [
    {id:'gpt-6.1-sol',label:'gpt-6.1-sol'}, {id:'gpt-6.1-terra',label:'gpt-6.1-terra'},
    {id:'gpt-6-sol',label:'gpt-6-sol'}, {id:'gpt-6-luna',label:'gpt-6-luna'},
    {id:'gpt-6-astra',label:'gpt-6-astra'},
  ],
  Claude: [{id:'claude-haiku-5-5',label:'claude-haiku-5-5'}, {id:'claude-fable-5-1',label:'claude-fable-5-1'}],
  DeepSeek: [{id:'deepseek-v4-pro',label:'deepseek-v4-pro'}, {id:'deepseek-v4.1-flash',label:'deepseek-v4.1-flash'}],
  Kimi: [{id:'kimi-k3.1-pro',label:'kimi-k3.1-pro'}, {id:'kimi-k3',label:'kimi-k3'}],
  GLM: [{id:'glm-5.3',label:'glm-5.3'}, {id:'glm-5.3-flash',label:'glm-5.3-flash'}],
  Hy: [{id:'hy3',label:'hy3'}, {id:'hy4-preview',label:'hy4-preview'}],
  Qwen: [{id:'qwen3.7-plus',label:'qwen3.7-plus'}],
  Doubao: [{id:'doubao-seed-2-0-lite-260428',label:'doubao-seed-2-0-lite-260428'}],
};
const leaderboardModelFamily = Object.fromEntries(Object.entries(leaderboardModels).flatMap(([family,models]) => models.map(model => [model.id,family])));
const sidebarPreferences = readSidebarPreferences();
const collapsedSections = new Set(sidebarPreferences.collapsedSections);
const expandedFamilies = new Set(sidebarPreferences.expandedFamilies);
const expandedBenchmarkFamilies = new Set(sidebarPreferences.expandedBenchmarkFamilies);
const expandedLeaderboardFamilies = new Set(sidebarPreferences.expandedLeaderboardFamilies);
const expandedTradingReturnFolders = new Set(sidebarPreferences.expandedTradingReturnFolders);

function readSidebarPreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem(sidebarStorageKey) || '{}');
    return {
      collapsedSections: Array.isArray(saved?.collapsedSections) ? saved.collapsedSections.filter(id => sidebarSectionIds.includes(id)) : [],
      expandedFamilies: Array.isArray(saved?.expandedFamilies) ? saved.expandedFamilies.filter(id => ['WTI','Brent','SC'].includes(id)) : ['WTI'],
      expandedBenchmarkFamilies: Array.isArray(saved?.expandedBenchmarkFamilies) ? saved.expandedBenchmarkFamilies.filter(id => ['WTI','Brent','SC'].includes(id)) : ['WTI','Brent','SC'],
      expandedLeaderboardFamilies: Array.isArray(saved?.expandedLeaderboardFamilies) ? saved.expandedLeaderboardFamilies.filter(id => Object.prototype.hasOwnProperty.call(leaderboardModels,id)) : ['GPT'],
      expandedTradingReturnFolders: Array.isArray(saved?.expandedTradingReturnFolders) ? saved.expandedTradingReturnFolders.filter(id => ['base-server','bmk','experiment'].includes(id)) : ['base-server','bmk','experiment'],
    };
  } catch { return {collapsedSections:[],expandedFamilies:['WTI'],expandedBenchmarkFamilies:['WTI','Brent','SC'],expandedLeaderboardFamilies:['GPT'],expandedTradingReturnFolders:['base-server','bmk','experiment']}; }
}
function saveSidebarPreferences() {
  try { localStorage.setItem(sidebarStorageKey,JSON.stringify({collapsedSections:[...collapsedSections],expandedFamilies:[...expandedFamilies],expandedBenchmarkFamilies:[...expandedBenchmarkFamilies],expandedLeaderboardFamilies:[...expandedLeaderboardFamilies],expandedTradingReturnFolders:[...expandedTradingReturnFolders]})); }
  catch { /* Folding still works when browser storage is unavailable. */ }
}
function renderSidebarSections() {
  $$('[data-nav-toggle]').forEach(button => {
    const section = button.dataset.navToggle;
    const expanded = !collapsedSections.has(section);
    button.setAttribute('aria-expanded',String(expanded));
    document.getElementById(button.getAttribute('aria-controls')).hidden = !expanded;
    const selected = section === 'news' ? !!state.category : section === 'contracts' ? !state.category && !state.researchSection : state.researchSection === section;
    button.classList.toggle('has-active-child',selected);
  });
  $$('[data-benchmark-family]').forEach(button => {
    const family = button.dataset.benchmarkFamily;
    const expanded = expandedBenchmarkFamilies.has(family);
    button.setAttribute('aria-expanded',String(expanded));
    document.getElementById(button.getAttribute('aria-controls')).hidden = !expanded;
    button.classList.toggle('has-active-child',state.researchSection === 'benchmark' && state.researchSubsection === family);
  });
  $$('[data-leaderboard-family]').forEach(button => {
    const family = button.dataset.leaderboardFamily;
    const expanded = expandedLeaderboardFamilies.has(family);
    button.setAttribute('aria-expanded',String(expanded));
    document.getElementById(button.getAttribute('aria-controls')).hidden = !expanded;
    button.classList.toggle('has-active-child',state.researchSection === 'leaderboard' && leaderboardModelFamily[state.researchSubsection] === family);
  });
  $$('[data-trading-return-folder]').forEach(button => {
    const folder = button.dataset.tradingReturnFolder;
    const expanded = expandedTradingReturnFolders.has(folder);
    button.setAttribute('aria-expanded',String(expanded));
    const child = document.getElementById(button.getAttribute('aria-controls'));
    if (child) child.hidden = !expanded;
  });
}
function toggleLeaderboardFamily(family) {
  if (!Object.prototype.hasOwnProperty.call(leaderboardModels,family)) return;
  if (expandedLeaderboardFamilies.has(family)) expandedLeaderboardFamilies.delete(family);
  else expandedLeaderboardFamilies.add(family);
  saveSidebarPreferences(); renderSidebarSections();
}
function toggleBenchmarkFamily(family) {
  if (!['WTI','Brent','SC'].includes(family)) return;
  if (expandedBenchmarkFamilies.has(family)) expandedBenchmarkFamilies.delete(family);
  else expandedBenchmarkFamilies.add(family);
  saveSidebarPreferences(); renderSidebarSections();
}
function toggleTradingReturnFolder(folder) {
  if (!['base-server','bmk','experiment'].includes(folder)) return;
  if (expandedTradingReturnFolders.has(folder)) expandedTradingReturnFolders.delete(folder);
  else expandedTradingReturnFolders.add(folder);
  saveSidebarPreferences(); renderSidebarSections();
}
function toggleSidebarSection(section) {
  if (!sidebarSectionIds.includes(section)) return;
  if (collapsedSections.has(section)) collapsedSections.delete(section);
  else collapsedSections.add(section);
  saveSidebarPreferences();
  renderSidebarSections();
}

async function api(path, options = {}) {
  const headers = {'Accept':'application/json', ...(options.method ? {'Content-Type':'application/json'} : {}), ...options.headers};
  let response;
  try { response = await fetch(path, {...options, headers}); }
  catch { throw new Error('无法连接本地服务，请确认服务仍在运行。'); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : `请求失败（${response.status}）`);
  return data;
}
function toast(message, error = false) {
  const element = document.createElement('div'); element.className = `toast${error ? ' error' : ''}`;
  element.innerHTML = `${icon(error ? 'alert' : 'check')}<span>${escapeHTML(message)}</span>`;
  $('#toast-stack').append(element); setTimeout(() => element.remove(), error ? 6500 : 3700);
}
function dateParts(value, timeZone = 'UTC') {
  if (!value) return null; const date = new Date(value); if (Number.isNaN(date.valueOf())) return null;
  return {date:date.toLocaleDateString('zh-CN',{timeZone,year:'numeric',month:'2-digit',day:'2-digit'}).replaceAll('/','.'),time:date.toLocaleTimeString('zh-CN',{timeZone,hour:'2-digit',minute:'2-digit',hour12:false}),full:date.toLocaleString('zh-CN',{timeZone,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false})};
}
function formatDate(value) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(value || '')) return `${value}（仅日期）`;
  const parts = dateParts(value); return parts ? `${parts.full} UTC+0` : '未填写';
}
function publicationParts(item) {
  const metadata = item.ingestion || {};
  if (['unverified_timezone','unverified_time'].includes(metadata.timezone_status)) {
    const raw = metadata.source_published_at || metadata.published_date || '';
    const match = raw.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/);
    const label = metadata.timezone_status === 'unverified_timezone' ? '时区待核实' : '首发时间待核实';
    return {date:(match?.[1] || metadata.published_date || '日期未确认').replaceAll('-','.'),time:`${match?.[2] || ''} ${label}`.trim(),full:`${raw || '未确认'}（${label}，未换算时区）`};
  }
  const point = item.published_at ? new Date(item.published_at) : null;
  if (metadata.publication_precision !== 'date' && point && !Number.isNaN(point.valueOf())) {
    const utc = point.toISOString().slice(0,19);
    return {date:utc.slice(0,10),time:`${utc.slice(11)} UTC+0`,full:`${utc.replace('T',' ')} UTC+0`};
  }
  const date = metadata.published_date || (metadata.publication_precision === 'date' ? item.published_at?.slice(0,10) : '');
  return /^\d{4}-\d{2}-\d{2}$/.test(date || '') ? {date:date.replaceAll('-','.'),time:'仅日期',full:`${date.replaceAll('-','.')}（仅日期）`} : null;
}
function publicationAuditSection(item) {
  const meta = item.ingestion || {};
  if (!meta.timezone_status) return '';
  const fields = entries => `<dl class="detail-fields">${entries.map(entry => detailField(...entry)).join('')}</dl>`;
  const labels = {verified_epoch:'已核实 · Unix 时间戳',verified_offset:'已核实 · 来源显式时区',verified_site_timezone:'已核实 · 来源时区规则',date_only:'仅日期 · 无精确时分',unverified_timezone:'时区待核实',unverified_time:'首发时间待核实'};
  return detailSection('首发时间核验',fields([['核验状态',labels[meta.timezone_status] || meta.timezone_status],['来源原始时间',meta.source_published_at],['来源时区',meta.source_timezone || '未声明'],['核验依据',meta.timezone_evidence,true],['原存时间（修正前）',meta.original_published_at],['核验时间',formatDate(meta.timezone_checked_at)]]));
}
function timestamp(value) { const date = Date.parse(value || ''); return Number.isNaN(date) ? 0 : date; }
function publicationDate(item) {
  const value = item.ingestion?.timezone_status?.startsWith('unverified') ? item.ingestion.published_date : item.published_at || item.ingestion?.published_date;
  if (!value) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const [year, month, day] = value.split('-').map(Number);
    return new Date(year, month - 1, day);
  }
  const date = dateParts(value,'UTC')?.date;
  if (!date) return null;
  const [year, month, day] = date.split('.').map(Number);
  return new Date(year, month - 1, day);
}
function startOfDay(date) { return new Date(date.getFullYear(), date.getMonth(), date.getDate()); }
function matchesTimeFilter(item) {
  if (!state.time) return true;
  const published = publicationDate(item);
  if (!published) return false;
  const [year, month, day] = dateParts(new Date(),'UTC').date.split('.').map(Number);
  const today = new Date(year, month - 1, day);
  let start = today;
  if (state.time === '7d') start = new Date(year, month - 1, day - 6);
  else if (state.time === '30d') start = new Date(year, month - 1, day - 29);
  else if (state.time === '90d') start = new Date(year, month - 1, day - 89);
  else if (state.time === 'year') start = new Date(year, 0, 1);
  const end = new Date(year, month - 1, day + 1);
  return published >= start && published < end;
}
function categoryName(id) { return state.config?.categories.find(category => category.id === id)?.name || id; }
function categoryPill(id) { return `<span class="category-pill" data-category="${escapeHTML(id)}">${escapeHTML(shortNames[id] || categoryName(id))}</span>`; }
function newsCategories(item) { return [...new Set(Array.isArray(item.categories) && item.categories.length ? item.categories : [item.category])]; }
function categoryPills(item) { return `<span class="category-pills">${newsCategories(item).map(categoryPill).join('')}</span>`; }
function matchesRecordType(item) { return state.demo === 'real' ? !item.is_demo : state.demo === 'demo' ? item.is_demo : true; }
function newsSourceName(item) { return item.source_name?.trim() || '未填写来源'; }
function newsOriginName(item) {
  return item.origin_name?.trim() || ({unresolved:'原文未明确机构',multiple_sources:'多方信息汇总',not_applicable:'无机构发布主体'}[item.origin_status] || '待核实');
}
function newsAuthorityLevel(item) {
  const authority = item.origin_authority || {};
  return authority.level || ({mixed:'多档位',partial:'未统一评级',ambiguous:'身份待核实'}[authority.status] || '未评级');
}
function provenance(item) {
  return `<div class="news-origin"><span class="provenance-label">来源</span><span>${escapeHTML(newsOriginName(item))}</span></div><div class="news-feed"><span class="provenance-label">信源</span><span>${escapeHTML(newsSourceName(item))}</span></div><div class="news-authority" title="${escapeHTML(item.origin_authority?.notes || '')}"><span class="provenance-label">置信等级</span><span>${escapeHTML(newsAuthorityLevel(item))}</span></div>`;
}
function renderSourceFilter() {
  const select = $('#source-filter');
  const sources = new Set(state.items.filter(matchesRecordType).map(newsSourceName));
  if (state.source) sources.add(state.source);
  const names = [...sources].sort((left,right) => left.localeCompare(right,'zh-CN'));
  const values = ['',...names];
  if (select.options.length !== values.length || values.some((value,index) => select.options[index]?.value !== value)) {
    select.innerHTML = `<option value="">全部信源</option>${names.map(name => `<option value="${escapeHTML(name)}">${escapeHTML(name)}</option>`).join('')}`;
  }
  select.value = state.source;
}
function directionClass(value) { return ({'偏多':'bullish','偏空':'bearish','混合':'mixed'})[value] || ''; }
function directionTag(value) { return `<span class="direction ${directionClass(value)}">${escapeHTML(value || '不确定')}</span>`; }
function verificationClass(value) { return value === '已确认' ? 'verified' : value === '存在争议' ? 'disputed' : ''; }
function filteredItems() {
  if (!state.category) return [];
  const query = state.query.toLocaleLowerCase().trim();
  const rows = state.items.filter(item => {
    if (state.category && !newsCategories(item).includes(state.category)) return false;
    if (state.source && newsSourceName(item) !== state.source) return false;
    if (state.asset && !item.assets.includes(state.asset)) return false;
    if (state.direction && item.direction !== state.direction) return false;
    if (state.verification && item.verification !== state.verification) return false;
    if (!matchesTimeFilter(item)) return false;
    if (!matchesRecordType(item)) return false;
    if (query) {
      const searchable = [item.title,item.summary,item.body,item.region,item.source_name,item.origin_name,item.event_id,...item.entities,...item.channels].join(' ').toLocaleLowerCase();
      if (!searchable.includes(query)) return false;
    }
    return true;
  });
  return rows.sort((a,b) => {
    if (state.sort === 'updated') return timestamp(b.updated_at) - timestamp(a.updated_at);
    const left = timestamp(a.published_at || a.ingestion?.published_date || a.created_at), right = timestamp(b.published_at || b.ingestion?.published_date || b.created_at);
    return state.sort === 'oldest' ? left - right : right - left;
  });
}
function selectedContract() {
  for (const family of window.OilContracts.families) {
    const contract = family.contracts.find(item => item.id === state.contractId);
    if (contract) return {family,contract};
  }
  const family = window.OilContracts.families[0];
  return {family,contract:family.contracts[0]};
}
function renderContractNavigation() {
  const selected = selectedContract();
  $('#contract-nav').innerHTML = window.OilContracts.families.map(family => {
    const expanded = expandedFamilies.has(family.id);
    const current = !state.category && !state.researchSection && selected.family.id === family.id;
    const familyLabel = ({WTI:'纽约原油',Brent:'布伦特原油',SC:'上海原油'})[family.id] || family.label || family.name;
    return `<div class="contract-family"><button class="contract-family-button ${current ? 'active' : ''}" data-family="${escapeHTML(family.id)}" aria-expanded="${expanded}" aria-controls="contracts-${escapeHTML(family.id)}"><span>${escapeHTML(family.id)}</span><span class="nav-heading-meta"><span class="family-description">${escapeHTML(familyLabel)}</span><span class="nav-chevron">${icon('chevron-right')}</span></span></button><div class="contract-months" id="contracts-${escapeHTML(family.id)}" role="group" aria-label="${escapeHTML(family.id)}月份合约" ${expanded ? '' : 'hidden'}>${family.contracts.map(contract => `<button class="contract-month-button ${!state.category && !state.researchSection && contract.id === selected.contract.id ? 'active' : ''}" data-contract="${escapeHTML(contract.id)}" aria-label="${escapeHTML(family.id)} ${escapeHTML(contract.code)} ${escapeHTML(contract.label)}合约" ${!state.category && !state.researchSection && contract.id === selected.contract.id ? 'aria-current="page"' : ''}><span>${escapeHTML(contract.code)}</span><small>${escapeHTML(contract.month.replace('-','.'))}</small></button>`).join('')}</div></div>`;
  }).join('');
}
function benchmarkV2Subtitle() {
  const range = window.OilResearch?.getBenchmarkDateRange?.() || {start:'2026-08-01',end:'2026-09-30',inclusive:true};
  return `Benchmark · 已知交易日与预测交易日均在 ${range.start} 至 ${range.end}${range.inclusive ? '（含首尾）' : ''} · 三市场主连日频 · 时分按 UTC+0 展示。`;
}
function renderNavigation() {
  renderContractNavigation();
  const realItems = state.items.filter(item => !item.is_demo);
  $('#all-count').textContent = state.realTotal || realItems.length;
  $('#category-nav').innerHTML = state.config.categories.map(category => {
    const count = state.categoryTotals[category.id] ?? realItems.filter(item => newsCategories(item).includes(category.id)).length;
    return `<button class="nav-item ${state.category === category.id ? 'active' : ''}" data-category="${category.id}" title="${escapeHTML(category.description)}" ${state.category === category.id ? 'aria-current="page"' : ''}><span>${escapeHTML(category.name)}</span><span class="nav-count">${count}</span></button>`;
  }).join('');
  const selected = selectedContract();
  $('#page-title').innerHTML = `${escapeHTML(state.category ? categoryName(state.category) : selected.contract.code)}<span class="title-dot">.</span>`;
  $('#page-subtitle').textContent = state.category ? state.config.categories.find(category => category.id === state.category).description : `${selected.family.id} · ${selected.contract.code} · ${selected.contract.label}合约`;
  $('#page-subtitle').hidden = state.researchSection === 'leaderboard';
  $$('.research-nav-item').forEach(button => {
    const active = button.dataset.researchSection === state.researchSection && button.dataset.researchSubsection === state.researchSubsection && (!button.dataset.researchMode || button.dataset.researchMode === state.researchMode);
    button.classList.toggle('active',active);
    if (active) button.setAttribute('aria-current','page'); else button.removeAttribute('aria-current');
  });
  if (state.researchSection) {
    const selectedModel = Object.values(leaderboardModels).flat().find(model => model.id === state.researchSubsection);
    const benchmarkTitle = ({'memory-leakage':'记忆泄漏','backtest-v1':'回测 v1','backtest-v2':'回测 v2',live:'实盘'})[state.researchSubsection] || '回测 v1';
    const titles = {leaderboard:['overview','summary'].includes(state.researchSubsection) ? 'Overview' : state.researchSubsection === 'experiment-setup' ? 'Experiment Setup' : selectedModel?.label || state.researchSubsection || 'Leaderboard',environment:state.researchSubsection || 'env01',benchmark:benchmarkTitle,'news-factor':state.researchSubsection === 'bmk' ? 'BMK' : state.researchSubsection === 'experiments' ? 'experiments' : state.researchSubsection === 'strategy-base' ? 'strategy base' : 'factor base','trading-return':({ 'model-server':'Model Server','evaluator-server':'Env Server','mcp-server':'MCP Server','bmk-backtesting':'Backtesting v1','bmk-live':'Live Trading','experiment-leaderboard':'Leaderboard','experiment-setup':'Setup' })[state.researchSubsection] || 'Trading Return'};
    const subtitles = {leaderboard:'Leaderboard · 主连日频预测：方向正确率与三项 MAE。',environment:state.researchSubsection === 'env02' ? 'Environment · 无搜索与工具接口，仅 evaluator agent。' : state.researchSubsection === 'env00-extend' ? 'Environment · 记忆泄漏闭卷作答与独立答案评估。' : state.researchSubsection === 'env00' ? 'Environment · 技术指标时间边界与四项预测评估。' : 'Environment · 统一新闻边界与答案评估规则。',benchmark:state.researchSubsection === 'memory-leakage' ? 'Benchmark · 2026 年 1—10 月，每月 10 道金融事件填空题。' : state.researchSubsection === 'backtest-v2' ? benchmarkV2Subtitle() : 'Benchmark · 按下一交易日收盘结算方向与涨跌幅，并预测当日高低点。','news-factor':state.researchSubsection === 'bmk' ? 'BMK · 日频期货价格分段与动态数据边界。' : state.researchSubsection === 'experiments' ? 'experiments · 按实验开始时间记录 BMK 实盘验证。' : '','trading-return':({ 'model-server':'Trading Return · Base Server · AIHub 模型可用性与连接测试。','evaluator-server':'Trading Return · Base Server · Env v1 预测环境与 Env v2 交易环境。','mcp-server':'Trading Return · Base Server · 带时间核验的检索服务。','bmk-backtesting':'Trading Return · BMK · Benchmark v2 回测题目。','bmk-live':'Trading Return · BMK · 与完整日 K 同步发布的实盘题目。','experiment-leaderboard':'Trading Return · Experiment · 实验排行榜。','experiment-setup':'Trading Return · Experiment · 配置并构建实验。' })[state.researchSubsection] || 'Trading Return'};
    $('#page-title').innerHTML = `${escapeHTML(titles[state.researchSection])}<span class="title-dot">.</span>`;
    $('#page-subtitle').textContent = subtitles[state.researchSection];
    $('.page-heading .eyebrow').textContent = ({leaderboard:'STRATEGY EVALUATION',environment:'EVALUATION ENVIRONMENT',benchmark:state.researchSubsection === 'memory-leakage' ? 'HISTORICAL MEMORY BENCHMARK' : 'FUTURES PRICE BENCHMARK','news-factor':'NEWS FACTOR MINING','trading-return':'TRADING RETURN'})[state.researchSection];
  } else $('.page-heading .eyebrow').textContent = 'CRUDE OIL INTELLIGENCE';
  if (state.category || state.researchSection) $('#overview-link').removeAttribute('aria-current');
  else $('#overview-link').setAttribute('aria-current','page');
  renderSidebarSections();
}
function renderMarket() {
  if (contractChart) contractChart.setContract(state.contractId);
  else contractChart = window.OilContracts.mount($('#overview'), {contractId:state.contractId});
}
function selectContract(id) {
  const family = window.OilContracts.families.find(item => item.contracts.some(contract => contract.id === id));
  if (!family) return;
  state.contractId = id;
  state.category = '';
  state.researchSection = '';
  expandedFamilies.add(family.id);
  collapsedSections.delete('contracts');
  saveSidebarPreferences();
  render(); closeMenu();
  window.scrollTo({top:0,behavior:'auto'});
}
function toggleContractFamily(id) {
  const family = window.OilContracts.families.find(item => item.id === id);
  if (!family) return;
  if (expandedFamilies.has(id)) {
    expandedFamilies.delete(id);
  } else {
    expandedFamilies.add(id);
  }
  saveSidebarPreferences();
  renderContractNavigation();
}
function renderSavedLabel() {
  const lastSaved = [...state.items].sort((a,b) => timestamp(b.updated_at) - timestamp(a.updated_at))[0];
  $('#saved-label').textContent = lastSaved ? `最近保存 ${dateParts(lastSaved.updated_at)?.time || ''}` : '本地档案';
}
function newsRow(item) {
  const published = publicationParts(item);
  return `<tr class="news-row" data-id="${item.id}" tabindex="0" aria-label="查看新闻：${escapeHTML(item.title)}"><td><div class="news-title-line"><span class="news-title">${escapeHTML(item.title)}</span>${item.is_demo ? '<span class="demo-badge">演示</span>' : ''}</div><div class="news-summary">${escapeHTML(item.summary || '尚未填写摘要')}</div><div class="news-metadata"><span>${escapeHTML(item.region || '地区待填写')}</span>${item.channels.slice(0,2).map(channel => `<span class="channel-mini">${escapeHTML(channel)}</span>`).join('')}</div></td><td>${categoryPills(item)}<span class="category-sub">${escapeHTML(item.stage)} · ${escapeHTML(item.source_type)}</span></td><td class="provenance-cell">${provenance(item)}</td><td><time class="publication-stamp" datetime="${escapeHTML(item.published_at)}">${escapeHTML(published?.full || '首发时间未填')}</time></td><td><div class="row-last-cell"><span class="verify-dot ${verificationClass(item.verification)}" title="${escapeHTML(item.verification)}" role="img" aria-label="${escapeHTML(item.verification)}"></span><span class="row-open-icon">${icon('chevron-right')}</span></div></td></tr>`;
}
function newsCard(item) {
  return `<article class="news-card" data-id="${item.id}" tabindex="0" aria-label="查看新闻：${escapeHTML(item.title)}"><div class="card-top">${categoryPills(item)}${item.is_demo ? '<span class="demo-badge">演示</span>' : `<span class="verify-dot ${verificationClass(item.verification)}" title="${escapeHTML(item.verification)}"></span>`}</div><h3 class="news-title">${escapeHTML(item.title)}</h3><p class="news-summary">${escapeHTML(item.summary || '尚未填写摘要')}</p><div class="card-provenance">${provenance(item)}</div><div class="card-bottom"><span class="news-metadata">${escapeHTML(item.region || '地区待填写')}</span><time class="publication-stamp" datetime="${escapeHTML(item.published_at)}">${escapeHTML(publicationParts(item)?.full || '首发时间未填')}</time></div></article>`;
}
function renderList() {
  renderSourceFilter();
  const items = filteredItems();
  const pages = Math.max(1, Math.ceil(items.length / state.perPage));
  state.page = Math.min(state.page, pages);
  const start = (state.page - 1) * state.perPage;
  const displayed = items.slice(start, start + state.perPage);
  $('#result-badge').textContent = items.length;
  const hasFilter = state.query || state.source || state.asset || state.direction || state.verification || state.demo !== 'real' || state.time;
  const categoryTotal = hasFilter
    ? state.items.filter(item => newsCategories(item).includes(state.category) && matchesRecordType(item)).length
    : (state.categoryTotals[state.category] ?? state.items.filter(item => newsCategories(item).includes(state.category) && matchesRecordType(item)).length);
  $('#result-description').textContent = hasFilter ? `筛选到 ${items.length} 条新闻，该分类共 ${categoryTotal} 条` : `该分类共 ${items.length} 条新闻 · 点击记录查看完整属性`;
  $('#list-view').classList.toggle('active', state.view === 'list');
  $('#grid-view').classList.toggle('active', state.view === 'grid');
  $('#more-filters').classList.toggle('active', !!(state.verification || state.demo !== 'real' || state.time) || moreFiltersOpen);
  $('#filter-dot').textContent = state.verification || state.demo !== 'real' || state.time ? ' ' : '';
  if (!items.length) {
    $('#news-content').innerHTML = `<div class="empty-state"><h3>${hasFilter ? '没有找到匹配的新闻' : '暂无新闻记录'}</h3><p>${hasFilter ? '调整关键词或筛选条件，查找其他记录。' : '此分类暂无已归档新闻，采集审核通过后会显示在这里。'}</p>${hasFilter ? '<button class="button secondary" id="empty-action">清除全部筛选</button>' : ''}</div>`;
    $('#empty-action')?.addEventListener('click', resetFilters);
  } else if (state.view === 'grid') {
    $('#news-content').innerHTML = `<div class="news-grid">${displayed.map(newsCard).join('')}</div>`;
  } else {
    $('#news-content').innerHTML = `<div class="table-wrap"><table class="news-table"><thead><tr><th>新闻事件 / 摘要</th><th>研究分类</th><th>信息溯源</th><th>首发时间</th><th>核验</th></tr></thead><tbody>${displayed.map(newsRow).join('')}</tbody></table></div>`;
  }
  $('#pagination').innerHTML = `<span>${items.length ? `显示第 ${start + 1}–${Math.min(start + state.perPage, items.length)} 条，共 ${items.length} 条` : '暂无记录'}</span><div class="pagination-right"><select id="page-size" aria-label="每页条数">${[10,20,50].map(size => `<option value="${size}" ${size === state.perPage ? 'selected' : ''}>${size} 条 / 页</option>`).join('')}</select><button class="icon-button" id="prev-page" aria-label="上一页" ${state.page <= 1 ? 'disabled' : ''}>${icon('chevron-left')}</button><span class="page-current">${state.page}</span><span>/ ${pages}</span><button class="icon-button" id="next-page" aria-label="下一页" ${state.page >= pages ? 'disabled' : ''}>${icon('chevron-right')}</button></div>`;
  $('#page-size').addEventListener('change', event => { state.perPage = Number(event.target.value); state.page = 1; renderList(); });
  $('#prev-page').addEventListener('click', () => { state.page--; renderList(); });
  $('#next-page').addEventListener('click', () => { state.page++; renderList(); });
}
function render() {
  if (!state.config) return;
  renderNavigation();
  const research = !!state.researchSection;
  const analyticalResearch = ['leaderboard','environment','benchmark'].includes(state.researchSection);
  const newsFactorResearch = state.researchSection === 'news-factor' && ['bmk','experiments'].includes(state.researchSubsection);
  const tradingReturnResearch = state.researchSection === 'trading-return' && ['model-server','evaluator-server','mcp-server','bmk-backtesting','bmk-live','experiment-leaderboard','experiment-setup'].includes(state.researchSubsection);
  const overview = !state.category && !research;
  $('#overview').hidden = !overview;
  $('#news-workspace').hidden = overview || research;
  $('#research-workspace').hidden = !research;
  if (overview) renderMarket();
  else {
    if (contractChart) { contractChart.destroy(); contractChart = null; }
    $('#overview').innerHTML = '';
    if (analyticalResearch) window.OilResearch?.show({section:state.researchSection,subsection:state.researchSubsection,mode:state.researchMode,family:state.researchFamily,query:state.researchQuery});
    else if (newsFactorResearch) window.NewsFactor?.show({subsection:state.researchSubsection});
    else if (tradingReturnResearch) window.TradingReturn?.show({subsection:state.researchSubsection});
    else if (research) { $('#research-workspace').innerHTML = ''; window.OilResearch?.hide(); window.NewsFactor?.hide(); window.TradingReturn?.hide(); }
    else {renderSavedLabel(); renderList();}
  }
  if (!analyticalResearch) window.OilResearch?.hide();
  if (!newsFactorResearch) window.NewsFactor?.hide();
  if (!tradingReturnResearch) window.TradingReturn?.hide();
}
async function reload(showToast = false) {
  const data = await api('/api/news?limit=3000');
  state.items = data.items;
  state.total = data.total || state.items.length;
  state.realTotal = data.real_total || state.items.filter(item => !item.is_demo).length;
  state.categoryTotals = data.category_totals || {};
  state.loading = false;
  render();
  if (showToast) toast('本地档案已刷新');
}
function changeCategory(id) {
  if (!state.config.categories.some(category => category.id === id)) return;
  state.category = id; state.researchSection = ''; state.page = 1;
  collapsedSections.delete('news'); saveSidebarPreferences(); render(); closeMenu();
  window.scrollTo({top:0,behavior:'auto'});
}
function showOverview(event) {
  event?.preventDefault();
  state.category = ''; state.researchSection = ''; state.page = 1;
  expandedFamilies.add(selectedContract().family.id);
  collapsedSections.delete('contracts');
  saveSidebarPreferences();
  render(); closeMenu();
  window.scrollTo({top:0,behavior:'auto'});
}
function selectResearch(section, subsection, mode = state.researchMode) {
  if (section === 'trading-return') {
    if (!['model-server','evaluator-server','mcp-server','bmk-backtesting','bmk-live','experiment-leaderboard','experiment-setup'].includes(subsection)) return;
    state.category = ''; state.researchSection = section; state.researchSubsection = subsection; state.researchMode = 'backtest'; state.researchQuery = '';
    collapsedSections.delete(section); saveSidebarPreferences();
    render(); closeMenu();
    window.scrollTo({top:0,behavior:'auto'});
    return;
  }
  if (section === 'news-factor') {
    if (!['bmk','experiments','factor-base','strategy-base'].includes(subsection)) return;
    state.category = ''; state.researchSection = section; state.researchSubsection = subsection; state.researchMode = 'backtest'; state.researchQuery = '';
    collapsedSections.delete(section); saveSidebarPreferences();
    render(); closeMenu();
    window.scrollTo({top:0,behavior:'auto'});
    return;
  }
  if (!['leaderboard','environment','benchmark'].includes(section)) return;
  if (section === 'benchmark') {
    if (['WTI','Brent','SC'].includes(subsection)) {
      state.researchFamily = subsection;
      subsection = mode === 'live' ? 'live' : 'backtest-v1';
    }
    if (!['memory-leakage','backtest-v1','backtest-v2','live'].includes(subsection)) return;
    mode = subsection === 'live' ? 'live' : 'backtest';
    if (state.researchSection !== section || state.researchSubsection !== subsection) state.researchQuery = '';
  }
  state.category = ''; state.researchSection = section; state.researchSubsection = subsection;
  state.researchMode = section === 'leaderboard' && ['overview','summary'].includes(subsection) ? 'backtest' : ['backtest','live'].includes(mode) ? mode : 'backtest';
  collapsedSections.delete(section); saveSidebarPreferences();
  render(); closeMenu();
  window.scrollTo({top:0,behavior:'auto'});
}
function resetFilters() {
  Object.assign(state,{query:'',source:'',asset:'',direction:'',verification:'',demo:'real',time:'',page:1});
  $('#search-input').value = '';
  ['source','asset','direction','verification','time'].forEach(name => $(`#${name}-filter`).value = '');
  $('#demo-filter').value = 'real';
  render();
}

function mountOverlay(html, kind) {
  if (!state.modal) returnFocus = document.activeElement;
  state.modal = kind; $('#overlay-root').innerHTML = html; document.body.classList.add('overlay-open');
  const overlay = $('.overlay');
  overlay.addEventListener('click', event => { if (event.target === overlay) requestCloseOverlay(); });
  $$('[data-close]', overlay).forEach(button => button.addEventListener('click', requestCloseOverlay));
  hydrateIcons(overlay);
  requestAnimationFrame(() => {
    const target = kind === 'form' ? $('#field-title') : $('[data-close]', overlay) || $('button',overlay);
    target?.focus();
  });
}
function closeOverlay() {
  const closingLivePredictions = state.modal === 'live-predictions';
  const closingLiveQuestionDetails = state.modal === 'live-question-details';
  if (state.modal === 'settings') settingsRequestVersion++;
  $('#overlay-root').innerHTML = ''; document.body.classList.remove('overlay-open');
  state.modal = null; state.detailId = null; currentFormItem = null; formDirty = false;
  if (closingLivePredictions) window.OilResearch?.closeLivePredictions();
  if (closingLiveQuestionDetails) window.OilResearch?.closeLiveQuestionDetails();
  if (returnFocus?.isConnected) returnFocus.focus();
}
function requestCloseOverlay() {
  if (formSaving || settingsSaving) return;
  if (state.modal === 'form' && formDirty) {
    if (!window.confirm('当前内容尚未保存，确定放弃这些修改吗？')) return;
  }
  closeOverlay();
}

function closeSettingsMenu(restoreFocus = false) {
  $('#settings-popover').hidden = true;
  $('#settings-button').setAttribute('aria-expanded','false');
  if (restoreFocus) $('#settings-button').focus();
}
function toggleSettingsMenu() {
  const open = $('#settings-popover').hidden;
  $('#settings-popover').hidden = !open;
  $('#settings-button').setAttribute('aria-expanded',String(open));
  if (open) $('#general-settings').focus();
}
function intervalParts(minutes) {
  if (minutes % 1440 === 0) return {value:minutes / 1440,unit:1440};
  if (minutes % 60 === 0) return {value:minutes / 60,unit:60};
  return {value:minutes,unit:1};
}
function settingsIntervalField(kind, title, description, minutes) {
  const interval = intervalParts(minutes);
  return `<div class="setting-row"><div class="setting-copy"><h3><label for="${kind}-update-value">${title}</label></h3><p>${description}</p></div><div class="setting-interval"><input id="${kind}-update-value" name="${kind}-value" type="number" min="1" max="${Math.floor(10080 / interval.unit)}" step="1" value="${interval.value}" required aria-label="${title}间隔"><select id="${kind}-update-unit" aria-label="${kind === 'news' ? '新闻' : '期货日K和实盘题目'}更新单位">${[[1,'分钟'],[60,'小时'],[1440,'天']].map(([value,label]) => `<option value="${value}" ${value === interval.unit ? 'selected' : ''}>${label}</option>`).join('')}</select></div><p class="update-status" data-settings-status="${kind}" aria-live="polite"></p></div>`;
}
function renderUpdateStatus() {
  if (state.modal !== 'settings') return;
  for (const kind of ['news','contracts']) {
    const element = $(`[data-settings-status="${kind}"]`);
    const job = updateSettings?.jobs?.[kind];
    if (!element || !job) continue;
    const labels = kind === 'news' ? {idle:'等待首次更新',running:'正在采集和审核',success:'更新完成',partial:'部分更新完成',failed:'更新未完成',interrupted:'上次更新中断'} : {idle:'等待首次日K和实盘题目更新',running:'正在更新期货日K和实盘题目',success:'更新成功',partial:'部分更新完成',failed:'更新失败',interrupted:'上次更新中断'};
    const status = labels[job.status] || '等待更新';
    const summary = job.summary;
    let detail = '';
    if (!job.running && summary) {
      if (kind === 'contracts') {
        const counts = [];
        if (Number.isInteger(summary.series_total)) counts.push(`月份合约行情 ${summary.updated} / ${summary.series_total} 组更新成功`);
        if (Number.isInteger(summary.continuous?.total)) {
          const completed = Number.isInteger(summary.continuous.completed_updated) ? summary.continuous.completed_updated : summary.continuous.updated;
          counts.push(`主连日K ${completed} / ${summary.continuous.total} 组更新成功`);
        }
        const live = summary.live_benchmark;
        if (live) {
          if (Number.isInteger(live.ready_questions) && Number.isInteger(live.total_questions)) counts.push(`实盘题目 ${live.ready_questions} / ${live.total_questions} 题就绪`);
          if (Array.isArray(live.unavailable_families) && live.unavailable_families.length) counts.push(`${live.unavailable_families.length} 个品类等待完整行情`);
          if (live.error && live.error !== job.error) counts.push(`实盘题目：${live.error}`);
        }
        detail = counts.join(' · ');
      }
      if (kind === 'news') {
        const counts = [];
        if (Number.isInteger(summary.created)) {
          counts.push(`本次新增 ${summary.created} 条新闻事件`);
          if (summary.updated) counts.push(`更新已有 ${summary.updated} 条`);
          if (summary.reactivated) counts.push(`重新收录 ${summary.reactivated} 条`);
          if (!summary.created && !summary.updated && !summary.reactivated && job.status === 'success') counts.push('暂无新的相关新闻');
        } else if (['failed','interrupted'].includes(job.status)) counts.push('本次新增数量尚未确认');
        else if (Number.isInteger(summary.written)) counts.push(`已处理 ${summary.written} 条记录`);
        const duplicates = (summary.unchanged || 0) + (summary.exact_duplicates || 0);
        if (duplicates) counts.push(`跳过重复 ${duplicates} 条`);
        if (summary.invalid_count) counts.push(`跳过内容不完整 ${summary.invalid_count} 条`);
        if (summary.time_skipped_count || summary.archive_time_skipped_count) counts.push(`跳过时间未核实 ${(summary.time_skipped_count || 0) + (summary.archive_time_skipped_count || 0)} 条`);
        detail = counts.join(' · ');
      }
    }
    const last = job.last_finished_at ? `上次 ${formatDate(job.last_finished_at)}` : '';
    const next = !job.running && job.next_run_at ? `下次 ${formatDate(job.next_run_at)}` : '';
    element.textContent = [status,detail,last,next,!job.running && job.error].filter(Boolean).join(' · ');
    element.classList.toggle('error',['partial','failed','interrupted'].includes(job.status));
  }
}
async function openGeneralSettings() {
  const version = ++settingsRequestVersion;
  closeSettingsMenu();
  closeMenu();
  mountOverlay(`<div class="overlay modal-overlay"><section class="modal settings-modal" role="dialog" aria-modal="true" aria-labelledby="settings-title"><header class="modal-header"><h2 id="settings-title">通用设置</h2><button type="button" class="icon-button" data-close aria-label="关闭通用设置">${icon('close')}</button></header><div class="settings-body" id="settings-content"><div class="loading-state"><div class="spinner"></div>正在读取设置</div></div></section></div>`, 'settings');
  returnFocus = $('#settings-button');
  try {
    const data = await api('/api/settings');
    if (state.modal !== 'settings' || version !== settingsRequestVersion) return;
    updateSettings = data;
    $('#settings-content').innerHTML = `<form id="settings-form">${settingsIntervalField('news','新闻更新','采集所有已接入信源，审核、分类后归档。',data.news_interval_minutes)}${settingsIntervalField('contracts','期货日K和实盘题目更新设置','默认按日更新，分别在各市场收盘后执行：WTI 纽约 17:35、Brent 伦敦 23:35、SC 上海 15:35。更新日K、月份合约行情及主连实盘题目；休市或完整行情未就绪时等待。',data.contract_interval_minutes)}<p id="settings-error" class="form-error" role="alert" hidden></p></form>`;
    $('.settings-modal').insertAdjacentHTML('beforeend',`<footer class="modal-footer"><button type="button" class="button secondary" id="settings-cancel">取消</button><button type="submit" form="settings-form" class="button primary" id="settings-save">保存设置</button></footer>`);
    $('#settings-cancel').addEventListener('click',requestCloseOverlay);
    $('#settings-form').addEventListener('submit',saveGeneralSettings);
    for (const kind of ['news','contracts']) {
      $(`#${kind}-update-unit`).addEventListener('change',() => {
        $(`#${kind}-update-value`).max = Math.floor(10080 / Number($(`#${kind}-update-unit`).value));
      });
    }
    renderUpdateStatus();
  } catch (error) {
    if (state.modal !== 'settings' || version !== settingsRequestVersion) return;
    $('#settings-content').innerHTML = `<p class="form-error" role="alert">${escapeHTML(error.message)}</p><button type="button" class="button secondary" id="settings-retry">重新读取</button>`;
    $('#settings-retry').addEventListener('click',openGeneralSettings);
  }
}
async function saveGeneralSettings(event) {
  event.preventDefault();
  if (settingsSaving) return;
  const errorElement = $('#settings-error');
  const news = Number($('#news-update-value').value) * Number($('#news-update-unit').value);
  const contracts = Number($('#contracts-update-value').value) * Number($('#contracts-update-unit').value);
  if (![news,contracts].every(value => Number.isSafeInteger(value) && value >= 1 && value <= 10080)) {
    errorElement.textContent = '更新间隔须为 1 分钟至 7 天之间的整数分钟。';
    errorElement.hidden = false;
    return;
  }
  settingsSaving = true;
  errorElement.hidden = true;
  $$('#settings-form input,#settings-form select,#settings-save,#settings-cancel,.settings-modal [data-close]').forEach(element => element.disabled = true);
  $('#settings-save').textContent = '正在保存…';
  try {
    updateSettings = await api('/api/settings',{method:'PUT',body:JSON.stringify({news_interval_minutes:news,contract_interval_minutes:contracts})});
    closeOverlay();
    toast('更新设置已保存');
  } catch (error) {
    errorElement.textContent = error.message;
    errorElement.hidden = false;
  } finally {
    settingsSaving = false;
    $$('#settings-form input,#settings-form select,#settings-save,#settings-cancel,.settings-modal [data-close]').forEach(element => element.disabled = false);
    if ($('#settings-save')) $('#settings-save').textContent = '保存设置';
  }
}
async function pollUpdateStatus() {
  if (document.hidden || updatePollRunning) return;
  updatePollRunning = true;
  try {
    updateSettings = await api('/api/settings');
    renderUpdateStatus();
    if (!observedUpdateFinishes) {
      observedUpdateFinishes = Object.fromEntries(['news','contracts'].map(kind => [kind,updateSettings.jobs?.[kind]?.last_finished_at]));
    } else {
      for (const kind of ['news','contracts']) {
        const finishedAt = updateSettings.jobs?.[kind]?.last_finished_at;
        if (!finishedAt || finishedAt === observedUpdateFinishes[kind]) continue;
        if (kind === 'news') await reload();
        if (kind === 'contracts') {
          if (contractChart && ['success','partial'].includes(updateSettings.jobs[kind].status)) contractChart.reloadCachedData();
          await window.OilResearch?.refresh();
        }
        observedUpdateFinishes[kind] = finishedAt;
      }
    }
  } catch {
    // A disconnected local service is reported by explicit user actions.
  } finally { updatePollRunning = false; }
}
function detailField(label, value, wide = false) {
  return `<div class="detail-field ${wide ? 'wide' : ''}"><dt>${escapeHTML(label)}</dt><dd class="${value ? '' : 'missing'}">${escapeHTML(value || '未填写')}</dd></div>`;
}
function detailSection(title, content) { return `<section class="detail-section"><h3>${title}</h3>${content}</section>`; }
const contentScopeLabels = {public_flash:'公开快讯',public_article:'公开文章',public_article_excerpt:'公开文章节选',public_article_lead:'公开导语',public_research_summary:'公开研报摘要',public_transcript:'公开文字稿',public_podcast_transcript:'公开播客文字稿',public_podcast_transcript_excerpt:'公开播客文字稿节选'};
function newsContentScope(item) {
  const stored = item.ingestion?.content_scope;
  if (contentScopeLabels[stored]) return {id:stored,label:contentScopeLabels[stored]};
  const match = String(item.update_note || '').match(/内容范围[：:]\s*(public_podcast_transcript_excerpt|public_podcast_transcript|public_research_summary|public_transcript|public_article_excerpt|public_article_lead|public_article|public_flash)\b/);
  return match ? {id:match[1],label:contentScopeLabels[match[1]]} : null;
}
function readableUpdateNote(value) {
  return String(value || '').replace(/(内容范围[：:]\s*)(public_podcast_transcript_excerpt|public_podcast_transcript|public_research_summary|public_transcript|public_article_excerpt|public_article_lead|public_article|public_flash)\b/g,(_,prefix,id) => prefix + contentScopeLabels[id]);
}
function agentReviewSection(item) {
  const review = item.agent_review;
  if (!review || typeof review !== 'object') return '';
  const reasons = Object.entries(review.category_reasons || {});
  const currentCategories = newsCategories(item);
  const changed = reasons.length !== currentCategories.length || reasons.some(([id]) => !currentCategories.includes(id));
  const note = changed ? '当前分类已人工调整，以下保留当时 Agent 的审核依据。' : '以下为当时 Agent 的审核记录，手工编辑不会重写这次审核。';
  const fields = [
    ['原油相关性',review.relevance ? '与原油相关' : '未认定与原油相关'],
    ['审核确信度',review.confidence],
    ['审核者',review.reviewer],
    ['审核时间',formatDate(review.reviewed_at)],
    ['相关性依据',review.relevance_reason,true],
    ...reasons.map(([id,reason]) => [`${categoryName(id)} · 分类依据`,reason,true]),
  ];
  const evidence = Array.isArray(review.evidence) ? review.evidence : [];
  return detailSection('Agent 分类依据', `<p class="agent-review-note">${escapeHTML(note)}</p><dl class="detail-fields">${fields.map(entry => detailField(...entry)).join('')}</dl>${evidence.length ? `<div class="agent-review-evidence"><h4>原文证据</h4><ol>${evidence.map(entry => `<li>${escapeHTML(entry)}</li>`).join('')}</ol></div>` : ''}`);
}
function openDetail(id) {
  const item = state.items.find(news => news.id === id); if (!item) return;
  currentFormItem = null; formDirty = false;
  const publicSummary = item.ingestion?.detail_access === 'requires_login';
  const displayURL = publicSummary ? item.ingestion.public_source_url || item.source_url : item.source_url;
  const safeURL = /^https?:\/\//i.test(displayURL) ? displayURL : '';
  const sourceLinkLabel = publicSummary ? '查看公开摘要' : item.ingestion?.source_page_kind === 'collection' ? '查看发布页' : '查看原文';
  const scope = newsContentScope(item);
  const bodyTitle = ['public_article_lead','public_article_excerpt','public_research_summary','public_transcript','public_podcast_transcript','public_podcast_transcript_excerpt'].includes(scope?.id) ? scope.label : '新闻正文 / 原始事实';
  const platform = item.ingestion?.collection_platform;
  const platformLabel = platform ? `<span>发布平台：${escapeHTML(platform)}</span>` : '';
  const fields = entries => `<dl class="detail-fields">${entries.map(entry => detailField(...entry)).join('')}</dl>`;
  mountOverlay(`<div class="overlay"><aside class="drawer" role="dialog" aria-modal="true" aria-labelledby="detail-title"><div class="drawer-header"><div class="drawer-heading">新闻详情${item.is_demo ? '<span class="demo-badge">演示记录</span>' : ''}</div><div class="drawer-actions"><button class="button secondary" id="edit-news">${icon('edit')}编辑</button><button class="icon-button" id="delete-news" title="删除新闻" aria-label="删除新闻">${icon('trash')}</button><button class="icon-button" data-close aria-label="关闭详情">${icon('close')}</button></div></div><div class="drawer-content"><h2 class="detail-title" id="detail-title">${escapeHTML(item.title)}</h2><div class="detail-source"><span>${escapeHTML(item.source_name || '来源待填写')}</span><span>·</span><span>${escapeHTML(item.source_type)}</span>${platformLabel}${scope ? `<span>内容范围：${escapeHTML(scope.label)}</span>` : ''}${safeURL ? `<a href="${escapeHTML(safeURL)}" target="_blank" rel="noopener noreferrer">${sourceLinkLabel} ${icon('arrow-up-right')}</a>` : ''}</div>${item.summary ? `<p class="detail-summary">${escapeHTML(item.summary)}</p>` : ''}<div class="detail-tags">${newsCategories(item).map(categoryPill).join('')}${item.assets.map(asset => `<span class="detail-tag">${escapeHTML(asset)}</span>`).join('')}${directionTag(item.direction)}<span class="detail-tag">${escapeHTML(item.verification)}</span></div>${detailSection('信息溯源',fields([['来源机构',newsOriginName(item)],['采集信源',newsSourceName(item)],['置信等级',newsAuthorityLevel(item)],['榜单匹配来源',item.origin_authority?.matched_source],['等级出处',item.origin_authority?.reference ? '数据库权威榜单 · ' + item.origin_authority.reference : ''],['榜单准入依据',item.origin_authority?.basis,true],['等级说明',item.origin_authority?.notes,true],['溯源结论',item.origin_reason,true],['溯源依据',item.origin_evidence,true],['溯源核对时间',item.origin_reviewed_at ? formatDate(item.origin_reviewed_at) : '']]))}${agentReviewSection(item)}${detailSection('影响渠道与研究判断',fields([['影响渠道',item.channels.join(' / '),true],['判断确信度',item.confidence],['具体合约',item.contract],['事件阶段',item.stage],['判断依据',item.rationale,true]]))}${detailSection(bodyTitle,`<div class="detail-body">${escapeHTML(item.body || '尚未保存正文。')}</div>`)}${publicationAuditSection(item)}${detailSection('事件时间线',fields([['首次发布时间',publicationParts(item)?.full || '未填写'],['事件发生时间',formatDate(item.occurred_at)],['采集可用时间',formatDate(item.collected_at)],['生效时间',formatDate(item.effective_at)]]))}${detailSection('数量、规模与事前预期',fields([['实际值',item.actual_value],['事前预期',item.expected_value],['当时可见的上期值',item.previous_value],['计量单位',item.unit],['影响规模',item.scale],['预计持续时间',item.duration]]))}${detailSection('对象与事件关联',fields([['地区',item.region],['相关主体 / 设施',item.entities.join('、')],['事件编号',item.event_id,true],['新增信息 / 更新说明',readableUpdateNote(item.update_note),true]]))}</div><div class="detail-footer"><span>录入 ${escapeHTML(formatDate(item.created_at))}</span><span>更新 ${escapeHTML(formatDate(item.updated_at))}</span></div></aside></div>`,'detail');
  state.detailId = id;
  $('#edit-news').addEventListener('click', () => openForm(item));
  $('#delete-news').addEventListener('click', () => deleteNews(item));
}
function toUTCDatetime(value) {
  if (!value) return ''; const date = new Date(value); if (Number.isNaN(date.valueOf())) return '';
  return date.toISOString().replace(/Z$/, '');
}
function field(name, label, options = {}) {
  const value = currentFormItem?.[name] ?? options.value ?? '';
  const {type = 'text',wide = false,required = false,placeholder = '',help = ''} = options;
  let input;
  if (type === 'textarea') input = `<textarea id="field-${name}" name="${name}" rows="${options.rows || 3}" placeholder="${escapeHTML(placeholder)}" ${required ? 'required' : ''}>${escapeHTML(value)}</textarea>`;
  else if (type === 'select') input = `<select id="field-${name}" name="${name}" ${required ? 'required' : ''}>${options.blank ? '<option value="">请选择研究分类</option>' : ''}${options.choices.map(choice => {const id = typeof choice === 'object' ? choice.id : choice; const name = typeof choice === 'object' ? `${choice.code} · ${choice.name}` : choice; return `<option value="${escapeHTML(id)}" ${value === id ? 'selected' : ''}>${escapeHTML(name)}</option>`;}).join('')}</select>`;
  else input = `<input id="field-${name}" name="${name}" type="${type}" value="${escapeHTML(type === 'datetime-local' ? toUTCDatetime(value) : value)}" ${type === 'datetime-local' ? 'step="0.001"' : ''} placeholder="${escapeHTML(placeholder)}" ${name === 'title' ? 'maxlength="400"' : ''} ${required ? 'required' : ''}>`;
  return `<div class="field ${wide ? 'wide' : ''}"><label for="field-${name}">${escapeHTML(label)}${required ? '<span class="required">*</span>' : ''}</label>${input}${help ? `<small>${escapeHTML(help)}</small>` : ''}</div>`;
}
function selectField(name,label,options = {}) { return field(name,label,{type:'select',choices:state.config[name],...options}); }
function checkField(name,label,choices,defaults = [],help = '') {
  const checked = currentFormItem?.[name] || defaults;
  return `<div class="field wide"><span class="field-label">${escapeHTML(label)}</span><div class="checkbox-group">${choices.map(choice => {const value = typeof choice === 'object' ? choice.id : choice; const text = typeof choice === 'object' ? choice.name : choice; return `<label class="check-chip"><input type="checkbox" name="${name}" value="${escapeHTML(value)}" ${checked.includes(value) ? 'checked' : ''}><span>${escapeHTML(text)}</span></label>`;}).join('')}</div>${help ? `<small>${escapeHTML(help)}</small>` : ''}</div>`;
}
function formSection(title,content,open = false) { return `<details class="form-section" ${open ? 'open' : ''}><summary>${title}</summary><div class="form-section-content">${content}</div></details>`; }
function openForm(item) {
  if (!item?.id) return;
  currentFormItem = item; formDirty = false;
  const regionField = field('entities_text','相关主体 / 设施',{value:item?.entities.join('、') || '',placeholder:'如：某炼厂、某港口；用顿号或逗号分隔'});
  const content =
    formSection('新闻内容与来源',
      field('title','新闻标题',{wide:true,required:true,placeholder:'用一句话描述这次新增的事实'})+
      field('summary','摘要',{wide:true,type:'textarea',rows:2,placeholder:'记录关键变化，尽量保留规模、时间和对象'})+
      field('body','正文 / 原始事实',{wide:true,type:'textarea',rows:5,placeholder:'粘贴新闻正文或原始公告；判断依据在下一组单独填写'})+
      field('origin_name','来源机构',{placeholder:'如：高盛、EIA、OPEC；不明确可留空',help:'实际发布报告、数据、公告或声明的机构。'})+
      field('source_name','采集信源',{placeholder:'如：财联社、金十数据、同花顺'})+
      selectField('source_type','来源性质')+
      field('source_url','原文链接',{type:'url',wide:true,placeholder:'https://…'}),true)+
    formSection('研究分类与影响',
      selectField('category','主类别',{choices:state.config.categories,blank:true,required:true,value:state.category})+
      field('region','相关国家 / 地区',{placeholder:'如：美国、中东、全球'})+
      checkField('categories','所属研究分类 · 可多选',state.config.categories,newsCategories(item),'主类别会自动包含在所属分类中；同一新闻在多个分类下共用一条记录。')+
      checkField('channels','影响渠道 · 可多选',state.config.channels)+
      checkField('assets','关联期货品种 · 可多选',state.config.assets || ['WTI','Brent','SC'])+
      field('contract','具体合约',{placeholder:'如：CLZ6 / Brent 2026-12 / SC2612'})+
      selectField('direction','对所选品种的影响方向',{value:'不确定'})+
      selectField('confidence','判断确信度',{value:'中'})+
      field('rationale','判断依据',{wide:true,type:'textarea',placeholder:'影响通过什么渠道发生？是否超出预期？有哪些相反作用？'}),true)+
    formSection('时间线与事件阶段',
      field('published_at','首次发布时间（UTC+0）',{type:'datetime-local',required:true,help:'按 UTC+0 填写，首次发布时间须与来源核验结果一致。'})+
      field('occurred_at','事件发生时间（UTC+0）',{type:'datetime-local'})+
      field('collected_at','采集可用时间（UTC+0）',{type:'datetime-local',value:new Date().toISOString()})+
      field('effective_at','生效时间（UTC+0）',{type:'datetime-local'})+
      selectField('stage','事件阶段',{value:'宣布'})+
      regionField)+
    formSection('数量、预期与影响规模',
      field('actual_value','实际值',{placeholder:'如：-200'})+
      field('expected_value','发布前市场预期',{placeholder:'如：-500；没有预期数据可留空'})+
      field('previous_value','当时可见的上期值',{placeholder:'保留当时版本，避免使用事后修订值'})+
      field('unit','计量单位',{placeholder:'如：万桶、万桶/日、%、美元/桶'})+
      field('scale','影响规模',{placeholder:'如：约30万桶/日受影响'})+
      field('duration','预计持续时间',{placeholder:'如：两周；待进一步确认'}))+
    formSection('核验与事件关联',
      selectField('verification','核验状态',{value:'待核验'})+
      field('event_id','事件编号',{placeholder:'如：OPEC-202610-01',help:'同一事件的后续进展沿用该编号。'})+
      field('update_note','本次新增信息 / 更新说明',{wide:true,type:'textarea',placeholder:'与上一条记录相比，哪些事实得到确认、扩大、恢复或取消？'}));
  mountOverlay(`<div class="overlay"><section class="drawer form-drawer" role="dialog" aria-modal="true" aria-labelledby="form-title"><div class="drawer-header"><h2 id="form-title">编辑新闻记录</h2><button class="icon-button" data-close aria-label="关闭表单">${icon('close')}</button></div><form id="news-form" style="display:flex;flex:1;flex-direction:column;min-height:0"><div class="drawer-content"><p class="form-intro">保存原始事实，也保留判断的来由。标题和主类别为必填，其余属性可逐步补全。${item?.is_demo ? ' 当前正在编辑演示记录。' : ''}</p><div id="form-error" hidden class="form-error" role="alert"></div>${content}</div><div class="form-footer"><span>本地持久化保存 · * 为必填</span><div><button class="button secondary" type="button" data-close>取消</button><button class="button primary" type="submit" id="save-news">${icon('check')}保存记录</button></div></div></form></section></div>`,'form');
  $('#news-form').addEventListener('input', () => {formDirty = true;});
  $('#news-form').addEventListener('change', () => {
    formDirty = true;
    const primary = $('#field-category').value;
    $$('input[name="categories"]',$('#news-form')).forEach(input => {if (input.value === primary) input.checked = true;});
  });
  $('#news-form').addEventListener('submit', saveNews);
}
async function saveNews(event) {
  event.preventDefault(); if (formSaving) return;
  const form = $('#news-form'), data = new FormData(form), payload = {};
  for (const [key,value] of data.entries()) {
    if (['assets','channels','categories','entities_text'].includes(key)) continue;
    payload[key] = value;
  }
  payload.assets = data.getAll('assets'); payload.channels = data.getAll('channels');
  payload.categories = [...new Set([payload.category, ...data.getAll('categories')])];
  payload.entities = String(data.get('entities_text') || '').split(/[、,，;；\n]/).map(text => text.trim()).filter(Boolean);
  ['published_at','occurred_at','collected_at','effective_at'].forEach(key => {
    // datetime-local controls here contain UTC clocks, independent of browser zone.
    const input = payload[key];
    if (currentFormItem?.[key] && input && new Date(`${input}Z`).valueOf() === new Date(currentFormItem[key]).valueOf()) payload[key] = currentFormItem[key];
    else payload[key] = input ? new Date(`${input}Z`).toISOString() : '';
  });
  payload.is_demo = currentFormItem?.is_demo || false;
  formSaving = true; $('#save-news').disabled = true; $('#save-news').textContent = '正在保存…'; $('#form-error').hidden = true;
  let saved;
  try {
    const editing = currentFormItem;
    saved = await api(`/api/news/${editing.id}`,{method:'PUT',body:JSON.stringify(payload)});
  } catch (error) {
    $('#form-error').textContent = error.message; $('#form-error').hidden = false;
    $('#form-error').scrollIntoView({block:'center',behavior:'smooth'});
  } finally {
    formSaving = false;
    if ($('#save-news')) {$('#save-news').disabled = false; $('#save-news').innerHTML = `${icon('check')}保存记录`;}
  }
  if (saved) {
    formDirty = false;
    try { await reload(); closeOverlay(); openDetail(saved.id); toast('新闻已保存到本地档案'); }
    catch { closeOverlay(); toast('记录已保存，但列表刷新失败。请点击刷新查看。',true); }
  }
}
async function deleteNews(item) {
  confirmAction({title:'删除这条新闻？',message:`“${item.title}”将从本地档案中移除。`,action:'删除记录',danger:true,onConfirm:async () => {
    await api(`/api/news/${item.id}`,{method:'DELETE'}); await reload(); toast('新闻记录已删除');
  }});
}
function confirmAction({title,message,action,onConfirm,danger = false}) {
  mountOverlay(`<div class="overlay modal-overlay"><section class="modal" role="dialog" aria-modal="true" aria-labelledby="confirm-title"><div class="modal-content"><h3 id="confirm-title">${escapeHTML(title)}</h3><p>${escapeHTML(message)}</p><div id="confirm-error" class="form-error" hidden style="margin-top:15px;margin-bottom:0"></div></div><div class="modal-footer"><button class="button secondary" data-close>取消</button><button class="button ${danger ? 'danger' : 'primary'}" id="confirm-action">${escapeHTML(action)}</button></div></section></div>`,'confirm');
  $('#confirm-action').addEventListener('click', async () => {
    const button = $('#confirm-action'); button.disabled = true; button.textContent = '处理中…';
    try { await onConfirm(); closeOverlay(); }
    catch(error) {$('#confirm-error').textContent = error.message; $('#confirm-error').hidden = false; button.disabled = false;button.textContent = action;}
  });
}
function closeMenu() {$('#sidebar').classList.remove('menu-open');$('.mobile-backdrop')?.remove();$('#menu-button').setAttribute('aria-expanded','false');}
function toggleMenu() {
  if ($('#sidebar').classList.contains('menu-open')) return closeMenu();
  $('#sidebar').classList.add('menu-open');$('#menu-button').setAttribute('aria-expanded','true');
  const backdrop = document.createElement('div');backdrop.className = 'mobile-backdrop';backdrop.addEventListener('click',closeMenu);document.body.append(backdrop);
}
function bindEvents() {
  window.TradingReturn?.initialize({api,toast});
  window.NewsFactor?.initialize({api,toast});
  window.OilResearch?.initialize({api,toast,onNewsSelect:id => {if (state.items.some(item => item.id === id)) openDetail(id);else toast('该新闻当前不在事件列表中，请查看引用原文。',true);},onModelSelect:(model,mode) => selectResearch('leaderboard',model,mode),onModeChange:mode => {state.researchMode = mode;if (state.researchSection === 'benchmark') state.researchSubsection = mode === 'live' ? 'live' : 'backtest-v1';renderNavigation();},onBenchmarkFilterChange:({family,query}) => {if (['all','WTI','Brent','SC'].includes(family)) state.researchFamily = family;if (typeof query === 'string') state.researchQuery = query;renderNavigation();},onBenchmarkDateRangeChange:() => {if (state.researchSection === 'benchmark' && state.researchSubsection === 'backtest-v2') renderNavigation();},openLivePredictions:html => mountOverlay(html,'live-predictions'),isLivePredictionsOpen:() => state.modal === 'live-predictions',closeLivePredictions:closeOverlay,openLiveQuestionDetails:html => mountOverlay(html,'live-question-details'),closeLiveQuestionDetails:closeOverlay});
  $('.sidebar-navigation').addEventListener('click',event => {
    const toggle = event.target.closest('[data-nav-toggle]');
    if (toggle) {toggleSidebarSection(toggle.dataset.navToggle);return;}
    const leaderboardFamily = event.target.closest('[data-leaderboard-family]');
    if (leaderboardFamily) {toggleLeaderboardFamily(leaderboardFamily.dataset.leaderboardFamily);return;}
    const family = event.target.closest('[data-benchmark-family]');
    if (family) {toggleBenchmarkFamily(family.dataset.benchmarkFamily);return;}
    const tradingReturnFolder = event.target.closest('[data-trading-return-folder]');
    if (tradingReturnFolder) {toggleTradingReturnFolder(tradingReturnFolder.dataset.tradingReturnFolder);return;}
    const button = event.target.closest('[data-research-section]');
    if (button) selectResearch(button.dataset.researchSection,button.dataset.researchSubsection,button.dataset.researchMode);
  });
  // Native selects retain focus after a mouse selection; show a focus ring only
  // for keyboard navigation, without forcing focus away from the control.
  document.addEventListener('pointerdown', () => {
    document.documentElement.dataset.inputModality = 'pointer';
  }, true);
  document.addEventListener('keydown', event => {
    if (!['Shift','Control','Alt','Meta'].includes(event.key)) {
      document.documentElement.dataset.inputModality = 'keyboard';
    }
  }, true);
  $('#overview-link').addEventListener('click', showOverview);
  $('#settings-button').addEventListener('click',toggleSettingsMenu);
  $('#general-settings').addEventListener('click',openGeneralSettings);
  document.addEventListener('click',event => {
    if (!event.target.closest('#sidebar-settings')) closeSettingsMenu();
  });
  $('#settings-button').addEventListener('keydown',event => {
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      event.preventDefault();
      if ($('#settings-popover').hidden) toggleSettingsMenu();
      else $('#general-settings').focus();
    }
  });
  $('#contract-nav').addEventListener('click', event => {
    const contract = event.target.closest('[data-contract]');
    if (contract) { selectContract(contract.dataset.contract); return; }
    const family = event.target.closest('[data-family]');
    if (family) toggleContractFamily(family.dataset.family);
  });
  $('#category-nav').addEventListener('click', event => {const button = event.target.closest('[data-category]');if (button) changeCategory(button.dataset.category);});
  $('#refresh-button').addEventListener('click', async () => {try {await reload(true);}catch(error){toast(error.message,true);}});
  $('#search-input').addEventListener('input', event => {state.query = event.target.value;state.page = 1;renderList();});
  ['source','asset','direction','verification','demo','time'].forEach(name => $(`#${name}-filter`).addEventListener('change', event => {state[name] = event.target.value;state.page = 1;renderList();}));
  $('#sort-select').addEventListener('change', event => {state.sort = event.target.value;state.page = 1;renderList();});
  $('#reset-filters').addEventListener('click', resetFilters);
  $('#more-filters').addEventListener('click', () => {moreFiltersOpen = !moreFiltersOpen;$('#advanced-filters').hidden = !moreFiltersOpen;$('#more-filters').setAttribute('aria-expanded',String(moreFiltersOpen));renderList();});
  $('#list-view').addEventListener('click', () => {state.view = 'list';renderList();});
  $('#grid-view').addEventListener('click', () => {state.view = 'grid';renderList();});
  $('#news-content').addEventListener('click', event => {const row = event.target.closest('[data-id]');if(row) openDetail(row.dataset.id);});
  $('#news-content').addEventListener('keydown', event => {if(['Enter',' '].includes(event.key) && event.target.matches('[data-id]')){event.preventDefault();openDetail(event.target.dataset.id);}});
  $('#menu-button').addEventListener('click',toggleMenu);
  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k' && !state.modal && state.category) {event.preventDefault();$('#search-input').focus();}
    if (event.key === 'Escape') {
      if(state.modal) requestCloseOverlay();
      else if (!$('#settings-popover').hidden) closeSettingsMenu(true);
      else closeMenu();
    }
    if (event.key === 'Tab' && state.modal) {
      const focusable = $$('button:not(:disabled),[href],input:not([type="hidden"]),select,textarea,[tabindex="0"]',$('#overlay-root')).filter(element => element.getClientRects().length);
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && document.activeElement === first) {event.preventDefault();last?.focus();}
      else if (!event.shiftKey && document.activeElement === last) {event.preventDefault();first?.focus();}
    }
  });
  window.addEventListener('beforeunload', event => {if(state.modal === 'form' && formDirty){event.preventDefault();event.returnValue='';}});
}
async function initialize() {
  hydrateIcons();
  renderSidebarSections();
  try {
    if (!window.OilContracts) throw new Error('合约图表未能加载，请刷新页面重试。');
    state.config = await api('/api/config'); await reload();bindEvents();
    pollUpdateStatus();
    setInterval(pollUpdateStatus,15000);
    document.addEventListener('visibilitychange',() => { if (!document.hidden) pollUpdateStatus(); });
    if (new URLSearchParams(location.search).get('settings') === 'sources') $('#source-maintenance').click();
  } catch(error) {
    $('#overview').innerHTML = `<div class="error-banner">${escapeHTML(error.message)}<br><button class="button secondary" id="retry-init">重新连接</button></div>`;
    $('#retry-init').addEventListener('click',initialize);
  }
}
initialize();
