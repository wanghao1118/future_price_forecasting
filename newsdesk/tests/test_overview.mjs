import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function harness(data,family='WTI',predictions=[],selectedDate='') {
  const source = fs.readFileSync(new URL('../static/benchmark.js',import.meta.url), 'utf8');
  const context = {window:{}};
  vm.createContext(context);
  vm.runInContext(source.replace('global.OilResearch =', `global.summaryTest = function(payload,family,predictions,selectedDate) {
    data=payload; view={section:'leaderboard',subsection:'overview',mode:'live'}; selectedRun='';
    overviewFamily=family; overviewDate=selectedDate;
    predictions.forEach(run=>overviewPredictions.set(run.run_id,{cases:run.cases}));
    return {cohort:comparisonCohort(),html:leaderboardSummary(),mode:mode(),date:overviewDate,consensus:dailyDirectionConsensus,tone:predictionTone};
  }; global.OilResearch =`),context);
  return context.window.summaryTest(data,family,predictions,selectedDate);
}

function validateSummary(data) {
  const {cohort,html,mode} = harness(data);
  assert.equal(mode,'backtest','Overview always uses backtests');
  const sections = ['WTI','Brent','SC'].flatMap(family=>{
    const markup=harness(data,family).html;
    const matches=[...markup.matchAll(/<section class="summary-chart" data-summary-family="([^"]+)"[\s\S]*?<\/section>/g)];
    assert.equal(matches.length,1,'Only the selected contract chart is displayed');
    assert.equal(matches[0][1],family);
    return matches;
  });
  for(const removed of ['统一使用 env01','结果覆盖说明','summary-overview','分别按各主连','数值越低越好']) assert.ok(!html.includes(removed));
  let accuracyValues=0;
  for (const section of sections) {
    const family = section[1];
    const rows = [...section[0].matchAll(/data-model="([^"]+)" data-accuracy="([^"]*)"/g)];
    const qualified=cohort.models.filter(model=>{
      const metric=cohort.latest.get(model.model)?.metrics.find(m=>m.family===family);
      return metric?.total>0 && metric.count/metric.total>0.9 && metric.count<=metric.total && metric.direction_accuracy!=null;
    });
    assert.deepEqual(rows.map(r=>r[1]).sort(),[...qualified].map(m=>m.model).sort(),'Only results above 90% success belong in the ranking');
    accuracyValues+=rows.length;
    let previous = Infinity;
    for (const [,model,value] of rows) {
      const metric = cohort.latest.get(model)?.metrics.find(m=>m.family===family);
      const expected = metric?.count > 0 && metric.direction_accuracy != null ? metric.direction_accuracy : null;
      assert.equal(value,expected === null ? '' : String(expected));
      const rank = value === '' ? -1 : Number(value);
      assert.ok(previous >= rank,'Each contract must be sorted descending');
      previous = rank;
    }
  }
  const table = html.match(/<table class="research-table summary-mae-table">[\s\S]*?<\/table>/)[0];
  assert.equal((table.match(/<thead>[\s\S]*?<\/thead>/)[0].match(/<tr>/g)||[]).length,2);
  assert.equal((table.match(/colspan="4"/g)||[]).length,3);
  assert.equal((table.match(/方向 Acc\./g)||[]).length,4);
  assert.ok(table.includes('rowspan="2"'));
  const rows = [...table.matchAll(/<tr data-summary-model="([^"]+)">([\s\S]*?)<\/tr>/g)];
  assert.equal(rows.length,cohort.models.length);
  let values=0;
  for (const [,model,row] of rows) {
    const cells=[...row.matchAll(/<td data-family="([^"]+)" data-metric="([^"]+)"[^>]*>([^<]+)(?:<sup[^>]*>†<\/sup>)?<\/td>/g)];
    assert.equal(cells.length,12);
    for (const [,family,key,text] of cells) {
      const metric=cohort.latest.get(model)?.metrics.find(m=>m.family===family);
      const expected=metric?.total>0 && metric.count/metric.total>0.9 && metric.count<=metric.total && metric[key]!=null ? key==='direction_accuracy' ? `${(Number(metric[key])*100).toFixed(2)}%` : Number(metric[key]).toFixed(4) : '—';
      assert.equal(text,expected,`${model} ${family} ${key}`); values++;
    }
  }
  return {models:cohort.latest.size,accuracy_values:accuracyValues,mae_values:values};
}

function testEdgeCases() {
  const metrics=['WTI','Brent','SC'].map(family=>({family,count:30,total:30,direction_accuracy:0,high_mae:0,low_mae:0,return_pct_mae:0,eligible_for_ranking:true}));
  const base={run_id:'20261102-a',model:'zero',mode:'backtest',total:90,succeeded:90,one_day:false,dataset_id:'qa-test',environment_id:'env01',protocol:'env01-v3',dataset_snapshot_sha256:'d',news_snapshot_sha256:'n',technical_snapshot_sha256:'t',metrics};
  const data={benchmarks:{backtest:{dataset_id:'qa-test',series_type:'dominant_continuous'}},strategies:['zero','partial','missing'].map(model=>({id:'aihub-'+model,model})),runs:[
    {...base,run_id:'20261103-one-case',model:'zero',one_day:true,total:1,metrics:metrics.map(m=>({...m,direction_accuracy:1}))},
    base,
    {...base,run_id:'20261101-partial',model:'partial',succeeded:21,metrics:metrics.map(m=>({...m,count:7,direction_accuracy:1,eligible_for_ranking:false}))},
    {...base,run_id:'20261031-other-snapshot',model:'missing',news_snapshot_sha256:'different'},
  ]};
  const result=validateSummary(data);assert.equal(result.models,2);
  const {html}=harness(data);
  assert.ok(html.includes('0.00%'));assert.ok(html.includes('0.0000'));assert.ok(html.includes('成功率须超过 90% 才展示指标'));
  assert.equal((html.match(/summary-provisional-cell/g)||[]).length,0);
  const partialOnly={...data,runs:[data.runs[2]],strategies:[data.strategies[1]]};
  assert.ok(harness(partialOnly).html.includes('当前合约暂无成功率超过 90% 的有效评分结果'));
  // Use each contract's successful / total questions, not completed attempts,
  // run-wide totals, direction accuracy, or rounded percentages.
  const boundary={...data,runs:[{...base,metrics:metrics.map((m,i)=>({...m,count:[26,27,28][i],eligible_for_ranking:false}))}],strategies:[data.strategies[0]]};
  const boundaryResult=validateSummary(boundary);
  assert.equal(boundaryResult.accuracy_values,1,'26/30 and exactly 27/30 are excluded; 28/30 is included');
  assert.equal((harness(boundary).html.match(/summary-provisional-cell/g)||[]).length,4,'Above-threshold partial scores retain their provisional marker');
  const unknownTotal={...boundary,runs:[{...base,metrics:metrics.map((m,i)=>({...m,total:[0,null,undefined][i]}))}]};
  assert.ok(harness(unknownTotal).html.includes('暂无可比较的全量回测'),'Unknown contract denominators cannot form a full comparison cohort');
  assert.ok(harness({...data,runs:[]}).html.includes('暂无可比较的全量回测'));
  return result;
}

function validateDailyPredictions(data,predictions) {
  let tables=0,cells=0;
  for(const family of ['WTI','Brent','SC']) {
    const dates=[...new Set(predictions.flatMap(run=>run.cases.filter(c=>c.family===family).map(c=>c.target_date)))].sort();
    assert.equal(harness(data,family,predictions).date,dates.at(-1));
    for(const selectedDate of dates) {
      const result=harness(data,family,predictions,selectedDate);
      assert.equal(result.date,selectedDate);
      const daily=result.html.match(/<table class="research-table overview-daily-table"[\s\S]*?<\/table>/)[0];
      assert.ok(daily.includes(`data-prediction-date="${selectedDate}"`));
      const rows=[...daily.matchAll(/<tr data-daily-model="([^"]+)">([\s\S]*?)<\/tr>/g)];
      assert.equal(rows.length,result.cohort.models.length);
      const votes={up:0,down:0,flat:0};
      const market=predictions.flatMap(run=>run.cases).find(c=>c.family===family && c.target_date===selectedDate && c.input_market?.close != null)?.input_market;
      assert.ok(result.html.includes('<h2>日频预测</h2>'));
      if(market) {
        assert.ok(market.date<selectedDate);
        assert.ok(result.html.includes(`前一交易日行情 · ${market.date}`));
        for(const key of ['open','close','high','low','return_pct']) {
          const expected=key==='return_pct'?`${market[key]>0?'+':''}${Number(market[key]).toFixed(2)}%`:Number(market[key]).toFixed(2);
          assert.ok(result.html.match(new RegExp(`data-input-metric="${key}"[^>]*>(.*?)</dd>`))[1]===expected);
        }
      }
      for(const [,model,markup] of rows) {
        const run=result.cohort.latest.get(model);
        const item=predictions.find(r=>r.run_id===run?.run_id)?.cases.find(c=>c.family===family && c.target_date===selectedDate);
        const scored=item?.status==='success';
        assert.ok(markup.startsWith(`<th scope="row">${model}</th>`),'Model column contains only the model name');
        const directionMarkup=markup.match(/data-daily-metric="direction"[^>]*>(.*?)<\/td>/)[1];
        assert.equal(directionMarkup.replace(/<[^>]+>/g,''),scored ? {up:'看涨',down:'看跌',flat:'持平'}[item.predicted.direction] : '—');
        if(scored && item.predicted.direction!=='flat') assert.ok(directionMarkup.includes(item.predicted.direction==='up'?'overview-up':'overview-down'));
        if(scored) votes[item.predicted.direction]++;
        for(const key of ['high','low','return_pct']) {
          const cell=markup.match(new RegExp(`data-daily-metric="${key}"[^>]*>([\\s\\S]*?)</td>`))[1];
          const value=scored?item.predicted[key]:null;
          const expected=value==null?'—':key==='return_pct'?`${value>0?'+':''}${Number(value).toFixed(2)}%`:Number(value).toFixed(2);
          const baseline=key==='return_pct'?0:market?.close;
          const tone=value==null||baseline==null?'':value>baseline?'overview-up':value<baseline?'overview-down':'';
          assert.ok(cell.includes(`<span class="${tone}">${expected}</span>`));
          if(scored) assert.ok(cell.includes(`MAE ${Number(item.absolute_errors[key]).toFixed(4)}`));
          else assert.ok(!cell.includes('MAE '));
          cells++;
        }
      }
      const total=Object.values(votes).reduce((a,b)=>a+b,0),max=Math.max(...Object.values(votes));
      const footer=daily.match(/<tfoot>[\s\S]*?<\/tfoot>/)[0];
      assert.ok(footer.includes('当日方向一致性'));
      assert.ok(footer.includes(`${(max/total*100).toFixed(2)}%`));
      assert.ok(footer.includes(`${total} / ${result.cohort.models.length} 份预测有效`));
      assert.ok(!footer.includes('标准差') && !footer.includes('σ'));
      tables++;
    }
  }
  const consensus=harness(data).consensus;
  const item=(direction,status='success')=>({status,predicted:{direction}});
  const tied=consensus([item('up'),item('down'),item('up','failed'),item('invalid')]);
  assert.equal(tied.total,2);assert.equal(tied.ratio,0.5);assert.equal(tied.leaders.length,2);
  assert.equal(consensus([]).ratio,null);
  const tone=harness(data).tone;
  assert.equal(tone(100,100),'');assert.equal(tone(101,100),'overview-up');assert.equal(tone(99,100),'overview-down');
  assert.equal(tone(100,null),'');assert.equal(tone(null,100),'');assert.equal(tone(0),'');
  return {daily_tables:tables,numeric_cells:cells,direction_only_consensus:true};
}

export {validateSummary,testEdgeCases,validateDailyPredictions};
