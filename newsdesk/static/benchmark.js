(function (global) {
  'use strict';

  const esc = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
  const families = ['WTI','Brent','SC'];
  const names = {WTI:'WTI 主连',Brent:'Brent 主连',SC:'SC 主连'};
  const modeNames = {backtest:'回测',live:'实盘'};
  const statuses = {waiting_dependency:'等待前置实验完成',queued:'等待预测',running:'预测中',completed:'已完成',partial:'部分完成',failed:'评估失败',interrupted:'已中断',pending:'待预测',succeeded:'成功',success:'已评分',skipped:'已跳过',predicted_pending:'等待结算',awaiting_settlement:'等待结算',waiting_new_completed_bar:'等待当日完整行情',ready:'可生成预测',forecast_closed:'已过提交截止',prediction_locked:'预测已锁定',prediction_generating:'预测生成中',prediction_failed:'本轮预测失败',pending_result:'待结算',settled:'已结算',late_forecast:'预测已逾期'};
  const isRunning = run => ['queued','running'].includes(run?.status);
  const isLegacy = run => !!run && !['backtest','live'].includes(run.mode);
  const isNoToolsRun = run => run?.environment_id === 'env02' || String(run?.protocol || '').startsWith('env02-');
  const isSearchRun = run => ['env03','env04'].includes(run?.environment_id) || run?.tool_mode === 'agent_initiated_indicators_only' || run?.tool_mode === 'agent_initiated_local_search' || /(?:^|-)v[345](?:$|-)/.test(run?.protocol_version || run?.protocol || '');
  const normalizeModelId = run => {
    const raw = String(run?.model_id || run?.model || run?.strategy_id || '').toLowerCase();
    const value = raw.includes('/') ? raw.slice(raw.lastIndexOf('/') + 1) : raw;
    return value === 'gpt6.1-sol' ? 'gpt-6.1-sol' : value.replace(/^aihub-/, '');
  };
  const isSummary = () => view?.section === 'leaderboard' && ['overview','summary'].includes(view.subsection);
  const isExperimentSetup = () => view?.section === 'leaderboard' && view.subsection === 'experiment-setup';
  const isMemoryBenchmark = () => view?.section === 'benchmark' && view.subsection === 'memory-leakage';
  const isBacktestV2 = () => view?.section === 'benchmark' && view.subsection === 'backtest-v2';
  const selectedModelId = () => view?.section === 'leaderboard' && !isSummary() && !isExperimentSetup() ? String(view.subsection || 'gpt-6.1-sol').toLowerCase() : '';
  const modelLabel = (model,run) => `${model}${run?.experiment_id ? `(${run.experiment_name || run.experiment_id})` : ''}`;
  const publishedExperiment = () => [...(data?.experiments || [])].filter(item => item.ready_for_leaderboard).sort((a,b) => String(b.created_at || '').localeCompare(String(a.created_at || '')))[0];
  const experimentNumber = item => {
    const match = /^Exp(\d+)$/i.exec(String(item?.experiment_id || ''));
    return match ? Number(match[1]) : null;
  };
  const experimentSort = (a,b) => {
    const left = experimentNumber(a), right = experimentNumber(b);
    if (left !== null || right !== null) {
      if (left === null) return 1;
      if (right === null) return -1;
      if (left !== right) return right - left;
    }
    return String(b.started_at || b.created_at || '').localeCompare(String(a.started_at || a.created_at || ''));
  };
  const publishedRun = run => !run.experiment_id || (data?.experiments || []).some(item => item.experiment_id === run.experiment_id && item.ready_for_leaderboard);
  const isThirtyMinuteRun = run => run?.retrieval_time_policy === 'forecast-start-minus-30m-utc-v1' || /(?:^|-)v5(?:$|-)/.test(run?.protocol || '');
  const isPreciseRun = run => isThirtyMinuteRun(run) || run?.retrieval_time_policy === 'forecast-start-minus-2h-utc-v1' || /(?:^|-)v4(?:$|-)/.test(run?.protocol || '');
  const retrievalBufferMinutes = item => {
    const minutes = item?.buffer_minutes ?? item?.retrieval_buffer_minutes ?? item?.retrieval_boundary?.buffer_minutes;
    if (Number.isFinite(minutes) && minutes > 0) return minutes;
    return isThirtyMinuteRun(item) ? 30 : 120;
  };
  const environmentVersion = run => isNoToolsRun(run) ? 'env02 · 无搜索与工具 · 仅 evaluator agent' : run?.environment_id === 'env00' || String(run?.protocol || '').startsWith('env00-') ? 'env00 · 仅技术指标 · UTC 提前30分钟' : run?.environment_id === 'env03' ? 'env03 · s01 搜索策略 · UTC 提前30分钟' : run?.environment_id === 'env04' ? 'env04 · s02 RSI 搜索策略 · UTC 提前30分钟' : isThirtyMinuteRun(run) ? 'env01 v5 · UTC 时分检索 · 提前30分钟' : isPreciseRun(run) ? 'env01 v4 · UTC 时分检索 · 提前2小时' : isLegacy(run) ? 'env01 v1 · 收盘价预测' : isSearchRun(run) ? 'env01 v3 · 主动本地搜索' : 'env01 v2 · 固定新闻输入';
  const isDiscardedRun = run => Boolean(run?.discarded_by_user || run?.discarded);
  const callCount = value => Array.isArray(value) ? value.length : value !== null && value !== undefined && Number.isFinite(Number(value)) ? Number(value) : null;
  const numeric = (value,digits = 2) => value !== null && value !== undefined && Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '—';
  const number = value => Number.isFinite(Number(value)) ? Number(value).toLocaleString('zh-CN') : '0';
  const percent = value => value !== null && value !== undefined && Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(2)}%` : '—';
  const change = value => value !== null && value !== undefined && Number.isFinite(Number(value)) ? `${Number(value) > 0 ? '+' : ''}${Number(value).toFixed(2)}%` : '—';
  const direction = value => ({up:'涨',down:'跌',flat:'平',上涨:'涨',下跌:'跌',持平:'平',涨:'涨',跌:'跌',平:'平'})[value] || (value ? esc(value) : '—');
  const directionTone = value => ({up:'overview-up',上涨:'overview-up',涨:'overview-up',down:'overview-down',下跌:'overview-down',跌:'overview-down',flat:'overview-flat',持平:'overview-flat',平:'overview-flat'})[value] || '';
  const date = value => value ? esc(String(value).slice(0,10).replaceAll('-','.')) : '—';
  const utcMinute = value => {
    if (!value || typeof value === 'string' && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)) return null;
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? null : parsed.toISOString().slice(0,16).replace('T',' ');
  };
  const stamp = value => value ? esc(utcMinute(value) ? `${utcMinute(value)} UTC+0` : `${value}（时区待核实）`) : '—';
  const status = value => `<span class="research-status ${esc(value || 'pending')}">${esc(statuses[value] || value || '待预测')}</span>`;
  const metricList = run => Array.isArray(run?.metrics) ? run.metrics : Object.entries(run?.metrics || {}).map(([family,metric]) => ({family,...metric}));
  const getMetric = (run,family) => metricList(run).find(metric => (metric.family || metric.id) === family);
  const hasSummaryCoverage = metric => {
    const count = Number(metric?.count),total = Number(metric?.total);
    return Number.isInteger(count) && Number.isInteger(total) && total > 0 && count <= total && count * 10 > total * 9;
  };
  const shortId = value => String(value || '').length > 21 ? `${String(value).slice(0,18)}…` : String(value || '—');
  let options;
  let root;
  let view = null;
  let active = false;
  let data = null;
  let loading = false;
  let error = '';
  let busy = '';
  let loadedAt = 0;
  let selectedRun = '';
  let page = 1;
  let benchmarkComposing = false;
  let overviewExperimentIds = [];
  let overviewFamily = 'WTI';
  let overviewDate = '';
  let environmentExample = null;
  let environmentExampleKey = '';
  let environmentExamplePending = false;
  let memoryDataset = null;
  let memoryPending = false;
  let memoryError = '';
  let memoryMonth = '';
  let selectedMemoryRun = '';
  let memoryResultMonth = '';
  let memoryResultPage = 1;
  const memoryRunCache = new Map();
  const memoryRunPending = new Set();
  const memoryRunErrors = new Map();
  const overviewPredictions = new Map();
  const overviewPending = new Set();
  const overviewErrors = new Map();
  const questionCache = new Map();
  const questionErrors = new Map();
  const questionPending = new Set();
  const runCache = new Map();
  const runPending = new Set();
  const traceCache = new Map();
  const tracePending = new Set();
  const traceErrors = new Map();
  const traceExpanded = new Set();
  const traceScrollPositions = new Map();
  let livePredictionSelection = null;
  let livePredictionPayload = null;
  let livePredictionError = '';
  let livePredictionPending = false;
  let livePredictionRequest = 0;
  let livePredictionSignature = '';
  let liveQuestionDetailsSelection = null;

  function questionWindows(item) {
    const windows = item?.time_windows || item?.display_time_windows;
    return windows?.timezone === 'UTC' ? windows : null;
  }
  function windowRange(window) {
    const start = utcMinute(window?.start_utc),end = utcMinute(window?.end_utc);
    const label = window?.trading_date ? `交易日 ${window.trading_date} · ` : '';
    if (!start || !end) return `${label}${window?.kind === 'closed' || window?.status === 'closed' ? '休市，无已确认交易时段' : '时段待核实'}`;
    const qualifiers = [];
    if (window.segments?.length > 1) qualifiers.push('分段');
    if (window.calendar_status === 'unconfirmed' || window.night_session_status === 'unconfirmed') qualifiers.push('含待核实时段');
    return `${start} — ${end}${qualifiers.length ? `（${qualifiers.join('；')}）` : ''}`;
  }
  function windowSegments(window,label) {
    const segments = (window?.segments || []).filter(segment => utcMinute(segment.start_utc) && utcMinute(segment.end_utc));
    const link = /^https?:\/\//i.test(window?.source_url || '') ? `<a href="${esc(window.source_url)}" target="_blank" rel="noopener noreferrer">交易所时段</a>` : '';
    return `<section class="benchmark-time-period"><h4>${label}${window?.trading_date ? ` · 交易日 ${esc(window.trading_date)}` : ''}</h4>${segments.length ? `<ol>${segments.map(segment => `<li>${segment.label ? `<span>${esc(segment.label)}</span>` : ''}<time>${esc(utcMinute(segment.start_utc))} — ${esc(utcMinute(segment.end_utc))}</time></li>`).join('')}</ol>` : `<p>${esc(windowRange(window))}</p>`}${window?.calendar_status === 'unconfirmed' || window?.night_session_status === 'unconfirmed' ? '<p>节假日、夜盘或特殊交易时段待核实。</p>' : ''}${link}</section>`;
  }
  function timeWindowsMarkup(item,{details = true,legacy = false,summary = false} = {}) {
    const windows = questionWindows(item);
    const known = windows?.known,forecast = windows?.forecast;
    if (summary) return `<span class="benchmark-time-summary"><span class="benchmark-utc-tag">UTC+0</span><span>已知 ${esc(windowRange(known))}</span><span>预测 ${esc(windowRange(forecast))}</span></span>`;
    const annotation = !item?.time_windows && !!item?.display_time_windows;
    const precise = isPreciseRun(item);
    const forecastStart = precise && windows?.status === 'ready' && forecast?.start_utc ? new Date(forecast.start_utc).getTime() : NaN;
    const buffer = retrievalBufferMinutes(item);
    const cutoff = isNoToolsRun(item) ? null : precise ? item?.cutoff_utc || item?.retrieval_boundary?.cutoff_utc || (Number.isFinite(forecastStart) ? new Date(forecastStart - buffer * 60 * 1000).toISOString() : null) : item?.time_windows?.information_cutoff_utc;
    const compact = `<div class="benchmark-time-windows"><span class="benchmark-utc-tag">UTC+0</span><div><b>已知</b><span>${esc(windowRange(known))}</span></div><div><b>预测</b><span>${esc(windowRange(forecast))}</span></div>${utcMinute(cutoff) ? `<div class="benchmark-news-cutoff"><b>${precise ? `检索截止 · 提前 ${buffer === 120 ? '2 小时' : `${buffer} 分钟`}` : '新闻截止'}</b><span>${esc(utcMinute(cutoff))}</span></div>` : ''}</div>`;
    if (!details) return compact;
    return compact + `<details class="benchmark-time-detail" data-case-key="${esc(item?.case_id || 'question')}:time-windows"><summary>查看交易时段明细${annotation || legacy ? ' · 历史展示补注' : ''}</summary><div>${windowSegments(known,'已知行情')}${windowSegments(forecast,'预测目标')}<p class="benchmark-time-basis">${windows ? 'UTC+0 · 交易所常规时段参考，供应商日线的精确聚合边界未经独立核实。' : '该记录未提供可核对的时分区间，不按日期推算。'}${annotation || legacy ? ' 此处为展示补注，原题、模型输入与原有成绩保持历史记录。' : ''}</p></div></details>`;
  }
  function questionTextMarkup(item) {
    const text = String(item?.question || '');
    if (!text) return '';
    const windows = item?.time_windows;
    const endpoints = [windows?.known?.start_utc,windows?.known?.end_utc,windows?.forecast?.start_utc,windows?.forecast?.end_utc].map(utcMinute);
    const repeated = windows?.timezone === 'UTC' && endpoints.every(value => value && text.includes(value));
    return repeated
      ? `<p class="benchmark-original-question">题目正文已折叠，时间窗口见上方。</p><details class="benchmark-question-detail" data-case-key="${esc(item.case_id || 'question')}:full-question"><summary>查看完整题目</summary><p>${esc(text)}</p></details>`
      : `<p class="benchmark-original-question">${esc(text)}</p>`;
  }

  function normalizeResearchView(next) {
    const normalized = {...next,mode:next.mode === 'live' ? 'live' : 'backtest'};
    if (normalized.section !== 'benchmark') return normalized;
    const legacyFamily = families.includes(normalized.subsection) ? normalized.subsection : null;
    if (legacyFamily) normalized.subsection = normalized.mode === 'live' ? 'live' : 'backtest-v1';
    if (normalized.subsection === 'live') normalized.mode = 'live';
    else if (['backtest-v1','backtest-v2','memory-leakage'].includes(normalized.subsection)) normalized.mode = 'backtest';
    normalized.family = families.includes(normalized.family) ? normalized.family : legacyFamily || 'all';
    normalized.query = typeof normalized.query === 'string' ? normalized.query : '';
    return normalized;
  }
  function mode() {
    if (view?.section === 'benchmark' && view.subsection === 'live') return 'live';
    if (view?.section === 'benchmark' && ['backtest-v1','backtest-v2','memory-leakage'].includes(view.subsection)) return 'backtest';
    return view?.section !== 'leaderboard' && view?.mode === 'live' ? 'live' : 'backtest';
  }
  function benchmarkFamily() {return families.includes(view?.family) ? view.family : families.includes(view?.subsection) ? view.subsection : 'all';}
  function benchmarkQuery() {return typeof view?.query === 'string' ? view.query : '';}
  function benchmarkSelectionLabel() {return names[benchmarkFamily()] || '全部合约';}
  function filterBenchmarkQuestions(questions,{query = true} = {}) {
    const family = benchmarkFamily();
    const terms = query ? benchmarkQuery().trim().toLocaleLowerCase().split(/\s+/).filter(Boolean) : [];
    return questions.filter(item => {
      if (family !== 'all' && item.family !== family) return false;
      const windows = questionWindows(item);
      const searchable = [item.question,item.contract_code,item.contract_id,item.family,names[item.family],
        item.input_date,item.target_date,item.actual_target_date,item.case_id,
        ...[item.input_date,item.target_date,item.actual_target_date].filter(Boolean).map(value => String(value).replaceAll('-','.')),
        windows?.known?.trading_date,windows?.forecast?.trading_date,
        windows?.known?.start_utc,windows?.known?.end_utc,windows?.forecast?.start_utc,windows?.forecast?.end_utc].filter(Boolean).join(' ').toLocaleLowerCase();
      return terms.every(term => searchable.includes(term));
    });
  }
  function setBenchmarkFilters({family = benchmarkFamily(),query = benchmarkQuery()} = {}) {
    if (view?.section !== 'benchmark' || isMemoryBenchmark()) return;
    view.family = families.includes(family) ? family : 'all';
    view.query = String(query || '');
    page = 1;
    options.onBenchmarkFilterChange?.({family:view.family,query:view.query});
    render();
    updateLiveCountdowns();
  }
  function benchmarkFilterMarkup(all,filtered) {
    const selected = benchmarkFamily(),query = benchmarkQuery();
    return `<div class="benchmark-filters"><label class="benchmark-family-select">合约<select id="benchmark-family-filter" aria-label="选择Benchmark合约"><option value="all" ${selected === 'all' ? 'selected' : ''}>全部合约</option>${families.map(family => `<option value="${family}" ${selected === family ? 'selected' : ''}>${esc(names[family])}</option>`).join('')}</select></label><div class="benchmark-search"><label for="benchmark-question-search">搜索题目</label><div><input type="search" id="benchmark-question-search" aria-label="搜索Benchmark题目" placeholder="题干、代码、日期或题号" value="${esc(query)}" autocomplete="off"><button type="button" class="research-link" data-benchmark-clear-search ${query ? '' : 'disabled'}>清空搜索</button></div></div><p class="benchmark-filter-count" data-benchmark-filter-count role="status">${number(filtered.length)} / ${number(all.length)} 题</p></div>`;
  }
  function benchmarkNoMatches() {
    return empty('没有匹配的题目','调整合约或搜索关键词后继续查看。','<button type="button" class="button secondary" data-benchmark-reset-filters>清空筛选</button>');
  }
  function benchmarkProfile() {return isBacktestV2() ? 'v2' : 'v1';}
  function benchmarkData(selectedMode,selectedProfile) {
    const profile = selectedProfile || (selectedMode === undefined ? benchmarkProfile() : 'v1');
    selectedMode = selectedMode || mode();
    if (selectedMode === 'backtest' && profile === 'v2') return data?.benchmarks?.backtest_v2 || {};
    const candidate = data?.benchmarks?.[selectedMode] || (selectedMode === 'backtest' ? data?.benchmark : null);
    // The old month-contract/close-only dataset belongs to the historical v1 table.
    return candidate?.series_type === 'dominant_continuous' || /v2/.test(candidate?.version || '') ? candidate : {};
  }
  function datasetId(selectedMode,selectedProfile) {return benchmarkData(selectedMode,selectedProfile).dataset_id || '';}
  function backtestV2DateRange(current = benchmarkData('backtest','v2')) {
    const requested = current?.date_range;
    const validDate = value => typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value)
      && Number.isFinite(Date.parse(`${value}T00:00:00Z`)) && new Date(`${value}T00:00:00Z`).toISOString().slice(0,10) === value;
    return requested && validDate(requested.start) && validDate(requested.end) && requested.start <= requested.end
      ? {start:requested.start,end:requested.end,inclusive:requested.inclusive !== false}
      : {start:'2026-08-01',end:'2026-09-30',inclusive:true};
  }
  function backtestV2PeriodLabel(range) {
    const [startYear,startMonth] = range.start.split('-').map(Number);
    const [endYear,endMonth] = range.end.split('-').map(Number);
    return startYear !== endYear ? `${startYear} 年 ${startMonth} 月—${endYear} 年 ${endMonth} 月`
      : startMonth === endMonth ? `${startYear} 年 ${startMonth} 月` : `${startYear} 年 ${startMonth}—${endMonth} 月`;
  }
  function liveHistoryData() {return data?.benchmarks?.live_history || {};}
  function modeRuns() {return (data?.runs || []).filter(run => !isLegacy(run) && run.mode === mode());}
  function selectedModelRuns() {
    const selected = selectedModelId();
    return modeRuns().filter(run => !selected || normalizeModelId(run) === selected);
  }
  function questionKey() {
    const current = benchmarkData();
    return `${mode()}:${benchmarkProfile()}:${current.dataset_id || ''}:${current.settled_questions || 0}:${current.ready_questions || 0}:${mode() === 'live' ? liveHistoryData().history_revision || 'history-loading' : ''}:all`;
  }

  function initialize(config) {
    if (options) return;
    options = config;
    root = document.querySelector('#research-workspace');
    root.addEventListener('click',handleClick);
    root.addEventListener('input',event => {
      if (event.target.id !== 'benchmark-question-search' || event.isComposing || benchmarkComposing) return;
      setBenchmarkFilters({query:event.target.value});
    });
    root.addEventListener('compositionstart',event => {if (event.target.id === 'benchmark-question-search') benchmarkComposing = true;});
    root.addEventListener('compositionend',event => {
      if (event.target.id !== 'benchmark-question-search') return;
      benchmarkComposing = false;setBenchmarkFilters({query:event.target.value});
    });
    root.addEventListener('change',event => {
      if (event.target.id === 'benchmark-family-filter') {setBenchmarkFilters({family:event.target.value});return;}
      if (event.target.matches('[data-overview-experiment]')) {
        const id = event.target.value;
        if (!overviewExperiments().some(item => item.experiment_id === id)) return;
        overviewExperimentIds = event.target.checked ? [...new Set([...overviewExperimentIds,id])] : overviewExperimentIds.filter(value => value !== id);
        overviewDate = '';render();loadOverviewPredictions();return;
      }
      if (event.target.id === 'memory-result-month-filter') {memoryResultMonth = event.target.value;memoryResultPage = 1;render();return;}
      if (event.target.id === 'memory-month-filter') {memoryMonth = event.target.value;page = 1;render();return;}
      if (['overview-family-filter','daily-family-filter'].includes(event.target.id)) {
        if (families.includes(event.target.value)) {overviewFamily = event.target.value;render();}
        return;
      }
      if (event.target.id === 'daily-date-filter') {overviewDate = event.target.value;render();return;}
      if (event.target.id === 'research-mode-filter') {
        view.mode = event.target.value; selectedRun = '';page = 1;
        options.onModeChange?.(view.mode);render();loadQuestions();loadRun();
      }
    });
    load();
    setInterval(() => {
      if (document.hidden || loading || busy) return;
      if (active || (data?.runs || []).some(isRunning)) load(true);
      if (livePredictionSelection && options.isLivePredictionsOpen?.()) loadLivePredictions();
    },8000);
    setInterval(updateLiveCountdowns,1000);
    document.addEventListener('visibilitychange',() => {
      if (!document.hidden) updateLiveCountdowns();
    });
  }

  function show(next) {
    const normalized = normalizeResearchView(next);
    const changed = !view || normalized.section !== view.section || normalized.subsection !== view.subsection || normalized.mode !== view.mode;
    const filtersChanged = !view || normalized.family !== view.family || normalized.query !== view.query;
    view = normalized;active = true;
    if (changed) {page = 1;selectedRun = '';selectedMemoryRun = '';memoryResultMonth = '';memoryResultPage = 1;}
    if (filtersChanged) page = 1;
    benchmarkComposing = false;
    render();
    if (!data || Date.now() - loadedAt > 15000) load();
    if (view.section === 'benchmark') loadQuestions();
    if (view.section === 'leaderboard') {loadRun();loadMemoryRun();}
    if (view.section === 'environment' && !['env00-extend','env02'].includes(view.subsection)) loadEnvironmentExample();
  }

  async function load(quiet = false) {
    if (!options || loading) return;
    loading = true;
    if (!quiet && !data && active) render();
    try {
      const result = await options.api('/api/research');
      const previous = `${datasetId('backtest')}:${datasetId('backtest','v2')}:${datasetId('live')}:${liveHistoryData().history_revision || ''}`;
      data = result;data.runs = Array.isArray(data.runs) ? data.runs : [];
      const validExperiments = overviewExperimentIds.filter(id => overviewExperiments().some(item => item.experiment_id === id));
      if (validExperiments.length !== overviewExperimentIds.length) {overviewExperimentIds = validExperiments;overviewDate = '';}
      if (previous !== `${datasetId('backtest')}:${datasetId('backtest','v2')}:${datasetId('live')}:${liveHistoryData().history_revision || ''}`) {questionCache.clear();questionErrors.clear();}
      error = '';loadedAt = Date.now();updateNavigation();
      if (active && view?.section === 'benchmark') loadQuestions();
      if (active && view?.section === 'leaderboard') {loadRun();loadMemoryRun();}
      if (active && view?.section === 'environment' && !['env00-extend','env02'].includes(view.subsection)) loadEnvironmentExample();
    } catch (caught) {error = caught.message;}
    finally {loading = false;if (active) render();}
  }

  function updateNavigation() {
    const modelId = run => {
      const raw = String(run?.model_id || run?.model || run?.strategy_id || '').toLowerCase();
      const value = raw.includes('/') ? raw.slice(raw.lastIndexOf('/') + 1) : raw;
      return value === 'gpt6.1-sol' ? 'gpt-6.1-sol' : value;
    };
    const familyOf = id => {
      const value = String(id || '').toLowerCase();
      if (value.startsWith('gpt')) return 'GPT';
      if (value.startsWith('claude')) return 'Claude';
      if (value.startsWith('deepseek')) return 'DeepSeek';
      if (value.startsWith('kimi')) return 'Kimi';
      if (value.startsWith('glm')) return 'GLM';
      if (value.startsWith('hy')) return 'Hy';
      if (value.startsWith('qwen')) return 'Qwen';
      if (value.startsWith('doubao')) return 'Doubao';
      return '';
    };
    document.querySelectorAll('[data-leaderboard-model-count]').forEach(element => {
      const model = element.dataset.leaderboardModelCount.toLowerCase();
      element.textContent = number(data.runs.filter(run => modelId(run) === model).length);
      const experiment = publishedExperiment();
      const run = experiment && data.runs.find(run => normalizeModelId(run) === model && run.experiment_id === experiment.experiment_id);
      const label = element.parentElement.querySelector('span:first-child');
      if (label) label.textContent = modelLabel(model,run);
    });
    document.querySelectorAll('[data-leaderboard-family-count]').forEach(element => {
      const family = element.dataset.leaderboardFamilyCount;
      element.textContent = number(data.runs.filter(run => familyOf(modelId(run)) === family).length);
    });
    document.querySelectorAll('[data-benchmark-count]').forEach(element => {
      const family = (benchmarkData('backtest').families || []).find(item => item.id === element.dataset.benchmarkCount);
      element.textContent = family ? number(family.count) : '—';
    });
    document.querySelectorAll('[data-benchmark-mode-count]').forEach(element => {
      const [familyId,selectedMode] = element.dataset.benchmarkModeCount.split(':');
      const summary = selectedMode === 'live' ? liveHistoryData() : benchmarkData(selectedMode);
      const family = (summary.families || []).find(item => item.id === familyId);
      element.textContent = family ? number(family.count) : '—';
    });
    document.querySelectorAll('[data-benchmark-section-count]').forEach(element => {
      const section = element.dataset.benchmarkSectionCount;
      const summary = section === 'live' ? liveHistoryData() : benchmarkData('backtest',section === 'backtest-v2' ? 'v2' : 'v1');
      element.textContent = summary.total_questions !== undefined ? number(summary.total_questions) : '—';
    });
    options?.onBenchmarkDateRangeChange?.(backtestV2DateRange());
  }

  async function loadQuestions(force = false) {
    if (isMemoryBenchmark()) {await loadMemoryQuestions(force);return;}
    if (view?.section !== 'benchmark' || mode() === 'backtest' && !benchmarkData().total_questions) return;
    const key = questionKey();
    if ((!force && questionCache.has(key)) || questionPending.has(key)) return;
    questionPending.add(key);
    const selectedMode = mode(),profile = benchmarkProfile();
    try {
      const result = await options.api(`/api/benchmark/questions?mode=${selectedMode}${selectedMode === 'live' ? '&history=1' : profile === 'v2' ? '&profile=v2' : ''}`);
      if (!Array.isArray(result.items)) throw new Error('题目列表格式无效，请重新读取');
      questionCache.set(key,result.items);questionErrors.delete(key);
    } catch (caught) {questionErrors.set(key,caught.message);}
    finally {questionPending.delete(key);if (active && view?.section === 'benchmark') render();}
  }
  async function loadMemoryQuestions(force = false) {
    if (memoryPending || memoryDataset && !force) return;
    memoryPending = true;memoryError = '';
    try {
      const result = await options.api('/api/benchmark?kind=memory-leakage');
      if (!Array.isArray(result.questions)) throw new Error('记忆泄漏题库格式无效');
      memoryDataset = result;
      document.querySelectorAll('[data-memory-question-count]').forEach(element => {element.textContent = number(result.total_questions ?? result.questions.length);});
    } catch (caught) {memoryError = caught.message;}
    finally {memoryPending = false;if (active && isMemoryBenchmark()) render();}
  }
  async function loadEnvironmentExample() {
    const current = benchmarkData('backtest');
    const family = ['SC','WTI','Brent'].find(id => (current.families || []).some(item => item.id === id && item.count > 0));
    const key = `${current.dataset_id || ''}:${family || ''}`;
    if (!family || environmentExamplePending || environmentExampleKey === key) return;
    environmentExamplePending = true;
    environmentExampleKey = key;
    environmentExample = null;
    try {
      const result = await options.api(`/api/benchmark/questions?family=${family}&mode=backtest`);
      environmentExample = (result.items || []).find(item => questionWindows(item)) || result.items?.[0] || null;
    } catch (_) {environmentExample = null;}
    finally {environmentExamplePending = false;if (active && view?.section === 'environment') render();}
  }

  function currentRunSummary() {
    const all = selectedModelRuns();
    const experiment = publishedExperiment();
    return all.find(run => run.run_id === selectedRun) ||
      (experiment && all.find(run => run.experiment_id === experiment.experiment_id)) ||
      all.find(run => !run.experiment_id && !run.one_day && !run.family && run.total === 90) ||
      all.find(run => !run.experiment_id) || all[0] || null;
  }
  function currentRun() {
    const summary = currentRunSummary();
    if (!summary) return null;
    const cached = runCache.get(summary.run_id)?.value;
    return cached ? {...cached,...summary,cases:cached.cases || []} : summary;
  }
  async function loadRun(force = false) {
    if (isExperimentSetup()) return;
    if (isSummary()) {await loadOverviewPredictions(force);return;}
    const run = currentRunSummary();
    if (!run || runPending.has(run.run_id)) return;
    const cached = runCache.get(run.run_id);
    if (!force && cached) {
      const age = Date.now() - cached.at;
      const changed = ['completed','status','succeeded','pending','updated_at','finished_at'].some(key => run[key] !== cached.value[key]);
      const retainedCases = Array.isArray(cached.value.cases) ? cached.value.cases.length : Object.keys(cached.value.cases || {}).length;
      if (age < 1500 || (!changed && retainedCases >= (Number(run.completed) || 0) && (!isRunning(run) || age < 5000))) return;
    }
    const id = run.run_id;runPending.add(id);
    try {runCache.set(id,{value:await options.api(`/api/evaluations/${encodeURIComponent(id)}`),at:Date.now()});}
    catch (caught) {if (active) options.toast(caught.message,true);}
    finally {runPending.delete(id);if (active && view?.section === 'leaderboard') render();}
  }

  function modeSelector() {
    return `<label class="research-mode-select">评估类型<select id="research-mode-filter" aria-label="选择回测或实盘"><option value="backtest" ${mode() === 'backtest' ? 'selected' : ''}>回测</option><option value="live" ${mode() === 'live' ? 'selected' : ''}>实盘</option></select></label>`;
  }
  function toolbar(text,actions = '') {
    return `<div class="research-toolbar"><p>${text}</p><div class="research-actions">${actions}<button type="button" class="button secondary" data-research-action="refresh" ${busy ? 'disabled' : ''}>刷新</button></div></div>`;
  }
  function buildButton() {
    const exists = !!benchmarkData().total_questions;
    const label = mode() === 'live' ? exists ? '更新实盘题目' : '构建实盘题目' : isBacktestV2() ? exists ? '重建回测 v2' : '构建回测 v2' : exists ? '重建回测集' : '构建回测集';
    return `<button type="button" class="button ${exists ? 'secondary' : 'primary'}" data-research-action="build" ${busy ? 'disabled' : ''}>${busy === 'build' ? '正在构建…' : label}</button>`;
  }
  function empty(title,description,action = '') {
    return `<div class="research-empty"><span class="research-empty-symbol" aria-hidden="true">↗</span><h3>${title}</h3><p>${description}</p>${action}</div>`;
  }
  function panel(title,sub,body,extra = '') {
    return `<section class="research-panel"><header class="research-panel-heading"><div><h2>${title}</h2>${sub ? `<p>${sub}</p>` : ''}</div>${extra}</header>${body}</section>`;
  }
  function dataSummary() {
    const current = benchmarkData();
    if (isBacktestV2()) {
      const range = backtestV2DateRange(current);
      const covered = (current.families || []).filter(family => family.count > 0);
      const counts = families.map(family => `${family} ${number((current.families || []).find(item => item.id === family)?.count)}`).join(' · ');
      const actualRange = (start,end) => start && end ? `实际覆盖 ${date(start)} — ${date(end)}` : '按可核验日线确定实际覆盖';
      return `<div class="research-stat-grid benchmark-v2-stat-grid"><div class="research-stat"><span>回测 v2 问题</span><strong>${number(current.total_questions)}</strong><small>实际可核验题量 · 日频四项预测</small></div><div class="research-stat"><span>已知交易日限制</span><strong class="research-date-range">${date(range.start)} <em>—</em> ${date(range.end)}</strong><small>${actualRange(current.input_start,current.input_end)}</small></div><div class="research-stat"><span>预测交易日限制</span><strong class="research-date-range">${date(range.start)} <em>—</em> ${date(range.end)}</strong><small>${actualRange(current.target_start,current.target_end)}</small></div><div class="research-stat"><span>主连品类</span><strong>${covered.length}<em>类</em></strong><small>${counts}${current.dataset_id ? `<br>版本 ${esc(shortId(current.dataset_id))}` : ' · 题库尚未构建'}</small></div></div>`;
    }
    return `<div class="research-stat-grid"><div class="research-stat"><span>${modeNames[mode()]}问题</span><strong>${number(current.total_questions)}</strong><small>日频 · 四项预测</small></div><div class="research-stat"><span>主连品类</span><strong>${(current.families || []).filter(family => family.count > 0).length || '—'}</strong><small>WTI · Brent · SC</small></div><div class="research-stat"><span>${mode() === 'live' ? '待结算问题' : '每品类回测窗口'}</span><strong>${number(mode() === 'live' ? current.pending_questions : current.days_per_contract || current.days_per_family || 30)}<em>${mode() === 'live' ? '题' : '交易日'}</em></strong><small>${mode() === 'live' ? '预测先锁定，结果随后结算' : '主连真实日线，不使用月份合约替代'}</small></div><div class="research-stat"><span>${mode() === 'live' ? '最新输入日期' : '真实答案日期'}</span><strong class="research-date-range">${mode() === 'live' ? date(current.input_end) : `${date(current.target_start)} <em>—</em> ${date(current.target_end)}`}</strong><small>${current.dataset_id ? `版本 ${esc(shortId(current.dataset_id))}` : '尚未构建主连测试集'}</small></div></div>`;
  }
  function backtestV2CoverageMarkup(current) {
    if (!isBacktestV2()) return '';
    const reasons = {data_gap_unverified:'行情缺口未核实',unconfirmed_time_windows:'特殊交易时段待核实',invalid_ohlc:'无效日线',zero_baseline:'基期收盘为零'};
    const included = (current.families || []).map(family => {
      const selections = family.selection ? [family.selection] : (family.contracts || []).map(contract => contract.selection).filter(Boolean);
      if (!selections.length) return null;
      const counts = {},pairs = [],missing = new Set(),noSeparate = new Set(),assignments = new Map();
      let candidates = 0,hasCandidates = false;
      selections.forEach(selection => {
        Object.entries(selection.excluded_counts || {}).forEach(([reason,value]) => {
          if (Number.isInteger(value) && value >= 0) counts[reason] = (counts[reason] || 0) + value;
        });
        (selection.excluded_pairs || []).forEach(pair => pairs.push(pair));
        (selection.unverified_missing_weekdays || []).forEach(day => missing.add(String(day)));
        (selection.no_separate_trade_dates || []).forEach(day => noSeparate.add(String(day)));
        (selection.trading_date_assignments || []).forEach(item => {
          if (item?.date && item?.assigned_trading_date) assignments.set(`${item.date}:${item.assigned_trading_date}`,item);
        });
        if (Number.isInteger(selection.candidate_pairs) && selection.candidate_pairs >= 0) {candidates += selection.candidate_pairs;hasCandidates = true;}
      });
      return {family,counts,pairs,missing:[...missing].sort(),noSeparate:[...noSeparate].sort(),assignments:[...assignments.values()],candidates:hasCandidates ? candidates : null,
              excluded:Object.values(counts).reduce((sum,value) => sum + value,0)};
    }).filter(Boolean);
    if (!included.length) return '';
    const total = included.reduce((sum,item) => sum + item.excluded,0);
    const rows = included.map(item => {
      const counts = Object.entries(item.counts).filter(([,value]) => value > 0).map(([reason,value]) => `${reasons[reason] || reason} ${number(value)}组`).join('、');
      const assignedDates = new Set(item.assignments.map(value => String(value.date)));
      const calendar = item.assignments.map(value => `无独立交易日 ${esc(value.date)}，归属 ${esc(value.assigned_trading_date)}`)
        .concat(item.noSeparate.filter(day => !assignedDates.has(day)).map(day => `无独立交易日 ${esc(day)}`)).join('；');
      return `<p data-v2-selection-family="${esc(item.family.id)}"><b>${esc(names[item.family.id] || item.family.id)}</b><span>${number(item.family.count)} 题${item.candidates !== null ? ` · 候选 ${number(item.candidates)} 组` : ''} · 剔除 ${number(item.excluded)} 组${counts ? `（${esc(counts)}）` : ''}${item.missing.length ? `；未核实缺失工作日：${item.missing.map(esc).join('、')}` : ''}${calendar ? `；${calendar}` : ''}</span></p>`;
    }).join('');
    const pairs = included.flatMap(item => item.pairs.map(pair => `<li data-v2-exclusion-reason="${esc(pair.reason)}"><b>${esc(item.family.id)}</b><span>已知 ${esc(pair.input_date || '未记录')} → 预测 ${esc(pair.target_date || '未记录')}：${esc(reasons[pair.reason] || pair.reason || '原因未记录')}${pair.missing_weekdays?.length ? `；缺失 ${pair.missing_weekdays.map(esc).join('、')}` : ''}</span></li>`)).join('');
    const assignments = included.flatMap(item => item.assignments.map(value => `<li data-v2-trading-date-assignment="${esc(value.date)}"><b>${esc(item.family.id)}</b><span>无独立交易日 ${esc(value.date)}，交易归属 ${esc(value.assigned_trading_date)}${/^https?:\/\//i.test(value.source_url || '') ? ` · <a href="${esc(value.source_url)}" target="_blank" rel="noopener noreferrer">交易所依据</a>` : ''}</span></li>`)).join('');
    return `<section class="benchmark-v2-coverage" aria-label="回测v2题量核验"><header><strong>题量核验</strong><span>按可核验的相邻交易日样本构建</span></header>${rows}${pairs || assignments ? `<details data-case-key="backtest-v2:selection"><summary>${pairs ? `查看 ${number(total)} 组剔除明细${assignments ? '与交易日归属' : ''}` : '查看交易日归属依据'}</summary><ol>${pairs}${assignments}</ol></details>` : ''}</section>`;
  }

  function runCoversFamily(run,family) {
    if (!run || (run.unavailable_families || []).includes(family)) return false;
    if (run.family && run.family !== family) return false;
    const metric = getMetric(run,family);
    if (metric) return Number(metric.total) > 0;
    const cases = Array.isArray(run.cases) ? run.cases : Object.values(run.cases || {});
    return cases.some(item => item.family === family);
  }
  function fourMetricCard(run,family) {
    const unavailable = !!run && (run.unavailable_families || []).includes(family);
    const metric = isLegacy(run) || unavailable ? null : getMetric(run,family);
    const unit = metric?.unit || (family === 'SC' ? '元 / 桶' : '美元 / 桶');
    const caption = unavailable ? '当前行情不可用' : !run ? '待评估' : metric?.eligible_for_ranking ? '完整结果' : run && metric ? '尚未完整结算' : '未参与本轮评估';
    const coverage = unavailable ? '缺少可用的当日完整主连行情，本轮未生成预测' : run && !metric ? '该品类未参与当前运行，未生成或评分' : `${number(metric?.count)} / ${number(metric?.total)} 题已评分${metric?.pending ? ` · ${number(metric.pending)} 题待结算` : ''} · 同模式、同环境协议比较`;
    return `<article class="research-metric research-four-metric"><div><span>${family} 主连</span><span class="research-metric-caption">${caption}</span></div><strong>${percent(metric?.direction_accuracy)}</strong><p>收盘涨跌方向正确率</p><dl class="research-mini-metrics"><div><dt>涨跌幅 MAE</dt><dd>${numeric(metric?.return_pct_mae,4)}<small>百分点</small></dd></div><div><dt>最高价 MAE</dt><dd>${numeric(metric?.high_mae)}<small>${esc(unit)}</small></dd></div><div><dt>最低价 MAE</dt><dd>${numeric(metric?.low_mae)}<small>${esc(unit)}</small></dd></div></dl><small>${coverage}</small></article>`;
  }
  function overviewExperiments() {
    return (data?.experiments || []).filter(item => item.experiment_id).sort(experimentSort);
  }
  const comparisonKey = model => model.comparison_key || model.model;
  const comparisonLabel = (model,run) => modelLabel(model.model,run || model);
  function comparableRuns(runs,anchor) {
    return anchor ? runs.filter(item => item.environment_id === anchor.environment_id && item.protocol === anchor.protocol && item.experiment_id === anchor.experiment_id &&
      ['dataset_snapshot_sha256','news_snapshot_sha256','technical_snapshot_sha256'].every(key => anchor[key] && item[key] === anchor[key])) : [];
  }
  function comparisonCohort() {
    const experiments = isSummary() ? overviewExperiments().filter(item => overviewExperimentIds.includes(item.experiment_id)) : [];
    const full = modeRuns().filter(item => !isDiscardedRun(item) && !item.one_day && !item.family && (isSearchRun(item) || isNoToolsRun(item)) &&
      families.every(family => Number(getMetric(item,family)?.total) > 0) &&
      families.reduce((sum,family) => sum + Number(getMetric(item,family).total),0) === Number(item.total))
      .sort((a,b) => String(b.started_at || b.run_id).localeCompare(String(a.started_at || a.run_id)));
    const strategies = (data?.strategies || []).filter(item => String(item.id || '').startsWith('aihub-'));
    const latest = new Map();
    if (experiments.length) {
      // Freeze the comparison cohort independently within each experiment.
      // Cross-experiment rows share a model name, never a result key.
      let anchor = null;
      for (const experiment of experiments) {
        const runs = full.filter(item => item.experiment_id === experiment.experiment_id && (!experiment.run_ids?.length || experiment.run_ids.includes(item.run_id)));
        anchor ||= runs[0] || null;
        comparableRuns(runs,runs[0]).forEach(item => {
          const key = `${normalizeModelId(item)}::${experiment.experiment_id}`;
          if (!latest.has(key)) latest.set(key,item);
        });
      }
      const ordered = [...experiments].sort((a,b) => a.experiment_id.localeCompare(b.experiment_id,undefined,{numeric:true}));
      const discarded = new Set(modeRuns().filter(item => isDiscardedRun(item)).map(item => `${normalizeModelId(item)}::${item.experiment_id}`));
      const models = strategies.flatMap(model => ordered.filter(experiment => !experiment.models?.length || experiment.models.includes(model.model)).map(experiment => ({
        ...model,comparison_key:`${model.model}::${experiment.experiment_id}`,experiment_id:experiment.experiment_id,experiment_name:experiment.name || experiment.experiment_id,
      }))).filter(model => !discarded.has(comparisonKey(model)));
      return {anchor,latest,models};
    }
    const published = full.filter(publishedRun);
    const anchor = published.find(item => item.run_id === selectedRun) || published[0];
    if (!anchor) return {anchor:null,latest,models:[]};
    comparableRuns(published,anchor).forEach(item => {const model = normalizeModelId(item);if (!latest.has(model)) latest.set(model,item);});
    return {anchor,latest,models:strategies};
  }
  function comparisonTable() {
    if (mode() !== 'backtest') return '';
    const {anchor,latest,models} = comparisonCohort();
    if (!anchor) return '';
    const rows = models.map(model => {
      const item = latest.get(model.model);
      if (!item) return `<tr><td><strong>${esc(model.model)}</strong><small>${esc(model.family)}</small></td><td colspan="3" class="research-muted">这组冻结数据尚无全量回测</td><td>0 / 90</td><td>未开始</td><td></td></tr>`;
      return `<tr class="${normalizeModelId(item) === selectedModelId() ? 'selected' : ''}"><td><strong>${esc(model.model)}</strong><small>${esc(model.family)}</small></td>${families.map(family => metricCell(item,family)).join('')}<td>${number(item.succeeded)} / ${number(item.total)}<small>${item.failed ? `${number(item.failed)} 题待处理` : '已评分'}</small></td><td>${status(item.status)}${item.runtime_notice ? `<small class="research-inline-error">${esc(item.runtime_notice)}</small>` : ''}<small>${item.batch_phase === 'pilot' || item.batch_phase === 'pilot_complete' ? '接口与归档验证阶段' : ''}</small></td><td><button type="button" class="research-link" data-compare-run="${esc(item.run_id)}" data-compare-model="${esc(model.model)}">查看记录</button></td></tr>`;
    }).join('');
    const table = `<div class="research-table-scroll"><table class="research-table leaderboard-table research-v2-leaderboard"><thead><tr><th>模型</th><th>WTI 主连 · USD/桶</th><th>Brent 主连 · USD/桶</th><th>SC 主连 · CNY/桶</th><th>成功 / 总题数</th><th>状态</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
    return panel('全量回测 · 模型对比','同一组 90 道题、同一 env01 协议、同一新闻与技术快照。各列依次显示方向正确率、涨跌幅 MAE（百分点）、最高价 / 最低价 MAE；未跑完的成绩标为暂定。',table,`<span class="count-badge">${latest.size} 个模型</span>`);
  }

  async function loadOverviewPredictions(force = false) {
    if (!isSummary()) return;
    const {latest} = comparisonCohort();
    const tasks = [...latest.values()].map(async run => {
      const id = run.run_id;
      const signature = [run.updated_at,run.status,run.succeeded,run.failed].join(':');
      if (overviewPending.has(id) || (!force && (overviewPredictions.get(id)?.signature === signature || overviewErrors.get(id)?.signature === signature))) return;
      overviewPending.add(id);
      try {
        const result = await options.api(`/api/evaluations/${encodeURIComponent(id)}`);
        if (result.run_id !== id || !Array.isArray(result.cases)) throw new Error('预测记录格式不完整');
        const cases = result.cases.map(item => ({case_id:item.case_id,family:item.family,
          target_date:item.actual_target_date || item.target_date,status:item.status,
          time_windows:item.time_windows,display_time_windows:item.display_time_windows,
          input_market:result.input_market?.[item.case_id] || {date:item.input_date,...item.current_bar,close:item.current_bar?.close ?? item.current_price},
          predicted:item.predicted,absolute_errors:item.absolute_errors,error:item.error}));
        overviewPredictions.set(id,{signature,cases});overviewErrors.delete(id);
      } catch (caught) {overviewErrors.set(id,{signature,message:caught.message});}
      finally {overviewPending.delete(id);}
    });
    await Promise.allSettled(tasks);
    if (active && isSummary()) render();
  }

  function accuracyChart(family,models,latest) {
    const ranked = models.map(model => {
      const run = latest.get(comparisonKey(model)),metric = getMetric(run,family);
      const value = metric?.direction_accuracy;
      const accuracy = Number(metric?.count) > 0 && value != null && Number.isFinite(Number(value)) && Number(value) >= 0 && Number(value) <= 1 ? Number(value) : null;
      return {model,run,metric,accuracy};
    }).filter(item => hasSummaryCoverage(item.metric) && item.accuracy !== null)
      .sort((a,b) => b.accuracy - a.accuracy || comparisonLabel(a.model,a.run).localeCompare(comparisonLabel(b.model,b.run)));
    const rows = ranked.map(({model,run,metric,accuracy}) => {
      const provisional = accuracy !== null && !metric.eligible_for_ranking;
      const coverage = `${number(metric?.count)} / ${number(metric?.total || 30)} 题已评分`;
      const label = `${comparisonLabel(model,run)}，${family} 方向准确率 ${percent(accuracy)}，${coverage}${provisional ? '，暂定结果' : ''}。查看回测记录`;
      return `<li class="summary-rank-row${provisional ? ' is-provisional' : ''}" data-model="${esc(comparisonKey(model))}" data-accuracy="${accuracy ?? ''}"><button type="button" class="summary-rank-button" ${run ? `data-compare-run="${esc(run.run_id)}" data-compare-model="${esc(model.model)}"` : 'disabled'} aria-label="${esc(label)}" title="${esc(label)}"><span class="summary-rank-label"><span>${esc(comparisonLabel(model,run))}${provisional ? '<em>暂定</em>' : ''}</span><strong>${percent(accuracy)}</strong></span><span class="summary-bar-track" aria-hidden="true"><span class="summary-bar-fill" style="width:${accuracy === null ? 0 : accuracy * 100}%"></span></span>${provisional ? `<small>${coverage}</small>` : accuracy === null ? '<small>暂无已评分结果</small>' : ''}</button></li>`;
    }).join('');
    return `<section class="summary-chart" data-summary-family="${family}" aria-label="${esc(names[family])}方向准确率">${ranked.length ? `<div class="summary-chart-scale" aria-hidden="true"><span>0%</span><span>50%</span><span>100%</span></div><ol class="summary-rank-list">${rows}</ol>` : '<p class="research-muted">当前合约暂无成功率超过 90% 的有效评分结果</p>'}</section>`;
  }

  function summaryTable(models,latest) {
    const columns = [['direction_accuracy','方向 Acc.'],['high_mae','最高 MAE'],['low_mae','最低 MAE'],['return_pct_mae','涨跌幅 MAE']];
    const valueOf = (model,family,key) => {
      const metric = getMetric(latest.get(comparisonKey(model)),family),value = metric?.[key];
      return hasSummaryCoverage(metric) && value != null && Number.isFinite(Number(value)) && Number(value) >= 0 && (key !== 'direction_accuracy' || Number(value) <= 1) ? Number(value) : null;
    };
    const headings = `<thead><tr><th rowspan="2" scope="col" class="summary-model-column">模型(实验)</th>${families.map(f => `<th colspan="4" scope="colgroup">${esc(names[f])}</th>`).join('')}</tr><tr>${families.map(f => columns.map(([key,label]) => `<th scope="col">${label}<small>${key === 'direction_accuracy' ? '%' : key === 'return_pct_mae' ? '百分点' : f === 'SC' ? '元 / 桶' : '美元 / 桶'}</small></th>`).join('')).join('')}</tr></thead>`;
    const rows = models.map(model => {
      const run = latest.get(comparisonKey(model)),peers = models.filter(item => item.model === model.model);
      return `<tr data-summary-model="${esc(comparisonKey(model))}"><th scope="row" class="summary-model-column">${run ? `<button type="button" class="research-link" data-compare-run="${esc(run.run_id)}" data-compare-model="${esc(model.model)}">${esc(comparisonLabel(model,run))}</button>` : esc(comparisonLabel(model,run))}</th>${families.map(f => {
        const metric = getMetric(run,f),qualified = hasSummaryCoverage(metric),provisional = qualified && !metric.eligible_for_ranking;
        return columns.map(([key]) => {
          const value = valueOf(model,f,key),candidates = peers.map(peer => {
            const raw = valueOf(peer,f,key);
            // Equal displayed scores are ties, including floating-point noise.
            return {model:peer,value:raw === null ? null : Number(key === 'direction_accuracy' ? (raw * 100).toFixed(2) : raw.toFixed(4))};
          }).filter(item => item.value !== null);
          const optimum = key === 'direction_accuracy' ? Math.max(...candidates.map(item => item.value)) : Math.min(...candidates.map(item => item.value));
          const tied = candidates.filter(item => item.value === optimum).sort((a,b) => String(b.model.experiment_id || '').localeCompare(String(a.model.experiment_id || ''),undefined,{numeric:true}));
          const best = candidates.length > 1 && comparisonKey(tied[0].model) === comparisonKey(model);
          const classes = [provisional ? 'summary-provisional-cell' : '',best ? 'summary-metric-best' : ''].filter(Boolean).join(' ');
          return `<td data-family="${f}" data-metric="${key}" class="${classes}" title="${number(metric?.count)} / ${number(metric?.total || 30)} 题已评分${!qualified ? ' · 成功率须超过 90% 才展示指标' : provisional ? ' · 暂定结果' : ''}${best ? ` · 同模型跨实验${key === 'direction_accuracy' ? '最高准确率' : '最低 MAE'}${tied.length > 1 ? ' · 并列时取最高实验版本' : ''}` : ''}">${key === 'direction_accuracy' ? percent(value) : numeric(value,4)}${provisional && value !== null ? '<sup aria-label="暂定结果">†</sup>' : ''}</td>`;
        }).join('');
      }).join('')}</tr>`;
    }).join('');
    return `<div class="research-table-scroll summary-table-scroll" tabindex="0" role="region" aria-label="各模型三主连方向 Acc. 与 MAE 对比表，可横向滚动"><table class="research-table summary-mae-table"><caption class="visually-hidden">各模型在 WTI、Brent、SC 主连回测中的方向 Acc.、最高价、最低价和收盘涨跌幅 MAE</caption>${headings}<tbody>${rows}</tbody></table></div>`;
  }

  function overviewFamilySelect(id,label) {
    return `<select id="${id}" aria-label="${label}">${families.map(f => `<option value="${f}" ${overviewFamily === f ? 'selected' : ''}>${esc(names[f])}</option>`).join('')}</select>`;
  }

  function overviewExperimentSelect(anchor) {
    const experiments = overviewExperiments();
    const selected = experiments.filter(item => overviewExperimentIds.length ? overviewExperimentIds.includes(item.experiment_id) : item.experiment_id === anchor?.experiment_id);
    const selection = `<fieldset class="overview-experiment-picker" id="overview-experiment-filter"><legend>回测实验 <span>可多选</span></legend><div class="overview-experiment-options"><button type="button" class="overview-default-experiment" data-overview-default aria-pressed="${!overviewExperimentIds.length}">默认结果</button>${experiments.map(item => `<label class="overview-experiment-option"><input type="checkbox" data-overview-experiment value="${esc(item.experiment_id)}" aria-label="${esc(item.name || item.experiment_id)}" ${overviewExperimentIds.includes(item.experiment_id) ? 'checked' : ''}><span>${esc(item.name || item.experiment_id)}</span><small>${esc(statuses[item.status] || item.status || '待预测')}</small></label>`).join('')}</div></fieldset>`;
    const summary = selected.length ? selected.map(experiment => `<div class="overview-experiment-count"><strong>${esc(experiment.name || experiment.experiment_id)}</strong>${status(experiment.status)}<span>已评分 ${number(experiment.succeeded)} / ${number(experiment.total)} 题${experiment.failed ? ` · 失败 ${number(experiment.failed)} 题` : ''}${!experiment.ready_for_leaderboard ? ' · 暂定结果' : ''}</span></div>`).join('') : '<span>历史回测结果</span>';
    return `<div class="overview-experiment-toolbar">${selection}<div class="overview-experiment-summary" role="status">${summary}</div></div>`;
  }

  function dailyDirectionConsensus(items) {
    const counts = {up:0,down:0,flat:0};
    for (const item of items) {
      if (item?.status === 'success' && Object.prototype.hasOwnProperty.call(counts,item.predicted?.direction)) counts[item.predicted.direction]++;
    }
    const total = Object.values(counts).reduce((sum,value) => sum + value,0);
    const maximum = Math.max(...Object.values(counts));
    return {counts,total,maximum,ratio:total ? maximum / total : null,
      leaders:total ? Object.keys(counts).filter(key => counts[key] === maximum) : []};
  }

  function predictionTone(value,baseline = 0) {
    if (value == null || baseline == null || !Number.isFinite(Number(value)) || !Number.isFinite(Number(baseline))) return '';
    return Number(value) > Number(baseline) ? 'overview-up' : Number(value) < Number(baseline) ? 'overview-down' : '';
  }

  function predictionDirection(value) {
    const label = {up:'看涨',down:'看跌',flat:'持平'}[value] || '—';
    const tone = value === 'up' ? 'overview-up' : value === 'down' ? 'overview-down' : '';
    return `<span class="${tone}">${label}</span>`;
  }

  function inputMarketMarkup(market,unit) {
    const fields = [['open','开盘价'],['close','收盘价'],['high','最高价'],['low','最低价'],['return_pct','涨跌幅']];
    return `<div class="overview-input-market"><p>前一交易日行情${market?.date ? ` · ${esc(market.date)}` : ''} <span>${unit}</span></p><dl>${fields.map(([key,label]) => `<div><dt>${label}</dt><dd data-input-metric="${key}" class="${key === 'return_pct' ? predictionTone(market?.[key]) : ''}">${key === 'return_pct' ? change(market?.[key]) : numeric(market?.[key],2)}</dd></div>`).join('')}</dl></div>`;
  }

  function dailyPredictionTable(models,latest) {
    const dates = [...new Set([...latest.values()].flatMap(run => (overviewPredictions.get(run.run_id)?.cases || [])
      .filter(item => item.family === overviewFamily).map(item => item.target_date).filter(Boolean)))].sort().reverse();
    // Keep the selected prediction date when available for the newly selected contract.
    if (!dates.includes(overviewDate)) overviewDate = dates[0] || '';
    const controls = `<div class="overview-controls">${overviewFamilySelect('daily-family-filter','日频预测主连')}<label class="overview-date-label">被预测日期<select id="daily-date-filter" aria-label="被预测日期" ${dates.length ? '' : 'disabled'}>${dates.length ? dates.map(value => `<option value="${esc(value)}" ${value === overviewDate ? 'selected' : ''}>${esc(value)}</option>`).join('') : '<option value="">暂无日期</option>'}</select></label></div>`;
    const selected = models.map(model => {
      const run = latest.get(comparisonKey(model));
      const item = (overviewPredictions.get(run?.run_id)?.cases || []).find(item => item.family === overviewFamily && item.target_date === overviewDate);
      return {model,run,item};
    });
    const unit = overviewFamily === 'SC' ? '元 / 桶' : '美元 / 桶';
    const market = selected.map(row => row.item?.input_market).find(item => item?.date && item.date < overviewDate && item.close != null);
    const columns = [['high','最高价',unit],['low','最低价',unit],['return_pct','收盘涨跌幅','% / 误差：百分点']];
    const rows = selected.map(({model,run,item}) => {
      const available = item?.status === 'success';
      const reason = overviewErrors.has(run?.run_id) ? '加载失败，可点击重试' : overviewPending.has(run?.run_id) ? '正在读取预测记录' : item?.status === 'failed' ? '本题预测失败' : '该日期暂无有效预测';
      const cells = columns.map(([key]) => {
        const value = available ? item.predicted?.[key] : null;
        const error = available ? item.absolute_errors?.[key] : null;
        return `<td data-daily-metric="${key}" title="${available ? 'MAE 为本题预测值与真实值的绝对误差' : reason}"><span class="${predictionTone(value,key === 'return_pct' ? 0 : market?.close ?? null)}">${key === 'return_pct' ? change(value) : numeric(value,2)}</span>${error != null ? `<small>MAE ${numeric(error,4)}</small>` : ''}</td>`;
      }).join('');
      return `<tr data-daily-model="${esc(comparisonKey(model))}"><th scope="row">${esc(comparisonLabel(model,run))}</th><td data-daily-metric="direction" title="${available ? '' : reason}">${available ? predictionDirection(item.predicted?.direction) : '—'}</td>${cells}</tr>`;
    }).join('');
    const consensus = dailyDirectionConsensus(selected.map(row => row.item));
    const leaders = consensus.leaders.map(predictionDirection).join(' / ');
    const tally = `看涨 ${consensus.counts.up} · 看跌 ${consensus.counts.down}${consensus.counts.flat ? ` · 持平 ${consensus.counts.flat}` : ''}`;
    const consensusValue = consensus.total < 2 ? (consensus.total ? '样本不足' : '—') : `${leaders}${consensus.leaders.length > 1 ? ' 并列' : ''} ${percent(consensus.ratio)}`;
    const footer = `<tfoot><tr><th scope="row">当日方向一致性</th><td colspan="4" class="overview-consensus" title="一致率 = 多数方向的有效预测数 ÷ 全部有效预测数；每个模型（实验）计一份预测，仅统计所选主连、被预测日期的成功预测。">${consensusValue}${consensus.total ? `<small>${tally} · ${consensus.total} / ${models.length} 份预测有效</small>` : ''}</td></tr></tfoot>`;
    const heading = `<thead><tr><th scope="col">模型(实验)</th><th scope="col">方向</th>${columns.map(([,label,units]) => `<th scope="col">${label}<small>${units}</small></th>`).join('')}</tr></thead>`;
    const failedLoads = [...latest.values()].filter(run => overviewErrors.has(run.run_id)).length;
    const notice = failedLoads ? `<div class="overview-load-notice" role="status">${failedLoads} 个模型的记录读取失败 <button type="button" class="research-link" data-research-action="refresh">重试</button></div>` : overviewPending.size ? '<div class="overview-load-notice" role="status">正在读取预测记录…</div>' : '';
    const table = `<div class="research-table-scroll" tabindex="0" role="region" aria-label="日频模型预测对比，可横向滚动"><table class="research-table overview-daily-table" data-prediction-date="${esc(overviewDate)}" data-prediction-family="${overviewFamily}">${heading}<tbody>${rows}</tbody>${footer}</table></div>`;
    const timeExample = selected.map(row => row.item).find(item => questionWindows(item));
    return panel('日频预测','',timeWindowsMarkup(timeExample || {},{details:true}) + inputMarketMarkup(market,unit) + notice + table,controls);
  }

  function leaderboardSummary() {
    const {anchor,latest,models} = comparisonCohort();
    const selector = overviewExperimentSelect(anchor);
    if (!anchor) return selector + empty(overviewExperimentIds.length ? '所选实验暂无回测结果' : '暂无可比较的全量回测','完成三个主连的回测后，这里将展示方向准确率排名和三项 MAE。') + memoryOverview();
    const chart = `<section class="research-panel overview-accuracy-panel"><header class="research-panel-heading"><div><h2>方向准确率</h2><p>仅展示当前合约测评成功率超过 90% 的结果。</p></div><div class="overview-controls">${overviewFamilySelect('overview-family-filter','方向准确率主连')}</div></header>${accuracyChart(overviewFamily,models,latest)}</section>`;
    const table = panel('MAE 对比',`各合约测评成功率须超过 90%，未达标的指标显示 —。${overviewExperimentIds.length > 1 ? '<span class="summary-best-legend">黄色</span>：同模型跨实验最优值；并列时取实验版本号最高者。' : ''}方向 Acc. 越高越好，MAE 越低越好。`,summaryTable(models,latest));
    return `<div class="overview-page">${selector}${chart}${table}${dailyPredictionTable(models,latest)}${memoryOverview()}</div>`;
  }

  function experimentSetup() {
    const experiments = (data?.experiments || []).filter(item => item.experiment_id).sort(experimentSort);
    if (!experiments.length) return memoryExperiments().length ? memoryExperimentSetup() : empty('尚无命名实验','只有明确指定实验号的实验才会记录在这里。');
    return `<div class="experiment-setup-grid">${experiments.map(item => {
      const boundary = isNoToolsRun(item) ? '不适用 · 无检索接口' : item.retrieval_buffer_minutes != null ? `预测交易区间开始前 ${item.retrieval_buffer_minutes} 分钟 · UTC` : item.retrieval_buffer_hours != null ? `预测交易区间开始前 ${item.retrieval_buffer_hours} 小时 · UTC` : '题目输入日及以前 · 历史日期门限';
      const familyCounts = families.map(family => `${family} ${number(item.family_question_counts?.[family] ?? item.families?.find(row => row.id === family)?.count ?? (item.questions_per_model === 90 ? 30 : 0))} 题`).join('、');
      const fields = [['环境',environmentVersion({environment_id:item.environment_id,protocol:item.protocol,mode:'backtest'})],
        ['Benchmark',`${item.benchmark_profile === 'v2' ? 'v2 · ' : ''}${item.questions_per_model} 题 / 模型 · ${item.full_backtest ? familyCounts : '单题验证'}`],
        ['被预测日期',`${item.target_start || '—'} — ${item.target_end || '—'}`],
        ['检索截止',boundary],['信息来源',isNoToolsRun(item) ? '仅题干与已知日线行情 · 新闻、技术指标、联网及其他工具全部关闭' : item.environment_id === 'env00' ? '仅冻结技术指标与题干日线 · 新闻、本地新闻搜索、联网搜索全部关闭' : '本地冻结新闻与技术指标 · 模型主动检索 · 联网搜索关闭'],
        ...(item.depends_on_experiment ? [['前置实验',`${item.depends_on_experiment} 执行、补跑和审计结束后启动`]] : []),
        ['预测与评分','收盘方向正确率；最高价、最低价、收盘涨跌幅 MAE'],
        ['留存内容',isNoToolsRun(item) ? '题干与已知行情、公开分析文档、四项预测和无工具权限校验记录' : item.environment_id === 'env00' ? '公开分析文档、四项预测、技术指标查询参数及完整返回' : '公开分析文档、四项预测、新闻与技术指标调用及完整返回'],
        ['结果发布',item.experiment_id ? item.ready_for_leaderboard ? '全量完成且审计通过 · 已发布到 Leaderboard' : '全量完成且审计通过后自动更新 Leaderboard' : '历史记录保留']];
      const profiles = Object.entries(item.model_profiles || {}).map(([model,profile]) => `<tr><th scope="row">${esc(model)}</th><td>${profile.max_output_tokens ? number(profile.max_output_tokens) : '原运行配置'}</td><td>${profile.max_rounds || '—'}</td><td>${esc(profile.api_protocol || (model.startsWith('gpt-') ? 'responses' : 'chat'))}</td><td>${esc(profile.provider || '默认')}</td><td>${profile.client_timeout_seconds || '—'} s</td></tr>`).join('');
      return `<article class="research-panel experiment-card" data-experiment-id="${esc(item.id)}"><header class="research-panel-heading"><div><h2>${esc(item.name)}</h2><p>${stamp(item.created_at)}</p></div><div class="experiment-card-progress">${status(item.status)}<span>${number(item.succeeded)} / ${number(item.total)} 题成功${item.failed ? ` · ${number(item.failed)} 题失败` : ''}</span></div></header><dl class="experiment-settings">${fields.map(([label,value]) => `<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl><div class="experiment-models"><strong>${item.models.length} 个模型</strong><p>${item.models.map(model => `<span>${esc(model)}</span>`).join('')}</p></div><details class="experiment-details" data-experiment-details="${esc(item.id)}"><summary>模型参数与冻结版本</summary><div class="research-table-scroll"><table class="research-table"><thead><tr><th>模型</th><th>输出上限</th><th>交互轮数</th><th>API 协议</th><th>提供方</th><th>超时</th></tr></thead><tbody>${profiles}</tbody></table></div><dl class="experiment-settings experiment-fingerprints">${[['Benchmark',item.dataset_id],['Benchmark 协议',item.benchmark_version],['评估协议',item.protocol],['题目快照',item.dataset_snapshot_sha256],['新闻快照',item.news_snapshot_sha256],['技术快照',item.technical_snapshot_sha256],['执行代码',item.code_frozen ? '已冻结，重试复用同一实现' : '历史记录']].map(([label,value]) => `<div><dt>${label}</dt><dd>${esc(value || '未记录')}</dd></div>`).join('')}</dl></details></article>`;
    }).join('')}</div>` + memoryExperimentSetup();
  }

  function leaderboard() {
    if (isSummary()) return leaderboardSummary();
    if (isExperimentSetup()) return experimentSetup();
    const runs = selectedModelRuns(),run = currentRun();
    // Execution progress follows this model's active/latest run, independently
    // of the completed experiment selected for result inspection below.
    const progressRun = runs.find(item => item.status === 'running') || runs.find(isRunning) || runs.find(item => item.status === 'waiting_dependency') || runs[0] || run;
    const actions = '<label class="research-mode-select">评估类型<select aria-label="评估类型"><option selected>回测</option></select></label>';
    const history = runs.length ? `<div class="research-table-scroll"><table class="research-table leaderboard-table research-v2-leaderboard"><thead><tr><th>模型(实验)</th><th>WTI 主连</th><th>Brent 主连</th><th>SC 主连</th><th>预测 / 评分</th><th>状态 / 时间</th><th></th></tr></thead><tbody>${[...runs].sort(experimentSort).map(item => `<tr class="${run?.run_id === item.run_id ? 'selected' : ''}"><td><strong>${esc(modelLabel(normalizeModelId(item),item))}</strong><small>${modeNames[item.mode]} · ${esc(environmentVersion(item))}</small><small title="${esc(item.dataset_id)}">${esc(shortId(item.dataset_id))}${item.dataset_id === datasetId() ? ' · 当前版本' : ' · 历史版本'}</small></td>${families.map(family => metricCell(item,family)).join('')}<td class="research-number">${number(item.completed)} / ${number(item.total)}<small>已评分 ${number(item.succeeded)}${item.pending ? ` · 待结算 ${number(item.pending)}` : ''}</small></td><td>${status(item.status)}<small>${stamp(item.finished_at || item.started_at)}</small></td><td><button type="button" class="research-link" data-open-run="${esc(item.run_id)}">查看</button>${item.mode === 'live' && !isRunning(item) ? `<button type="button" class="research-link" data-settle-run="${esc(item.run_id)}" ${busy ? 'disabled' : ''}>更新结算</button>` : ''}</td></tr>`).join('')}</tbody></table></div>` : empty(`尚无${modeNames[mode()]}评估记录`,mode() === 'live' ? '先生成实盘题目，再锁定四项预测；下一交易日行情完整后评分。' : '主连回测集就绪后，可运行方向与三项数值预测。');
    return toolbar('',actions) + (progressRun ? progressMarkup(progressRun) : '') + panel('回测实验记录','',history,`<span class="count-badge">${runs.length}</span>`) + memoryModelRecords() + (run ? runDetails(run) : '');
  }
  function metricCell(run,family) {
    const metric = getMetric(run,family);
    if ((run.unavailable_families || []).includes(family)) return '<td class="research-metric-cell"><strong>—</strong><small>当前行情不可用</small><small>本轮未生成预测</small></td>';
    if (!metric || Number(metric.total) === 0) return '<td class="research-metric-cell"><strong>—</strong><small>未参与本轮评估</small></td>';
    return `<td class="research-metric-cell"><strong>${percent(metric?.direction_accuracy)}</strong><small>幅 ${numeric(metric?.return_pct_mae,4)} pp</small><small>高 ${numeric(metric?.high_mae)} / 低 ${numeric(metric?.low_mae)}</small><small>${number(metric?.count)} / ${number(metric?.total)}${metric?.eligible_for_ranking ? ' · 完整' : ' · 暂定'}</small></td>`;
  }
  function progressMarkup(run) {
    const total = Number(run.total) || 0,completed = Number(run.completed) || 0;
    const percentValue = total ? Math.min(100,Math.round(completed / total * 100)) : 0;
    return `<section class="research-progress-panel" aria-label="评估进度" data-progress-run="${esc(run.run_id)}"><div class="research-progress-heading"><div><strong>${esc(modelLabel(normalizeModelId(run),run))}</strong>${status(run.status)}<span>已处理 ${number(completed)} / ${number(total)} 题</span></div><span>${percentValue}%</span></div><div class="research-progress-track" role="progressbar" aria-label="已处理题目" aria-valuemin="0" aria-valuemax="${total || 100}" aria-valuenow="${completed}"><span style="width:${percentValue}%"></span></div><p>已评分 ${number(run.succeeded)} <span>·</span> 待结算 ${number(run.pending)} <span>·</span> 失败 ${number(run.failed)} <span>·</span> ${esc(environmentVersion(run))}${isSearchRun(run) ? ` <span>·</span> 主动搜索 ${callCount(run.search_calls) === null ? '待归档' : `${number(callCount(run.search_calls))} 次`}${callCount(run.search_used_cases) === null ? '' : ` / ${number(run.search_used_cases)} 题调用`}${callCount(run.news_used_cases) === null ? '' : ` <span>·</span> 获得合格新闻 ${number(run.news_used_cases)} 题`} <span>·</span> ${callCount(run.trace_cases) === null ? '交互记录待归档' : `有交互记录 ${number(run.trace_cases)} 题`}` : ''} <span>·</span> ${isRunning(run) ? '后台预测中，页面自动更新' : run.status === 'awaiting_settlement' ? '预测已锁定，等待完整目标日行情；更新结算不会重新预测' : `更新于 ${stamp(run.finished_at || run.updated_at)}`}</p></section>`;
  }
  function valuesFor(item,key) {
    if (item[key] && typeof item[key] === 'object') return item[key];
    return Object.fromEntries(['direction','return_pct','high','low'].map(field => [field,item[`${key}_${field}`]]));
  }
  function fourValuesTable(predicted,actual,errors,correct,unit) {
    const rows = [
      ['收盘涨跌方向',direction(predicted?.direction),direction(actual?.direction),correct === true ? '正确' : correct === false ? '错误' : '等待结算'],
      ['收盘涨跌幅',change(predicted?.return_pct),change(actual?.return_pct),errors?.return_pct == null ? '—' : `${numeric(errors.return_pct,4)} 个百分点`],
      ['日内最高价',numeric(predicted?.high),numeric(actual?.high),errors?.high == null ? '—' : `${numeric(errors.high)} ${esc(unit)}`],
      ['日内最低价',numeric(predicted?.low),numeric(actual?.low),errors?.low == null ? '—' : `${numeric(errors.low)} ${esc(unit)}`],
    ];
    return `<div class="research-table-scroll"><table class="research-table research-answer-comparison"><thead><tr><th>预测项</th><th>模型预测</th><th>真实结果</th><th>评估</th></tr></thead><tbody>${rows.map(row => `<tr>${row.map(value => `<td>${value}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
  }

  const traceKey = (runId,caseId) => `${runId}:${caseId}`;
  const traceUrl = (runId,caseId) => `/api/evaluations/${encodeURIComponent(runId)}/cases/${encodeURIComponent(caseId)}/trace`;
  const jsonText = value => typeof value === 'string' ? value : JSON.stringify(value ?? null,null,2);
  const tracePre = (value,scrollKey = '') => `<pre${scrollKey ? ` data-trace-scroll-key="${esc(scrollKey)}"` : ''}>${esc(jsonText(value))}</pre>`;

  function analysisItems(value) {
    if (value == null || value === '') return [];
    return Array.isArray(value) ? value : [value];
  }
  function analysisText(value) {
    if (typeof value === 'string') return value;
    if (value && typeof value === 'object') {
      return value.text || value.summary || value.reason || value.title || JSON.stringify(value);
    }
    return String(value ?? '');
  }
  function analysisList(title, value, key) {
    const items = analysisItems(value).map(analysisText).filter(Boolean);
    if (!items.length) return '';
    return `<section class="research-public-analysis-block"><h4>${esc(title)}</h4><ol>${items.map((item,index) => `<li><span>${String(index+1).padStart(2,'0')}</span><p>${esc(item)}</p></li>`).join('')}</ol></section>`;
  }
  function publicAnalysisMarkup(trace, key) {
    const events = Array.isArray(trace?.conversation) ? trace.conversation : [];
    let analysis = trace?.public_analysis || trace?.public_analysis_log || trace?.decision_log;
    if (!analysis) {
      const eventItems = events.filter(event => ['public_analysis','analysis_step','decision_log'].includes(event.type));
      if (eventItems.length) analysis = {steps:eventItems.map(event => event.text || event.result || event)};
    }
    if (!analysis) return '';
    if (typeof analysis === 'string') analysis = {summary:analysis};
    const document = analysis.analysis_document || analysis.decision_document || analysis.long_analysis || analysis.reasoning_report;
    const summary = analysis.summary || analysis.decision_summary || analysis.conclusion || analysis.rationale;
    const steps = analysis.steps || analysis.analysis_steps || analysis.reasoning_steps || analysis.process;
    const supporting = analysis.supporting_evidence || analysis.supporting || analysis.evidence_for;
    const contrary = analysis.contrary_evidence || analysis.counter_evidence || analysis.evidence_against;
    const uncertainty = analysis.uncertainty || analysis.risks || analysis.caveats;
    const mapping = analysis.forecast_mapping || analysis.prediction_link || analysis.forecast_reason;
    const body = `${document ? `<article class="research-public-analysis-document"><header><strong>长篇决策分析文档</strong><span>模型实际输出</span></header><p>${esc(analysisText(document))}</p></article>` : ''}${summary ? `<div class="research-public-analysis-summary"><strong>决策摘要</strong><p>${esc(analysisText(summary))}</p></div>` : ''}${analysisList('分析步骤',steps,key+':steps')}${analysisList('支持证据',supporting,key+':supporting')}${analysisList('反方证据与冲突信息',contrary,key+':contrary')}${analysisList('不确定性与风险',uncertainty,key+':uncertainty')}${mapping ? `<div class="research-public-analysis-mapping"><strong>从证据到预测</strong><p>${esc(analysisText(mapping))}</p></div>` : ''}`;
    if (!body) return '';
    return `<section class="research-public-analysis"><header><div><span class="research-public-analysis-kicker">PUBLIC DECISION DOCUMENT</span><h3>模型长篇分析与决策证据链</h3></div><span class="research-public-analysis-badge">可见输出</span></header><p class="research-public-analysis-note">这里展示模型实际输出的长篇分析文档、证据和不确定性；平台隐藏的内部思维链不会显示或补写。</p>${body}</section>`;
  }

  function traceEventMarkup(event,index,prefix) {
    const scrollKey = `${prefix}:event:${index}:${event.event_id || 'unidentified'}:${event.type || 'event'}`;
    const titles = {question_context:'题目与预测日期',user_message:'模型初始输入',visible_reasoning_summary:'可见推理摘要',assistant_message:'模型公开消息',public_analysis:'模型公开分析',analysis_step:'公开分析步骤',decision_log:'公开决策日志',tool_call:'调用环境搜索',tool_result:'环境返回给模型的内容',final_answer:'模型最终答案'};
    const title = titles[event.type] || '公开交互事件';
    let body;
    if (event.type === 'tool_call') body = `<p class="research-trace-scope">${esc(event.server || 'env01')} · ${esc(event.tool || 'search_news')} · ${event.tool === 'search_indicators' ? '模型提交的技术指标查询参数' : '模型提交的参数'}</p>${tracePre(event.arguments ?? event.text ?? {},scrollKey)}`;
    else if (event.type === 'tool_result') body = `<p class="research-trace-scope">模型可见 · 以下为本次实际返回的${event.tool === 'search_indicators' ? '技术指标内容' : '新闻内容'}及范围标记</p>${tracePre(event.result ?? event.text ?? {},scrollKey)}`;
    else body = tracePre(event.text ?? event.answer ?? event.result ?? event,scrollKey);
    return `<article class="research-trace-event research-trace-${esc(event.type || 'event')}"><header><span>${String(index+1).padStart(2,'0')}</span><h4>${title}</h4></header>${body}</article>`;
  }

  function traceBody(runId,caseId) {
    const key = traceKey(runId,caseId),trace = traceCache.get(key);
    if (tracePending.has(key)) return '<p class="research-muted" role="status">正在读取本题交互记录…</p>';
    if (traceErrors.has(key)) return `<p class="research-error-text" role="alert">${esc(traceErrors.get(key))}</p><button type="button" class="research-link" data-load-trace="${esc(caseId)}" data-trace-run="${esc(runId)}">重新读取</button>`;
    if (!trace) return '<p class="research-muted">展开后读取本题记录。</p>';
    const events = Array.isArray(trace.conversation) ? trace.conversation : [];
    const noTools = isNoToolsRun(trace) || isNoToolsRun((data?.runs || []).find(run => run.run_id === runId));
    const calls = Array.isArray(trace.search_calls) ? trace.search_calls : [];
    const indicatorCalls = Array.isArray(trace.indicator_calls) ? trace.indicator_calls : [];
    const summaries = events.filter(event => event.type === 'visible_reasoning_summary');
    const summaryNote = summaries.length ? `已返回 ${number(summaries.length)} 段可见推理摘要。` : '本题未返回可见推理摘要；没有生成或补写思考内容。';
    const validation = Array.isArray(trace.validation_errors) && trace.validation_errors.length ? `<div class="research-inline-error"><strong>交互校验未通过</strong>${trace.validation_errors.map(message => `<p>${esc(message)}</p>`).join('')}</div>` : '';
    const audit = calls.length ? `<details class="research-trace-audit" data-case-key="${esc(key)}:audit"><summary>time gate 宿主审核摘要 · ${number(calls.length)} 次搜索</summary><p class="research-trace-scope">宿主审核记录，单独列示；仅“环境返回给模型的内容”代表模型实际看到的新闻。</p>${calls.map((call,index) => `<article class="research-trace-receipt"><h4>搜索 ${index+1} · ${esc(call.query ?? call.keywords ?? '')}</h4>${call.time_gate && callCount(call.time_gate.checked) !== null ? `<p class="research-trace-scope">time gate 检查 ${number(call.time_gate.checked)} 条 · 放行 ${number(call.time_gate.accepted)} 条 · 拒绝 ${number(call.time_gate.rejected)} 条 · 实际返回 ${number(call.returned_count)} 条</p>` : ''}${tracePre({call_id:call.call_id,query:call.query,status:call.status,returned_count:call.returned_count,time_gate:call.time_gate ?? null,response_sha256:call.response_sha256,receipt_sha256:call.receipt_sha256},`${key}:audit:${call.call_id || index}`)}</article>`).join('')}</details>` : '';
    const indicatorAudit = indicatorCalls.length ? `<details class="research-trace-audit" data-case-key="${esc(key)}:indicator-audit"><summary>time gate 技术指标审核摘要 · ${number(indicatorCalls.length)} 次检索</summary><p class="research-trace-scope">以下摘要对应 search_indicators 的实际回执；完整指标返回内容位于上方“环境返回给模型的内容”事件。</p>${indicatorCalls.map((call,index) => `<article class="research-trace-receipt"><h4>指标检索 ${index+1} · ${esc(call.indicator || '')}</h4><p class="research-trace-scope">${call.start_time ? stamp(call.start_time) : esc(call.start_date || '')} 至 ${call.end_time || call.response?.end_time ? stamp(call.end_time || call.response.end_time) : esc(call.end_date || '')}${call.cutoff_utc || call.time_gate?.cutoff_utc ? ` · 截止 ${stamp(call.cutoff_utc || call.time_gate.cutoff_utc)}` : ''} · 检查 ${number(call.time_gate?.checked)} 条 · 放行 ${number(call.time_gate?.accepted)} 条 · 拒绝 ${number(call.time_gate?.rejected)} 条 · 返回 ${number(call.returned_count)} 条</p>${tracePre({call_id:call.call_id,indicator:call.indicator,start_time:call.start_time,end_time:call.end_time,cutoff_utc:call.cutoff_utc,start_date:call.start_date,end_date:call.end_date,status:call.status,returned_count:call.returned_count,time_gate:call.time_gate ?? null,response_sha256:call.response_sha256,receipt_sha256:call.receipt_sha256},`${key}:indicator-audit:${call.call_id || index}`)}</article>`).join('')}</details>` : '';
    return `<div class="research-trace-notice"><strong>交互记录与长篇决策文档</strong><p>${noTools ? '保存题目与已知行情、预测日期、模型公开分析文档、最终答案及无工具权限校验记录。' : '保存题目与预测日期、工具调用、实际召回的新闻和技术指标、模型公开分析文档及最终答案。'}平台隐藏的内部思维链无法导出。${summaryNote}</p></div>${publicAnalysisMarkup(trace,key)}${validation}${events.length ? `<div class="research-trace-events">${events.map((event,index) => traceEventMarkup(event,index,key)).join('')}</div>` : '<p class="research-muted">当前没有可读取的公开交互事件。</p>'}${audit}${indicatorAudit}${trace.final_answer ? `<article class="research-trace-final"><h4>四项结构化预测</h4>${tracePre(trace.final_answer,`${key}:final`)}</article>` : '<p class="research-muted">最终四项预测尚未归档。</p>'}<div class="research-trace-actions"><a class="research-link" href="${traceUrl(runId,caseId)}?download=1" download>下载本题交互记录 JSON</a><button type="button" class="research-link" data-load-trace="${esc(caseId)}" data-trace-run="${esc(runId)}">重新读取记录</button></div>`;
  }

  function traceMarkup(run,item) {
    if (!isSearchRun(run) && !item.conversation_available) return '';
    const key = traceKey(run.run_id,item.case_id),expanded = traceExpanded.has(key);
    const count = callCount(item.search_calls);
    const indicatorCount = callCount(item.indicator_calls);
    return `<div class="research-trace"><div class="research-trace-toolbar"><span>${isNoToolsRun(run) ? '无搜索与工具接口' : `${count === null ? '主动搜索次数待归档' : `主动搜索 ${number(count)} 次`}${indicatorCount === null ? '' : ` · 技术指标 ${number(indicatorCount)} 次`}`} ${item.visible_reasoning_summary_count ? ` · 可见摘要 ${number(item.visible_reasoning_summary_count)} 段` : ''}</span><button type="button" class="button secondary" data-toggle-trace="${esc(item.case_id)}" data-trace-run="${esc(run.run_id)}" aria-expanded="${expanded}">${expanded ? '收起交互轨迹' : '查看交互轨迹'}</button></div>${expanded ? `<div class="research-trace-body">${traceBody(run.run_id,item.case_id)}</div>` : ''}</div>`;
  }

  async function loadTrace(runId,caseId,force = false) {
    const key = traceKey(runId,caseId);
    if (tracePending.has(key) || (!force && traceCache.has(key))) return;
    tracePending.add(key);traceErrors.delete(key);render();
    try {traceCache.set(key,await options.api(traceUrl(runId,caseId)));}
    catch (caught) {traceErrors.set(key,caught.message || '本题交互记录读取失败');}
    finally {tracePending.delete(key);if (active && view?.section === 'leaderboard') render();}
  }

  function runDetails(run) {
    const cases = Array.isArray(run.cases) ? run.cases : Object.values(run.cases || {});
    let body;
    if (!cases.length) body = `<div class="research-note">${runPending.has(run.run_id) ? '正在读取逐题记录…' : '尚无逐题输出。'}</div>`;
    else body = `<div class="research-case-list">${cases.map(item => {
      const unit = item.unit || (item.family === 'SC' ? '元 / 桶' : '美元 / 桶');
      const predicted = valuesFor(item,'predicted'),actual = valuesFor(item,'actual');
      const comparison = isLegacy(run) ? `<div class="research-case-values"><div><span>v1 预测收盘价</span><strong>${numeric(item.predicted_price)}</strong></div><div><span>真实收盘价</span><strong>${numeric(item.actual_price)}</strong></div><div><span>绝对误差</span><strong>${numeric(item.absolute_error)}</strong></div><div><span>放行新闻</span><strong>${number(item.news_count)}</strong></div></div>` : fourValuesTable(predicted,actual,item.absolute_errors || item.errors,item.direction_correct,unit);
      return `<details class="research-case" data-case-key="${esc(item.case_id)}"><summary><span class="research-case-name">${esc(item.family || item.contract_code || item.case_id)}${isLegacy(run) ? ' · v1 收盘价' : ' 主连'}<small>目标交易日 ${date(item.actual_target_date || actual.target_date || item.target_date)}</small>${timeWindowsMarkup({...item,environment_id:run.environment_id},{summary:true})}</span>${status(item.status)}<span class="research-case-error">${isLegacy(run) ? `AE ${numeric(item.absolute_error)}` : item.direction_correct === true ? '方向正确' : item.direction_correct === false ? '方向错误' : '未结算'}</span></summary><div class="research-case-content">${timeWindowsMarkup({...item,environment_id:run.environment_id},{legacy:isLegacy(run)})}${questionTextMarkup(item)}${comparison}${!isLegacy(run) ? `<p class="research-muted">${isNoToolsRun(run) ? '无搜索与工具调用' : `${isSearchRun(run) ? '搜索返回去重新闻' : '放行新闻'} ${number(item.news_count)} 条`} · ${esc(unit)}${item.locked_at || item.submitted_at ? ` · 预测锁定于 ${stamp(item.locked_at || item.submitted_at)}` : ''}</p>` : ''}${traceMarkup(run,item)}${item.raw_output !== undefined ? `<details class="research-raw-output" data-case-key="${esc(item.case_id)}:raw"><summary>原始模型输出</summary><pre>${esc(typeof item.raw_output === 'string' ? item.raw_output : JSON.stringify(item.raw_output,null,2))}</pre></details>` : ''}${item.error ? `<p class="research-error-text">${esc(typeof item.error === 'string' ? item.error : item.error.message || '模型输出未通过校验')}</p>` : ''}</div></details>`;
    }).join('')}</div>`;
    return panel(isLegacy(run) ? '旧版逐题记录 · v1' : '逐题预测与结算',`运行 ${esc(shortId(run.run_id))} · ${isLegacy(run) ? '原收盘价结果保留，不混入新版四项评分。' : '方向按收盘结算，涨跌幅误差以百分点计。'}`,body);
  }

  const memoryMonths = Array.from({length:10},(_,index) => `2026-${String(index+1).padStart(2,'0')}`);
  const memoryExperiments = () => Array.isArray(data?.memory_benchmark?.experiments) ? data.memory_benchmark.experiments : [];
  const memoryRuns = () => Array.isArray(data?.memory_benchmark?.runs) ? data.memory_benchmark.runs : [];
  const memoryRunSignature = run => [run?.run_id,run?.status,run?.resolved,run?.graded,run?.completed,run?.correct,run?.failed,run?.pending,run?.updated_at,run?.finished_at].join(':');
  const memoryRunStatus = run => {
    const value = run?.status || 'pending';
    const labels = {pending:'待完成',prepared:'待完成',queued:'待完成',running:'进行中',completed:'已完成',partial:'待完成',failed:'已结束',interrupted:'待完成',cancelled:'已结束'};
    return `<span class="research-status ${esc(value)}">${esc(labels[value] || value)}</span>`;
  };
  function memoryExpectedModels(experiment) {
    const configured = experiment?.models;
    const list = Array.isArray(configured) ? configured : configured && typeof configured === 'object' ? Object.keys(configured) : data?.memory_benchmark?.models || [];
    return [...new Set(list.map(item => typeof item === 'string' ? item : item?.id || item?.model || item?.name).filter(Boolean))];
  }
  function memoryMetric(run,month = '') {
    if (!month) return run?.metrics?.overall || run?.overall || {};
    const months = run?.metrics?.months || run?.months || [];
    return Array.isArray(months) ? months.find(item => item.month === month) || {} : months[month] || {};
  }
  function memoryAccuracy(metric) {
    const total = Number(metric?.total),correct = Number(metric?.correct);
    if (total > 0 && metric?.correct != null && Number.isFinite(correct) && correct >= 0 && correct <= total) return correct / total;
    const value = metric?.accuracy;
    if (value != null && Number.isFinite(Number(value)) && Number(value) >= 0 && Number(value) <= 1) return Number(value);
    return Number(metric?.total) > 0 ? (Number(metric.correct) || 0) / Number(metric.total) : null;
  }
  function memoryResolved(metric) {
    const count = metric?.resolved ?? metric?.completed ?? ((Number(metric?.graded) || 0) + (Number(metric?.failed) || 0));
    return Math.max(0,Number(count) || 0);
  }
  function memoryWrong(metric) {
    return Math.max(0,Number(metric?.wrong ?? metric?.incorrect ?? (memoryResolved(metric)-(Number(metric?.correct) || 0))) || 0);
  }
  function memoryCoverage(metric,total = 100) {
    const count = Number(metric?.total) || total,resolved = memoryResolved(metric);
    const pending = metric?.pending == null ? Math.max(0,count-resolved) : Number(metric.pending) || 0;
    return `已完成 ${number(resolved)} / ${number(count)} · 待完成 ${number(pending)}`;
  }
  function memoryCohort() {
    const experiments = [...memoryExperiments()].sort((a,b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
    const anchor = experiments[0] || [...memoryRuns()].sort((a,b) => String(b.created_at || b.started_at || '').localeCompare(String(a.created_at || a.started_at || '')))[0];
    if (!anchor) return {anchor:null,models:[],latest:new Map()};
    const runs = memoryRuns().filter(run => (!anchor.experiment_id || run.experiment_id === anchor.experiment_id) && (!anchor.dataset_id || run.dataset_id === anchor.dataset_id));
    const latest = new Map();
    [...runs].sort((a,b) => String(b.created_at || b.started_at || '').localeCompare(String(a.created_at || a.started_at || ''))).forEach(run => {
      const model = normalizeModelId(run);if (model && !latest.has(model)) latest.set(model,run);
    });
    const models = [...new Set([...memoryExpectedModels(anchor),...latest.keys()])];
    return {anchor,models,latest};
  }
  function memoryMetricCell(run,month = '',tag = 'td') {
    const metric = memoryMetric(run,month),value = memoryAccuracy(metric),complete = metric.complete === true;
    return `<${tag} data-memory-month-metric="${esc(month || 'overall')}" data-memory-accuracy="${value ?? ''}" class="memory-accuracy-cell${value !== null && !complete ? ' is-provisional' : ''}" title="${esc(memoryCoverage(metric,month ? 10 : 100))}"><strong>${percent(value)}</strong><small>完成 ${number(memoryResolved(metric))} / ${number(metric.total || (month ? 10 : 100))}${value !== null && !complete ? ' · 进行中' : ''}</small></${tag}>`;
  }
  function memoryMonthlyTable(rows,{modelPage = false} = {}) {
    return `<div class="research-table-scroll" tabindex="0" role="region" aria-label="记忆泄漏各模型分月与总正确率，可横向滚动"><table class="research-table memory-monthly-table"><thead><tr><th scope="col">模型(实验)</th>${memoryMonths.map(month => `<th scope="col">${Number(month.slice(-2))} 月</th>`).join('')}<th scope="col">总正确率</th><th scope="col">状态 / 进度</th><th scope="col"></th></tr></thead><tbody>${rows.map(({model,run}) => `<tr data-memory-model="${esc(model)}"${run ? ` data-memory-run="${esc(run.run_id)}"` : ''}><th scope="row"><span>${esc(modelLabel(model,run))}</span>${modelPage && run?.dataset_id ? `<small title="${esc(run.dataset_id)}">${esc(shortId(run.dataset_id))}</small>` : ''}</th>${memoryMonths.map(month => memoryMetricCell(run,month)).join('')}${memoryMetricCell(run)}<td>${memoryRunStatus(run)}<small>${esc(memoryCoverage(memoryMetric(run)))}</small></td><td>${run ? `<button type="button" class="research-link" data-open-memory-run="${esc(run.run_id)}" data-memory-model="${esc(model)}">查看逐题</button>` : '<span class="research-muted">尚无运行</span>'}</td></tr>`).join('')}</tbody></table></div>`;
  }
  function memoryOverview() {
    const {anchor,models,latest} = memoryCohort();
    if (!anchor) return '';
    const ranked = models.map(model => ({model,run:latest.get(model)})).sort((a,b) => (memoryAccuracy(memoryMetric(b.run)) ?? -1) - (memoryAccuracy(memoryMetric(a.run)) ?? -1) || a.model.localeCompare(b.model));
    const chart = `<section class="summary-chart memory-summary-chart" aria-label="记忆泄漏总正确率排名"><div class="summary-chart-scale" aria-hidden="true"><span>0%</span><span>50%</span><span>100%</span></div><ol class="summary-rank-list">${ranked.map(({model,run}) => {
      const metric = memoryMetric(run),accuracy = memoryAccuracy(metric),provisional = accuracy !== null && metric.complete !== true;
      return `<li class="summary-rank-row${provisional ? ' is-provisional' : ''}" data-memory-rank-model="${esc(model)}" data-memory-rank-accuracy="${accuracy ?? ''}"><button type="button" class="summary-rank-button" ${run ? `data-open-memory-run="${esc(run.run_id)}" data-memory-model="${esc(model)}"` : 'disabled'}><span class="summary-rank-label"><span>${esc(modelLabel(model,run))}${provisional ? '<em>进行中</em>' : ''}</span><strong>${percent(accuracy)}</strong></span><span class="summary-bar-track" aria-hidden="true"><span class="summary-bar-fill" style="width:${accuracy === null ? 0 : accuracy * 100}%"></span></span><small>${esc(memoryCoverage(metric))}</small></button></li>`;
    }).join('')}</ol></section>`;
    const heading = `<div class="memory-section-intro"><h2>记忆泄漏 · ${esc(anchor.experiment_name || anchor.experiment_id || '闭卷实验')}</h2><p>env00-extend · 正确记1，其余记0；总分固定100，分月10。正确率按固定题数计算，完成进度另列。</p><small>${esc(anchor.dataset_id || '')}</small></div>`;
    return `<section class="memory-results-overview" data-memory-overview>${heading}${panel('总正确率排名','2026 年 1—10 月 · 闭卷事实填空',chart)}${panel('分月与总正确率','每月 10 题；单元格同时显示完成进度。',memoryMonthlyTable(models.map(model => ({model,run:latest.get(model)}))))}</section>`;
  }
  function memoryExperimentSetup() {
    const experiments = [...memoryExperiments()].sort((a,b) => String(b.created_at || '').localeCompare(String(a.created_at || '')));
    if (!experiments.length) return '';
    return `<section class="memory-experiment-setup"><div class="memory-section-intro"><h2>记忆泄漏实验</h2><p>闭卷事实问答 · env00-extend</p></div><div class="experiment-setup-grid">${experiments.map(item => {
      const models = memoryExpectedModels(item),runs = memoryRuns().filter(run => run.experiment_id === item.experiment_id && run.dataset_id === item.dataset_id);
      const total = Number(item.expected_total || item.total) || models.length*100,resolved = memoryResolved(item);
      const fields = [['环境','env00-extend · 题干闭卷作答'],['Benchmark',`${number(item.total_per_model || 100)} 题 / 模型 · 2026 年 1—10 月每月 10 题`],['信息权限','仅题干；新闻、技术指标、联网及其他工具全部关闭'],['时间门控','不调用检索接口；任何取证或工具调用均被拒绝并留存'],['独立评估','Codex CLI evaluator 读取参考答案与模型答案，逐题判断语义是否正确'],['评分规则','正确记1，其余记0；总分固定100，分月10'],['冻结题库',item.dataset_id || '未记录'],['评估协议',item.protocol || 'env00-extend-memory-closed-book-v1']];
      const rows = models.map(model => {
        const run = runs.find(candidate => normalizeModelId(candidate) === model),metric = memoryMetric(run);
        return `<tr><th scope="row">${esc(model)}</th><td>${number(memoryResolved(metric))} / ${number(metric.total || 100)}</td><td>${percent(memoryAccuracy(metric))}${memoryAccuracy(metric) !== null && !metric.complete ? '<small>进行中</small>' : ''}</td><td>${number(memoryWrong(metric))}</td><td>${memoryRunStatus(run)}</td><td>${run ? `<button type="button" class="research-link" data-open-memory-run="${esc(run.run_id)}" data-memory-model="${esc(model)}">查看逐题</button>` : '—'}</td></tr>`;
      }).join('');
      return `<article class="research-panel experiment-card memory-experiment-card" data-memory-experiment-id="${esc(item.experiment_id || item.batch_id)}"><header class="research-panel-heading"><div><h2>${esc(item.experiment_name || item.experiment_id)}</h2><p>${stamp(item.created_at)}</p></div><div class="experiment-card-progress">${memoryRunStatus(item)}<span>已完成 ${number(resolved)} / ${number(total)} 题 · ${models.length} 个模型</span></div></header><dl class="experiment-settings">${fields.map(([label,value]) => `<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl><div class="research-table-scroll"><table class="research-table memory-experiment-progress"><thead><tr><th>模型</th><th>已完成</th><th>正确率</th><th>错误</th><th>状态</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></article>`;
    }).join('')}</div></section>`;
  }
  function memoryCurrentSummary() {
    const model = selectedModelId();
    if (!model) return null;
    const runs = memoryRuns().filter(run => normalizeModelId(run) === model);
    return runs.find(run => run.run_id === selectedMemoryRun) || [...runs].sort((a,b) => String(b.created_at || b.started_at || '').localeCompare(String(a.created_at || a.started_at || '')))[0] || null;
  }
  async function loadMemoryRun(force = false) {
    const run = memoryCurrentSummary();
    if (!run || memoryRunPending.has(run.run_id)) return;
    const signature = memoryRunSignature(run),cached = memoryRunCache.get(run.run_id);
    if (!force && cached?.signature === signature && (!isRunning(run) || Date.now()-cached.at < 7000)) return;
    const id = run.run_id;memoryRunPending.add(id);memoryRunErrors.delete(id);
    try {
      const value = await options.api(`/api/evaluations/${encodeURIComponent(id)}`);
      memoryRunCache.set(id,{signature,value,at:Date.now()});
      // An open trace must follow newly saved answers and independent verdicts.
      const visibleCases = new Set([...(root?.querySelectorAll('[data-memory-result-case]') || [])].map(element => element.dataset.memoryResultCase));
      for (const key of traceCache.keys()) if (key.startsWith(`${id}:`)) {
        traceCache.delete(key);
        if (traceExpanded.has(key)) {
          const caseId = key.slice(id.length+1);
          if (visibleCases.has(caseId)) loadTrace(id,caseId,true);
          else traceExpanded.delete(key);
        }
      }
    }
    catch (caught) {memoryRunErrors.set(id,caught.message || '记忆泄漏逐题结果读取失败');}
    finally {memoryRunPending.delete(id);if (active && view?.section === 'leaderboard') render();}
  }
  function memoryModelRecords() {
    const model = selectedModelId();
    const runs = memoryRuns().filter(run => normalizeModelId(run) === model).sort((a,b) => String(b.created_at || b.started_at || '').localeCompare(String(a.created_at || a.started_at || '')));
    if (!runs.length) return '';
    const summary = memoryCurrentSummary(),cached = memoryRunCache.get(summary?.run_id)?.value;
    const run = cached ? {...cached,...summary,cases:cached.cases || []} : summary;
    return `<section class="memory-model-records">${panel('记忆泄漏实验记录','闭卷填空正确率；每月 10 题，共 100 题。',memoryMonthlyTable(runs.map(item => ({model,run:item})),{modelPage:true}))}${run ? memoryRunDetails(run) : ''}</section>`;
  }
  function memoryCaseVerdict(item) {
    const correct = typeof item.correct === 'boolean' ? item.correct : item.evaluator?.correct;
    if (item.outcome === 'correct' || item.status === 'graded' && correct === true) return {label:'正确',className:'memory-correct'};
    if (item.outcome === 'incorrect' || ['graded','prediction_failed','judge_failed','tool_violation'].includes(item.status)) return {label:'错误',className:'memory-incorrect'};
    return {label:'待完成',className:'memory-ungraded'};
  }
  function memoryTraceBody(runId,caseId) {
    const key = traceKey(runId,caseId);
    if (traceErrors.has(key)) return `<p class="research-error-text">${esc(traceErrors.get(key))}</p><button type="button" class="research-link" data-load-trace="${esc(caseId)}" data-trace-run="${esc(runId)}">重新读取记录</button>`;
    if (!traceCache.has(key)) return '<p class="research-muted">正在读取作答与独立评估记录…</p>';
    return `<p class="research-muted">保存模型原始输出、工具权限审计与独立 evaluator 的判定；模型作答时不提供参考答案。</p>${tracePre(traceCache.get(key),`${key}:memory`)}<a class="research-link" href="${traceUrl(runId,caseId)}?download=1" download>下载本题记录 JSON</a>`;
  }
  function memoryRunDetails(run) {
    const id = run.run_id,metric = memoryMetric(run);
    const cached = memoryRunCache.has(id),cases = Array.isArray(run.cases) ? run.cases : Object.values(run.cases || {});
    let body;
    if (memoryRunErrors.has(id)) body = `<div class="research-note research-error-text">${esc(memoryRunErrors.get(id))}<button type="button" class="research-link" data-research-action="retry-memory-run">重新读取</button></div>`;
    else if (!cached) body = '<div class="loading-state"><div class="spinner"></div>正在读取 100 道逐题作答与评估结果</div>';
    else {
      const selected = memoryResultMonth ? cases.filter(item => item.month === memoryResultMonth) : cases;
      const pages = Math.max(1,Math.ceil(selected.length/10));memoryResultPage = Math.max(1,Math.min(memoryResultPage,pages));
      const rows = selected.slice((memoryResultPage-1)*10,memoryResultPage*10).map(item => {
        const verdict = memoryCaseVerdict(item),key = traceKey(id,item.case_id),expanded = traceExpanded.has(key);
        const modelAnswer = item.model_answer;
        const reason = verdict.className === 'memory-ungraded' ? '' : item.status === 'graded' ? item.reason || item.evaluator?.reason || '' : '未给出正确答案。';
        return `<tr data-memory-result-case="${esc(item.case_id)}"><td><strong>${esc(item.month)}</strong><small>${esc(item.case_id)}</small></td><td class="memory-result-question"><p>${esc(item.question || '')}</p><div><span>参考答案</span><strong>${esc(item.reference_answer ?? item.answer ?? '—')}</strong></div></td><td class="memory-result-answer">${modelAnswer == null || modelAnswer === '' ? '<span class="research-muted">—</span>' : esc(modelAnswer)}</td><td class="memory-result-reason"><span class="memory-verdict ${verdict.className}">${verdict.label}</span>${reason ? `<p>${esc(reason)}</p>` : ''}</td><td><button type="button" class="research-link" data-toggle-trace="${esc(item.case_id)}" data-trace-run="${esc(id)}" aria-expanded="${expanded}">${expanded ? '收起记录' : '查看记录'}</button><a class="research-link" href="${traceUrl(id,item.case_id)}?download=1" download>JSON</a></td></tr>${expanded ? `<tr class="memory-result-trace-row"><td colspan="5"><div class="research-trace memory-result-trace"><div class="research-trace-body">${memoryTraceBody(id,item.case_id)}</div></div></td></tr>` : ''}`;
      }).join('');
      const controls = `<div class="memory-result-controls"><span>${esc(memoryCoverage(metric))}</span><label class="memory-month-select">月份<select id="memory-result-month-filter" aria-label="筛选记忆泄漏评估月份"><option value="">全部月份 · ${cases.length} 题</option>${memoryMonths.map(month => `<option value="${month}" ${memoryResultMonth === month ? 'selected' : ''}>${Number(month.slice(-2))} 月 · ${cases.filter(item => item.month === month).length} 题</option>`).join('')}</select></label></div>`;
      const table = `<div class="research-table-scroll"><table class="research-table memory-results-table"><thead><tr><th>月份 / 题号</th><th>题目 / 参考答案</th><th>模型答案</th><th>判定 / 评估理由</th><th>作答与评估记录</th></tr></thead><tbody>${rows}</tbody></table></div>`;
      const pagination = `<div class="research-pagination"><span>${selected.length ? `显示 ${(memoryResultPage-1)*10+1}–${Math.min(memoryResultPage*10,selected.length)} / ${selected.length} 题` : '当前月份尚无逐题记录'}</span><div><button type="button" class="button secondary" data-research-action="memory-result-previous" ${memoryResultPage<=1 ? 'disabled' : ''}>上一页</button><span>${memoryResultPage} / ${pages}</span><button type="button" class="button secondary" data-research-action="memory-result-next" ${memoryResultPage>=pages ? 'disabled' : ''}>下一页</button></div></div>`;
      body = controls + table + pagination;
    }
    const subtitle = `${esc(modelLabel(normalizeModelId(run),run))} · ${esc(id)} · ${esc(memoryCoverage(metric))}`;
    return panel('记忆泄漏 · 逐题作答与评估',subtitle,body,`<span class="memory-run-accuracy">${percent(memoryAccuracy(metric))}${metric.complete ? '' : '<small>进行中</small>'}</span>`);
  }
  function memoryEnvironment() {
    const identity = `<section class="research-identity"><div class="environment-mark" aria-hidden="true">00+</div><div><div class="research-kicker">CLOSED-BOOK MEMORY EVALUATION</div><h2>env00-extend</h2><p>记忆泄漏 · 100 道金融事件填空题 · 独立会话</p></div><div class="research-tag-group"><span class="research-tag">仅题干</span><span class="research-tag">禁止全部检索与工具</span></div></section>`;
    const agents = `<div class="research-agent-grid"><article class="research-agent-card"><div class="research-agent-number">AGENT 01</div><h3>time gate agent</h3><p>采用闭卷权限门控，只允许题干进入模型。</p><ul><li>新闻、本地新闻、技术指标、联网搜索以及其他工具全部关闭。</li><li>参考答案、别名、原文证据、事件标题和新闻引用不进入作答提示。</li><li>每题使用独立会话；工具调用被拒绝并在审计记录中留存。</li></ul></article><article class="research-agent-card"><div class="research-agent-number">AGENT 02</div><h3>independent evaluator</h3><p>由独立 Codex CLI 会话评估模型答案，读取冻结参考答案与规范化别名。</p><ul><li>逐题判断事实含义是否正确，并保存判定理由。</li><li>正确记1，其余记0；总分固定100，分月10。</li><li>完成进度单独显示，已完成题目只标为正确或错误。</li></ul></article></div>`;
    const protocol = `<dl class="research-protocol">${[['实验','Exp00-extend'],['Benchmark','记忆泄漏 · 2026 年 1—10 月，每月 10 题，共 100 题'],['模型输入','仅题干；每题独立作答，不传递前题上下文'],['权限门控','新闻、技术指标、浏览器、联网及其他工具全部禁止'],['独立评估','Codex CLI evaluator 在模型完成作答后读取参考答案并评判'],['评分规则','正确记1，其余记0；总分固定100，分月10。总正确率 = 答对数 / 100，分月正确率 = 当月答对数 / 10；完成进度另列'],['可核对记录','冻结题目、参考答案、模型答案、正确/错误判定、评估理由、工具审计与原始输出']].map(([label,value])=>`<div><dt>${label}</dt><dd>${value}</dd></div>`).join('')}</dl>`;
    return toolbar('闭卷诊断模型是否掌握当次报道的历史事实；单题答对不独立证明训练数据泄漏。') + identity + agents + panel('环境协议','env00-extend · 独立闭卷作答与答案评估',protocol);
  }

  function technicalEnvironment() {
    const identity = `<section class="research-identity"><div class="environment-mark" aria-hidden="true">00</div><div><div class="research-kicker">EVALUATION ENVIRONMENT</div><h2>env00</h2><p>主连日频 · 仅技术指标 · 四项预测</p></div><div class="research-tag-group"><span class="research-tag">2 agents</span><span class="research-tag">禁止新闻搜索</span></div></section>`;
    const agents = `<div class="research-agent-grid"><article class="research-agent-card"><div class="research-agent-number">AGENT 01</div><h3>time gate agent</h3><p>检查每次技术指标查询的时间范围，再复核实际召回的记录。</p><ul><li>截止时间为预测交易时段开始前 30 分钟，统一 UTC+0，等于截止的记录可返回。</li><li>模型自主选择指标、开始时间和结束时间；省略结束时间则使用固定截止。</li><li>越界、时间不明或缺少时区的查询被拒绝，返回原因供模型修正。</li></ul></article><article class="research-agent-card"><div class="research-agent-number">AGENT 02</div><h3>evaluator agent</h3><p>提取并评估下一交易日的四项预测。</p><ul><li>方向：次日收盘相对当天收盘的方向正确率。</li><li>最高价、最低价：分别计算 MAE。</li><li>收盘涨跌幅：(次日收盘 / 当日收盘 − 1) × 100%，计算 MAE（百分点）。</li></ul></article></div>`;
    const protocol = `<dl class="research-protocol"><div><dt>唯一数据接口</dt><dd>env00.search_indicators</dd></div><div><dt>可用信息</dt><dd>题干已知日线，以及经 time gate 验证的冻结 K 线、量价、持仓等技术指标。</dd></div><div><dt>查询范围</dt><dd>2026-01-01 00:00 UTC+0 起，最晚到预测交易时段开始前 30 分钟。缺失指标明确返回不可用。</dd></div><div><dt>搜索权限</dt><dd>新闻、联网搜索和本地新闻搜索全部关闭；执行层不注册新闻工具，并拒绝越权调用。</dd></div><div><dt>分析与记录</dt><dd>模型自由撰写详细公开分析文档，保存题目、预测日期、全部技术指标查询及返回和四项最终答案。</dd></div><div><dt>实验</dt><dd>Exp00 · 与 Exp01 使用相同冻结题目、技术指标及模型参数；等待 Exp01 执行、补跑和审计结束后启动。</dd></div></dl>`;
    return toolbar('', '') + identity + agents + panel('技术指标获取接口与环境规则','',protocol);
  }

  function noToolsEnvironment() {
    const identity = `<section class="research-identity"><div class="environment-mark" aria-hidden="true">02</div><div><div class="research-kicker">EVALUATION ENVIRONMENT</div><h2>env02</h2><p>主连日频 · 仅题干与已知行情 · 四项预测</p></div><div class="research-tag-group"><span class="research-tag">1 agent</span><span class="research-tag">无搜索与工具接口</span></div></section>`;
    const agents = `<div class="research-agent-grid"><article class="research-agent-card"><div class="research-agent-number">AGENT 01</div><h3>evaluator agent</h3><p>模型独立完成预测后，提取四项答案并对照独立保存的真实结果评分。</p><ul><li>方向：次日收盘相对当天收盘的方向正确率。</li><li>最高价、最低价：分别计算 MAE，按主连原币种报告。</li><li>收盘涨跌幅：(次日收盘 / 当日收盘 − 1) × 100%，计算 MAE（百分点）。</li><li>实盘先锁定预测，真实行情完整后结算。</li></ul></article></div>`;
    const fields = [['环境协议','env02-no-tools-close-direction-return-range-v1'],['模型输入','仅题干、已知日线行情及已知／预测交易区间；每题独立会话'],['模型接口','无；不注册新闻、技术指标、联网搜索或任何其他工具'],['执行限制','关闭浏览器、文件、shell、应用、插件、外部记忆与多 Agent 工具；模型尝试调用工具时拒绝该次结果，不执行调用'],['环境 Agent','仅 evaluator agent；不配置检索时间审核 Agent'],['时间区间','已知与预测交易区间统一显示 UTC+0；无检索截止时间'],['可核对记录','实际模型输入、公开分析文档、最终四项预测、评分及无工具权限校验记录'],['比较范围','同模式、同环境协议、同数据版本、同品类；USD / CNY 分开报告']];
    const protocol = `<dl class="research-protocol">${fields.map(([label,value]) => `<div><dt>${label}</dt><dd>${esc(value)}</dd></div>`).join('')}</dl>`;
    return toolbar('模型仅依据题干与已知行情直接预测；环境不提供搜索或工具接口。') + identity + agents + panel('环境协议','真实答案由评估侧保存，不进入模型提示。',protocol);
  }

  function environment() {
    if (view.subsection === 'env00-extend') return memoryEnvironment();
    if (view.subsection === 'env00') return technicalEnvironment();
    if (view.subsection === 'env02') return noToolsEnvironment();
    if (view.subsection === 'env03' || view.subsection === 'env04') return skillEnvironment(view.subsection);
    const identity = `<section class="research-identity"><div class="environment-mark" aria-hidden="true">01</div><div><div class="research-kicker">EVALUATION ENVIRONMENT · V5</div><h2>env01</h2><p>主连日频 · 模型主动搜索 · 四项预测</p></div><div class="research-tag-group"><span class="research-tag">2 agents</span><span class="research-tag">新闻 + 技术指标</span><span class="research-tag">逐次 time gate</span></div></section>`;
    const steps = [['当天主连行情','输入已完成的日线 OHLC'],['模型选择查询','自主决定新闻关键词和指标时间段'],['time gate agent','提供预测交易时段开始前 30 分钟的 UTC 截止'],['env01 · search_news / search_indicators','按关键词、指标与精确时间检索，再审核召回时间'],['模型读取与继续搜索','越界返回原因，模型修正后重查'],['锁定四项预测','方向 / 涨跌幅 / 高点 / 低点'],['evaluation agent','目标日收盘后独立评分']];
    const flow = `<ol class="research-search-flow" aria-label="env01 主动搜索评估流程">${steps.map(([title,description],index) => `<li><span>${String(index+1).padStart(2,'0')}</span><strong>${title}</strong><small>${description}</small></li>`).join('')}</ol>`;
    const agents = `<div class="research-agent-grid"><article class="research-agent-card"><div class="research-agent-number">AGENT 01</div><h3>time gate agent</h3><p>模型每次调用 search_news 或 search_indicators，先提供统一截止时间，接口按时分筛选，再审核召回结果，最后向模型返回合格内容。</p><ul><li>关键词、指标和时间段由模型主动输入，只检索本轮冻结的本地档案。</li><li>页面统一用 UTC+0 显示已知与预测区间。新闻与技术指标的检索截止统一为预测交易时段开始前 30 分钟，含夜盘与跨日时段；等于截止的记录可以返回。</li><li>技术查询须指定指标与开始时分，可选结束时分；省略结束时间时使用统一截止。历史起点为 UTC+0 2026-01-01 00:00。</li><li>越界、缺少时分或无时区的查询整次拒绝，向模型返回原因与合法时间范围，允许修正后重新查询。</li><li>时间不明、晚于截止或审核排除的内容不放行，不进入模型返回。</li><li>调用参数、实际返回正文和 time gate 审核摘要分别归档，预测锁定后不重跑模型。</li></ul></article><article class="research-agent-card"><div class="research-agent-number">AGENT 02</div><h3>evaluation agent</h3><p>提取四项预测，读取独立保存的真实结果并评分。</p><ul><li>方向：比较下一交易日收盘价与当天收盘价，以正确率评价。</li><li>涨跌幅：(次日收盘 / 当日收盘 − 1) × 100%，MAE 单位为百分点。</li><li>高点 / 低点：下一交易日日内最高价、最低价，分别计算 MAE。</li><li>实盘没有完整真值时只保存预测，等待结算，不生成模拟分数。</li></ul></article></div>`;
    const protocol = `<dl class="research-protocol"><div><dt>环境版本</dt><dd>env01 v5 · UTC 时分检索 · 开始前 30 分钟截止</dd></div><div><dt>时间区间</dt><dd>每题分别列明“已知 YYYY-MM-DD HH:mm — YYYY-MM-DD HH:mm”和“预测 YYYY-MM-DD HH:mm — YYYY-MM-DD HH:mm”，均为 UTC+0；分段时段可展开查看，特殊时段未核实则明确留空。</dd></div><div><dt>行情依据</dt><dd>WTI / Brent / SC 主连，供应商真实日频序列；时段按各自交易所规则，Brent 主连与 NYMEX BZ 月份合约分别处理。</dd></div><div><dt>模型入口</dt><dd>Codex CLI / AIHub · 每题独立会话</dd></div><div><dt>信息获取</dt><dd>模型提交新闻关键词或指标时间段 → time gate 提供截止 → 本地按时间检索 → time gate 复核 → 返回合格内容；模型可继续查询</dd></div><div><dt>信息范围</dt><dd>两类检索只返回在预测交易时段开始前 30 分钟已可用的新闻和指标，精确到 UTC+0 时分；题干已知行情仍依 Benchmark 输入规则。</dd></div><div><dt>联网搜索</dt><dd><span class="research-status completed">关闭</span> · 仅开放环境本地新闻与指标工具</dd></div><div><dt>方向结算</dt><dd>次日收盘价相对当日收盘价：涨 / 跌 / 平</dd></div><div><dt>数值指标</dt><dd>涨跌幅 MAE（百分点）、最高价 MAE、最低价 MAE</dd></div><div><dt>实盘流程</dt><dd>先锁定预测，目标交易日行情完整后评分；休市等待下一实际交易日</dd></div><div><dt>比较范围</dt><dd>同模式、同环境协议、同数据版本、同品类；USD / CNY 分开报告</dd></div><div><dt>可见记录</dt><dd>题目与预测日期、查询参数、实际返回新闻和技术指标、长篇公开分析文档和最终答案；隐藏内部思考无法导出</dd></div><div><dt>历史运行</dt><dd>v2、v3 与 v4 历史结果保留原协议；v4 提前2小时，v5 提前30分钟，成绩按协议分别比较</dd></div></dl>`;
    const example = environmentExample ? panel('题目时间区间示例',`${esc(names[environmentExample.family] || environmentExample.family || '')} · 当前题库 · ${esc(environmentExample.case_id || '')}`,timeWindowsMarkup({...environmentExample,retrieval_time_policy:'forecast-start-minus-30m-utc-v1'})) : panel('题目时间区间示例','UTC+0',`<p class="research-muted benchmark-time-placeholder">${environmentExamplePending ? '正在读取当前题库的已知与预测区间…' : '当前没有可核对时分区间的题目；生成题库后在此显示实际区间。'}</p>`);
    return toolbar('回测与实盘均为日频；已知区间与预测区间统一显示 UTC+0。检索截止为预测交易时段开始前 30 分钟，由 time gate 提供并复核。') + identity + flow + example + agents + panel('环境协议','真实答案由评估侧保存，不进入模型提示。',protocol) + '<div class="research-note"><strong>历史新闻范围</strong><p>历史回填新闻按原始首发时间筛选；本轮冻结新闻库，搜索不额外访问外网。逐题记录保留实际返回范围与审核摘要，长篇公开分析文档以模型实际返回为准。</p></div>';
  }

  function skillEnvironment(id) {
    const skill = id === 'env03' ? 's01' : 's02';
    const title = id === 'env03' ? 's01 搜索策略' : 's02 RSI 搜索策略';
    const identity = `<section class="research-identity"><div class="environment-mark" aria-hidden="true">${id.slice(-2)}</div><div><div class="research-kicker">EVALUATION ENVIRONMENT · SKILL</div><h2>${id}</h2><p>主连日频 · ${title} · 四项预测</p></div><div class="research-tag-group"><span class="research-tag">2 agents</span><span class="research-tag">新闻 + 技术指标</span><span class="research-tag">冻结 skill</span></div></section>`;
    const protocol = `<dl class="research-protocol"><div><dt>环境协议</dt><dd>${id} · env01 v5 UTC+0 提前30分钟截止</dd></div><div><dt>Skill</dt><dd>${skill} · 每个运行冻结正文和 SHA-256；Skill 只约束搜索顺序与分析分支，不提供参考答案。</dd></div><div><dt>数据接口</dt><dd>${id}.search_news / ${id}.search_indicators · 仅本地冻结新闻和技术指标</dd></div><div><dt>检索流程</dt><dd>模型先读取 skill，根据当前K线、指标和搜索结果选择下一步关键词；time gate 逐次复核新闻与指标。</dd></div><div><dt>评估</dt><dd>方向按次日收盘判断；涨跌幅、高点、低点分别计算 MAE；记录保留公开分析文档和完整工具轨迹。</dd></div></dl>`;
    return toolbar(`${id} 在 env01 检索协议上加载 ${skill}，用于比较检索策略对方向和数值误差的影响。`) + identity + panel('环境协议','skill 搜索策略实验',protocol);
  }

  function actualFor(item) {
    if (item.actual && typeof item.actual === 'object') return item.actual;
    if (item.actual_direction != null) return {direction:item.actual_direction,return_pct:item.actual_return_pct,high:item.actual_high,low:item.actual_low};
    return null;
  }
  function forecastStateFor(item,run) {
    if (!run || !runCoversFamily(run,item.family)) return null;
    const retained = runCache.get(run.run_id)?.value;
    const cases = Array.isArray(retained?.cases) ? retained.cases : Object.values(retained?.cases || {});
    const ids = new Set([item.case_id,...(item.versions || []).filter(value => value.dataset_id === (run.source_dataset_id || run.dataset_id)).map(value => value.case_id)]);
    const matched = cases.find(value => ids.has(value.case_id));
    if (cases.length && !matched) return null;
    if (matched?.status === 'failed') return 'prediction_failed';
    if (matched?.predicted || ['success','predicted_pending'].includes(matched?.status)) return 'prediction_locked';
    const metric = getMetric(run,item.family);
    if (Number(metric?.count) > 0 || Number(metric?.pending) > 0) return 'prediction_locked';
    return isRunning(run) ? 'prediction_generating' : 'prediction_failed';
  }
  function liveReadiness(item,run) {
    let state = forecastStateFor(item,run) || item.live_status || item.forecast_status || 'waiting_new_completed_bar';
    if (state === 'ready' && Date.now() >= liveDeadline(item)) state = 'forecast_closed';
    return `<div class="benchmark-live-readiness" data-live-readiness-state="${esc(state)}">${status(state)}${state === 'waiting_new_completed_bar' ? '<small>等待收盘后的完整日线。</small>' : state === 'forecast_closed' ? '<small>本题未提交预测，已停止接收作答。</small>' : ''}</div>`;
  }
  function answerMarkup(item,unit,run) {
    const actual = actualFor(item);
    const state = forecastStateFor(item,run);
    if (!actual) return `<div class="benchmark-pending-answer">${status(state === 'prediction_generating' ? state : 'pending_result')}<small>真实结果尚未完整，不计分</small></div>`;
    return `<dl class="benchmark-four-answer ${directionTone(actual.direction)}"><div><dt>方向</dt><dd>${direction(actual.direction)}</dd></div><div><dt>涨跌幅</dt><dd>${change(actual.return_pct)}</dd></div><div><dt>最高价</dt><dd>${numeric(actual.high)}</dd></div><div><dt>最低价</dt><dd>${numeric(actual.low)}</dd></div></dl>${unit ? `<small class="research-muted">高 / 低：${esc(unit)}</small>` : ''}`;
  }
  function benchmark() {
    if (isMemoryBenchmark()) return memoryBenchmark();
    if (mode() === 'live') return liveBenchmark();
    const familyId = benchmarkFamily(),current = benchmarkData(),version = isBacktestV2() ? 'v2' : 'v1';
    const range = isBacktestV2() ? backtestV2DateRange(current) : null;
    const key = questionKey(),all = questionCache.get(key) || [],questions = filterBenchmarkQuestions(all);
    const pages = Math.max(1,Math.ceil(questions.length / 15));page = Math.max(1,Math.min(page,pages));
    const unitFor = item => item.unit || (item.family === 'SC' ? '元 / 桶' : '美元 / 桶');
    let body;
    if (!current.total_questions) body = empty(`尚未构建回测 ${version}`,range ? `沿用 v1 的出题口径；已知和预测交易日均须位于 ${range.start} 至 ${range.end}，按真实可核验日线确定题量。` : '每个主连品类构建最近 30 个已完成交易日的四项 QA。',buildButton());
    else if (questionErrors.has(key)) body = `<div class="research-note research-error-text">${esc(questionErrors.get(key))}<button class="research-link" type="button" data-research-action="retry-questions">重新读取</button></div>`;
    else if (!questionCache.has(key)) body = '<div class="loading-state"><div class="spinner"></div>正在读取主连问题</div>';
    else if (!all.length) body = empty('当前题库暂无可用问题','主连来源缺少合格日线时保留空结果，不用月份合约或模拟行情替代。');
    else if (!questions.length) body = benchmarkNoMatches();
    else body = `<div class="research-table-scroll"><table class="research-table benchmark-table benchmark-four-table"><thead><tr><th>主连 / 已知交易日</th><th>预测交易日</th><th>评估器题目</th><th>真实答案</th></tr></thead><tbody>${questions.slice((page-1)*15,page*15).map(item => `<tr data-backtest-case="${esc(item.case_id)}"><td><strong>${esc(item.contract_code || item.family)}</strong><small>${date(item.input_date)}</small><small class="research-price-label">收 ${numeric(item.current_price)} <span>${esc(unitFor(item))}</span></small>${item.current_bar ? `<small>高 ${numeric(item.current_bar.high)} / 低 ${numeric(item.current_bar.low)}</small>` : ''}</td><td>${date(item.actual_target_date || item.target_date)}<small>${item.actual_target_date ? '实际交易日' : item.target_date_is_expected ? '下一交易日预估' : '下一交易日'}</small>${item.target_date_is_expected ? `<small>${esc(item.live_target_date_note || '休市时等待下一实际交易日')}</small>` : ''}</td><td class="benchmark-question">${timeWindowsMarkup(item)}${questionTextMarkup(item)}<small class="research-case-id">${esc(item.case_id)}</small></td><td>${answerMarkup(item,unitFor(item))}</td></tr>`).join('')}</tbody></table></div><div class="research-pagination"><span>显示 ${(page-1)*15+1}–${Math.min(page*15,questions.length)} / ${number(questions.length)} 题</span><div><button type="button" class="button secondary" data-research-action="previous" ${page <= 1 ? 'disabled' : ''}>上一页</button><span>${page} / ${pages}</span><button type="button" class="button secondary" data-research-action="next" ${page >= pages ? 'disabled' : ''}>下一页</button></div></div>`;
    const heading = toolbar(`回测 ${version}${range ? ` · ${backtestV2PeriodLabel(range)}` : ''} · 日频 · UTC+0`,buildButton());
    const issueFamilies = familyId === 'all' ? families : [familyId];
    const sourceWarning = issueFamilies.map(id => {
      const issue = current.errors?.[id] || (current.families || []).find(item => item.id === id)?.error;
      return issue ? `<div class="research-inline-error" role="status">${esc(names[id])} 来源未就绪：${esc(typeof issue === 'string' ? issue : issue.message || issue.type || '无合格主连日线')}</div>` : '';
    }).join('');
    const units = familyId === 'all' ? '各合约报价单位（WTI / Brent 为美元 / 桶，SC 为元 / 桶）' : familyId === 'SC' ? '元 / 桶' : '美元 / 桶';
    const note = `<div class="research-note"><strong>统一结算口径</strong><p>方向和涨跌幅均按下一交易日收盘价相对当天收盘价计算；高点、低点取下一交易日日内真实最高、最低价。回测真值与模型输入隔离。涨跌幅 MAE 使用百分点，高低价 MAE 使用${esc(units)}。</p>${current.dataset_id ? `<p>主连版本 ${esc(current.dataset_id)} · ${esc(current.coverage || '')}</p>` : ''}</div>`;
    return heading + sourceWarning + dataSummary() + backtestV2CoverageMarkup(current) + panel(`回测 ${version} · 主连 QA`,`${range ? `已知与预测交易日均在 ${range.start} 至 ${range.end}${range.inclusive ? '（含首尾）' : ''}，时分按 UTC+0 展示。` : ''}在题库内切换合约，或按题干、合约代码、已知与预测日期、题号搜索。`,benchmarkFilterMarkup(all,questions) + body) + note;
  }

  function liveRunFor(item) {
    const versions = item.versions || [{dataset_id:item.dataset_id,case_id:item.case_id}];
    return (data?.runs || []).filter(run => run.mode === 'live' && runCoversFamily(run,item.family) &&
      versions.some(version => version.dataset_id === (run.source_dataset_id || run.dataset_id)))
      .sort((a,b) => String(b.started_at || b.run_id).localeCompare(String(a.started_at || a.run_id)))[0] || null;
  }
  function liveDeadline(item) {
    const start = questionWindows(item)?.forecast?.start_utc;
    if (utcMinute(start)) return Date.parse(start) - 30 * 60 * 1000;
    const deadline = item?.submission_deadline_utc || item?.forecast_deadline || item?.live_deadline_at;
    return utcMinute(deadline) ? Date.parse(deadline) : NaN;
  }
  function liveCountdownValue(deadline,now = Date.now()) {
    if (!Number.isFinite(deadline)) return '—';
    const seconds = Math.max(0,Math.ceil((deadline - now) / 1000));
    return [Math.floor(seconds / 3600),Math.floor(seconds % 3600 / 60),seconds % 60].map(value => String(value).padStart(2,'0')).join(':');
  }
  function liveCountdownMarkup(item,now = Date.now()) {
    const deadline = liveDeadline(item),known = Number.isFinite(deadline),expired = known && now >= deadline;
    const basis = utcMinute(questionWindows(item)?.forecast?.start_utc) ? '预测开始前 30 分钟' : '按已保存的截止时间';
    return `<div class="benchmark-live-countdown${expired ? ' is-expired' : ''}" data-live-countdown="${known ? esc(new Date(deadline).toISOString()) : ''}" role="timer" aria-label="提交截止倒计时"><span>距提交截止</span><strong data-countdown-value>${liveCountdownValue(deadline,now)}</strong><small data-countdown-status>${!known ? '截止时间待核实' : expired ? '已截止' : basis}</small></div>`;
  }
  function liveDeadlineMarkup(deadline) {
    return `<div class="benchmark-submission-deadline"><span>提交截止</span>${Number.isFinite(deadline) ? `<time datetime="${esc(new Date(deadline).toISOString())}">${stamp(new Date(deadline).toISOString())}</time>` : '<small>截止时间待核实</small>'}</div>`;
  }
  function liveStatusMarkup(item,latest = false,now = Date.now()) {
    if (actualFor(item)) return status('settled');
    const deadline = liveDeadline(item);
    return latest && Number.isFinite(deadline) && now < deadline ? liveCountdownMarkup(item,now) : liveDeadlineMarkup(deadline);
  }
  function updateLiveCountdowns() {
    if (!active || !root || document.hidden || view?.section !== 'benchmark' || mode() !== 'live') return;
    const now = Date.now();
    root.querySelectorAll('[data-live-countdown]').forEach(element => {
      const deadline = Date.parse(element.dataset.liveCountdown),expired = Number.isFinite(deadline) && now >= deadline;
      if (expired) {
        const cell = element.closest('[data-live-status]');
        if (cell) cell.innerHTML = liveDeadlineMarkup(deadline);
        return;
      }
      const value = element.querySelector('[data-countdown-value]');
      if (value) value.textContent = liveCountdownValue(deadline,now);
    });
    const evaluate = root.querySelector('[data-live-evaluate-deadline]');
    if (evaluate && now >= Number(evaluate.dataset.liveEvaluateDeadline)) {
      evaluate.disabled = true;
    }
    const ready = root.querySelector('[data-live-ready-count]');
    if (ready) ready.textContent = number(filterBenchmarkQuestions(questionCache.get(questionKey()) || [],{query:false}).filter(item =>
      item.is_current && item.live_status === 'ready' && now < liveDeadline(item)).length);
  }
  function liveSummaryMarkup(history,family,questions) {
    const range = questions.map(item => item.actual_target_date || item.target_date).filter(Boolean).sort();
    const coverage = families.map(id => `${id} ${number((history.families || []).find(item => item.id === id)?.count)}`).join(' · ');
    const ready = questions.length ? questions.filter(item => item.is_current && item.live_status === 'ready' && Date.now() < liveDeadline(item)).length : family?.ready;
    return `<div class="research-stat-grid"><div class="research-stat"><span>累计实盘问题</span><strong>${number(history.total_questions)}</strong><small>${coverage}</small></div><div class="research-stat"><span>合约范围题目</span><strong>${number(family?.count)}<em>题</em></strong><small>${esc(benchmarkSelectionLabel())} · 按交易日累计</small></div><div class="research-stat"><span>待结算问题</span><strong>${number(family?.pending)}<em>题</em></strong><small>已结算 ${number(family?.settled)} · 当前可预测 <span data-live-ready-count>${number(ready)}</span></small></div><div class="research-stat"><span>预测交易日</span><strong class="research-date-range">${range.length ? `${date(range[0])}${range[0] !== range.at(-1) ? ` <em>—</em> ${date(range.at(-1))}` : ''}` : '—'}</strong><small>收盘后发布下一交易日题目，并结算此前题目</small></div></div>`;
  }
  function liveQuestionRow(item,unit,latest = false) {
    const actual = actualFor(item);
    const answer = actual ? answerMarkup(item) : `<div class="benchmark-pending-answer">${status('pending_result')}</div>`;
    return `<tr data-live-case="${esc(item.case_id)}"><td class="benchmark-question"><div class="benchmark-live-question-heading"><strong>${esc(names[item.family] || item.family)} · 预测交易日 ${date(item.actual_target_date || item.target_date)}</strong></div><button type="button" class="research-link" data-live-question-details="${esc(item.case_id)}" data-live-dataset="${esc(item.dataset_id)}" aria-label="查看 ${esc(names[item.family] || item.family)} ${esc(item.target_date)} 明细">查看明细</button></td><td data-live-answer>${answer}</td><td data-live-status>${liveStatusMarkup(item,latest)}</td><td><button type="button" class="button secondary" data-live-predictions="${esc(item.case_id)}" data-live-dataset="${esc(item.dataset_id)}" aria-label="查看 ${esc(names[item.family] || item.family)} ${esc(item.target_date)} 模型预测">查看</button></td></tr>`;
  }
  function liveBenchmark() {
    const familyId = benchmarkFamily(),history = liveHistoryData(),current = benchmarkData('live');
    const family = familyId === 'all' ? {count:history.total_questions,pending:history.pending_questions,settled:history.settled_questions,ready:history.ready_questions} : (history.families || []).find(item => item.id === familyId);
    const key = questionKey();
    const all = [...(questionCache.get(key) || [])].sort((a,b) => String(b.target_date).localeCompare(String(a.target_date)) || String(b.input_date).localeCompare(String(a.input_date)));
    const selected = filterBenchmarkQuestions(all,{query:false}),questions = filterBenchmarkQuestions(all);
    const latest = new Map();
    all.forEach(item => {if (!latest.has(item.family)) latest.set(item.family,item);});
    const pages = Math.max(1,Math.ceil(questions.length / 15));page = Math.max(1,Math.min(page,pages));
    const unit = familyId === 'all' ? '各合约报价单位（WTI / Brent 为美元 / 桶，SC 为元 / 桶）' : family?.unit || (familyId === 'SC' ? '元 / 桶' : '美元 / 桶');
    const heading = toolbar('实盘 · 日频 · UTC+0',buildButton());
    const issueFamilies = familyId === 'all' ? families : [familyId];
    const warnings = [...issueFamilies.map(id => {
      const issue = current.errors?.[id];
      return issue ? `${names[id]} 来源未就绪：${typeof issue === 'string' ? issue : issue.message || issue.type}` : '';
    }),...(Array.isArray(history.errors) ? history.errors.map(item => typeof item === 'string' ? item : item.message || item.error || '历史题目读取失败') : Object.values(history.errors || {}).map(item => typeof item === 'string' ? item : item.message || item.error || '历史题目读取失败'))].filter(Boolean);
    let body;
    if (questionErrors.has(key)) body = `<div class="research-note research-error-text">${esc(questionErrors.get(key))}<button class="research-link" type="button" data-research-action="retry-questions">重新读取</button></div>`;
    else if (!questionCache.has(key)) body = '<div class="loading-state"><div class="spinner"></div>正在读取累计实盘题目</div>';
    else if (!all.length) body = empty('尚无已发布的实盘题目','完整收盘日线就绪后发布下一交易日题目；历史题目按交易日持续保留。');
    else if (!questions.length) body = benchmarkNoMatches();
    else body = `<div class="research-table-scroll"><table class="research-table benchmark-table benchmark-four-table benchmark-live-table"><thead><tr><th>具体问题</th><th>答案</th><th>状态</th><th>模型预测</th></tr></thead><tbody>${questions.slice((page-1)*15,page*15).map(item => liveQuestionRow(item,item.unit || (item.family === 'SC' ? '元 / 桶' : '美元 / 桶'),item === latest.get(item.family))).join('')}</tbody></table></div><div class="research-pagination"><span>显示 ${(page-1)*15+1}–${Math.min(page*15,questions.length)} / ${number(questions.length)} 题</span>${pages > 1 ? `<div><button type="button" class="button secondary" data-research-action="previous" ${page <= 1 ? 'disabled' : ''}>上一页</button><span>${page} / ${pages}</span><button type="button" class="button secondary" data-research-action="next" ${page >= pages ? 'disabled' : ''}>下一页</button></div>` : '<span>按预测交易日倒序</span>'}</div>`;
    const note = `<div class="research-note"><strong>每日发布与结算</strong><p>各主连收盘后的完整日线到齐后，发布下一交易日的四项预测题，同时结算此前题目。方向和涨跌幅按目标收盘相对冻结输入收盘计算，高低点取目标交易日真实最高、最低价；涨跌幅 MAE 使用百分点，高低价 MAE 使用${esc(unit)}。已锁定的预测只补评分。</p><p>仅累计在提交截止前有效发布的题目；同一交易日多次刷新合并展示。真实结果未完整时继续待结算，历史题目不可补交预测。</p></div>`;
    return heading + warnings.map(text => `<div class="research-inline-error" role="status">${esc(text)}</div>`).join('') + liveSummaryMarkup(history,family,selected) + panel('实盘 QA','在此切换合约查看累计题目、倒计时、模型预测和结算结果。',benchmarkFilterMarkup(all,questions) + body,`<span class="count-badge">${number(selected.length)} 题</span>`) + note;
  }

  function liveDetailTimeMarkup(window) {
    const segments = window?.segments || [];
    if (segments.length > 1) return `<ul class="live-question-time-segments">${segments.map(segment => `<li>${segment.label ? `<span>${esc(segment.label)}</span>` : ''}${utcMinute(segment.start_utc) && utcMinute(segment.end_utc) ? `<time>${esc(utcMinute(segment.start_utc))} — ${esc(utcMinute(segment.end_utc))} UTC+0</time>` : '<span>时段待核实</span>'}</li>`).join('')}</ul>`;
    return utcMinute(window?.start_utc) && utcMinute(window?.end_utc) ? `<time>${esc(windowRange(window))} UTC+0</time>` : '<span>时段待核实</span>';
  }
  function liveQuestionDetailsMarkup(item) {
    const windows = questionWindows(item);
    const cutoff = item.time_windows?.information_cutoff_utc;
    return `<dl class="live-question-details-fields"><div><dt>已知时间</dt><dd>${liveDetailTimeMarkup(windows?.known)}</dd></div><div><dt>预测时间</dt><dd>${liveDetailTimeMarkup(windows?.forecast)}</dd></div><div><dt>事件截止</dt><dd>${utcMinute(cutoff) ? stamp(cutoff) : '待核实'}</dd></div><div><dt>题目</dt><dd class="live-question-full-text">${esc(item.question || '未记录题目')}</dd></div></dl>`;
  }
  function openLiveQuestionDetails(item) {
    if (!options.openLiveQuestionDetails) return;
    liveQuestionDetailsSelection = item;
    options.openLiveQuestionDetails(`<div class="overlay modal-overlay"><section class="modal live-question-details-modal" role="dialog" aria-modal="true" aria-labelledby="live-question-details-title"><header class="modal-header"><h2 id="live-question-details-title">题目明细</h2><button type="button" class="icon-button" data-close aria-label="关闭题目明细">×</button></header><div class="live-question-details-content">${liveQuestionDetailsMarkup(item)}</div></section></div>`);
  }
  function closeLiveQuestionDetails() {
    const selection = liveQuestionDetailsSelection;
    liveQuestionDetailsSelection = null;
    if (selection && root) [...root.querySelectorAll('[data-live-question-details]')].find(button => button.dataset.liveQuestionDetails === selection.case_id && button.dataset.liveDataset === selection.dataset_id)?.focus();
  }

  function livePredictionsTable(payload) {
    const question = payload.question || {},unit = question.unit || (question.family === 'SC' ? '元 / 桶' : '美元 / 桶');
    const items = Array.isArray(payload.items) ? payload.items : [];
    const available = item => ['predicted_pending','success'].includes(item.status) && item.predicted && typeof item.predicted === 'object';
    const rows = items.map(item => {
      const hasPrediction = available(item),scored = hasPrediction && item.actual && typeof item.absolute_errors === 'object';
      const cells = ['high','low','return_pct'].map(key => {
        const value = hasPrediction ? item.predicted[key] : null;
        const absoluteError = scored ? item.absolute_errors?.[key] : null;
        return `<td data-live-prediction-metric="${key}"><span class="${predictionTone(value,key === 'return_pct' ? 0 : item.current_price ?? question.current_price)}">${key === 'return_pct' ? change(value) : numeric(value)}</span>${absoluteError != null ? `<small>MAE ${numeric(absoluteError,4)}</small>` : ''}</td>`;
      }).join('');
      const timing = [item.submitted_at ? `提交 ${stamp(item.submitted_at)}` : '',item.prediction_locked_at ? `锁定 ${stamp(item.prediction_locked_at)}` : ''].filter(Boolean).join(' · ');
      const failure = item.error || (!hasPrediction && item.latest_attempt?.error);
      return `<tr data-live-prediction-model="${esc(item.model)}"><th scope="row">${esc(modelLabel(item.model,item))}</th><td data-live-prediction-metric="direction">${hasPrediction ? predictionDirection(item.predicted.direction) : '—'}</td>${cells}<td title="${timing}">${status(item.status)}${failure ? `<small class="research-inline-error">${esc(failure)}</small>` : ''}</td></tr>`;
    }).join('');
    const consensus = dailyDirectionConsensus(items.map(item => ({...item,status:available(item) ? 'success' : item.status})));
    const leaders = consensus.leaders.map(predictionDirection).join(' / ');
    const tally = `看涨 ${consensus.counts.up} · 看跌 ${consensus.counts.down}${consensus.counts.flat ? ` · 持平 ${consensus.counts.flat}` : ''}`;
    const consensusValue = consensus.total < 2 ? consensus.total ? '样本不足' : '—' : `${leaders}${consensus.leaders.length > 1 ? ' 并列' : ''} ${percent(consensus.ratio)}`;
    return `<div class="research-table-scroll live-predictions-table-scroll" tabindex="0" role="region" aria-label="实盘模型预测对比，可横向滚动"><table class="research-table overview-daily-table live-predictions-table"><thead><tr><th scope="col">模型（实验）</th><th scope="col">方向</th><th scope="col">最高价<small>${esc(unit)}</small></th><th scope="col">最低价<small>${esc(unit)}</small></th><th scope="col">收盘涨跌幅<small>% / 误差：百分点</small></th><th scope="col">状态</th></tr></thead><tbody>${rows || '<tr><td colspan="6" class="research-muted">暂无模型预测记录</td></tr>'}</tbody><tfoot><tr><th scope="row">方向一致性</th><td colspan="5" class="overview-consensus">${consensusValue}${consensus.total ? `<small>${tally} · ${consensus.total} / ${items.length} 个模型有效</small>` : ''}</td></tr></tfoot></table></div>`;
  }
  function renderLivePredictions() {
    if (!livePredictionSelection || !options.isLivePredictionsOpen?.()) return;
    const content = document.querySelector('#live-predictions-content');
    if (!content) return;
    const position = content.querySelector('.live-predictions-table-scroll');
    const scroll = {top:content.scrollTop,left:position?.scrollLeft || 0};
    if (livePredictionError && !livePredictionPayload) {
      content.innerHTML = `<div class="research-note research-error-text" role="alert">${esc(livePredictionError)}<button type="button" class="research-link" data-live-predictions-retry>重新读取</button></div>`;
    } else if (!livePredictionPayload) {
      content.innerHTML = '<div class="loading-state"><div class="spinner"></div>正在读取模型预测</div>';
    } else {
      const payload = livePredictionPayload,question = {...livePredictionSelection,...payload.question};
      const unit = question.unit || (question.family === 'SC' ? '元 / 桶' : '美元 / 桶');
      const completed = payload.items.filter(item => ['predicted_pending','success'].includes(item.status) && item.predicted).length;
      const notice = livePredictionError ? `<div class="research-inline-error" role="status">${esc(livePredictionError)} <button type="button" class="research-link" data-live-predictions-retry>重新读取</button></div>` : '';
      const issues = (payload.errors || []).map(item => `<div class="research-inline-error" role="status">${esc(typeof item === 'string' ? item : item.message || item.error || '部分预测记录校验失败')}</div>`).join('');
      content.innerHTML = `<div class="live-predictions-summary"><p>预测交易日 ${date(question.target_date)} · UTC+0</p><span>已锁定 ${completed} / ${payload.items.length} 个模型${actualFor(question) ? '' : ' · 待结算'}</span></div>${inputMarketMarkup({date:question.input_date,...question.current_bar,close:question.current_price},esc(unit))}${notice}${issues}${livePredictionsTable({...payload,question})}<p class="live-predictions-note">预测在截止前提交并锁定；真实结果完整后显示误差。页面自动更新预测进度。</p>`;
    }
    content.scrollTop = scroll.top;
    const table = content.querySelector('.live-predictions-table-scroll');
    if (table) table.scrollLeft = scroll.left;
  }
  async function loadLivePredictions(force = false) {
    if (!livePredictionSelection || !options.isLivePredictionsOpen?.() || livePredictionPending && !force) return;
    const selection = livePredictionSelection,request = ++livePredictionRequest;
    livePredictionPending = true;
    try {
      const payload = await options.api(`/api/benchmark?mode=live&kind=predictions&dataset_id=${encodeURIComponent(selection.dataset_id)}&case_id=${encodeURIComponent(selection.case_id)}`);
      if (request !== livePredictionRequest || !livePredictionSelection || !options.isLivePredictionsOpen?.()) return;
      if (payload.dataset_id !== selection.dataset_id || payload.case_id !== selection.case_id || !Array.isArray(payload.items)) throw new Error('模型预测与所选题目不匹配，请重新读取');
      const signature = JSON.stringify(payload);
      const changed = signature !== livePredictionSignature || !!livePredictionError;
      livePredictionPayload = payload;livePredictionError = '';livePredictionSignature = signature;
      if (changed) renderLivePredictions();
    } catch (caught) {
      if (request !== livePredictionRequest || !livePredictionSelection || !options.isLivePredictionsOpen?.()) return;
      livePredictionError = caught.message;renderLivePredictions();
    } finally {if (request === livePredictionRequest) livePredictionPending = false;}
  }
  function openLivePredictions(item) {
    if (!options.openLivePredictions) return;
    livePredictionRequest++;livePredictionPending = false;livePredictionSelection = item;
    livePredictionPayload = null;livePredictionError = '';livePredictionSignature = '';
    options.openLivePredictions(`<div class="overlay modal-overlay"><section class="modal live-predictions-modal" role="dialog" aria-modal="true" aria-labelledby="live-predictions-title"><header class="modal-header"><div><h2 id="live-predictions-title">模型预测 · ${esc(names[item.family] || item.family)}</h2><p>${date(item.target_date)}</p></div><button type="button" class="icon-button" data-close aria-label="关闭模型预测">×</button></header><div id="live-predictions-content" class="live-predictions-content"></div></section></div>`);
    document.querySelector('#live-predictions-content')?.addEventListener('click',event => {if (event.target.closest('[data-live-predictions-retry]')) loadLivePredictions(true);});
    renderLivePredictions();loadLivePredictions();
  }
  function closeLivePredictions() {
    const selection = livePredictionSelection;
    livePredictionRequest++;livePredictionSelection = null;livePredictionPayload = null;livePredictionPending = false;
    livePredictionError = '';livePredictionSignature = '';
    if (selection && root) [...root.querySelectorAll('[data-live-predictions]')].find(button => button.dataset.livePredictions === selection.case_id && button.dataset.liveDataset === selection.dataset_id)?.focus();
  }

  function memoryBenchmark() {
    const heading = toolbar(`${esc(memoryDataset?.year || 2026)} 年 · 1—10 月 · 闭卷事实填空`,memoryDataset ? '<a class="button secondary" href="/api/benchmark?kind=memory-leakage&amp;download=1" download="memory-leakage-benchmark.json">下载题库 JSON</a>' : '');
    if (memoryError) return heading + empty('记忆泄漏题库暂时不可用',esc(memoryError),'<button type="button" class="button secondary" data-research-action="retry-memory">重新读取</button>');
    if (!memoryDataset) return heading + '<div class="loading-state"><div class="spinner"></div>正在读取记忆泄漏题库</div>';
    const all = memoryDataset.questions;
    const months = [...new Set(all.map(item => item.month))].sort();
    if (memoryMonth && !months.includes(memoryMonth)) memoryMonth = '';
    const questions = memoryMonth ? all.filter(item => item.month === memoryMonth) : all;
    const pages = Math.max(1,Math.ceil(questions.length / 10));page = Math.max(1,Math.min(page,pages));
    const monthLabel = month => `${Number(String(month).slice(-2))} 月`;
    const filter = `<label class="memory-month-select">月份<select id="memory-month-filter" aria-label="筛选记忆泄漏题目月份"><option value="">全部月份 · ${number(all.length)} 题</option>${months.map(month => `<option value="${esc(month)}" ${memoryMonth === month ? 'selected' : ''}>${esc(month)} · ${all.filter(item => item.month === month).length} 题</option>`).join('')}</select></label>`;
    const summary = `<div class="research-stat-grid memory-stat-grid"><div class="research-stat"><span>填空题</span><strong>${number(all.length)}</strong><small>每个事件一道题</small></div><div class="research-stat"><span>覆盖月份</span><strong>${months.length}<em>个月</em></strong><small>${esc(memoryDataset.year || 2026)} 年 1—10 月</small></div><div class="research-stat"><span>每月题量</span><strong>${[...new Set(months.map(month => all.filter(item => item.month === month).length))].join(' / ')}<em>题</em></strong><small>从现有金融事件库筛选</small></div><div class="research-stat"><span>测试方式</span><strong class="research-date-range">闭卷 · 单空作答</strong><small>评估时只提供题干，关闭检索与联网</small></div></div>`;
    const coverage = `<div class="memory-months" aria-label="各月题目分布"><button type="button" class="memory-month ${!memoryMonth ? 'active' : ''}" data-memory-month="" aria-pressed="${!memoryMonth}">全部<span>${all.length}</span></button>${months.map(month => `<button type="button" class="memory-month ${memoryMonth === month ? 'active' : ''}" data-memory-month="${esc(month)}" aria-pressed="${memoryMonth === month}">${monthLabel(month)}<span>${all.filter(item => item.month === month).length}</span></button>`).join('')}</div>`;
    const cards = questions.slice((page-1)*10,page*10).map(item => {
      const source = item.source || {};
      const sourceUrl = /^https?:\/\//i.test(source.url || '') ? `<a href="${esc(source.url)}" target="_blank" rel="noopener noreferrer">${esc(source.title || '查看原文')}</a>` : `<span>${esc(source.title || '新闻标题未记录')}</span>`;
      const evidence = typeof item.evidence === 'string' ? item.evidence : item.evidence?.text || '';
      const aliases = Array.isArray(item.answer_aliases) ? item.answer_aliases.filter(alias => alias !== item.answer) : [];
      const answer = String(item.answer ?? '');
      const index = all.indexOf(item) + 1;
      const temporalFocus = ({data_release:'数据发布',policy_decision:'政策决定',event_detail:'事件进展',forecast_update:'预测更新'})[item.temporal_focus] || '';
      const temporalRationale = typeof item.temporal_rationale === 'string' ? item.temporal_rationale.trim() : '';
      return `<article class="memory-question-card" data-memory-case="${esc(item.case_id)}"><header><span class="memory-question-number">${String(index).padStart(3,'0')}</span><div><span class="memory-question-month">${esc(item.month)}</span><h3>${esc(item.event || '金融事件')}</h3></div><span class="memory-question-type">填空题</span></header><p class="memory-question-text">${esc(item.question)}</p><div class="memory-answer"><span>答案</span><strong>${esc(answer)}</strong>${aliases.length ? `<small>接受：${aliases.map(esc).join(' / ')}</small>` : ''}</div><div class="memory-source"><span class="memory-source-label">引用新闻</span><div>${sourceUrl}<p>${esc(source.source_name || '来源未记录')} · ${stamp(source.published_at)}</p>${source.news_id ? `<button type="button" class="research-link memory-news-record" data-memory-news="${esc(source.news_id)}">查看事件库记录</button>` : ''}</div></div>${evidence || item.event_rationale || temporalRationale ? `<details class="memory-evidence" data-case-key="${esc(item.case_id)}:memory-evidence"><summary>查看出题依据与事件重要性</summary>${evidence ? `<blockquote>${esc(evidence)}</blockquote>` : ''}${item.event_rationale ? `<p>${esc(item.event_rationale)}</p>` : ''}${temporalRationale ? `<p><b>时效性依据：</b>${temporalFocus ? `${esc(temporalFocus)} · ` : ''}${esc(temporalRationale)}</p>` : ''}<small>${esc(item.case_id)}</small></details>` : ''}</article>`;
    }).join('');
    const pagination = `<div class="research-pagination"><span>${questions.length ? `显示 ${(page-1)*10+1}–${Math.min(page*10,questions.length)} / ${number(questions.length)} 题` : '暂无题目'}</span><div><button type="button" class="button secondary" data-research-action="previous" ${page <= 1 ? 'disabled' : ''}>上一页</button><span>${page} / ${pages}</span><button type="button" class="button secondary" data-research-action="next" ${page >= pages ? 'disabled' : ''}>下一页</button></div></div>`;
    const methodology = memoryDataset.methodology || {};
    const methodologyFields = [
      ['事件选择','selection','从现有原油与宏观金融事件库中，按编辑判断每月选择 10 个重要事件，各引用一条新闻制作单空填空题。重要性选择含主观判断，覆盖范围受现有事件库限制。'],
      ['月份口径','month_basis','按引用新闻的首次发布时间（UTC+0）归属月份。'],
      ['时效性要求','temporal_specificity','填空考查当次报道的新事实，排除长期身份、地理常识和可由题干推算的答案；日期仅用于定位。'],
      ['评估方式','evaluation','正式评估须使用独立闭卷会话，仅输入题干，不提供答案、引用或证据，关闭检索与联网。'],
      ['解释边界','limitation','答对表示模型可能掌握相关历史事实，单次正确回答不能独立证明训练数据泄漏。'],
    ];
    const note = `<div class="research-note memory-methodology"><strong>构建与使用口径</strong>${methodologyFields.map(([label,key,fallback]) => `<p><b>${label}：</b>${esc(typeof methodology[key] === 'string' && methodology[key].trim() ? methodology[key] : fallback)}</p>`).join('')}<p>题库版本 ${esc(memoryDataset.dataset_id || '未记录')}</p></div>`;
    return `<div class="memory-benchmark">${heading}${summary}${panel('记忆泄漏 · 题目与答案','核对当次新闻的具体事实、标准答案和引用。',coverage + `<div class="memory-question-list">${cards || '<p class="research-muted">当前月份暂无题目。</p>'}</div>` + pagination,filter)}${note}</div>`;
  }

  function render() {
    if (!active || !root || !view) return;
    if (benchmarkComposing && view.section === 'benchmark' && !isMemoryBenchmark()) return;
    if (!data && !isMemoryBenchmark()) {root.innerHTML = error ? empty('评估工作台暂时不可用',esc(error),'<button type="button" class="button secondary" data-research-action="refresh">重新读取</button>') : '<div class="loading-state"><div class="spinner"></div>正在读取评估工作台</div>';return;}
    const activeSearch = root.querySelector('#benchmark-question-search');
    const searchSelection = activeSearch && document.activeElement === activeSearch
      ? {start:activeSearch.selectionStart,end:activeSearch.selectionEnd,direction:activeSearch.selectionDirection}
      : null;
    const opened = new Set([...root.querySelectorAll('details[data-case-key][open]')].map(element => element.dataset.caseKey));
    const openExperiments = new Set([...root.querySelectorAll('details[data-experiment-details][open]')].map(element => element.dataset.experimentDetails));
    const legacyOpen = !!root.querySelector('.research-legacy-history[open]');
    root.querySelectorAll('pre[data-trace-scroll-key]').forEach(element => {traceScrollPositions.set(element.dataset.traceScrollKey,{top:element.scrollTop,left:element.scrollLeft});});
    if (selectedModelId()) {
      const title = document.querySelector('#page-title');
      if (title) title.innerHTML = `${esc(modelLabel(selectedModelId(),currentRunSummary()))}<span class="title-dot">.</span>`;
    }
    root.innerHTML = (error && !isMemoryBenchmark() ? `<div class="research-inline-error" role="alert">${esc(error)}</div>` : '') + (view.section === 'leaderboard' ? leaderboard() : view.section === 'environment' ? environment() : benchmark());
    root.querySelectorAll('details[data-case-key]').forEach(element => {element.open = opened.has(element.dataset.caseKey);});
    root.querySelectorAll('details[data-experiment-details]').forEach(element => {element.open = openExperiments.has(element.dataset.experimentDetails);});
    if (legacyOpen && root.querySelector('.research-legacy-history')) root.querySelector('.research-legacy-history').open = true;
    root.querySelectorAll('pre[data-trace-scroll-key]').forEach(element => {const position = traceScrollPositions.get(element.dataset.traceScrollKey);if (position) {element.scrollTop = position.top;element.scrollLeft = position.left;}});
    if (searchSelection) {
      const search = root.querySelector('#benchmark-question-search');
      search?.focus?.({preventScroll:true});
      if (searchSelection.start !== null && searchSelection.end !== null) search?.setSelectionRange?.(searchSelection.start,searchSelection.end,searchSelection.direction || 'none');
    }
  }

  async function handleClick(event) {
    if (event.target.closest('[data-benchmark-clear-search]')) {
      setBenchmarkFilters({query:''});root.querySelector('#benchmark-question-search')?.focus?.();return;
    }
    if (event.target.closest('[data-benchmark-reset-filters]')) {
      setBenchmarkFilters({family:'all',query:''});root.querySelector('#benchmark-question-search')?.focus?.();return;
    }
    if (event.target.closest('[data-overview-default]')) {
      overviewExperimentIds = [];overviewDate = '';render();loadOverviewPredictions();return;
    }
    const details = event.target.closest('[data-live-question-details]');
    if (details) {
      const question = (questionCache.get(questionKey()) || []).find(item => item.case_id === details.dataset.liveQuestionDetails && item.dataset_id === details.dataset.liveDataset);
      if (question) openLiveQuestionDetails(question);
      return;
    }
    const predictions = event.target.closest('[data-live-predictions]');
    if (predictions) {
      const question = (questionCache.get(questionKey()) || []).find(item => item.case_id === predictions.dataset.livePredictions && item.dataset_id === predictions.dataset.liveDataset);
      if (question) openLivePredictions(question);
      return;
    }
    const openMemory = event.target.closest('[data-open-memory-run]');
    if (openMemory) {
      const model = openMemory.dataset.memoryModel;
      if (model && model !== selectedModelId()) options.onModelSelect?.(model,'backtest');
      selectedMemoryRun = openMemory.dataset.openMemoryRun;memoryResultMonth = '';memoryResultPage = 1;
      render();loadMemoryRun(true);return;
    }
    const memoryFilter = event.target.closest('[data-memory-month]');
    if (memoryFilter) {memoryMonth = memoryFilter.dataset.memoryMonth;page = 1;render();return;}
    const memoryNews = event.target.closest('[data-memory-news]');
    if (memoryNews) {options.onNewsSelect?.(memoryNews.dataset.memoryNews);return;}
    const compare = event.target.closest('[data-compare-run]');
    if (compare) {
      options.onModelSelect?.(compare.dataset.compareModel,mode());
      selectedRun = compare.dataset.compareRun;render();loadRun(true);return;
    }
    const traceToggle = event.target.closest('[data-toggle-trace]');
    if (traceToggle) {
      const runId = traceToggle.dataset.traceRun,caseId = traceToggle.dataset.toggleTrace,key = traceKey(runId,caseId);
      if (traceExpanded.has(key)) {traceExpanded.delete(key);render();}
      else {traceExpanded.add(key);render();loadTrace(runId,caseId);}
      return;
    }
    const traceLoad = event.target.closest('[data-load-trace]');
    if (traceLoad) {loadTrace(traceLoad.dataset.traceRun,traceLoad.dataset.loadTrace,true);return;}
    const open = event.target.closest('[data-open-run]');
    if (open) {selectedRun = open.dataset.openRun;render();loadRun(true);return;}
    const settle = event.target.closest('[data-settle-run]');
    const action = settle ? 'settle' : event.target.closest('[data-research-action]')?.dataset.researchAction;
    if (!action) return;
    if (action === 'memory-result-next' || action === 'memory-result-previous') {memoryResultPage += action === 'memory-result-next' ? 1 : -1;render();return;}
    if (action === 'retry-memory-run') {loadMemoryRun(true);render();return;}
    if (action === 'retry-memory' || action === 'refresh' && isMemoryBenchmark()) {await loadMemoryQuestions(true);return;}
    if (action === 'next' || action === 'previous') {page += action === 'next' ? 1 : -1;render();return;}
    if (action === 'retry-questions') {questionErrors.delete(questionKey());loadQuestions(true);render();return;}
    if (action === 'refresh') {await load();if (view?.section === 'leaderboard') {loadRun(true);loadMemoryRun(true);}if (view?.section === 'benchmark') loadQuestions(true);return;}
    if (busy) return;
    busy = action;render();
    try {
      if (action === 'build') {
        const buildMode = mode(),profile = benchmarkProfile();
        const payload = profile === 'v2' ? {mode:'backtest',profile:'v2',refresh:true} : {days:30,mode:buildMode,refresh:true};
        await options.api('/api/benchmark/build',{method:'POST',body:JSON.stringify(payload)});
        questionCache.clear();questionErrors.clear();await load();
        options.toast(`${profile === 'v2' ? '回测 v2' : modeNames[buildMode]}主连题目已构建，来源与版本已保存`);
      } else if (action === 'evaluate') {
        if (mode() === 'live' && view.section === 'benchmark' && !(questionCache.get(questionKey()) || []).some(item =>
          item.is_current && item.live_status === 'ready' && Date.now() < liveDeadline(item))) {
          throw new Error('当前没有可提交的实盘题目；提交截止为预测时段开始前 30 分钟。');
        }
        const selected = selectedModelId();
        const strategyId = view.section === 'leaderboard' && selected ? `aihub-${selected}` : 'gpt6.1-sol';
        const payload = {strategy_id:strategyId,environment_id:'env01',dataset_id:datasetId(),workers:4,mode:mode(),...(view.section === 'leaderboard' ? {one_day:true} : {}),...(view.section === 'benchmark' && benchmarkFamily() !== 'all' ? {family:benchmarkFamily()} : {})};
        const result = await options.api('/api/evaluations',{method:'POST',body:JSON.stringify(payload)});
        selectedRun = result.run_id;await load();loadRun(true);
        options.toast(mode() === 'live' ? '实盘预测已提交，将先锁定输出，再等待真实结果' : '四项回测已开始，结果将自动更新');
      } else if (action === 'settle') {
        await options.api(`/api/evaluations/${encodeURIComponent(settle.dataset.settleRun)}/settle`,{method:'POST',body:JSON.stringify({refresh:true})});
        selectedRun = settle.dataset.settleRun;questionCache.clear();await load();loadQuestions(true);loadRun(true);
        options.toast('已更新实际行情结算；没有完整真值的问题继续等待，预测不会重跑');
      }
    } catch (caught) {options.toast(caught.message,true);}
    finally {busy = '';render();}
  }

  global.OilResearch = {initialize,show,getBenchmarkDateRange:() => backtestV2DateRange(),closeLivePredictions,closeLiveQuestionDetails,hide(){active = false;if (livePredictionSelection) options.closeLivePredictions?.();if (liveQuestionDetailsSelection) options.closeLiveQuestionDetails?.();},refresh(){return load();}};
})(window);
