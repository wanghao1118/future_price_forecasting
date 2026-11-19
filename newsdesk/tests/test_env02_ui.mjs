import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../static/benchmark.js', import.meta.url), 'utf8');
const index = fs.readFileSync(new URL('../static/index.html', import.meta.url), 'utf8');
const context = {window:{}};
vm.createContext(context);
vm.runInContext(source.replace('global.OilResearch =', `global.env02Test = {
  environment: () => {view={section:'environment',subsection:'env02',mode:'backtest'};return environment();},
  version: environmentVersion,
  windows: timeWindowsMarkup,
  trace: () => {data={runs:[{run_id:'env02-run',environment_id:'env02'}]};traceCache.set(traceKey('env02-run','case'),{environment_id:'env02',conversation:[],search_calls:[],indicator_calls:[]});return traceBody('env02-run','case');},
  traceLink: () => traceMarkup({run_id:'env02-run',environment_id:'env02'},{case_id:'case',conversation_available:true,search_calls:0,indicator_calls:0}),
  setup: () => {data={experiments:[{experiment_id:'Example02',environment_id:'env02',questions_per_model:90,full_backtest:true,models:[],model_profiles:{}}]};return experimentSetup();}
};global.OilResearch =`), context);
const test = context.window.env02Test;
const markup = test.environment();
assert.ok(index.includes('data-research-subsection="env02"><span>env02</span><span class="nav-count">1 agent</span>'));
assert.equal((markup.match(/class="research-agent-card"/g) || []).length, 1);
assert.ok(markup.includes('<h3>evaluator agent</h3>'));
assert.ok(markup.includes('无搜索与工具接口') && markup.includes('env02-no-tools-close-direction-return-range-v1'));
assert.ok(!markup.includes('<h3>time gate agent</h3>') && !markup.includes('search_news') && !markup.includes('search_indicators'));
assert.ok(test.version({environment_id:'env02',retrieval_time_policy:'forecast-start-minus-30m-utc-v1'}).startsWith('env02'));
const windows = test.windows({environment_id:'env02',retrieval_time_policy:'forecast-start-minus-30m-utc-v1',time_windows:{status:'ready',known:{start_utc:'2026-10-19T22:00:00Z',end_utc:'2026-10-20T21:00:00Z'},forecast:{start_utc:'2026-10-20T22:00:00Z',end_utc:'2026-10-21T21:00:00Z'},information_cutoff_utc:'2026-10-20T21:30:00Z'}});
assert.ok(windows.includes('已知') && windows.includes('预测') && windows.includes('UTC+0'));
assert.ok(!windows.includes('检索截止') && !windows.includes('新闻截止'));
const setup = test.setup();
assert.ok(setup.includes('仅题干与已知日线行情') && setup.includes('不适用 · 无检索接口'));
assert.ok(!setup.includes('模型主动检索') && !setup.includes('提前30分钟'));
assert.ok(test.trace().includes('无工具权限校验记录'));
assert.ok(!test.trace().includes('实际召回的新闻和技术指标'));
assert.ok(test.traceLink().includes('无搜索与工具接口') && !test.traceLink().includes('主动搜索'));
console.log(JSON.stringify({env02_navigation:'passed',single_evaluator:'passed',no_search_interface:'passed',protocol_labels:'passed',result_trace:'passed'}));
