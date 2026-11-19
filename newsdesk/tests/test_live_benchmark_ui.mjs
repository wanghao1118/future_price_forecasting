import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function harness(payload, {family = 'SC', mode = 'live', questions} = {}) {
  const source = fs.readFileSync(new URL('../static/benchmark.js', import.meta.url), 'utf8');
  let nowValue = Date.parse('2026-11-03T09:00:00Z');
  class TestDate extends Date {
    constructor(...args) {super(...(args.length ? args : [nowValue]));}
    static now() {return nowValue;}
  }
  const navigation = ['WTI', 'Brent', 'SC'].flatMap(id => ['backtest', 'live'].map(selectedMode => ({
    dataset: {benchmarkModeCount: `${id}:${selectedMode}`}, textContent: '',
  })));
  const timers=[],listeners=new Map();
  let mountedRoot=null;
  const context = {window: {}, Date:TestDate, setInterval(callback,milliseconds) {timers.push({callback,milliseconds});return timers.length;}, document: {
    hidden:false,
    addEventListener(name,callback) {listeners.set(name,callback);},
    querySelectorAll(selector) {return selector === '[data-benchmark-mode-count]' ? navigation : [];},
    querySelector(selector) {return selector==='#research-workspace'?mountedRoot:null;},
  }};
  vm.createContext(context);
  vm.runInContext(source.replace('global.OilResearch =', `global.liveBenchmarkTest = {
    setData(payload) {data=payload;},
    setView(family,selectedMode) {view={section:'benchmark',subsection:family,mode:selectedMode};page=1;},
    cache(items) {questionCache.set(questionKey(),items);},
    cacheRun(run) {runCache.set(run.run_id,{value:run,at:Date.now()});},
    html() {return benchmark();}, key() {return questionKey();},
    hasCache() {return questionCache.has(questionKey());},
    async fetch(api,force=false) {options={api,toast(){}};await loadQuestions(force);},
    async reload(api) {options={api,toast(){}};await load();},
    navigation() {updateNavigation();},
    deadline(item) {return liveDeadline(item);},
    countdown(item,now=Date.now()) {return liveCountdownMarkup(item,now);},
    status(item,latest=true) {return liveStatusMarkup(item,latest);},
    mount(element) {root=element;active=true;},
    tick() {updateLiveCountdowns();},
    renderPage() {render();},
    async initialize(api) {initialize({api,toast(){}});await Promise.resolve();},
    async action(action,api) {
      options={api,toast(){}};
      await handleClick({target:{closest(selector) {
        return selector==='[data-research-action]' ? {dataset:{researchAction:action}} : null;
      }}});
    }
  };global.OilResearch =`), context);
  const test = context.window.liveBenchmarkTest;
  test.setData(payload);test.setView(family,mode);
  if (questions !== undefined) test.cache(questions);
  return {...test, navigationElements: navigation, timers, listeners,
    mount(element) {mountedRoot=element;test.mount(element);},
    setNow(value) {nowValue=typeof value==='number'?value:Date.parse(value);},
    setHidden(hidden) {context.document.hidden=hidden;},
  };
}

function question(caseId, datasetId, input, target, extra = {}) {
  return {case_id:caseId, dataset_id:datasetId, family:'SC', contract_id:'sc-main', contract_code:'SC0',
    currency:'CNY', unit:'元 / 桶', input_date:input, target_date:target,
    current_price:510, current_bar:{open:507,high:515,low:503,close:510},
    question:`${caseId}：预测方向、收盘涨跌幅、最高价及最低价。`,
    time_windows:{timezone:'UTC',status:'ready',known:{start_utc:`${input}T01:00:00Z`,end_utc:`${input}T07:00:00Z`},
      forecast:{start_utc:`${target}T01:00:00Z`,end_utc:`${target}T07:00:00Z`}},
    forecast_deadline:'2099-01-01T00:00:00Z', published_at:`${input}T08:00:00Z`,
    versions:[{dataset_id:datasetId,case_id:caseId,published_at:`${input}T08:00:00Z`}],
    live_status:'forecast_closed',forecast_status:'forecast_closed',settlement_status:'pending',actual:null,is_current:false,
    ...extra};
}

function fixture() {
  const questions = [question('sc-old','qa-old','2026-10-29','2026-10-30'),
    question('sc-middle','qa-middle','2026-11-02','2026-11-03'),
    question('sc-current','qa-current','2026-11-03','2026-11-04',{
      is_current:true,live_status:'ready',forecast_status:'ready',
    })];
  const families = ['WTI','Brent','SC'].map(id => ({id,name:`${id} 主连`,unit:id==='SC'?'元 / 桶':'美元 / 桶',
    count:id==='SC'?3:2,settled:0,pending:id==='SC'?3:2,ready:id==='SC'?1:0}));
  const payload = {runs:[],strategies:[],benchmarks:{
    live:{dataset_id:'qa-current',series_type:'dominant_continuous',total_questions:3,ready_questions:1,
      pending_questions:3,settled_questions:0,input_end:'2026-11-03',families:families.map(item=>({...item,count:1}))},
    live_history:{mode:'live',history:true,history_revision:'history-one',series_type:'dominant_continuous',
      total_questions:7,ready_questions:1,pending_questions:7,settled_questions:0,input_start:'2026-10-29',
      input_end:'2026-11-03',target_start:'2026-10-30',target_end:'2026-11-04',families,errors:{}},
    backtest:{dataset_id:'qa-backtest',series_type:'dominant_continuous',total_questions:90,days_per_contract:30,
      families:families.map(item=>({...item,count:30})),target_start:'2026-09-11',target_end:'2026-10-30'},
  }};
  return {payload,questions};
}

function rows(html) {
  return new Map([...html.matchAll(/<tr\b[^>]*data-live-case="([^"]+)"[^>]*>([\s\S]*?)<\/tr>/g)]
    .map(([,id,markup])=>[id,markup]));
}

function evaluateButton(html) {
  const tag = html.match(/<button\b[^>]*data-research-action="evaluate"[^>]*>/)?.[0];
  return {present:!!tag,disabled:!!tag && /\bdisabled\b/.test(tag)};
}

export async function testLiveBenchmarkUI() {
  const {payload,questions}=fixture();
  const test=harness(payload,{questions});
  const html=test.html(), rendered=rows(html);
  assert.equal(rendered.size,3,'All three accumulated SC trading days must be reachable together');
  assert.ok(html.includes('累计实盘问题') && html.includes('合约范围题目'));
  const headings=[...html.match(/<thead>[\s\S]*?<\/thead>/)[0].matchAll(/<th\b[^>]*>(.*?)<\/th>/g)].map(([,text])=>text);
  assert.deepEqual(headings,['具体问题','答案','状态','模型预测']);
  assert.equal(evaluateButton(html).present,false,'The unified model runner has no single-model submit button');
  for(const [id,row] of rendered) {
    if(!row.includes('data-live-countdown'))assert.ok(row.includes('UTC+0'));
    assert.ok(!row.includes('benchmark-four-answer'),'Unsettled questions must not acquire numerical truth');
    assert.ok(!row.includes('MAE 0') && !row.includes('0.00%'),'Missing scores must never become zero');
    assert.equal((row.match(/<td\b/g)||[]).length,4);
    const item=questions.find(item=>item.case_id===id);
    assert.ok(row.includes(`data-live-predictions="${id}"`) && row.includes(`data-live-dataset="${item.dataset_id}"`),
      'Each view button binds its own canonical question and dataset');
    const questionCell=row.match(/<td class="benchmark-question">([\s\S]*?)<\/td>/)[1];
    assert.ok(questionCell.includes(`SC 主连 · 预测交易日 ${item.target_date.replaceAll('-','.')}`));
    assert.equal(questionCell.replace(/<[^>]+>/g,' ').replace(/\s+/g,' ').trim(),
      `SC 主连 · 预测交易日 ${item.target_date.replaceAll('-','.')} 查看明细`,
      'The visible question cell contains only its trading-day title and details action');
    assert.ok(questionCell.includes(`data-live-question-details="${id}"`) && questionCell.includes(`data-live-dataset="${item.dataset_id}"`));
    assert.ok(questionCell.includes('查看明细'));
    for(const removed of ['已知收盘','benchmark-time-windows','benchmark-original-question','查看发布记录','benchmark-latest-tag',item.question]) {
      assert.ok(!questionCell.includes(removed),`The compact question cell excludes ${removed}`);
    }
    assert.ok(row.includes('待结算'));
  }
  assert.ok(rendered.get('sc-middle').includes('提交截止'));
  assert.ok(rendered.get('sc-current').includes('data-live-countdown'));
  test.navigation();
  assert.equal(test.navigationElements.find(item=>item.dataset.benchmarkModeCount==='SC:live').textContent,'3');
  assert.equal(test.navigationElements.find(item=>item.dataset.benchmarkModeCount==='WTI:live').textContent,'2');
  assert.equal(test.navigationElements.find(item=>item.dataset.benchmarkModeCount==='SC:backtest').textContent,'30');

  const settled=structuredClone(questions);
  settled[0].actual={direction:'up',return_pct:1.25,high:523.6,low:505.2,target_date:'2026-10-30'};
  settled[0].actual_target_date='2026-10-30';settled[0].settlement_status='settled';
  const settledRow=rows(harness(payload,{questions:settled}).html()).get('sc-old');
  for(const value of ['benchmark-four-answer','+1.25%','523.60','505.20','已结算']) assert.ok(settledRow.includes(value));
  assert.ok(!settledRow.match(/<td data-live-answer>([\s\S]*?)<\/td>/)[1].includes('高 / 低：'));

  const historicalOnly=structuredClone(questions.slice(0,2));
  historicalOnly[0].live_status='ready';historicalOnly[0].forecast_status='ready';
  assert.equal(evaluateButton(harness(payload,{questions:historicalOnly}).html()).present,false);
  const closed=structuredClone(questions);
  closed[2].live_status=closed[2].forecast_status='forecast_closed';
  assert.equal(evaluateButton(harness(payload,{questions:closed}).html()).present,false);

  const malicious=structuredClone(questions);
  Object.assign(malicious[0],{case_id:'<img src=x onerror=alert(1)>',question:'<script>alert(2)</script>',
    contract_code:'<b>SC</b>',unit:'<img src=x>',published_at:'<script>time</script>'});
  const escaped=harness(payload,{questions:malicious}).html();
  assert.ok(!escaped.includes('<script>') && !escaped.includes('<img src=x') && !escaped.includes('<b>SC</b>'));
  assert.ok(!escaped.includes('alert(2)'), 'The full question body is loaded only in its detail dialog');

  const requests=[];
  const fetchTest=harness(payload);
  assert.equal(rows(fetchTest.html()).size,0,'Unloaded cumulative history never falls back to the current single question');
  await fetchTest.fetch(async path=>{requests.push(path);return {items:questions};});
  assert.deepEqual(requests,['/api/benchmark/questions?mode=live&history=1']);
  assert.equal(rows(fetchTest.html()).size,3);
  const oldKey=fetchTest.key();
  const next=structuredClone(payload);next.benchmarks.live_history.history_revision='history-two';
  fetchTest.setData(next);
  assert.notEqual(fetchTest.key(),oldKey,'A historical revision must invalidate the rendered cache even when current dataset is unchanged');
  assert.equal(fetchTest.hasCache(),false);
  await fetchTest.fetch(async path=>{requests.push(path);return {items:questions.slice(1)};});
  assert.equal(rows(fetchTest.html()).size,2);
  const reloaded=harness(payload,{questions});
  await reloaded.reload(async path=>{assert.equal(path,'/api/research');return next;});
  assert.equal(reloaded.hasCache(),false,'Catalog refresh discards old history when its revision changes');

  const failed=harness(payload);
  await failed.fetch(async()=>{throw new Error('历史读取失败 <script>');});
  const failedHtml=failed.html();
  assert.ok(failedHtml.includes('历史读取失败 &lt;script&gt;') && failedHtml.includes('data-research-action="retry-questions"'));
  await failed.fetch(async()=>({items:questions}),true);
  assert.equal(rows(failed.html()).size,3);assert.ok(!failed.html().includes('历史读取失败'));
  const malformed=harness(payload);
  await malformed.fetch(async()=>({items:{case_id:'wrong-shape'}}));
  assert.equal(malformed.hasCache(),false,'Malformed history is not cached as an empty successful result');
  assert.ok(malformed.html().includes('data-research-action="retry-questions"'));

  const emptyPayload=structuredClone(payload);
  emptyPayload.benchmarks.live_history={...emptyPayload.benchmarks.live_history,total_questions:0,ready_questions:0,
    pending_questions:0,families:[],history_revision:'empty'};
  const empty=harness(emptyPayload,{questions:[]});
  assert.equal(rows(empty.html()).size,0);assert.ok(evaluateButton(empty.html()).disabled || !evaluateButton(empty.html()).present);
  const withoutCurrent=structuredClone(payload);
  withoutCurrent.benchmarks.live={...withoutCurrent.benchmarks.live,dataset_id:null,total_questions:0,ready_questions:0};
  const retained=harness(withoutCurrent),retainedRequests=[];
  await retained.fetch(async path=>{retainedRequests.push(path);return {items:questions};});
  assert.equal(retainedRequests.length,1,'Retained history remains fetchable even when the current dataset is empty');
  assert.equal(rows(retained.html()).size,3);
  const missingHistory=structuredClone(payload);delete missingHistory.benchmarks.live_history;
  assert.equal(rows(harness(missingHistory).html()).size,0,'Older current-only catalog data is not presented as accumulated history');

  const backtestQuestion={...questions[0],case_id:'backtest-case',mode:'backtest',actual:{direction:'down',return_pct:-2.5,high:510,low:490}};
  const backtest=harness(payload,{mode:'backtest'}), backtestRequests=[];
  await backtest.fetch(async path=>{backtestRequests.push(path);return {items:[backtestQuestion]};});
  assert.deepEqual(backtestRequests,['/api/benchmark/questions?mode=backtest']);
  assert.ok(backtest.html().includes('真实答案') && backtest.html().includes('-2.50%'));
  assert.ok(backtest.html().includes('高 / 低：元 / 桶'),'The backtest answer keeps its original unit caption');
  assert.equal(evaluateButton(backtest.html()).present,false);

  return {accumulated_sc_questions:rendered.size,question_bound_buttons:'passed',truth_isolation:'passed',
    navigation_counts:'passed',history_fetch_and_retry:'passed',revision_invalidation:'passed',
    four_columns:'passed',backtest_regression:'passed',escaping:'passed'};
}

export async function testLiveCountdownUI() {
  const {payload,questions}=fixture();
  const latest=questions.at(-1);
  latest.time_windows.forecast.start_utc='2026-11-03T13:00:00Z';
  // A stale old deadline must not override the forecast-start-minus-30m rule.
  latest.forecast_deadline='2026-11-03T13:00:00Z';
  const test=harness(payload,{questions});
  test.setNow('2026-11-03T12:29:58Z');
  assert.equal(new Date(test.deadline(latest)).toISOString(),'2026-11-03T12:30:00.000Z');
  const initial=test.html(), rendered=rows(initial);
  assert.equal((initial.match(/\bdata-live-countdown(?:=|\s|>)/g)||[]).length,1,
    'Only the latest trading-day question owns a live countdown');
  assert.ok(rendered.get('sc-current').includes('data-live-countdown') && rendered.get('sc-current').includes('00:00:02'));
  assert.ok(!rendered.get('sc-old').includes('data-live-countdown') && !rendered.get('sc-middle').includes('data-live-countdown'));
  assert.equal(evaluateButton(initial).present,false);
  test.setNow('2026-11-03T12:29:59Z');
  assert.ok(test.countdown(latest).includes('00:00:01'),'Countdown follows elapsed wall time to the second');
  test.setNow('2026-11-03T12:29:59.999Z');
  assert.ok(test.countdown(latest).includes('00:00:01'),'A remaining millisecond is not shown as an early zero');
  test.setNow('2026-11-03T12:30:00Z');
  const expired=test.html();
  const expiredRow=rows(expired).get('sc-current');
  assert.ok(expiredRow.includes('提交截止') && expiredRow.includes('2026-11-03 12:30 UTC+0'));
  assert.ok(!expiredRow.includes('data-live-countdown') && !expiredRow.includes('00:00:00'),
    'An expired question displays its UTC cutoff time instead of a zero countdown');
  test.setNow('2026-11-03T15:00:00Z');
  assert.ok(test.status(latest).includes('提交截止') && !test.status(latest).includes('data-live-countdown'));

  const crossing=structuredClone(latest);
  crossing.time_windows.forecast.start_utc='2026-11-04T00:15:00Z';
  crossing.forecast_deadline='2026-11-04T00:15:00Z';
  assert.equal(new Date(test.deadline(crossing)).toISOString(),'2026-11-03T23:45:00.000Z',
    'Subtracting the buffer crosses UTC dates correctly');
  test.setNow('2026-11-03T23:44:59Z');
  assert.ok(test.countdown(crossing).includes('00:00:01'));
  crossing.time_windows.forecast.start_utc='2026-11-04T00:15:00+08:00';
  assert.equal(new Date(test.deadline(crossing)).toISOString(),'2026-11-03T15:45:00.000Z',
    'Explicit non-UTC offsets are interpreted as instants, independent of the host timezone');
  test.setNow('2026-11-03T15:44:59Z');
  assert.ok(test.countdown(crossing).includes('00:00:01'));
  const unzoned=structuredClone(latest);
  unzoned.time_windows.forecast.start_utc='2026-11-03T13:00:00';
  unzoned.forecast_deadline='2026-11-03T12:30:00';
  assert.ok(!Number.isFinite(test.deadline(unzoned)),'A missing timezone is never interpreted using the local computer clock');
  assert.ok(test.countdown(unzoned).includes('截止时间待核实'));

  const lockedPayload=structuredClone(payload);
  lockedPayload.runs=[{run_id:'locked-current',mode:'live',dataset_id:'qa-current',family:'SC',
    status:'awaiting_settlement',metrics:[{family:'SC',total:1,count:0,pending:1}]}];
  const locked=harness(lockedPayload,{questions});
  locked.setNow('2026-11-03T12:30:01Z');
  const lockedRow=rows(locked.html()).get('sc-current');
  assert.ok(lockedRow.includes('提交截止') && lockedRow.includes('data-live-predictions="sc-current"'),
    'A closed question still exposes its saved model predictions');
  const completed=structuredClone(latest);
  completed.actual={direction:'up',return_pct:1,high:520,low:505};
  assert.ok(test.status(completed).includes('已结算') && !test.status(completed).includes('data-live-countdown'));

  // Minimal DOM doubles record which nodes the registered timer mutates. The
  // open publication details must survive both timer ticks and catalog renders.
  const timerTest=harness(payload,{questions});
  timerTest.setNow('2026-11-03T12:29:58Z');
  const deadline=timerTest.deadline(latest);
  const value={textContent:'00:00:02'},caption={textContent:'预测开始前 30 分钟'};
  const detail={dataset:{caseKey:'sc-current:publication'},open:true};
  const statusCell={innerHTML:'countdown'};
  const row={querySelector(selector) {return selector==='[data-live-status]'?statusCell:null;}};
  const classes=new Map();
  const countdown={dataset:{liveCountdown:new Date(deadline).toISOString()},
    classList:{toggle(name,state) {classes.set(name,state);}},
    querySelector(selector) {return selector==='[data-countdown-value]'?value:selector==='[data-countdown-status]'?caption:null;},
    closest(selector) {return selector==='[data-live-case]'?row:selector==='[data-live-status]'?statusCell:null;}};
  const readyCount={textContent:'1'};
  let fullWrites=0,markup='';
  const root={
    addEventListener() {},
    querySelectorAll(selector) {
      if(selector==='[data-live-countdown]')return [countdown];
      if(selector==='details[data-case-key][open]')return detail.open?[detail]:[];
      if(selector==='details[data-case-key]')return [detail];
      return [];
    },
    querySelector(selector) {
      return selector==='[data-live-ready-count]'?readyCount:null;
    },
    set innerHTML(value) {fullWrites++;markup=value;},
    get innerHTML() {return markup;},
  };
  timerTest.mount(root);
  await timerTest.initialize(async path=>{assert.equal(path,'/api/research');return payload;});
  await new Promise(resolve=>setImmediate(resolve));
  const timer=timerTest.timers.find(item=>item.milliseconds===1000);
  assert.ok(timer,'A one-second callback is registered for the live countdown');
  const writesBeforeTick=fullWrites;
  timerTest.setNow('2026-11-03T12:29:59Z');timer.callback();
  assert.equal(value.textContent,'00:00:01');
  assert.equal(fullWrites,writesBeforeTick,'A one-second update does not rebuild the whole workspace');
  assert.equal(detail.open,true);
  const catalogTimer=timerTest.timers.find(item=>item.milliseconds===8000);
  assert.ok(catalogTimer,'Existing catalog polling remains scheduled');
  catalogTimer.callback();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(detail.open,true,'The regular catalog render preserves expanded details');
  assert.ok(root.innerHTML.includes('00:00:01'),'Catalog rendering initializes the countdown from current wall time');
  const writesBeforeHidden=fullWrites;
  timerTest.setHidden(true);timerTest.setNow('2026-11-03T12:35:00Z');timer.callback();
  assert.equal(value.textContent,'00:00:01','Hidden pages do not churn countdown DOM');
  timerTest.setHidden(false);
  const visibility=timerTest.listeners.get('visibilitychange');
  assert.ok(visibility,'Returning to the page schedules an immediate wall-clock update');
  visibility();
  assert.ok(statusCell.innerHTML.includes('提交截止') && statusCell.innerHTML.includes('2026-11-03 12:30 UTC+0'));
  assert.ok(!statusCell.innerHTML.includes('data-live-countdown') && !statusCell.innerHTML.includes('00:00:00'));
  assert.equal(readyCount.textContent,'0');
  assert.equal(fullWrites,writesBeforeHidden,'Resuming a hidden page updates local nodes without replacing expanded content');
  assert.equal(detail.open,true);

  return {latest_only:'passed',forecast_start_minus_30m:'passed',per_second_wall_clock:'passed',
    expired_shows_utc_cutoff:'passed',utc_date_and_offset:'passed',saved_prediction_access:'passed',
    local_dom_tick:'passed',poll_preserves_details:'passed',background_resume:'passed'};
}

if(process.argv[1] && import.meta.url===new URL(process.argv[1],'file:').href) {
  console.log(JSON.stringify({...await testLiveBenchmarkUI(),countdown:await testLiveCountdownUI()}));
}
