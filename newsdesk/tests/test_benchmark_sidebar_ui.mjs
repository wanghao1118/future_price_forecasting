import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../static/app.js', import.meta.url), 'utf8');
const html = fs.readFileSync(new URL('../static/index.html', import.meta.url), 'utf8');
const sidebar = html.match(/<nav id="benchmark-nav"[\s\S]*?<\/nav>/)[0];
assert.deepEqual([...sidebar.matchAll(/data-research-subsection="([^"]+)"/g)].map(([,id]) => id),
  ['memory-leakage', 'backtest-v1', 'backtest-v2', 'live']);
assert.ok(!sidebar.includes('data-benchmark-family'), 'Contract selection belongs inside the subsection');

let callbacks, renders = 0, navigationRenders = 0;
const element = {addEventListener() {}};
const context = {localStorage: {getItem() {return null;}, setItem() {}},
  document: {querySelector() {return element;}, querySelectorAll() {return [];}, addEventListener() {}},
  window: {OilResearch: {initialize(options) {callbacks = options;}}, scrollTo() {}, addEventListener() {}},
  recordRender() {renders++;}, recordNavigationRender() {navigationRenders++;},
};
vm.createContext(context);
vm.runInContext(source.replace(/\ninitialize\(\);\s*$/, `
render = () => recordRender();
renderNavigation = () => recordNavigationRender();
closeMenu = () => {};
window.sidebarTest = {selectResearch, bindEvents, snapshot: () => JSON.stringify(state)};
`), context);
const test = context.window.sidebarTest;
const snapshot = () => JSON.parse(test.snapshot());
test.bindEvents();

test.selectResearch('benchmark', 'backtest-v1', 'live');
assert.equal(snapshot().researchSubsection, 'backtest-v1');
assert.equal(snapshot().researchMode, 'backtest');
assert.equal(snapshot().researchFamily, 'all');
const beforeFilter = renders;
callbacks.onBenchmarkFilterChange({family: 'SC', query: '2026-10-30'});
assert.equal(snapshot().researchFamily, 'SC');
assert.equal(snapshot().researchQuery, '2026-10-30');
assert.equal(renders, beforeFilter, 'Typing does not remount the research workspace');
assert.ok(navigationRenders > 0);
test.selectResearch('benchmark', 'backtest-v1');
assert.equal(snapshot().researchQuery, '2026-10-30', 'Re-selecting the current subsection preserves its filter');
test.selectResearch('benchmark', 'backtest-v2', 'live');
assert.equal(snapshot().researchSubsection, 'backtest-v2');
assert.equal(snapshot().researchMode, 'backtest');
assert.equal(snapshot().researchFamily, 'SC');
assert.equal(snapshot().researchQuery, '', 'The independent September subsection clears the previous search');
test.selectResearch('benchmark', 'live', 'backtest');
assert.equal(snapshot().researchSubsection, 'live');
assert.equal(snapshot().researchMode, 'live');
assert.equal(snapshot().researchFamily, 'SC');
assert.equal(snapshot().researchQuery, '', 'A different subsection starts without a hidden search');
test.selectResearch('benchmark', 'memory-leakage', 'live');
assert.equal(snapshot().researchMode, 'backtest');
assert.equal(snapshot().researchSubsection, 'memory-leakage');
test.selectResearch('benchmark', 'WTI', 'live');
assert.equal(snapshot().researchSubsection, 'live');
assert.equal(snapshot().researchFamily, 'WTI');
test.selectResearch('benchmark', 'Brent', 'backtest');
assert.equal(snapshot().researchSubsection, 'backtest-v1');
assert.equal(snapshot().researchFamily, 'Brent');
const beforeInvalid = test.snapshot();
test.selectResearch('benchmark', 'invalid');
assert.equal(test.snapshot(), beforeInvalid);
console.log(JSON.stringify({sidebar_sections: 4, canonical_routes: 'passed', legacy_routes: 'passed', filter_state: 'passed'}));
