import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const flush = async () => {
  for (let index = 0; index < 3; index++) await new Promise(resolve => setImmediate(resolve));
};
const decode = value => String(value).replace(/&quot;/g, '"').replace(/&#39;/g, "'")
  .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const count = html => html.match(/data-benchmark-filter-count[^>]*>([^<]+)</)?.[1].trim();
const rows = html => [...html.matchAll(/data-backtest-case="([^"]+)"/g)].map(match => match[1]);

function fixture({missingV2 = false, sharedDatasetId = false,augustIncluded = false} = {}) {
  const families = ['WTI', 'Brent', 'SC'];
  const v2Counts = augustIncluded ? [41,42,41] : [18,19,20];
  const v2Start = augustIncluded ? '2026-08-01' : '2026-09-01';
  const day = (profile,index) => new Date(Date.parse(`${profile === 'v2' ? v2Start : '2026-09-01'}T00:00:00Z`) + index * 86400000).toISOString().slice(0,10);
  const makeQuestions = (profile, counts) => families.flatMap((family, familyIndex) =>
    Array.from({length: counts[familyIndex]}, (_, index) => ({
      case_id: `${profile}-${family}-${String(index + 1).padStart(2, '0')}`,
      family, mode: 'backtest', contract_id: `${family.toLowerCase()}-main`,
      contract_code: {WTI: 'CL.1:NYM', Brent: 'IB.1:IEU', SC: 'SC0'}[family],
      input_date: day(profile,index),
      target_date: day(profile,index + 1),
      question: `${profile} ${family} 价格走势，第 ${index + 1} 题。`,
      unit: family === 'SC' ? '元 / 桶' : '美元 / 桶', current_price: 70,
      current_bar: {open: 69, high: 73, low: 68, close: 70},
      actual: {direction: 'up', return_pct: 1, high: 74, low: 69},
    })));
  const v1 = makeQuestions('v1', [30, 30, 30]), v2 = makeQuestions('v2', v2Counts);
  const summary = (profile, counts) => ({
    dataset_id: sharedDatasetId ? 'qa-shared' : `qa-${profile}`, profile,
    series_type: 'dominant_continuous', total_questions: counts.reduce((sum, value) => sum + value, 0),
    families: families.map((id, index) => ({id, count: counts[index], unit: id === 'SC' ? '元 / 桶' : '美元 / 桶'})),
    input_start: profile === 'v2' ? v2Start : '2026-09-01', input_end: profile === 'v2' ? '2026-09-29' : '2026-09-30',
    target_start: day(profile,1), target_end: profile === 'v2' ? '2026-09-30' : '2026-10-01',
    ...(profile === 'v2' ? {date_range: {start: v2Start, end: '2026-09-30', inclusive: true,
      fields: ['input_date', 'target_date']}, expected_questions: counts.reduce((sum,value) => sum + value,0)} : {days_per_contract: 30}),
  });
  const catalog = {runs: [], strategies: [], experiments: [], benchmarks: {
    backtest: summary('v1', [30, 30, 30]), live: {}, live_history: {},
    ...(!missingV2 ? {backtest_v2: summary('v2', v2Counts)} : {}),
  }};
  if (!missingV2) catalog.benchmarks.backtest_v2.families.forEach((family, index) => {
    family.contracts = [{selection: {
      candidate_pairs: v2Counts[index] + (augustIncluded ? [0,0,0] : [2,2,0])[index], eligible_pairs: v2Counts[index],
      excluded_counts: {data_gap_unverified: index === 0 && !augustIncluded ? 1 : 0,
        unconfirmed_time_windows: (augustIncluded ? [0,0,0] : [1,2,0])[index], invalid_ohlc: 0, zero_baseline: 0},
      unverified_missing_weekdays: index === 0 && !augustIncluded ? ['2026-09-07'] : [],
      ...(index === 0 && augustIncluded ? {
        no_separate_trade_dates:['2026-09-07'],
        trading_date_assignments:[{date:'2026-09-07',assigned_trading_date:'2026-09-08',
          reason:'verified_exchange_calendar_combined_trade_date',
          source_url:'https://www.cmegroup.com/tools-information/holiday-calendar.html#tradeDate=2026-09-08'}],
      } : {}),
      excluded_pairs: index === 0 && !augustIncluded ? [
        {input_date: '2026-09-04', target_date: '2026-09-08', reason: 'data_gap_unverified', missing_weekdays: ['2026-09-07']},
        {input_date: '2026-09-08', target_date: '2026-09-09', reason: 'unconfirmed_time_windows'},
      ] : index === 1 && !augustIncluded ? [
        {input_date: '2026-09-04', target_date: '2026-09-07', reason: 'unconfirmed_time_windows'},
        {input_date: '2026-09-07', target_date: '2026-09-08', reason: 'unconfirmed_time_windows'},
      ] : [],
    }}];
  });
  return {catalog, v1, v2};
}

function harness(payload) {
  const handlers = new Map(), requests = [], filterChanges = [], notices = [];
  let html = '', search = null, family = null;
  const document = {hidden: false, activeElement: null, addEventListener() {}, querySelectorAll() {return [];},
    querySelector(selector) {return selector === '#research-workspace' ? root : root.querySelector(selector);}};
  const control = (id, value) => ({id, value, selectionStart: 0, selectionEnd: 0,
    focus() {document.activeElement = this;}, setSelectionRange(start, end) {this.selectionStart = start;this.selectionEnd = end;}});
  const root = {addEventListener(name, handler) {handlers.set(name, handler);}, querySelectorAll() {return [];},
    querySelector(selector) {return selector === '#benchmark-question-search' ? search : selector === '#benchmark-family-filter' ? family : null;},
    get innerHTML() {return html;}, set innerHTML(value) {
      html = value;
      const input = html.match(/<input\b[^>]*id="benchmark-question-search"[^>]*>/)?.[0];
      search = input ? control('benchmark-question-search', decode(input.match(/\bvalue="([^"]*)"/)?.[1] || '')) : null;
      const select = html.match(/<select\b[^>]*id="benchmark-family-filter"[^>]*>([\s\S]*?)<\/select>/)?.[1];
      family = select ? control('benchmark-family-filter', select.match(/<option value="([^"]+)" selected/)?.[1] || 'all') : null;
    }};
  const context = {window: {}, document, setInterval() {return 1;}};
  vm.createContext(context);
  // The public controller and delegated events run the production source as-is.
  vm.runInContext(fs.readFileSync(new URL('../static/benchmark.js', import.meta.url), 'utf8'), context);
  const controller = context.window.OilResearch;
  controller.initialize({
    async api(path, request) {
      requests.push({path, request});
      const url = new URL(path, 'http://fixture.local');
      if (url.pathname === '/api/research') return structuredClone(payload.catalog);
      if (url.pathname === '/api/benchmark/questions') {
        assert.equal(request?.method || 'GET', 'GET');
        assert.equal(url.searchParams.get('mode'), 'backtest');
        assert.equal(url.searchParams.has('family'), false);
        assert.equal(url.searchParams.has('history'), false);
        const items = url.searchParams.get('profile') === 'v2' ? payload.v2 : payload.v1;
        return {items: structuredClone(items), total: items.length};
      }
      if (url.pathname === '/api/benchmark/build') {
        assert.equal(request.method, 'POST');
        const body = JSON.parse(request.body);
        assert.deepEqual(body, {mode: 'backtest', profile: 'v2', refresh: true});
        assert.equal(Object.hasOwn(body, 'days'), false, 'The fixed v2 range does not pass v1 rolling-day parameters');
        const before = JSON.stringify(payload.catalog.benchmarks.backtest);
        payload.catalog.benchmarks.backtest_v2 = {...payload.catalog.benchmarks.backtest_v2, dataset_id: 'qa-v2-rebuilt'};
        assert.equal(JSON.stringify(payload.catalog.benchmarks.backtest), before);
        return structuredClone(payload.catalog.benchmarks.backtest_v2);
      }
      throw new Error(`Unexpected API call: ${path}`);
    },
    onBenchmarkFilterChange(value) {filterChanges.push({...value});},
    toast(message, failure) {notices.push({message, failure});if (failure) throw new Error(message);},
  });
  return {requests, filterChanges, notices, get html() {return html;},range() {return controller.getBenchmarkDateRange();},
    async ready() {await flush();},
    async show(subsection, settings = {}) {
      controller.show({section: 'benchmark', subsection, family: 'all', query: '', mode: 'backtest', ...settings});
      await flush();
    },
    async select(value) {family.value = value;handlers.get('change')({target: family});await flush();},
    async search(value) {search.value = value;search.focus();handlers.get('input')({target: search});await flush();},
    async click(selector, dataset = {}) {
      await handlers.get('click')({target: {closest(candidate) {return candidate === selector ? {dataset} : null;}}});
      await flush();
    },
  };
}

function testAppSubtitleRange() {
  let range = null;
  const element = {addEventListener() {}};
  const context = {localStorage:{getItem() {return null;},setItem() {}},
    document:{querySelector() {return element;},querySelectorAll() {return [];},addEventListener() {}},
    window:{OilResearch:{getBenchmarkDateRange:() => range},scrollTo() {},addEventListener() {}}};
  vm.createContext(context);
  const source = fs.readFileSync(new URL('../static/app.js',import.meta.url),'utf8');
  vm.runInContext(source.replace(/\ninitialize\(\);\s*$/, '\nwindow.rangeSubtitleTest = benchmarkV2Subtitle;\n'),context);
  assert.ok(context.window.rangeSubtitleTest().includes('2026-08-01 至 2026-09-30'),'An unbuilt v2 defaults to the expanded August–September range');
  range = {start:'2026-09-01',end:'2026-09-30',inclusive:true};
  assert.ok(context.window.rangeSubtitleTest().includes('2026-09-01 至 2026-09-30'),'The app header preserves a frozen September dataset range');
  range = {start:'2026-08-01',end:'2026-09-30',inclusive:true};
  assert.ok(context.window.rangeSubtitleTest().includes('2026-08-01 至 2026-09-30'));
}

export async function testBenchmarkV2UI() {
  const payload = fixture(), test = harness(payload), frozenV1 = JSON.stringify(payload.catalog.benchmarks.backtest);
  await test.ready();await test.show('backtest-v1');
  assert.equal(count(test.html), '90 / 90 题');
  assert.ok(rows(test.html).every(identifier => identifier.startsWith('v1-')));
  assert.ok(!test.html.includes('benchmark-v2-coverage'), 'Selection coverage is exclusive to the v2 page');
  await test.show('backtest-v2');
  assert.equal(count(test.html), '57 / 57 题');
  assert.ok(rows(test.html).every(identifier => identifier.startsWith('v2-')));
  assert.ok(test.html.includes('2026 年 9 月') && !test.html.includes('2026 年 8—9 月'), 'A historical September-only dataset is labelled from its own manifest');
  for (const text of ['回测 v2', '已知交易日限制', '预测交易日限制', '2026.09.01', '2026.09.30', '含首尾', 'UTC+0', 'WTI 18', 'Brent 19', 'SC 20']) {
    assert.ok(test.html.includes(text), `The v2 scope and actual counts include ${text}`);
  }
  assert.ok(!test.html.includes('每品类回测窗口'), 'The v2 summary does not describe a rolling 30-day quota');
  const coverage = test.html.match(/<section class="benchmark-v2-coverage"[\s\S]*?<\/section>/)?.[0];
  assert.ok(coverage?.includes('题量核验') && coverage.includes('查看 4 组剔除明细'));
  assert.ok(coverage.includes('WTI 主连') && coverage.includes('18 题 · 候选 20 组 · 剔除 2 组'));
  assert.ok(coverage.includes('Brent 主连') && coverage.includes('19 题 · 候选 21 组 · 剔除 2 组'));
  assert.ok(coverage.includes('SC 主连') && coverage.includes('20 题 · 候选 20 组 · 剔除 0 组'));
  assert.ok(coverage.includes('未核实缺失工作日：2026-09-07'));
  assert.ok(coverage.includes('行情缺口未核实 1组') && coverage.includes('特殊交易时段待核实 2组'));
  assert.equal([...coverage.matchAll(/data-v2-exclusion-reason=/g)].length, 4);
  assert.ok(test.requests.some(item => item.path === '/api/benchmark/questions?mode=backtest&profile=v2'));
  assert.ok(test.requests.some(item => item.path === '/api/benchmark/questions?mode=backtest'));

  await test.select('SC');assert.equal(count(test.html), '20 / 57 题');
  assert.ok(rows(test.html).every(identifier => identifier.startsWith('v2-SC-')));
  await test.search('V2-sc-19');assert.equal(count(test.html), '1 / 57 题');
  assert.deepEqual(rows(test.html), ['v2-SC-19']);
  await test.click('[data-benchmark-clear-search]');assert.equal(count(test.html), '20 / 57 题');
  await test.show('backtest-v1');assert.equal(count(test.html), '90 / 90 题');
  assert.ok(rows(test.html).every(identifier => identifier.startsWith('v1-')));
  await test.show('backtest-v2');assert.equal(count(test.html), '57 / 57 题');
  const readsBeforeBuild = test.requests.filter(item => item.path.includes('/api/benchmark/questions')).length;
  assert.equal(readsBeforeBuild, 2, 'Switching profiles reuses each independent cached dataset');
  await test.click('[data-research-action]', {researchAction: 'build'});
  const builds = test.requests.filter(item => item.path === '/api/benchmark/build');
  assert.equal(builds.length, 1);assert.equal(JSON.stringify(payload.catalog.benchmarks.backtest), frozenV1);
  assert.equal(count(test.html), '57 / 57 题');assert.ok(test.html.includes('qa-v2-rebuilt'));
  assert.ok(test.notices.some(item => item.message.includes('回测 v2')));

  const missing = harness(fixture({missingV2: true}));await missing.ready();await missing.show('backtest-v2');
  assert.equal(count(missing.html), '0 / 0 题');assert.ok(missing.html.includes('尚未构建回测 v2'));
  assert.ok(missing.html.includes('2026.08.01') && missing.html.includes('2026-08-01 至 2026-09-30'));
  assert.ok(missing.html.includes('2026 年 8—9 月'));
  assert.ok(!missing.html.includes('v1-WTI-') && !missing.html.includes('90 / 90'));
  assert.equal(missing.requests.filter(item => item.path.includes('/api/benchmark/questions')).length, 0,
    'An unbuilt v2 never fetches or displays the v1 dataset');
  const noSelectionPayload = fixture();
  noSelectionPayload.catalog.benchmarks.backtest_v2.families.forEach(family => {delete family.contracts;});
  const noSelection = harness(noSelectionPayload);await noSelection.ready();await noSelection.show('backtest-v2');
  assert.equal(count(noSelection.html), '57 / 57 题');
  assert.ok(!noSelection.html.includes('benchmark-v2-coverage'), 'Missing manifest selection is not replaced with invented exclusions');

  const shared = harness(fixture({sharedDatasetId: true}));await shared.ready();
  await shared.show('backtest-v1');assert.equal(count(shared.html), '90 / 90 题');
  await shared.show('backtest-v2');assert.equal(count(shared.html), '57 / 57 题');
  assert.ok(rows(shared.html).every(identifier => identifier.startsWith('v2-')));
  assert.equal(shared.requests.filter(item => item.path.includes('/api/benchmark/questions')).length, 2,
    'Profile identity separates caches even if catalog dataset IDs are accidentally equal');
  const expanded = harness(fixture({augustIncluded:true}));await expanded.ready();await expanded.show('backtest-v2');
  assert.equal(count(expanded.html),'124 / 124 题');
  assert.ok(expanded.html.includes('2026 年 8—9 月') && expanded.html.includes('2026.08.01') && expanded.html.includes('2026.09.30'));
  assert.ok(expanded.html.includes('已知与预测交易日均在 2026-08-01 至 2026-09-30（含首尾）'));
  const correctedCoverage = expanded.html.match(/<section class="benchmark-v2-coverage"[\s\S]*?<\/section>/)?.[0];
  assert.ok(correctedCoverage.includes('41 题 · 候选 41 组 · 剔除 0 组'));
  assert.ok(correctedCoverage.includes('42 题 · 候选 42 组 · 剔除 0 组'));
  assert.ok(correctedCoverage.includes('41 题 · 候选 41 组 · 剔除 0 组'));
  assert.ok(correctedCoverage.includes('查看交易日归属依据'));
  assert.ok(!correctedCoverage.includes('2026-08-07') && !correctedCoverage.includes('2026-08-31'),
    'Brent August 7 and 31 are not retained as exclusions after their regular sessions are verified');
  assert.equal([...correctedCoverage.matchAll(/data-v2-exclusion-reason=/g)].length,0,
    'All verified candidate pairs are retained after the Brent early close and WTI trade-date assignment are confirmed');
  assert.ok(!correctedCoverage.includes('特殊交易时段待核实'),
    'Verified Brent September 7 early closing is no longer shown as an unknown special session');
  assert.ok(!correctedCoverage.includes('未核实缺失工作日：2026-09-07'),
    'The current WTI calendar no longer calls September 7 an unverified missing trading day');
  assert.ok(correctedCoverage.includes('无独立交易日 2026-09-07，归属 2026-09-08'));
  assert.ok(correctedCoverage.includes('data-v2-trading-date-assignment="2026-09-07"') && correctedCoverage.includes('交易所依据'));
  assert.equal(expanded.range().start,'2026-08-01');
  assert.equal(test.range().start,'2026-09-01','The public range getter follows each independent dataset');
  await expanded.select('SC');assert.equal(count(expanded.html),'41 / 124 题');
  await expanded.search('2026-08-15');assert.equal(count(expanded.html),'2 / 124 题');
  assert.deepEqual(rows(expanded.html),['v2-SC-14','v2-SC-15']);
  await expanded.click('[data-benchmark-clear-search]');assert.equal(count(expanded.html),'41 / 124 题');
  testAppSubtitleRange();
  return {profile_isolation: 'passed', missing_profile_no_fallback: 'passed', family_and_search: 'passed',
    exact_date_scope: 'passed', august_september_range:'passed',historical_september_range:'passed',dynamic_app_subtitle:'passed',actual_counts: 'passed', selection_exclusions: 'passed',corrected_brent_exclusions:'passed',confirmed_wti_trade_assignment:'passed',
    profile_get_and_rebuild: 'passed', v1_retained: 'passed'};
}

if (process.argv[1] && import.meta.url === new URL(process.argv[1], 'file:').href) {
  console.log(JSON.stringify(await testBenchmarkV2UI()));
}
