import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

export async function testMemoryLeakageUI() {
  const source = fs.readFileSync(new URL('../static/benchmark.js',import.meta.url),'utf8');
  const context = {window:{},document:{querySelectorAll:()=>[]}};
  vm.createContext(context);
  vm.runInContext(source.replace('global.OilResearch =', `global.memoryTest = {
    render(payload,month='',pageNumber=1) {
      memoryDataset=payload;memoryError='';memoryMonth=month;page=pageNumber;
      view={section:'benchmark',subsection:'memory-leakage',mode:'backtest'};
      return {html:benchmark(),page};
    },
    async load(api) {
      options={api};view={section:'benchmark',subsection:'memory-leakage'};
      memoryDataset=null;memoryError='';data=null;
      await loadQuestions();
      return {dataset:memoryDataset,error:memoryError,html:memoryBenchmark()};
    }
  };global.OilResearch =`),context);
  const fixture = {year:2026,dataset_id:'memory-test',total_questions:100,questions:Array.from({length:100},(_,index)=>({
    case_id:`memory-${index+1}`,month:`2026-${String(Math.floor(index/10)+1).padStart(2,'0')}`,
    event:`事件 ${index+1}`,event_rationale:'重大事件依据',question:`第 ${index+1} 题：____`,answer:`答案 ${index+1}`,
    answer_aliases:[],evidence:'来源证据',source:{news_id:`news-${index+1}`,title:`新闻 ${index+1}`,
      source_name:'金融新闻',published_at:'2026-01-02T03:04:00Z',url:`https://example.com/news/${index+1}`},
  }))};
  const render = context.window.memoryTest.render;
  const allCases = new Set();
  for (let page=1;page<=10;page++) {
    const result = render(fixture,'',page);
    const cases = [...result.html.matchAll(/data-memory-case="([^"]+)"/g)];
    assert.equal(cases.length,10,'Ten review cards per page');
    cases.forEach(([,id])=>allCases.add(id));
    assert.equal((result.html.match(/class="memory-answer"/g)||[]).length,10);
    assert.equal((result.html.match(/class="memory-source"/g)||[]).length,10);
    assert.ok(result.html.includes('单次正确回答不能独立证明训练数据泄漏'));
    for (const forbidden of ['data-research-action="evaluate"','data-research-action="build"','research-mode-filter']) assert.ok(!result.html.includes(forbidden));
  }
  assert.equal(allCases.size,100,'Every frozen question is reachable exactly once across pages');
  for (let month=1;month<=10;month++) {
    const selected = `2026-${String(month).padStart(2,'0')}`;
    const {html,page} = render(fixture,selected,100);
    assert.equal(page,1,'Changing month clamps pagination');
    const cases = [...html.matchAll(/data-memory-case="([^"]+)"/g)].map(([,id])=>id);
    assert.deepEqual(cases,fixture.questions.filter(item=>item.month===selected).map(item=>item.case_id));
    assert.ok(html.includes(`data-memory-month="${selected}" aria-pressed="true"`));
  }
  const malicious = structuredClone(fixture);
  Object.assign(malicious.questions[0],{question:'<img src=x onerror=alert(1)>',answer:'<script>alert(2)</script>',temporal_focus:'data_release',temporal_rationale:'本次初值 <script>应安全显示</script>'});
  malicious.questions[0].source.url='javascript:alert(3)';
  const safe = render(malicious).html;
  assert.ok(safe.includes('&lt;img src=x onerror=alert(1)&gt;'));
  assert.ok(!safe.includes('<script>') && !safe.includes('href="javascript:'));
  assert.ok(safe.includes('<b>时效性依据：</b>数据发布 · 本次初值 &lt;script&gt;应安全显示&lt;/script&gt;'));
  assert.ok(!safe.includes('data_release'));
  assert.ok(safe.includes('填空考查当次报道的新事实，排除长期身份、地理常识和可由题干推算的答案；日期仅用于定位。'));
  const methodology = {selection:'选题范围 <script>原油与宏观</script>',month_basis:'首发 UTC+0 归月',temporal_specificity:'时效标准 <b>具体新闻事实</b>',evaluation:'独立闭卷评估',limitation:'编辑选择具有主观性'};
  const withMethodology = render({...fixture,methodology}).html;
  for (const key of ['month_basis','evaluation','limitation']) assert.ok(withMethodology.includes(methodology[key]));
  assert.ok(withMethodology.includes('选题范围 &lt;script&gt;原油与宏观&lt;/script&gt;'));
  assert.ok(withMethodology.includes('时效标准 &lt;b&gt;具体新闻事实&lt;/b&gt;'));
  assert.ok(withMethodology.includes('核对当次新闻的具体事实、标准答案和引用。'));
  assert.ok(withMethodology.includes('kind=memory-leakage&amp;download=1'));
  const requests = [];
  const loaded = await context.window.memoryTest.load(async path=>{requests.push(path);return fixture;});
  assert.deepEqual(requests,['/api/benchmark?kind=memory-leakage'],'Independent of forecasting datasets and public proxy compatible');
  assert.equal(loaded.dataset.questions.length,100);
  const failed = await context.window.memoryTest.load(async()=>{throw new Error('读取失败');});
  assert.equal(failed.error,'读取失败');
  assert.ok(failed.html.includes('data-research-action="retry-memory"'));
  return {questions:allCases.size,monthly_filters:10,pagination:'passed',sources_and_answers:'passed',escaping:'passed',loading:'passed'};
}

if (process.argv[1] && import.meta.url === new URL(process.argv[1],'file:').href) {
  console.log(JSON.stringify(await testMemoryLeakageUI()));
}
