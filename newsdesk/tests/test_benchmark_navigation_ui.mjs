import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const families = ['WTI', 'Brent', 'SC'];
const flush = async () => {
  // Exercise the public async controller rather than calling its filters or
  // renderer directly. Requests are fulfilled by the HTTP boundary below.
  for (let index = 0; index < 3; index++) await new Promise(resolve => setImmediate(resolve));
};
const decode = value => String(value).replace(/&quot;/g, '"').replace(/&#39;/g, "'")
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');

function fixture() {
  const backtest = families.flatMap(family => Array.from({length: 30}, (_, index) => {
    const input = new Date(Date.UTC(2026, 8, index + 1)).toISOString().slice(0, 10);
    const target = new Date(Date.UTC(2026, 8, index + 2)).toISOString().slice(0, 10);
    return {
      case_id: `bt-${family}-${String(index + 1).padStart(2, '0')}`, family,
      contract_id: `${family.toLowerCase()}-main`,
      contract_code: {WTI: 'CL.1:NYM', Brent: 'IB.1:IEU', SC: 'SC0'}[family],
      currency: family === 'SC' ? 'CNY' : 'USD', unit: family === 'SC' ? '元 / 桶' : '美元 / 桶',
      input_date: input, target_date: target, current_price: 70 + index,
      current_bar: {open: 69 + index, high: 73 + index, low: 68 + index, close: 70 + index},
      question: family === 'SC' && index === 28
        ? '核对 </p><script>fixture()</script> 后给出四项预测。'
        : `${family} 题目 ${index + 1}：${family === 'SC' && index === 4 ? '库存变化' : '价格走势'}，预测方向及高低点。`,
      actual: {direction: 'up', return_pct: 1, high: 75 + index, low: 69 + index},
      mode: 'backtest',
    };
  }));
  const live = families.flatMap((family, index) => {
    const code = {WTI: 'CL.1:NYM', Brent: 'IB.1:IEU', SC: 'SC0'}[family];
    const dataset = `qa-${'abc'[index].repeat(20)}`;
    const current = {
      case_id: `live-${family}-current`, dataset_id: dataset, family,
      contract_id: `${family.toLowerCase()}-main`, contract_code: code,
      input_date: '2026-11-03', target_date: '2026-11-04', current_price: 71,
      current_bar: {open: 70, high: 73, low: 69, close: 71},
      unit: family === 'SC' ? '元 / 桶' : '美元 / 桶',
      question: `LIVE_BODY_${family}_CURRENT：对应独立冻结题目。`,
      time_windows: {timezone: 'UTC', status: 'ready',
        known: {trading_date: '2026-11-03', start_utc: '2026-11-02T13:00:00Z', end_utc: '2026-11-03T07:00:00Z'},
        forecast: {trading_date: '2026-11-04',
          start_utc: {WTI: '2026-11-03T23:00:00Z', Brent: '2026-11-04T01:00:00Z', SC: '2026-11-03T13:00:00Z'}[family],
          end_utc: '2026-11-04T23:00:00Z'}, information_cutoff_utc: '2026-11-03T07:00:00Z'},
      is_current: true, live_status: 'ready', forecast_status: 'ready', actual: null,
      settlement_status: 'pending', versions: [{dataset_id: dataset, case_id: `live-${family}-current`}],
    };
    const old = {...structuredClone(current), case_id: `live-${family}-old`,
      question: `LIVE_BODY_${family}_OLD：历史题目。`, input_date: '2026-11-02', target_date: '2026-11-03',
      is_current: false, live_status: 'forecast_closed', forecast_status: 'forecast_closed'};
    return [old, current];
  });
  const summaryFamilies = families.map(id => ({id, name: `${id} 主连`, count: 30,
    unit: id === 'SC' ? '元 / 桶' : '美元 / 桶'}));
  const catalog = {runs: [], strategies: [], experiments: [], benchmarks: {
    backtest: {dataset_id: 'qa-backtest', series_type: 'dominant_continuous', total_questions: 90,
      days_per_contract: 30, families: summaryFamilies, target_start: '2026-09-02', target_end: '2026-10-01'},
    live: {dataset_id: 'qa-live', series_type: 'dominant_continuous', total_questions: 3,
      ready_questions: 3, families: summaryFamilies.map(item => ({...item, count: 1}))},
    live_history: {history: true, history_revision: 'all-families-one', series_type: 'dominant_continuous',
      total_questions: 6, pending_questions: 6, settled_questions: 0, ready_questions: 3, errors: {},
      families: summaryFamilies.map(item => ({...item, count: 2, pending: 2, settled: 0, ready: 1}))},
  }};
  const memory = {year: 2026, dataset_id: 'memory-frozen', total_questions: 100,
    questions: Array.from({length: 100}, (_, index) => ({case_id: `memory-${index + 1}`,
      month: `2026-${String(Math.floor(index / 10) + 1).padStart(2, '0')}`, event: '历史事件',
      question: `记忆题目 ${index + 1}：____`, answer: `独立参考答案 ${index + 1}`,
      source: {title: '原始报道', url: 'https://example.com/report', published_at: '2026-01-02T08:00:00Z'}}))};
  return {catalog, backtest, live, memory};
}

function harness(payload) {
  const listeners = new Map(), timers = [], requests = [], filterChanges = [];
  let html = '', search = null, selectedFamily = null, details = '', detailOpens = 0;
  let predictions = '', predictionsOpen = false;
  const predictionContent = {innerHTML: '', scrollTop: 0, addEventListener() {}, querySelector() {return null;}};
  const document = {hidden: false, activeElement: null, addEventListener() {}, querySelectorAll() {return [];},
    querySelector(selector) {
      if (selector === '#research-workspace') return root;
      if (selector === '#live-predictions-content') return predictionsOpen ? predictionContent : null;
      return root.querySelector(selector);
    }};
  const control = (id, value = '') => ({id, value, selectionStart: 0, selectionEnd: 0, selectionDirection: 'none',
    focus() {document.activeElement = this;}, matches(selector) {return selector === `#${id}`;},
    setSelectionRange(start, end, direction = 'none') {
      this.selectionStart = start; this.selectionEnd = end; this.selectionDirection = direction;
    }});
  const root = {addEventListener(name, callback) {listeners.set(name, callback);},
    querySelectorAll() {return [];},
    querySelector(selector) {
      return selector === '#benchmark-question-search' ? search : selector === '#benchmark-family-filter' ? selectedFamily : null;
    },
    get innerHTML() {return html;},
    set innerHTML(value) {
      html = value;
      const input = html.match(/<input\b[^>]*id="benchmark-question-search"[^>]*>/)?.[0];
      search = input ? control('benchmark-question-search', decode(input.match(/\bvalue="([^"]*)"/)?.[1] || '')) : null;
      const select = html.match(/<select\b[^>]*id="benchmark-family-filter"[^>]*>([\s\S]*?)<\/select>/)?.[1];
      selectedFamily = select ? control('benchmark-family-filter', select.match(/<option value="([^"]+)" selected/)?.[1] || 'all') : null;
    }};
  class TestDate extends Date {
    constructor(...args) {super(...(args.length ? args : ['2026-11-03T09:00:00Z']));}
    static now() {return Date.parse('2026-11-03T09:00:00Z');}
  }
  const context = {window: {}, document, Date: TestDate,
    setInterval(callback, milliseconds) {timers.push({callback, milliseconds}); return timers.length;}};
  vm.createContext(context);
  // Production source is executed unchanged: interactions use the same public
  // initialize/show entry points and registered DOM events as the application.
  vm.runInContext(fs.readFileSync(new URL('../static/benchmark.js', import.meta.url), 'utf8'), context);
  const controller = context.window.OilResearch;
  const config = {
    async api(path, request) {
      requests.push({path, request});
      assert.equal(request?.method || 'GET', 'GET', 'Navigation and filtering never build, evaluate or settle a dataset');
      const url = new URL(path, 'http://fixture.local');
      if (url.pathname === '/api/research') return structuredClone(payload.catalog);
      if (url.pathname === '/api/benchmark/questions') {
        assert.equal(url.searchParams.has('family'), false, 'Every question request reads all families before local filtering');
        const live = url.searchParams.get('mode') === 'live';
        assert.equal(url.searchParams.get('history'), live ? '1' : null);
        const items = live ? payload.live : payload.backtest;
        return {items: structuredClone(items), total: items.length, mode: live ? 'live' : 'backtest'};
      }
      if (url.searchParams.get('kind') === 'memory-leakage') return structuredClone(payload.memory);
      if (url.searchParams.get('kind') === 'predictions') {
        const item = payload.live.find(item => item.case_id === url.searchParams.get('case_id') && item.dataset_id === url.searchParams.get('dataset_id'));
        assert.ok(item, 'Prediction requests retain the selected case and dataset binding');
        return {question: structuredClone(item), dataset_id: item.dataset_id, case_id: item.case_id,
          items: [], summary: {expected_models: 0}, errors: []};
      }
      throw new Error(`Unexpected read: ${path}`);
    },
    toast(message, failure) {if (failure) throw new Error(message);},
    onBenchmarkFilterChange(value) {filterChanges.push({...value});},
    openLiveQuestionDetails(markup) {details = markup; detailOpens++;},
    closeLiveQuestionDetails() {controller.closeLiveQuestionDetails();},
    openLivePredictions(markup) {predictions = markup; predictionsOpen = true;},
    isLivePredictionsOpen() {return predictionsOpen;},
    closeLivePredictions() {predictionsOpen = false; controller.closeLivePredictions();},
  };
  controller.initialize(config);
  return {
    requests, filterChanges, document, timers,
    get html() {return html;}, get search() {return search;}, get details() {return details;},
    get detailOpens() {return detailOpens;}, get predictions() {return predictions;},
    get predictionHTML() {return predictionContent.innerHTML;},
    async ready() {await flush();},
    async show(subsection, {family = 'all', query = ''} = {}) {
      controller.show({section: 'benchmark', subsection, family, query, mode: subsection === 'live' ? 'live' : 'backtest'});
      await flush();
    },
    async family(value) {
      assert.ok(selectedFamily, 'The contract selector remains reachable');
      selectedFamily.value = value;
      await listeners.get('change')({target: selectedFamily}); await flush();
    },
    async query(value, event = {}) {
      assert.ok(search, 'The question search remains reachable');
      search.value = value; search.focus(); search.setSelectionRange(2, 5, 'forward');
      await listeners.get('input')({target: search, ...event}); await flush();
    },
    async composition(name, value) {
      search.value = value; search.focus();
      await listeners.get(name)({target: search}); await flush();
    },
    async click(selector, dataset = {}) {
      await listeners.get('click')({target: {closest(candidate) {return candidate === selector ? {dataset} : null;}}});
      await flush();
    },
    async next() {await this.click('[data-research-action]', {researchAction: 'next'});},
    async tickCatalog() {timers.find(item => item.milliseconds === 8000).callback(); await flush();},
  };
}

function renderedRows(html, kind = 'backtest') {
  return new Map([...html.matchAll(new RegExp(`<tr\\b[^>]*data-${kind}-case="([^"]+)"[^>]*>([\\s\\S]*?)</tr>`, 'g'))]
    .map(([, identifier, markup]) => [identifier, markup]));
}
function count(html) {
  return html.match(/<p\b[^>]*data-benchmark-filter-count[^>]*>([\s\S]*?)<\/p>/)?.[1].replace(/<[^>]+>/g, '').trim();
}

export async function testBenchmarkNavigationUI() {
  const payload = fixture(), test = harness(payload);
  await test.ready(); await test.show('backtest-v1');
  assert.equal(count(test.html), '90 / 90 题');
  assert.ok(test.html.includes('回测 v1') && test.html.includes('id="benchmark-family-filter"') && test.html.includes('id="benchmark-question-search"'));
  const reached = new Set();
  for (let page = 1; page <= 6; page++) {
    const rows = renderedRows(test.html);
    assert.equal(rows.size, 15);
    rows.forEach((_, identifier) => {assert.equal(reached.has(identifier), false); reached.add(identifier);});
    if (page < 6) await test.next();
  }
  assert.equal(reached.size, 90, 'All frozen questions remain reachable exactly once across pagination');

  await test.family('SC');
  assert.equal(count(test.html), '30 / 90 题');
  assert.ok(test.html.includes('显示 1–15 / 30 题'));
  assert.ok([...renderedRows(test.html).keys()].every(identifier => identifier.startsWith('bt-SC-')));
  assert.ok([...renderedRows(test.html).values()].every(markup => markup.includes('元 / 桶')));
  await test.next(); assert.ok(test.html.includes('显示 16–30 / 30 题'));
  await test.query('2026-09-22');
  assert.equal(count(test.html), '2 / 90 题');
  assert.deepEqual([...renderedRows(test.html).keys()], ['bt-SC-21', 'bt-SC-22']);
  assert.ok(test.html.includes('显示 1–2 / 2 题'), 'Typing a date resets an old pagination offset');
  assert.equal(test.document.activeElement, test.search);
  assert.deepEqual([test.search.selectionStart, test.search.selectionEnd], [2, 5]);
  await test.tickCatalog();
  assert.equal(count(test.html), '2 / 90 题');
  assert.equal(test.search.value, '2026-09-22', 'Catalog refresh preserves the entered search');
  assert.equal(test.document.activeElement, test.search);

  await test.query('BT-sc-29');
  assert.equal(count(test.html), '1 / 90 题');
  assert.deepEqual([...renderedRows(test.html).keys()], ['bt-SC-29']);
  assert.ok(test.html.includes('&lt;script&gt;fixture()&lt;/script&gt;') && !test.html.includes('<script>'));
  await test.click('[data-benchmark-clear-search]');
  assert.equal(count(test.html), '30 / 90 题', 'Clearing search retains the chosen contract');
  assert.equal(test.search.value, '');
  await test.family('all'); assert.equal(count(test.html), '90 / 90 题');
  await test.query('<script>unknown-query()</script>');
  assert.equal(count(test.html), '0 / 90 题');
  assert.equal(renderedRows(test.html).size, 0);
  assert.ok(test.html.includes('没有匹配的题目') && test.html.includes('data-benchmark-reset-filters'));
  assert.ok(test.html.includes('&lt;script&gt;unknown-query()&lt;/script&gt;') && !test.html.includes('<script>'));
  await test.click('[data-benchmark-reset-filters]');
  assert.equal(count(test.html), '90 / 90 题');
  assert.ok(test.html.includes('显示 1–15 / 90 题'));

  await test.composition('compositionstart', '库存');
  await test.query('库存', {isComposing: true});
  assert.equal(count(test.html), '90 / 90 题', 'IME drafts do not prematurely replace the visible results');
  await test.composition('compositionend', '库存');
  assert.equal(count(test.html), '1 / 90 题');
  assert.deepEqual([...renderedRows(test.html).keys()], ['bt-SC-05']);
  await test.click('[data-benchmark-reset-filters]');
  assert.equal(test.requests.filter(item => item.path.startsWith('/api/benchmark/questions?')).length, 1,
    'Contract, query, clearing and pagination reuse the same all-family read');

  await test.show('memory-leakage');
  assert.equal((test.html.match(/data-memory-case=/g) || []).length, 10);
  assert.ok(test.html.includes('独立参考答案 1') && test.html.includes('下载题库 JSON'));
  assert.ok(!test.html.includes('benchmark-question-search'), 'Memory keeps its independent month-based review');

  await test.show('live');
  let liveRows = renderedRows(test.html, 'live');
  assert.equal(liveRows.size, 6);
  assert.equal(count(test.html), '6 / 6 题');
  for (const family of families) {
    const current = liveRows.get(`live-${family}-current`), old = liveRows.get(`live-${family}-old`);
    assert.ok(current.includes('data-live-countdown'), `${family}'s newest question has its own deadline countdown`);
    assert.ok(!old.includes('data-live-countdown'), 'Older rows do not acquire a current countdown');
    const item = payload.live.find(item => item.case_id === `live-${family}-current`);
    assert.ok(current.includes(`data-live-question-details="${item.case_id}"`) && current.includes(`data-live-dataset="${item.dataset_id}"`));
  }
  assert.equal((test.html.match(/\bdata-live-countdown(?:=|\s|>)/g) || []).length, 3);
  await test.query('live-SC-old');
  assert.equal(count(test.html), '1 / 6 题');
  assert.ok(!test.html.includes('data-live-countdown'), 'Searching an old question does not promote it to latest');
  await test.click('[data-benchmark-clear-search]');
  await test.family('SC'); assert.equal(count(test.html), '2 / 6 题');
  await test.family('all'); assert.equal(count(test.html), '6 / 6 题');

  const beforeInvalid = test.detailOpens;
  const sc = payload.live.find(item => item.case_id === 'live-SC-current');
  await test.click('[data-live-question-details]', {liveQuestionDetails: sc.case_id, liveDataset: 'wrong-version'});
  assert.equal(test.detailOpens, beforeInvalid, 'A case id alone cannot select another frozen dataset');
  for (const family of families) {
    const item = payload.live.find(item => item.case_id === `live-${family}-current`);
    await test.click('[data-live-question-details]', {liveQuestionDetails: item.case_id, liveDataset: item.dataset_id});
    assert.ok(test.details.includes(`LIVE_BODY_${family}_CURRENT`));
    for (const other of families.filter(other => other !== family)) assert.ok(!test.details.includes(`LIVE_BODY_${other}_CURRENT`));
    await test.click('[data-live-predictions]', {livePredictions: item.case_id, liveDataset: item.dataset_id});
    assert.ok(test.predictions.includes(`${family} 主连`) && test.predictionHTML.includes('暂无模型预测记录'));
    assert.equal(test.requests.at(-1).path,
      `/api/benchmark?mode=live&kind=predictions&dataset_id=${item.dataset_id}&case_id=${item.case_id}`);
  }
  const reads = test.requests.filter(item => item.path.startsWith('/api/benchmark/questions?')).map(item => item.path);
  assert.deepEqual(reads, ['/api/benchmark/questions?mode=backtest', '/api/benchmark/questions?mode=live&history=1']);
  assert.ok(test.filterChanges.length > 8, 'Control interactions are reported to the containing navigation state');
  assert.ok(test.requests.every(item => !item.request?.method || item.request.method === 'GET'));
  return {reachable_backtest_questions: reached.size, contract_filter: 'passed', date_and_case_search: 'passed',
    pagination_reset: 'passed', clear_and_empty: 'passed', escaping_and_ime: 'passed', preserved_memory: 'passed',
    live_family_countdowns: 3, frozen_case_binding: 'passed', read_only_requests: 'passed'};
}

if (process.argv[1] && import.meta.url === new URL(process.argv[1], 'file:').href) {
  console.log(JSON.stringify(await testBenchmarkNavigationUI()));
}
