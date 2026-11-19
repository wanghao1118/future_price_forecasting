import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function harness(items) {
  const source=fs.readFileSync(new URL('../static/benchmark.js',import.meta.url),'utf8');
  let dialog='',opened=false,focused='',opens=0;
  const buttons=items.map(item=>({dataset:{liveQuestionDetails:item.case_id,liveDataset:item.dataset_id},
    focus(){focused=item.case_id;}}));
  const root={querySelectorAll(selector){return selector==='[data-live-question-details]'?buttons:[];}};
  const context={window:{},document:{querySelectorAll(){return [];},querySelector(){return null;}}};
  vm.createContext(context);
  vm.runInContext(source.replace('global.OilResearch =',`global.questionDetailsTest={
    configure(config,payload,items,element){options=config;data=payload;root=element;
      view={section:'benchmark',subsection:'SC',mode:'live'};questionCache.set(questionKey(),items);},
    markup:liveQuestionDetailsMarkup,open:openLiveQuestionDetails,close:closeLiveQuestionDetails,
    async click(item){await handleClick({target:{closest(selector){return selector==='[data-live-question-details]'?
      {dataset:{liveQuestionDetails:item.case_id,liveDataset:item.dataset_id}}:null;}}});}
  };global.OilResearch =`),context);
  const test=context.window.questionDetailsTest;
  const config={api(){throw new Error('Question details must read the already-loaded frozen question without fetching or evaluating');},toast(){},
    openLiveQuestionDetails(html){dialog=html;opened=true;opens++;},
    closeLiveQuestionDetails(){opened=false;test.close();}};
  test.configure(config,{runs:[],benchmarks:{live:{dataset_id:'qa-current',series_type:'dominant_continuous'},
    live_history:{history_revision:'frozen-history'}}},items,root);
  return {...test,close(){opened=false;test.close();},get dialog(){return dialog;},get opened(){return opened;},
    get focused(){return focused;},get opens(){return opens;}};
}

function fixture() {
  const period=(day,next)=>({start_utc:`${day}T13:00:00Z`,end_utc:`${next}T07:00:00Z`,
    segments:[{label:'夜盘',start_utc:`${day}T13:00:00Z`,end_utc:`${day}T18:30:00Z`},
      {label:'日盘第一段',start_utc:`${next}T01:00:00Z`,end_utc:`${next}T02:15:00Z`},
      {label:'日盘第二段',start_utc:`${next}T02:30:00Z`,end_utc:`${next}T03:30:00Z`},
      {label:'日盘第三段',start_utc:`${next}T05:30:00Z`,end_utc:`${next}T07:00:00Z`}],
    source_url:'https://exchange.example/session',calendar_status:'verified_2026'});
  return {case_id:'sc-detail',dataset_id:'qa-frozen-version-sentinel',family:'SC',input_date:'2026-11-03',target_date:'2026-11-04',
    current_price:510,current_bar:{open:507,close:510,high:515,low:503},unit:'元 / 桶',
    question:'题目甲：已知真实收盘 510，请预测四项结果。<script>不得执行</script>',
    forecast_deadline:'2026-11-03T12:30:00Z',submission_deadline_utc:'2026-11-03T12:30:00Z',
    published_at:'2026-11-03T08:34:01Z',versions:[{dataset_id:'qa-hidden-version',case_id:'sc-hidden-revision'}],
    time_windows:{timezone:'UTC',status:'ready',known:period('2026-11-02','2026-11-03'),
      forecast:period('2026-11-03','2026-11-04'),information_cutoff_utc:'2026-11-03T07:00:00Z'}};
}

export async function testLiveQuestionDetailsUI() {
  const item=fixture(),other={...structuredClone(item),case_id:'sc-other',dataset_id:'qa-other',
    target_date:'2026-11-05',question:'题目乙：属于另一个冻结交易日。'};
  const test=harness([item,other]);
  const markup=test.markup(item);
  const labels=[...markup.matchAll(/<dt\b[^>]*>(.*?)<\/dt>/g)].map(([,label])=>label);
  assert.deepEqual(labels,['已知时间','预测时间','事件截止','题目']);
  const eventField=value=>value.match(/<dt\b[^>]*>事件截止<\/dt>\s*<dd\b[^>]*>([\s\S]*?)<\/dd>/)[1];
  assert.ok(eventField(markup).includes('2026-11-03 07:00 UTC+0'));
  assert.ok(!markup.includes('<script>') && markup.includes('&lt;script&gt;不得执行&lt;/script&gt;'));
  for(const removed of ['冻结版本','查看发布记录','首次有效发布','交易所依据','交易所时段','qa-frozen-version-sentinel',
    'qa-hidden-version','sc-hidden-revision','2026-11-03 08:34','提交截止'])assert.ok(!markup.includes(removed),`The detail dialog excludes ${removed}`);
  assert.ok(markup.includes('UTC+0'));
  for(const point of ['2026-11-02 13:00','2026-11-02 18:30','2026-11-03 01:00','2026-11-03 02:15',
    '2026-11-03 02:30','2026-11-03 03:30','2026-11-03 05:30','2026-11-03 07:00',
    '2026-11-03 13:00','2026-11-04 07:00'])assert.ok(markup.includes(point),`SC segment endpoint ${point} remains inspectable`);
  assert.ok(!markup.includes('2026-11-03 12:30'),'The event cutoff is not silently replaced by the submission cutoff');

  const historical=structuredClone(item);
  historical.display_time_windows=historical.time_windows;delete historical.time_windows;
  const historicalMarkup=test.markup(historical);
  assert.ok(historicalMarkup.includes('2026-11-02 18:30') && historicalMarkup.includes('2026-11-04 07:00'),
    'Historical display-only trading windows preserve their saved actual segment endpoints');
  assert.ok(eventField(historicalMarkup).includes('待核实'),'Display-only annotations cannot invent an original event cutoff');
  const unzoned=structuredClone(item);
  unzoned.time_windows.information_cutoff_utc='2026-11-03T07:00:00';
  const unzonedMarkup=test.markup(unzoned);
  assert.ok(!unzonedMarkup.includes('2026-11-03 12:30'),'An unverifiable event timestamp must never borrow the submit cutoff');
  assert.ok(eventField(unzonedMarkup).includes('待核实'));
  const offset=structuredClone(item);
  offset.time_windows.known.segments[0].start_utc='2026-11-02T21:00:00+08:00';
  offset.time_windows.information_cutoff_utc='2026-11-03T15:00:00+08:00';
  const converted=test.markup(offset);
  assert.ok(converted.includes('2026-11-02 13:00') && eventField(converted).includes('2026-11-03 07:00 UTC+0'),
    'Explicit source offsets are converted to UTC without using the computer timezone');

  await test.click({...item,dataset_id:'wrong-dataset'});
  assert.equal(test.opened,false,'The detail button binds both dataset and case, not just the displayed case ID');
  await test.click(item);
  assert.equal(test.opened,true);assert.ok(test.dialog.includes('role="dialog"') && test.dialog.includes('aria-modal="true"'));
  assert.ok(test.dialog.includes('题目甲') && !test.dialog.includes('题目乙'));
  await test.click(other);
  assert.ok(test.dialog.includes('题目乙') && !test.dialog.includes('题目甲'),'Switching questions replaces the detail content');
  assert.equal(test.opens,2);
  test.close();assert.equal(test.opened,false);
  assert.equal(test.focused,'sc-other','Closing restores focus to the detail button for the selected frozen question');
  test.open(item);test.close();assert.equal(test.focused,'sc-detail');

  return {four_detail_fields:'passed',event_cutoff_separate:'passed',utc_segments:'passed',
    historical_windows:'passed',question_binding:'passed',close_focus:'passed',escaping:'passed'};
}

if(process.argv[1] && import.meta.url===new URL(process.argv[1],'file:').href) {
  console.log(JSON.stringify(await testLiveQuestionDetailsUI()));
}
