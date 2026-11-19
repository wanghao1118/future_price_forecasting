import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const flush=()=>new Promise(resolve=>setImmediate(resolve));

function harness() {
  const source=fs.readFileSync(new URL('../static/benchmark.js',import.meta.url),'utf8');
  let opened=false,overlay='',contentHTML='',writes=0,apiHandler=async()=>({});
  const requests=[],timers=[],contentListeners=new Map();
  const scroller={scrollLeft:29};
  const content={scrollTop:37,querySelector(selector){return selector==='.live-predictions-table-scroll'?scroller:null;},
    addEventListener(name,callback){contentListeners.set(name,callback);},
    set innerHTML(value){contentHTML=value;writes++;},get innerHTML(){return contentHTML;}};
  const root={addEventListener(){},querySelectorAll(){return [];},querySelector(){return null;},innerHTML:''};
  const document={hidden:false,querySelector(selector){return selector==='#live-predictions-content'?(opened?content:null):selector==='#research-workspace'?root:null;},
    querySelectorAll(){return [];},addEventListener(){}};
  const context={window:{},document,setInterval(callback,milliseconds){timers.push({callback,milliseconds});return timers.length;}};
  vm.createContext(context);
  vm.runInContext(source.replace('global.OilResearch =',`global.predictionsTest={
    configure(config){options=config;}, initialize(config){initialize(config);},
    table:livePredictionsTable,open:openLivePredictions,load:loadLivePredictions,
    close:closeLivePredictions,
    catalog(payload,items){data=payload;view={section:'benchmark',subsection:'SC',mode:'live'};questionCache.set(questionKey(),items);},
    async click(item){await handleClick({target:{closest(selector){return selector==='[data-live-predictions]'?
      {dataset:{livePredictions:item.case_id,liveDataset:item.dataset_id}}:null;}}});}
  };global.OilResearch =`),context);
  const test=context.window.predictionsTest;
  const config={
    async api(path,options){requests.push({path,options});assert.ok(!options?.method || options.method==='GET','Reading predictions never starts a model or writes data');return apiHandler(path,options);},
    toast(){},openLivePredictions(html){opened=true;overlay=html;},isLivePredictionsOpen(){return opened;},
    closeLivePredictions(){opened=false;test.close();},
  };
  return {...test,requests,timers,content,scroller,
    configure(api){apiHandler=api;test.configure(config);},
    async initialize(api){apiHandler=api;test.initialize(config);await flush();},
    setAPI(api){apiHandler=api;},
    close(){opened=false;test.close();},
    retry(){contentListeners.get('click')?.({target:{closest(selector){return selector==='[data-live-predictions-retry]'?{}:null;}}});},
    get html(){return contentHTML;},get overlay(){return overlay;},get writes(){return writes;},get opened(){return opened;},
  };
}

function fixture(caseId='sc-current',datasetId='qa-current',target='2026-11-04') {
  const question={family:'SC',case_id:caseId,dataset_id:datasetId,input_date:'2026-11-03',target_date:target,current_price:510,
    unit:'元 / 桶',current_bar:{open:507,close:510,high:515,low:503},actual:null};
  const predicted={direction:'up',high:520,low:505,return_pct:1.2};
  const items=Array.from({length:17},(_,index)=>({model:`model-${index+1}`,experiment_id:'Exp01',experiment_name:'Exp01',
    run_id:`run-${index+1}`,status:index===0?'predicted_pending':index===1?'success':index===2?'failed':'running',
    predicted:index<2?{...predicted,direction:index===0?'up':'down',return_pct:index===0?1.2:-.5}:null,
    actual:index===1?{direction:'down',high:519,low:504,return_pct:-1}:null,
    absolute_errors:index<2?{high:1,low:1,return_pct:.5}:null,
    submitted_at:'2026-11-03T09:00:00Z',prediction_locked_at:index<2?'2026-11-03T09:01:00Z':null,
    dataset_id:datasetId,case_id:caseId,current_price:510,unit:'元 / 桶',
    latest_attempt:index===2?{run_id:'failed-attempt',status:'failed',error:'服务暂不可用'}:null,
    time_audit:{integrity_verified:true,submitted_before_deadline:true,locked_before_deadline:index<2},
  }));
  return {mode:'live',dataset_id:datasetId,case_id:caseId,question,items,total:items.length,
    summary:{expected_models:17,predicted:2,settled:1,failed:1,waiting:14},errors:[]};
}

function modelRows(html) {
  return new Map([...html.matchAll(/<tr data-live-prediction-model="([^"]+)">([\s\S]*?)<\/tr>/g)]
    .map(([,model,markup])=>[model,markup]));
}

export async function testLivePredictionsUI() {
  const payload=fixture(),test=harness(),html=test.table(payload),rows=modelRows(html);
  assert.equal(rows.size,17,'The dialog presents every configured base model');
  assert.ok(html.includes('overview-daily-table'));
  const headings=[...html.match(/<thead>[\s\S]*?<\/thead>/)[0].matchAll(/<th\b[^>]*>([\s\S]*?)<\/th>/g)]
    .map(([,text])=>text.replace(/<small>[\s\S]*?<\/small>/g,''));
  assert.deepEqual(headings,['模型（实验）','方向','最高价','最低价','收盘涨跌幅','状态']);
  const pending=rows.get('model-1'),settled=rows.get('model-2'),failed=rows.get('model-3');
  for(const value of ['model-1(Exp01)','看涨','520.00','505.00','+1.20%'])assert.ok(pending.includes(value));
  assert.ok(!pending.includes('MAE '),'A locked prediction without truth has no numerical error, even if stale error fields exist');
  assert.equal((settled.match(/MAE /g)||[]).length,3);
  assert.ok(settled.includes('看跌') && settled.includes('-0.50%'));
  for(const [metric,tone] of [['direction','overview-up'],['high','overview-up'],['low','overview-down'],['return_pct','overview-up']]) {
    const cell=pending.match(new RegExp(`data-live-prediction-metric="${metric}"[^>]*>([\\s\\S]*?)</td>`))[1];
    assert.ok(cell.includes(tone),`${metric} uses the existing red/green comparison convention`);
  }
  assert.ok(failed.includes('服务暂不可用') && !failed.includes('520.00') && !failed.includes('MAE '));
  assert.ok(html.includes('2 / 17 个模型有效'),'Direction agreement excludes failed and waiting rows');
  const invalid=structuredClone(payload);
  Object.assign(invalid.items[0],{model:'<img src=x>',experiment_name:'<script>x</script>'});
  invalid.items[2].latest_attempt.error='<script>failure</script>';
  const safe=test.table(invalid);
  assert.ok(!safe.includes('<script>') && !safe.includes('<img src=x>'));
  assert.ok(safe.includes('&lt;img src=x&gt;') && safe.includes('&lt;script&gt;failure&lt;/script&gt;'));

  const opening=harness();
  let resolveFirst;
  opening.configure(()=>new Promise(resolve=>{resolveFirst=resolve;}));
  opening.open(payload.question);
  assert.ok(opening.opened && opening.overlay.includes('role="dialog"') && opening.overlay.includes('关闭模型预测'));
  assert.ok(opening.html.includes('正在读取模型预测'));
  const firstRequest=opening.requests[0].path;
  assert.equal(firstRequest,'/api/benchmark?mode=live&kind=predictions&dataset_id=qa-current&case_id=sc-current');
  resolveFirst(payload);await flush();
  assert.equal(modelRows(opening.html).size,17);
  assert.ok(opening.html.includes('2026.11.04'));
  assert.equal(opening.content.scrollTop,37);assert.equal(opening.scroller.scrollLeft,29);
  const previousWrites=opening.writes;
  opening.setAPI(async()=>payload);await opening.load();
  assert.equal(opening.writes,previousWrites,'Unchanged polling results preserve the existing dialog DOM');
  opening.close();assert.equal(opening.opened,false);
  const countBeforeClosed=opening.requests.length;await opening.load(true);
  assert.equal(opening.requests.length,countBeforeClosed,'A closed dialog stops requesting progress');

  const errors=harness();
  errors.configure(async()=>{throw new Error('读取失败 <script>');});
  errors.open(payload.question);await flush();
  assert.ok(errors.html.includes('读取失败 &lt;script&gt;') && errors.html.includes('data-live-predictions-retry'));
  errors.setAPI(async()=>payload);errors.retry();await flush();
  assert.equal(modelRows(errors.html).size,17);assert.ok(!errors.html.includes('读取失败'));
  errors.setAPI(async()=>({...payload,case_id:'another-case'}));await errors.load(true);
  assert.ok(errors.html.includes('与所选题目不匹配'),'Mismatched response identities are rejected');
  assert.equal(modelRows(errors.html).size,17,'A failed refresh retains previously verified predictions');

  const race=harness(),resolvers=new Map();
  race.configure(path=>new Promise(resolve=>resolvers.set(path,resolve)));
  const other=fixture('sc-older','qa-older','2026-11-03');other.items[0].model='other-question-model';
  race.open(payload.question);const first=race.requests.at(-1).path;
  race.open(other.question);const second=race.requests.at(-1).path;
  resolvers.get(second)(other);await flush();
  assert.ok(race.html.includes('other-question-model'));
  resolvers.get(first)(payload);await flush();
  assert.ok(race.html.includes('other-question-model') && !race.html.includes('model-1(Exp01)'),
    'A late response from the previous question never overwrites the selected question');
  race.open(payload.question);const closing=race.requests.at(-1).path;
  race.close();const closedWrites=race.writes;
  resolvers.get(closing)(payload);await flush();
  assert.equal(race.opened,false);assert.equal(race.writes,closedWrites,'A late response cannot reopen a closed dialog');

  const clicked=harness();clicked.configure(async()=>payload);
  clicked.catalog({runs:[],benchmarks:{live:{dataset_id:'qa-current',series_type:'dominant_continuous'},live_history:{history_revision:'one'}}},[payload.question]);
  await clicked.click({...payload.question,dataset_id:'wrong-dataset'});await flush();
  assert.equal(clicked.opened,false,'A stale or forged button identity does not select another question');
  await clicked.click(payload.question);await flush();assert.equal(clicked.opened,true);
  assert.equal(clicked.requests.length,1);

  const poll=harness();
  await poll.initialize(async path=>path==='/api/research'?{runs:[],benchmarks:{}}:payload);
  poll.open(payload.question);await flush();
  const beforePoll=poll.requests.length;
  const interval=poll.timers.find(item=>item.milliseconds===8000);assert.ok(interval);
  interval.callback();await flush();
  assert.equal(poll.requests.length,beforePoll+1,'The existing eight-second interval refreshes open-dialog progress');
  assert.ok(poll.requests.at(-1).path.includes('kind=predictions'));
  assert.ok(poll.requests.every(item=>!item.options?.method || item.options.method==='GET'));
  poll.close();const afterClose=poll.requests.length;interval.callback();await flush();
  assert.equal(poll.requests.length,afterClose);

  return {configured_models:rows.size,pending_predictions_visible:'passed',mae_requires_truth:'passed',
    color_conventions:'passed',question_identity:'passed',loading_retry_and_close:'passed',
    stale_response_isolation:'passed',read_only_progress_poll:'passed',escaping:'passed'};
}

if(process.argv[1] && import.meta.url===new URL(process.argv[1],'file:').href) {
  console.log(JSON.stringify(await testLivePredictionsUI()));
}
