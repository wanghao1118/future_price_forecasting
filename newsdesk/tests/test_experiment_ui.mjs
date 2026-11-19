import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

export function testExperimentUI() {
  const source=fs.readFileSync(new URL('../static/benchmark.js',import.meta.url),'utf8');
  const context={window:{}};vm.createContext(context);
  vm.runInContext(source.replace('global.OilResearch =',`global.experimentTest=(payload,subsection,selection='',experiment='',predictions=[])=>{
    data=payload;view={section:'leaderboard',subsection,mode:'backtest'};selectedRun=selection;overviewExperimentIds=Array.isArray(experiment)?experiment:experiment?[experiment]:[];
    predictions.forEach(run=>overviewPredictions.set(run.run_id,{cases:run.cases}));
    return {html:leaderboard(),run:currentRunSummary(),cohort:comparisonCohort()};
  };global.OilResearch =`),context);
  const render=context.window.experimentTest;
  const metrics=['WTI','Brent','SC'].map(family=>({family,count:30,total:30,direction_accuracy:.5,high_mae:1,low_mae:1,return_pct_mae:1,eligible_for_ranking:true}));
  const base={mode:'backtest',environment_id:'env01',total:90,metrics,status:'completed',completed:90,succeeded:90,failed:0,news_snapshot_sha256:'n',technical_snapshot_sha256:'t',dataset_snapshot_sha256:'d',tool_mode:'agent_initiated_local_search'};
  const old=['a','b'].map(model=>({...base,run_id:'old-'+model,model,started_at:'2026-11-01',dataset_id:'old',protocol:'env01-v3'}));
  const current=['a','b'].map(model=>({...base,run_id:'new-'+model,model,started_at:'2026-11-02',dataset_id:'new',protocol:'env01-v5',experiment_id:'Exp01',experiment_name:'Exp01'}));
  const experiment={id:'Exp01',experiment_id:'Exp01',name:'Exp01',ready_for_leaderboard:false,models:['a','b'],model_profiles:{},total:180,succeeded:180,protocol:'env01-v5',questions_per_model:90,full_backtest:true};
  const payload={benchmarks:{backtest:{dataset_id:'new',series_type:'dominant_continuous'}},runs:[...current,...old],experiments:[experiment],strategies:['a','b'].map(model=>({id:'aihub-'+model,model}))};
  assert.equal(render(payload,'overview').cohort.anchor.dataset_id,'old','Keep historical comparison until all models pass audit');
  assert.equal(render(payload,'a').run.run_id,'old-a');
  let page=render(payload,'a').html;
  for(const removed of ['全量回测 · 模型对比','research-identity','research-metric-grid','方向按次日收盘结算；正确率越高越好','构建回测集','开始单日评测']) assert.ok(!page.includes(removed));
  assert.ok(page.includes('<th>模型(实验)</th>'));
  assert.ok(!page.includes('new-b') && !page.includes('old-b'),'Model pages only contain their own records');
  assert.ok(page.includes('a(Exp01)'));
  // A published historical result must not conceal a newer running experiment.
  Object.assign(current[0],{status:'running',completed:12,succeeded:10,failed:2});
  const activePage=render(payload,'a','old-a');
  const progress=activePage.html.match(/<section class="research-progress-panel"[\s\S]*?<\/section>/)[0];
  assert.equal(activePage.run.run_id,'old-a','Historical result inspection remains available');
  assert.ok(progress.includes('data-progress-run="new-a"') && progress.includes('a(Exp01)'));
  assert.ok(progress.includes('aria-valuenow="12"') && progress.includes('已处理 12 / 90 题'));
  assert.ok(progress.includes('失败 2') && !progress.includes('90 / 90'));
  const otherProgress=render(payload,'b').html.match(/<section class="research-progress-panel"[\s\S]*?<\/section>/)[0];
  assert.ok(otherProgress.includes('data-progress-run="new-b"') && !otherProgress.includes('new-a'));
  Object.assign(current[0],{status:'completed',completed:90,succeeded:90,failed:0});
  experiment.ready_for_leaderboard=true;
  assert.equal(render(payload,'overview').cohort.anchor.experiment_id,'Exp01');
  page=render(payload,'overview').html;
  assert.ok(page.includes('a(Exp01)') && page.includes('b(Exp01)'));
  assert.equal(render(payload,'a').run.run_id,'new-a');
  assert.equal(render(payload,'a','old-a').run.run_id,'old-a','Historical selections remain accessible');
  assert.equal(render(payload,'a','new-b').run.run_id,'new-a','Cannot select another model through a run id');
  assert.ok(render(payload,'experiment-setup').html.includes('data-experiment-id="Exp01"'));
  const exp00={...experiment,id:'Exp00',experiment_id:'Exp00',name:'Exp00',created_at:'2026-11-03',environment_id:'env00',ready_for_leaderboard:false,depends_on_experiment:'Exp01'};
  experiment.created_at='2026-11-02';
  const zeroRuns=current.map(r=>({...r,run_id:'zero-'+r.model,environment_id:'env00',protocol:'env00-indicators-v1-utc-30m',tool_mode:'agent_initiated_indicators_only',experiment_id:'Exp00',experiment_name:'Exp00',status:'waiting_dependency',completed:0,succeeded:0,started_at:'2026-11-03'}));
  payload.experiments=[exp00,experiment];payload.runs=[...zeroRuns,...current,...old];
  let setup=render(payload,'experiment-setup').html;
  assert.ok(setup.indexOf('data-experiment-id="Exp01"')<setup.indexOf('data-experiment-id="Exp00"'));
  Object.assign(current[0],{status:'running',completed:89,succeeded:88,failed:1});
  page=render(payload,'a').html;
  assert.ok(page.includes('回测实验记录') && !page.includes('回测策略记录'));
  assert.ok(page.indexOf('data-open-run="new-a"')<page.indexOf('data-open-run="zero-a"'));
  assert.ok(page.includes('data-progress-run="new-a"'),'Waiting Exp00 must not replace active Exp01 progress');
  Object.assign(current[0],{status:'completed',completed:90,succeeded:90,failed:0});
  assert.equal(render(payload,'overview').cohort.anchor.experiment_id,'Exp01');
  exp00.ready_for_leaderboard=true;zeroRuns.forEach(r=>Object.assign(r,{status:'completed',completed:90,succeeded:90}));
  assert.equal(render(payload,'a').run.experiment_id,'Exp00','Publication is independent of card display order');
  assert.equal(render(payload,'overview').cohort.anchor.experiment_id,'Exp00');

  // Manual inspection can select an unfinished experiment without publishing
  // it, mixing runs from other batches, or changing model-page defaults.
  experiment.ready_for_leaderboard=false;experiment.status='partial';experiment.succeeded=179;experiment.failed=1;
  exp00.ready_for_leaderboard=false;exp00.status='partial';exp00.succeeded=178;exp00.failed=2;
  experiment.run_ids=current.map(r=>r.run_id);exp00.run_ids=zeroRuns.map(r=>r.run_id);
  zeroRuns.forEach(r=>Object.assign(r,{metrics:metrics.map(m=>({...m,direction_accuracy:.8,high_mae:2,low_mae:3,return_pct_mae:4}))}));
  payload.runs.unshift({...zeroRuns[0],run_id:'unregistered-newer-run',started_at:'2026-11-05',metrics:metrics.map(m=>({...m,direction_accuracy:1}))});
  payload.runs.unshift({...current[0],run_id:'live-run',mode:'live',total:1,started_at:'2026-11-06'});
  const predictions=[...current,...zeroRuns,...old].map(r=>({run_id:r.run_id,cases:[{case_id:'day',family:'WTI',target_date:'2026-10-30',status:'success',predicted:{direction:'up',high:r.experiment_id==='Exp00'?222:111,low:100,return_pct:1},absolute_errors:{high:1,low:1,return_pct:1}}]}));
  const manual01=render(payload,'overview','','Exp01',predictions);
  const manual00=render(payload,'overview','','Exp00',predictions);
  assert.equal(manual01.cohort.anchor.experiment_id,'Exp01');
  assert.equal(manual00.cohort.anchor.experiment_id,'Exp00');
  assert.deepEqual([...manual00.cohort.latest.values()].map(r=>r.run_id).sort(),['zero-a','zero-b']);
  assert.ok(manual00.html.includes('80.00%') && manual00.html.includes('2.0000') && manual00.html.includes('222.00'));
  assert.ok(!manual00.html.includes('111.00') && !manual00.html.includes('a(Exp01)'));
  assert.ok(manual01.html.includes('111.00') && !manual01.html.includes('222.00'));
  assert.ok(manual00.html.includes('已评分 178 / 180 题 · 失败 2 题 · 暂定结果'));
  const selector=manual00.html.match(/<fieldset[^>]*id="overview-experiment-filter"[\s\S]*?<\/fieldset>/)[0];
  assert.ok(selector.indexOf('value="Exp01"')<selector.indexOf('value="Exp00"'));
  assert.ok(selector.includes('value="Exp00" aria-label="Exp00" checked'));
  assert.equal(render(payload,'overview').cohort.anchor.run_id,'old-a','Manual viewing does not change default publication');
  assert.equal(render(payload,'a','','Exp00').run.run_id,'old-a','Overview filter does not change model-page defaults');
  const emptySelection=render({...payload,runs:old},'overview','','Exp01');
  assert.equal(emptySelection.cohort.anchor,null);
  assert.ok(emptySelection.html.includes('所选实验暂无回测结果') && emptySelection.html.includes('overview-experiment-filter'));

  // Same model in different experiments is a separate rank item and an
  // adjacent table row. Values must never overwrite each other.
  current[0].metrics=metrics.map(m=>({...m,direction_accuracy:.9,high_mae:1,low_mae:5,return_pct_mae:4}));
  const multi=render(payload,'overview','',['Exp01','Exp00'],predictions);
  assert.equal(multi.cohort.latest.size,4);
  assert.deepEqual([...multi.cohort.latest.values()].map(r=>r.run_id).sort(),['new-a','new-b','zero-a','zero-b']);
  const chart=multi.html.match(/<section class="summary-chart"[\s\S]*?<\/section>/)[0];
  const ranks=[...chart.matchAll(/data-model="([^"]+)" data-accuracy="([^"]*)"/g)].map(([,id,value])=>[id,Number(value)]);
  assert.deepEqual(ranks,[['a::Exp01',.9],['a::Exp00',.8],['b::Exp00',.8],['b::Exp01',.5]]);
  const table=multi.html.match(/<table class="research-table summary-mae-table">[\s\S]*?<\/table>/)[0];
  const rows=[...table.matchAll(/<tr data-summary-model="([^"]+)">([\s\S]*?)<\/tr>/g)];
  assert.deepEqual(rows.map(r=>r[1]),['a::Exp00','a::Exp01','b::Exp00','b::Exp01']);
  const cell=(markup,id,family,key)=>{
    const row=[...markup.matchAll(/<tr data-summary-model="([^"]+)">([\s\S]*?)<\/tr>/g)].find(r=>r[1]===id)?.[2];
    return row.match(new RegExp(`<td data-family="${family}" data-metric="${key}"[^>]*>[\\s\\S]*?</td>`))[0];
  };
  for(const family of ['WTI','Brent','SC']) {
    assert.ok(cell(table,'a::Exp01',family,'direction_accuracy').includes('90.00%'));
    for(const key of ['direction_accuracy','high_mae']) {
      assert.ok(cell(table,'a::Exp01',family,key).includes('summary-metric-best'));
      assert.ok(!cell(table,'a::Exp00',family,key).includes('summary-metric-best'));
    }
    assert.ok(cell(table,'a::Exp00',family,'low_mae').includes('summary-metric-best'),'Highlight lower MAE');
    assert.ok(!cell(table,'a::Exp01',family,'low_mae').includes('summary-metric-best'));
    assert.ok(cell(table,'a::Exp01',family,'return_pct_mae').includes('summary-metric-best'),'Highest experiment version wins a tied MAE');
    assert.ok(!cell(table,'a::Exp00',family,'return_pct_mae').includes('summary-metric-best'));
  }
  const daily=multi.html.match(/<table class="research-table overview-daily-table"[\s\S]*?<\/table>/)[0];
  assert.deepEqual([...daily.matchAll(/data-daily-model="([^"]+)"/g)].map(m=>m[1]),['a::Exp00','a::Exp01','b::Exp00','b::Exp01']);
  assert.ok(daily.includes('111.00') && daily.includes('222.00'));
  assert.ok(multi.html.includes('value="Exp01" aria-label="Exp01" checked') && multi.html.includes('value="Exp00" aria-label="Exp00" checked'));
  assert.ok(!manual00.html.includes('summary-metric-best'),'Single experiment has no comparison highlight');

  current[0].metrics=current[0].metrics.map(m=>({...m,count:0}));
  current[1].metrics=metrics.map(m=>({...m,high_mae:null,low_mae:0,eligible_for_ranking:false}));
  zeroRuns[1].metrics=zeroRuns[1].metrics.map(m=>({...m,low_mae:0}));
  const missing=render(payload,'overview','',['Exp00','Exp01']).html;
  assert.ok(cell(missing,'a::Exp01','WTI','direction_accuracy').includes('>—</td>'));
  assert.ok(!cell(missing,'a::Exp00','WTI','direction_accuracy').includes('summary-metric-best'),'One valid result is insufficient for cross-experiment comparison');
  assert.ok(!cell(missing,'b::Exp00','WTI','high_mae').includes('summary-metric-best'),'Null is not zero');
  assert.ok(cell(missing,'b::Exp01','WTI','low_mae').includes('summary-metric-best'),'Highest experiment version wins a zero-valued tie');
  assert.ok(!cell(missing,'b::Exp00','WTI','low_mae').includes('summary-metric-best'));
  assert.ok(cell(missing,'b::Exp01','WTI','low_mae').includes('summary-provisional-cell'),'Keep provisional marker alongside the highlight');

  const versions=['Exp2','Exp10'].map(id=>({...exp00,id,experiment_id:id,name:id,run_ids:['version-'+id],models:['a']}));
  const versionRuns=versions.map(e=>({...zeroRuns[0],model:'a',run_id:e.run_ids[0],experiment_id:e.id,experiment_name:e.name}));
  versionRuns[1].metrics=versionRuns[1].metrics.map(m=>({...m,direction_accuracy:m.direction_accuracy-1e-8,high_mae:m.high_mae+1e-8,low_mae:m.low_mae+1e-8,return_pct_mae:m.return_pct_mae+1e-8}));
  const versionPage=render({...payload,experiments:versions,runs:versionRuns},'overview','',['Exp10','Exp2']).html;
  for(const family of ['WTI','Brent','SC']) for(const key of ['direction_accuracy','high_mae','low_mae','return_pct_mae']) {
    assert.ok(cell(versionPage,'a::Exp10',family,key).includes('summary-metric-best'),'Equal displayed values use numeric experiment versions, ignoring floating-point noise');
    assert.ok(!cell(versionPage,'a::Exp2',family,key).includes('summary-metric-best'));
  }
  versionRuns[1].metrics=versionRuns[1].metrics.map(m=>({...m,count:27,direction_accuracy:1,high_mae:0,low_mae:0,return_pct_mae:0}));
  const gatedPage=render({...payload,experiments:versions,runs:versionRuns},'overview','',['Exp10','Exp2']).html;
  assert.ok(!gatedPage.includes('data-model="a::Exp10"'),'An apparent best score with only 90% success cannot rank');
  assert.ok(gatedPage.includes('data-model="a::Exp2"'));
  for(const family of ['WTI','Brent','SC']) for(const key of ['direction_accuracy','high_mae','low_mae','return_pct_mae']) {
    const excluded=cell(gatedPage,'a::Exp10',family,key);
    assert.ok(excluded.includes('>—</td>') && !excluded.includes('summary-metric-best'),'Excluded scores display a plain dash');
    assert.ok(!cell(gatedPage,'a::Exp2',family,key).includes('summary-metric-best'),'Below-threshold peers cannot affect the highlight');
  }
  const v2Experiments=['Exp02','Exp03','Exp04'].map((id,i)=>({...experiment,id,experiment_id:id,name:id,environment_id:['env02','env00','env01'][i],models:['a'],run_ids:['v2-'+id],questions_per_model:124,benchmark_profile:'v2',family_question_counts:{WTI:41,Brent:42,SC:41},total:124,ready_for_leaderboard:false}));
  const v2Runs=v2Experiments.map(e=>({...base,run_id:e.run_ids[0],model:'a',experiment_id:e.id,environment_id:e.environment_id,protocol:e.id==='Exp02'?'env02-no-tools-close-direction-return-range-v1':base.protocol,tool_mode:e.id==='Exp02'?'closed_book_no_tools':base.tool_mode,total:124,metrics:metrics.map((m,i)=>({...m,count:0,total:[41,42,41][i]}))}));
  const v2Payload={...payload,experiments:v2Experiments,runs:[...v2Runs,...old]};
  const v2Page=render(v2Payload,'overview','',['Exp02','Exp03','Exp04']);
  assert.equal(v2Page.cohort.latest.size,3,'All three environments support the full 124-question benchmark');
  assert.ok(v2Page.html.includes('a(Exp02)') && v2Page.html.includes('a(Exp03)') && v2Page.html.includes('a(Exp04)'));
  const v2ModelPage=render(v2Payload,'a').html;
  assert.ok(v2ModelPage.indexOf('data-open-run="v2-Exp04"') < v2ModelPage.indexOf('data-open-run="v2-Exp03"'));
  assert.ok(v2ModelPage.indexOf('data-open-run="v2-Exp03"') < v2ModelPage.indexOf('data-open-run="v2-Exp02"'),
    'Model pages list named backtest experiments by descending experiment number');
  assert.equal(render(v2Payload,'overview').cohort.anchor.dataset_id,'old','Queued v2 results cannot replace published defaults');
  const v2Setup=render(v2Payload,'experiment-setup').html;
  assert.ok(v2Setup.includes('v2 · 124 题 / 模型 · WTI 41 题、Brent 42 题、SC 41 题'));

  // Strategy environments env03/env04 use their own v1 protocol names,
  // while still being full active-search backtests eligible for provisional
  // leaderboard comparison.
  const strategyExperiments=['Exp05','Exp06'].map((id,i)=>({...experiment,id,experiment_id:id,name:id,environment_id:i ? 'env04' : 'env03',models:['a'],run_ids:[`strategy-${id}`],questions_per_model:90,total:90,ready_for_leaderboard:false}));
  const strategyRuns=strategyExperiments.map((e,i)=>({...base,run_id:e.run_ids[0],model:'a',experiment_id:e.id,experiment_name:e.name,environment_id:e.environment_id,protocol:i ? 'env04-agent-search-close-direction-return-range-v1-utc-30m-s02' : 'env03-agent-search-close-direction-return-range-v1-utc-30m-s01',tool_mode:i ? 'agent_initiated_local_search_with_s02' : 'agent_initiated_local_search_with_s01',total:90,metrics}));
  const strategyPage=render({...payload,experiments:strategyExperiments,runs:strategyRuns},'overview','',strategyExperiments.map(e=>e.id));
  assert.equal(strategyPage.cohort.latest.size,2,'env03/env04 v1 protocols are full active-search backtests');
  assert.ok(strategyPage.html.includes('a(Exp05)') && strategyPage.html.includes('a(Exp06)'));
  return {publication_gate:'passed',model_isolation:'passed',active_progress:'passed',simplified_model_page:'passed',experiment_cards:'passed',manual_experiment_switch:'passed',multi_experiment_ranking:'passed',grouped_metric_best_values:'passed',missing_and_tied_metrics:'passed',numeric_version_tiebreak:'passed',success_coverage_gate:'passed',v2_all_environments:'passed'};
}
