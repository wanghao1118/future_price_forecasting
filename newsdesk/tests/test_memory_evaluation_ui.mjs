import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

export async function testMemoryEvaluationUI() {
  const source = fs.readFileSync(new URL('../static/benchmark.js',import.meta.url),'utf8');
  const context = {window:{},document:{querySelectorAll:()=>[]}};
  vm.createContext(context);
  vm.runInContext(source.replace('global.OilResearch =', `global.memoryEvaluationTest = {
    show(payload,subsection='overview',config={}) {
      data=payload;view={section:'leaderboard',subsection,mode:'backtest'};
      selectedRun=config.priceRun || '';selectedMemoryRun=config.memoryRun || '';
      memoryResultMonth=config.month || '';memoryResultPage=config.page || 1;
      if (config.run) memoryRunCache.set(config.run.run_id,{signature:memoryRunSignature(config.run),value:config.run,at:Date.now()});
      return {html:leaderboard(),price:comparisonCohort(),memory:memoryCohort(),priceRun:selectedRun,memoryRun:selectedMemoryRun};
    },
    environment() {view={section:'environment',subsection:'env00-extend'};return environment();},
    verdict(item) {return memoryCaseVerdict(item);},
    async load(payload,model,api,force=false) {
      data=payload;view={section:'leaderboard',subsection:model,mode:'backtest'};selectedMemoryRun='';options={api};
      await loadMemoryRun(force);
      return {html:memoryModelRecords(),priceRun:selectedRun,error:memoryRunErrors.get(memoryCurrentSummary()?.run_id)};
    },
    trace(runId,caseId,trace) {traceCache.set(traceKey(runId,caseId),trace);return memoryTraceBody(runId,caseId);},
    clear() {memoryRunCache.clear();memoryRunErrors.clear();memoryRunPending.clear();}
  };global.OilResearch =`),context);
  const api = context.window.memoryEvaluationTest;
  const models = Array.from({length:17},(_,index)=>`test-model-${String(index).padStart(2,'0')}`);
  const months = Array.from({length:10},(_,index)=>`2026-${String(index+1).padStart(2,'0')}`);
  const metric = (correct,graded,total,failed=0)=>({correct,graded,total,failed,resolved:graded+failed,completed:graded+failed,wrong:graded+failed-correct,pending:total-graded-failed,accuracy:correct/total,completion:(graded+failed)/total,complete:graded+failed===total});
  const runs = models.map((model,index)=>({
    run_id:`memory-${index}`,model,model_id:model,experiment_id:'Exp00-extend',experiment_name:'Exp00-extend',environment_id:'env00-extend',
    benchmark_kind:'memory-leakage',dataset_id:'memory-frozen-v2',status:index===0?'completed':index===1?'running':'queued',
    total:100,graded:index===0?100:index===1?20:0,completed:index===0?100:index===1?20:0,failed:index===1?2:0,
    created_at:'2026-11-03T08:00:00Z',updated_at:'2026-11-03T08:01:00Z',
    metrics:{overall:index===0?metric(0,100,100):index===1?metric(15,20,100,2):metric(0,0,100),
      months:months.map((month,position)=>({month,...(index===0?metric(0,10,10):index===1&&position<2?metric(position===0?7:8,10,10):metric(0,0,10))}))},
  }));
  const experiment = {experiment_id:'Exp00-extend',experiment_name:'Exp00-extend',environment_id:'env00-extend',dataset_id:'memory-frozen-v2',
    models:Object.fromEntries(runs.map(run=>[run.model,run.run_id])),created_at:'2026-11-03T08:00:00Z',status:'running',completed:120,expected_total:1700,total_per_model:100};
  const payload = {runs:[],experiments:[],strategies:[],memory_benchmark:{experiments:[experiment],runs,models:models.map(id=>({id,name:id}))}};
  const overview = api.show(payload);
  assert.equal(overview.price.anchor,null,'Memory runs cannot become price cohort anchors');
  assert.equal(overview.memory.models.length,17);
  assert.ok(overview.html.includes('data-memory-overview'),'Memory results remain visible without any price results');
  const ranks = [...overview.html.matchAll(/data-memory-rank-model="([^"]+)" data-memory-rank-accuracy="([^"]*)"/g)];
  assert.equal(ranks.length,17);
  assert.equal(ranks[0][1],models[1]);assert.equal(ranks[0][2],'0.15');
  assert.equal(ranks[1][1],models[0]);assert.equal(ranks[1][2],'0');
  const rows = [...overview.html.matchAll(/<tr data-memory-model="([^"]+)"[^>]*>([\s\S]*?)<\/tr>/g)];
  assert.equal(rows.length,17);
  for (const [,model,row] of rows) {
    const run = runs.find(run=>run.model===model);
    const cells = [...row.matchAll(/data-memory-month-metric="([^"]+)" data-memory-accuracy="([^"]*)"/g)];
    assert.equal(cells.length,11);
    cells.forEach(([,month,value])=>{
      const expected = month==='overall'?run.metrics.overall:run.metrics.months.find(metric=>metric.month===month);
      assert.equal(value,expected.accuracy==null?'':String(expected.accuracy),`${model} ${month}`);
    });
  }
  assert.ok(rows[0][2].includes('0.00%'),'A completed zero score is visible');
  assert.ok(rows[2][2].includes('<strong>0.00%</strong>'),'Running scores also use the fixed total, including zero correct so far');
  assert.ok(overview.html.includes('进行中') && overview.html.includes('已完成 22 / 100 · 待完成 78'));
  assert.ok(overview.html.includes('正确记1，其余记0；总分固定100，分月10'));
  assert.ok(!overview.html.includes('失败与待处理题不计为答错') && !overview.html.includes('已评分数'));
  const oldDenominator = structuredClone(payload);
  oldDenominator.memory_benchmark.runs[1].metrics.overall.accuracy=.75;
  assert.ok(api.show(oldDenominator).html.includes('data-memory-rank-accuracy="0.15"'),'The UI computes correct/total even if a stale API response contains the old denominator');
  const setup = api.show(payload,'experiment-setup').html;
  assert.ok(setup.includes('data-memory-experiment-id="Exp00-extend"'));
  assert.ok(setup.includes('1,700') && setup.includes('17 个模型'));
  assert.equal((setup.match(/data-open-memory-run=/g)||[]).length,17);
  assert.ok(setup.includes('新闻、技术指标、联网及其他工具全部关闭'));
  const environment = api.environment();
  assert.ok(environment.includes('env00-extend') && environment.includes('Codex CLI'));
  assert.ok(environment.includes('参考答案、别名、原文证据、事件标题和新闻引用不进入作答提示'));
  assert.ok(environment.includes('新闻、本地新闻、技术指标、联网搜索以及其他工具全部关闭'));
  const cases = Array.from({length:100},(_,index)=>({
    case_id:`case-${String(index).padStart(3,'0')}`,month:months[Math.floor(index/10)],question:`新闻事实 ${index}：____`,
    reference_answer:`参考值 ${index}`,model_answer:index===2?'':`回答 ${index}`,status:index===2?'prediction_failed':index===3?'pending':'graded',
    correct:index===2||index===3?null:index%2===0,reason:index===2||index===3?'':`判定依据 ${index}`,error:index===2?'API timeout':''
  }));
  const detailRun = {...runs[0],cases};
  const seen = new Set();
  for (let page=1;page<=10;page++) {
    const result = api.show(payload,models[0],{priceRun:'price-stays-selected',memoryRun:runs[0].run_id,run:detailRun,page});
    assert.equal(result.priceRun,'price-stays-selected','Memory inspection must not alter selected price run');
    assert.ok(!result.html.includes('memory-1"'),'A model page contains only its own memory runs');
    const ids = [...result.html.matchAll(/data-memory-result-case="([^"]+)"/g)].map(([,id])=>id);
    assert.equal(ids.length,10);ids.forEach(id=>seen.add(id));
    assert.ok(result.html.includes('参考答案') && result.html.includes('模型答案'));
    assert.ok(result.html.includes('/cases/case-') && result.html.includes('/trace?download=1'));
  }
  assert.equal(seen.size,100);
  for (const month of months) {
    const html = api.show(payload,models[0],{memoryRun:runs[0].run_id,run:detailRun,month}).html;
    const ids = [...html.matchAll(/data-memory-result-case="([^"]+)"/g)].map(([,id])=>id);
    assert.deepEqual(ids,cases.filter(item=>item.month===month).map(item=>item.case_id));
  }
  const firstPage = api.show(payload,models[0],{run:detailRun}).html;
  const failedRow = firstPage.match(/<tr data-memory-result-case="case-002"[\s\S]*?<\/tr>/)[0];
  assert.ok(failedRow.includes('memory-incorrect') && failedRow.includes('错误'),'A completed failed attempt scores wrong');
  assert.ok(!failedRow.includes('作答失败') && !failedRow.includes('API timeout'),'Failure classifications stay in the trace only');
  const pendingRow = firstPage.match(/<tr data-memory-result-case="case-003"[\s\S]*?<\/tr>/)[0];
  assert.ok(pendingRow.includes('待完成') && !pendingRow.includes('memory-incorrect'));
  for (const status of ['prediction_failed','judge_failed','tool_violation']) assert.equal(api.verdict({status,correct:null}).label,'错误');
  for (const status of ['pending','predicting','awaiting_judge','judging']) assert.equal(api.verdict({status,correct:null}).label,'待完成');
  assert.equal(api.verdict({status:'graded',correct:false,model_answer:'UNKNOWN'}).label,'错误');
  assert.equal(api.verdict({status:'graded',correct:true}).label,'正确');
  const allFailed = structuredClone(payload);
  allFailed.memory_benchmark.runs[0].metrics.overall=metric(0,0,100,100);
  assert.ok(api.show(allFailed).html.includes('已完成 100 / 100 · 待完成 0'),'Completion includes failed attempts while the score remains zero');
  const malicious = structuredClone(detailRun);
  Object.assign(malicious.cases[0],{question:'<img onerror=alert(1)>',model_answer:'<script>model</script>',reason:'<b>reason</b>'});
  const safe = api.show(payload,models[0],{run:malicious}).html;
  assert.ok(safe.includes('&lt;img onerror=alert(1)&gt;') && safe.includes('&lt;script&gt;model&lt;/script&gt;') && safe.includes('&lt;b&gt;reason&lt;/b&gt;'));
  const trace = api.trace('memory-0','case-000',{prediction:{raw_output:'<script>raw</script>'},evaluator:{reason:'evaluator result'}});
  assert.ok(trace.includes('&lt;script&gt;raw&lt;/script&gt;') && trace.includes('evaluator result'));
  api.clear();
  const requests = [];
  await api.load(payload,models[0],async path=>{requests.push(path);return detailRun;});
  await api.load(payload,models[0],async path=>{requests.push(path);return detailRun;});
  assert.equal(requests.length,1,'Completed identical summary does not repeatedly fetch cases');
  runs[0].updated_at='2026-11-03T08:02:00Z';
  await api.load(payload,models[0],async path=>{requests.push(path);return detailRun;});
  assert.equal(requests.length,2,'Changed summary signature re-fetches case records');
  assert.equal(requests[0],'/api/evaluations/memory-0');
  api.clear();
  const unloaded = api.show(payload,models[0]).html;
  assert.ok(unloaded.includes('正在读取 100 道逐题作答与评估结果'));
  const unavailable = await api.load(payload,models[0],async()=>{throw new Error('网络不可用');});
  assert.ok(unavailable.html.includes('网络不可用') && unavailable.html.includes('retry-memory-run'));
  assert.equal(payload.runs.length,0);assert.equal(payload.experiments.length,0);
  return {models:17,month_cells:187,reachable_cases:seen.size,accuracy_zero:'passed',binary_fixed_denominator:'passed',price_isolation:'passed',loading_and_polling:'passed'};
}

if (process.argv[1] && import.meta.url === new URL(process.argv[1],'file:').href) console.log(JSON.stringify(await testMemoryEvaluationUI()));
