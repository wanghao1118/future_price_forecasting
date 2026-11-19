(function (global) {
  'use strict';

  const HOUR = 60 * 60 * 1000;
  const PERIOD_LABELS = {'1h': '1小时', '1d': '日K', '1w': '周K', '1M': '月K'};
  const UP = '#bc6b51';
  const DOWN = '#638773';
  const INDICATOR_LABELS = [
    ['price_trend', '价格趋势'], ['volume', '成交量'], ['price_volume', '量价关系'],
    ['open_interest', '持仓量及变化'], ['member_ratio', '会员多空持仓比'],
    ['member_net', '会员净持仓'], ['pcr', 'PCR'], ['range_position', '10日区间位置'],
    ['ma_trend', '均线趋势'], ['returns', '多周期收益动量'], ['rsi14', 'RSI（14）'],
    ['williams_r14', 'Williams %R（14）'], ['adx14', 'ADX（14）'], ['cci20', 'CCI（20）'],
    ['atr14', 'ATR（14）'], ['bollinger', '布林带（20，2σ）'],
    ['realized_vol20', '年化波动率（20日）'], ['oi_change', '持仓量变化'],
  ];
  const months = ['2026-12', '2027-01', '2027-02'];
  const monthLabels = ['2026年12月', '2027年1月', '2027年2月'];
  const families = [
    {id: 'AU', name: 'AU', label: '上海黄金', exchange: 'SHFE', currency: 'CNY', unit: '元 / 克', codes: ['AU2612', 'AU2701', 'AU2702']},
  ].map(family => ({...family, contracts: months.map((month, index) => ({
    id: `${family.id.toLowerCase()}-${month.replace('-', '')}`,
    code: family.codes[index], month, label: monthLabels[index],
    familyId: family.id, exchange: family.exchange,
    currency: family.currency, unit: family.unit,
  }))}));

  function escapeHTML(value) {
    return String(value ?? '').replace(/[&<>"']/g, char => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
  }
  function clamp(value, minimum, maximum) { return Math.max(minimum, Math.min(maximum, value)); }
  function resolveContract(id) {
    for (const family of families) {
      const contract = family.contracts.find(item => item.id === id);
      if (contract) return {...contract, family};
    }
    return null;
  }
  function groupKey(time, period) {
    const date = new Date(time);
    const day = Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate());
    if (period === '1d') return day;
    if (period === '1w') return day - ((date.getUTCDay() + 6) % 7) * 24 * HOUR;
    if (period === '1M') return Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), 1);
    throw new Error('不支持的聚合周期');
  }

  function aggregateSession(sessions, period) {
    const confirmed = sessions.filter(session => session?.timezone === 'UTC' && session.start_utc && session.end_utc);
    if (!confirmed.length) return null;
    const partial = confirmed.length < sessions.length || sessions.some(session => session?.calendar_status === 'unconfirmed' || session?.kind !== 'regular_schedule');
    const start = confirmed.map(session => session.start_utc).sort()[0];
    const end = confirmed.map(session => session.end_utc).sort().at(-1);
    return {
      timezone: 'UTC', kind: 'aggregated_schedule', start_utc: start, end_utc: end,
      label: `${PERIOD_LABELS[period]}覆盖${sessions.length}个交易日的常规时段（非连续${partial ? '，部分时段待确认' : ''}）`,
      trading_dates: sessions.map(session => session?.trading_date).filter(Boolean),
      segments: confirmed.flatMap(session => session.segments || []),
      source_url: confirmed[0].source_url,
    };
  }

  // Weekly/monthly bars aggregate provider daily bars, retaining exchange trading dates.
  function aggregateBars(bars, period) {
    const result = [];
    let key;
    let sessions = [];
    for (const bar of bars) {
      const nextKey = groupKey(bar.time, period);
      if (nextKey !== key) {
        result.push({...bar});
        key = nextKey;
        sessions = [bar.session];
      } else {
        sessions.push(bar.session);
        const group = result[result.length - 1];
        group.high = Math.max(group.high, bar.high);
        group.low = Math.min(group.low, bar.low);
        group.close = bar.close;
        group.volume = group.volume === null || bar.volume === null ? null : group.volume + bar.volume;
      }
      result[result.length - 1].session = aggregateSession(sessions, period);
    }
    return result;
  }
  function price(value) { return value === null || value === undefined || !Number.isFinite(Number(value)) ? '—' : Number(value).toFixed(2); }
  function volume(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
    if (value >= 100000000) return `${(value / 100000000).toFixed(2)}亿`;
    if (value >= 10000) return `${(value / 10000).toFixed(2)}万`;
    return Math.round(value).toLocaleString('zh-CN');
  }
  function dateLabel(time, period, full = false) {
    const date = new Date(time);
    if (Number.isNaN(date.valueOf())) return '—';
    const text = date.toISOString().replace('T', ' ');
    return text.slice(full ? 0 : 5, period === '1h' ? 16 : 10);
  }

  function utcTimestamp(value, seconds = false) {
    if (!value) return '—';
    if (typeof value === 'string' && !/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value)) return `${value}（时区待确认）`;
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? String(value) : date.toISOString().slice(0, seconds ? 19 : 16).replace('T', ' ');
  }
  function fetchedLabel(value) {
    return utcTimestamp(value, true);
  }
  function asOfLabel(value, period) {
    if (!value) return '—';
    if (period !== '1h' || /^\d{4}-\d{2}-\d{2}$/.test(value)) return String(value).slice(0,10);
    return fetchedLabel(value) + ' UTC+0';
  }
  function sessionText(session, purpose = 'daily') {
    const heading = purpose === 'indicator' ? '交易日对应时段' : purpose === 'aggregate' ? '聚合常规交易时段范围' : '日K常规交易时段';
    const prefix = `UTC+0 · ${heading}：`;
    if (session?.kind === 'closed') return prefix + (session.label || '休市，无常规交易时段');
    if (session?.timezone !== 'UTC' || !session.start_utc || !session.end_utc) return prefix + '时段待确认';
    if (purpose !== 'aggregate' && session.kind !== 'aggregated_schedule' && session.segments?.length > 1) {
      const groups = [];
      const relativeDay = date => {
        const distance = Math.round((Date.parse(date) - Date.parse(session.trading_date)) / (24 * HOUR));
        const relative = distance === 0 ? '当日' : distance === -1 ? '前一日' : distance === 1 ? '次日' : distance < 0 ? `前${-distance}日` : `后${distance}日`;
        return `${Number.isFinite(distance) ? relative : ''}(${date})`;
      };
      session.segments.forEach(segment => {
        const start = utcTimestamp(segment.start_utc), end = utcTimestamp(segment.end_utc);
        const startDay = start.slice(0,10), endDay = end.slice(0,10);
        const key = `${segment.label || ''}|${startDay}|${endDay}`;
        const clock = `${start.slice(11)}–${startDay === endDay ? end.slice(11) : `${relativeDay(endDay)} ${end.slice(11)}`}`;
        const previous = groups[groups.length - 1];
        if (previous?.key === key) previous.clocks.push(clock);
        else groups.push({key,heading:`${segment.label || ''} ${relativeDay(startDay)}`,clocks:[clock]});
      });
      const caution = session.night_session_status === 'unconfirmed' || session.calendar_status === 'unconfirmed' ? '；夜盘及休市安排待确认' : '';
      return prefix + groups.map(group => `${group.heading} ${group.clocks.join(' / ')}`).join('；') + caution;
    }
    const range = `${utcTimestamp(session.start_utc)} → ${utcTimestamp(session.end_utc)}`;
    const label = String(session.label || '').replace('（UTC+0；', '（').replace('（UTC+0）', '');
    return `${prefix}${label.includes(String(session.start_utc).slice(0,10)) && label.includes(String(session.end_utc).slice(0,10)) ? label : `${label ? `${label}；` : ''}${range}`}`;
  }

  class ContractChart {
    constructor(container, options = {}) {
      this.container = container;
      this.contract = resolveContract(options.contractId) || resolveContract('au-202612');
      this.period = '1d';
      this.count = 60;
      this.offset = 0;
      this.hover = null;
      this.drag = null;
      this.frame = null;
      this.destroyed = false;
      this.listeners = [];
      this.data = {'1h':[], '1d':[], '1w':[], '1M':[]};
      this.payloads = {};
      this.loading = true;
      this.error = null;
      this.controller = null;
      this.requestVersion = 0;
      this.indicatorController = null;
      this.indicatorVersion = 0;
      this.indicatorPayload = null;
      this.indicatorLoading = true;
      this.indicatorError = null;
      this.indicatorHistoryController = null;
      this.indicatorHistoryPayload = null;
      this.indicatorHistoryLoading = true;
      this.indicatorHistoryError = null;
      this.indicatorHistoryVersion = 0;
      this.indicatorHistoryTimer = null;
      this.indicatorPollTimer = null;
      this.indicatorHistoryRangeKey = '';
      this.indicatorHistoryRanges = [];
      this.indicatorViewportKey = '';
      this.indicatorPlots = new Map();
      this.indicatorHover = new Map();
      this.mountMarkup();
      this.attachEvents();
      this.observer = new ResizeObserver(() => this.requestDraw());
      this.observer.observe(this.stage);
      this.indicatorObserver = new ResizeObserver(() => this.drawIndicatorCharts());
      this.indicatorObserver.observe(this.indicatorNode);
      this.updateHeader();
      this.requestDraw();
      this.loadPeriod(this.period);
      this.loadIndicators();
    }
    mountMarkup() {
      this.container.innerHTML = `<section class="market-panel" aria-label="合约K线图"><header class="market-header"><div class="market-identity"><h2 class="market-contract-title"></h2><div class="market-meta"></div></div></header><div class="quote-summary"><div class="quote-main"><div class="quote-price"></div><div class="quote-change"></div></div><div class="quote-metrics"></div></div><div class="chart-toolbar"><div class="period-controls" role="group" aria-label="K线周期">${Object.entries(PERIOD_LABELS).map(([value,label]) => `<button type="button" data-period="${value}" aria-label="切换${label}" aria-pressed="${value === this.period}">${label}</button>`).join('')}</div><div class="range-controls" role="group" aria-label="显示K线数量">${[30,60,120,366].map(count => `<button type="button" data-count="${count}" aria-label="显示最近${count}根K线" aria-pressed="${count === this.count}">${count === 366 ? '2026 年以来' : `${count} 根`}</button>`).join('')}</div><button type="button" class="chart-reset">恢复视图</button><button type="button" class="chart-refresh" data-market-refresh>更新行情</button></div><div class="chart-readout" aria-live="polite"></div><div class="chart-stage"><canvas class="chart-canvas" tabindex="0" role="img"></canvas><div class="chart-tooltip" role="status" hidden></div><div class="chart-status" role="status">正在抓取真实行情…</div></div><footer class="chart-footer"><span class="chart-source">正在连接行情来源</span><span class="chart-view-hint">悬停查看 · 滚轮缩放 · 拖动平移</span></footer></section>`;
      this.stage = this.container.querySelector('.chart-stage');
      this.canvas = this.container.querySelector('.chart-canvas');
      this.tooltip = this.container.querySelector('.chart-tooltip');
      this.readout = this.container.querySelector('.chart-readout');
      this.statusNode = this.container.querySelector('.chart-status');
      this.context = this.canvas.getContext('2d');
      const indicators = document.createElement('section');
      indicators.className = 'market-indicators';
      indicators.setAttribute('aria-label', '合约技术与持仓指标');
      indicators.innerHTML = '<header class="indicator-heading"><h3>合约指标走势 <small>UTC+0</small></h3><button type="button" class="chart-refresh" data-indicators-refresh>更新指标</button></header><p class="indicator-status" role="status" hidden></p><dl class="indicator-rows"></dl>';
      this.container.querySelector('.market-panel').append(indicators);
      this.indicatorNode = indicators;
      this.renderIndicators();
    }
    listen(target, name, handler, options) {
      target.addEventListener(name, handler, options);
      this.listeners.push(() => target.removeEventListener(name, handler, options));
    }
    attachEvents() {
      this.listen(this.container, 'click', event => {
        const periodButton = event.target.closest('[data-period]');
        const rangeButton = event.target.closest('[data-count]');
        if (periodButton) {
          this.period = periodButton.dataset.period;
          this.offset = 0;
          this.hover = null;
          this.tooltip.hidden = true;
          this.loadPeriod(this.period);
        } else if (rangeButton) {
          this.count = Number(rangeButton.dataset.count);
          this.offset = 0;
          this.hover = null;
          this.tooltip.hidden = true;
          this.updateHeader();
          this.requestDraw();
        } else if (event.target.closest('.chart-reset')) this.reset();
        else if (event.target.closest('[data-market-refresh]')) {
          this.loadPeriod(this.period, true);
          this.loadIndicators(true);
        } else if (event.target.closest('[data-indicators-refresh]')) this.loadIndicators(true);
      });
      this.listen(this.container, 'pointermove', event => {
        const canvas = event.target.closest('[data-indicator-chart]');
        if (!canvas) return;
        const plot = this.indicatorPlots.get(canvas.dataset.indicatorChart);
        if (!plot?.points.length) return;
        const x = event.clientX - canvas.getBoundingClientRect().left;
        const index = plot.points.reduce((nearest, point, candidate) => Math.abs(plot.x(candidate) - x) < Math.abs(plot.x(nearest) - x) ? candidate : nearest, 0);
        this.inspectIndicator(canvas, index);
      });
      this.listen(this.container, 'wheel', event => {
        const canvas = event.target.closest('[data-indicator-chart]');
        if (!canvas || !event.deltaY || !this.bars().length) return;
        event.preventDefault();
        const rect = canvas.getBoundingClientRect();
        const plot = this.indicatorPlots.get(canvas.dataset.indicatorChart);
        const left = plot?.left ?? 18;
        const width = plot ? plot.right - plot.left : Math.max(1, rect.width - 90);
        this.zoom(event.deltaY < 0 ? .82 : 1.22, (event.clientX - rect.left - left) / width);
      }, {passive:false});
      this.listen(this.container, 'pointerout', event => {
        const canvas = event.target.closest('[data-indicator-chart]');
        if (!canvas || canvas.contains(event.relatedTarget)) return;
        this.indicatorHover.delete(canvas.dataset.indicatorChart);
        canvas.parentElement.querySelector('.indicator-chart-tooltip').hidden = true;
        this.drawIndicatorCharts();
      });
      this.listen(this.container, 'keydown', event => {
        const canvas = event.target.closest('[data-indicator-chart]');
        if (!canvas || !['ArrowLeft', 'ArrowRight', 'Escape'].includes(event.key)) return;
        const id = canvas.dataset.indicatorChart;
        const plot = this.indicatorPlots.get(id);
        if (!plot?.points.length) return;
        event.preventDefault();
        if (event.key === 'Escape') {
          this.indicatorHover.delete(id);
          canvas.parentElement.querySelector('.indicator-chart-tooltip').hidden = true;
          this.drawIndicatorCharts();
          return;
        }
        const current = this.indicatorHover.get(id) ?? plot.points.length - 1;
        this.inspectIndicator(canvas, clamp(current + (event.key === 'ArrowLeft' ? -1 : 1), 0, plot.points.length - 1));
      });
      this.listen(this.canvas, 'pointerdown', event => {
        if (event.button !== 0 || !this.plot) return;
        this.drag = {x:event.clientX,offset:this.offset,id:event.pointerId};
        this.canvas.setPointerCapture(event.pointerId);
        this.canvas.classList.add('dragging');
        this.tooltip.hidden = true;
      });
      this.listen(this.canvas, 'pointermove', event => {
        if (!this.plot) return;
        const rect = this.canvas.getBoundingClientRect();
        const x = event.clientX - rect.left;
        const y = event.clientY - rect.top;
        if (this.drag) {
          this.offset = clamp(this.drag.offset + Math.round((event.clientX - this.drag.x) / this.plot.step), 0, Math.max(0,this.bars().length-this.count));
          this.hover = null;
          this.requestDraw();
          return;
        }
        this.inspect(x,y);
      });
      const finishDrag = event => {
        if (!this.drag) return;
        if (this.canvas.hasPointerCapture(event.pointerId)) this.canvas.releasePointerCapture(event.pointerId);
        this.drag = null;
        this.canvas.classList.remove('dragging');
      };
      this.listen(this.canvas,'pointerup',finishDrag);
      this.listen(this.canvas,'pointercancel',finishDrag);
      this.listen(this.canvas,'pointerleave',() => {
        if (this.drag) return;
        this.hover = null;
        this.tooltip.hidden = true;
        this.requestDraw();
      });
      this.listen(this.canvas,'wheel',event => {
        if (!this.plot || !event.deltaY) return;
        event.preventDefault();
        const rect = this.canvas.getBoundingClientRect();
        this.zoom(event.deltaY < 0 ? .82 : 1.22,(event.clientX-rect.left-this.plot.left)/this.plot.width);
      },{passive:false});
      this.listen(this.canvas,'keydown',event => {
        if (!this.plot || !this.bars().length) return;
        if (!['ArrowLeft','ArrowRight','+','-','=','Escape'].includes(event.key)) return;
        event.preventDefault();
        if (event.key === 'Escape') {this.hover=null;this.tooltip.hidden=true;this.requestDraw();return;}
        if (['+','=','-'].includes(event.key)) {this.zoom(event.key === '-' ? 1.22 : .82,.5);return;}
        const visible = this.visible();
        const current = this.hover === null ? visible.start+visible.items.length-1 : this.hover;
        this.hover = clamp(current+(event.key === 'ArrowLeft' ? -1 : 1),visible.start,visible.start+visible.items.length-1);
        const index = this.hover-visible.start;
        this.showTooltip(this.bars()[this.hover],this.plot.left+(index+.5)*this.plot.step,this.plot.top+30);
        this.requestDraw();
      });
    }
    async loadPeriod(period, refresh = false) {
      const base = period === '1h' ? '1h' : '1d';
      if (this.controller) this.controller.abort();
      const version = ++this.requestVersion;
      if (!refresh && this.data[base].length) {
        this.loading = false;
        this.error = null;
        this.showStatus('');
        this.updateHeader(); this.requestDraw();
        return;
      }
      const controller = new AbortController();
      this.controller = controller;
      this.loading = true;
      this.error = null;
      this.plot = null;
      this.showStatus('正在抓取真实行情…');
      this.updateHeader(); this.requestDraw();
      try {
        const query = new URLSearchParams({period:base});
        if (refresh) query.set('refresh','1');
        const response = await fetch(`/api/market/${encodeURIComponent(this.contract.id)}?${query}`, {signal:controller.signal,headers:{Accept:'application/json'}});
        const payload = await response.json();
        if (!response.ok) throw new Error(typeof payload.error === 'string' ? payload.error : `行情请求失败（${response.status}）`);
        if (!Array.isArray(payload.bars) || !payload.bars.length) throw new Error('来源没有返回该月份合约的有效行情。');
        if (payload.contract?.id !== this.contract.id || payload.period !== base) throw new Error('行情合约或周期与请求不一致，已停止展示。');
        if (this.destroyed || version !== this.requestVersion) return;
        this.data[base] = payload.bars;
        this.payloads[base] = payload;
        if (base === '1d') {
          this.data['1w'] = aggregateBars(payload.bars,'1w');
          this.data['1M'] = aggregateBars(payload.bars,'1M');
        }
        this.loading = false;
        this.showStatus('');
      } catch (error) {
        if (error.name === 'AbortError' || this.destroyed || version !== this.requestVersion) return;
        this.loading = false;
        this.error = error.message || '行情抓取失败，请稍后重试。';
        this.showStatus(this.error, true);
      } finally {
        if (!this.destroyed && version === this.requestVersion) {
          this.updateHeader(); this.requestDraw();
        }
      }
    }
    showStatus(message, error = false) {
      this.statusNode.hidden = !message;
      this.statusNode.classList.toggle('error',error);
      this.statusNode.innerHTML = message ? `${error ? '<strong>行情暂不可用</strong>' : ''}<p>${escapeHTML(message)}</p>${error ? '<button type="button" data-market-refresh>重试抓取</button>' : ''}` : '';
    }
    cancelIndicatorHistory() {
      this.indicatorHistoryVersion++;
      if (this.indicatorHistoryController) this.indicatorHistoryController.abort();
      if (this.indicatorHistoryTimer !== null) clearTimeout(this.indicatorHistoryTimer);
      if (this.indicatorPollTimer !== null) clearTimeout(this.indicatorPollTimer);
      this.indicatorHistoryTimer = null;
      this.indicatorPollTimer = null;
      this.indicatorHistoryRangeKey = '';
    }
    async loadIndicators(refresh = false) {
      if (this.destroyed) return;
      if (this.indicatorController) this.indicatorController.abort();
      this.cancelIndicatorHistory();
      this.indicatorHistoryRanges = [];
      const version = ++this.indicatorVersion;
      this.indicatorError = null;
      this.indicatorHistoryError = null;
      this.indicatorLoading = refresh;
      this.indicatorHistoryLoading = true;
      this.renderIndicators();
      if (!refresh) {this.syncIndicatorViewport(true);return;}
      const contractId = this.contract.id;
      const controller = new AbortController();
      this.indicatorController = controller;
      try {
        const response = await fetch(`/api/market/${encodeURIComponent(contractId)}/indicators?refresh=1`, {signal:controller.signal,headers:{Accept:'application/json'}});
        const payload = await response.json();
        if (!response.ok) throw new Error(typeof payload.error === 'string' ? payload.error : `指标请求失败（${response.status}）`);
        if (payload.contract?.id !== contractId || !Array.isArray(payload.rows)) throw new Error('指标合约或数据格式与请求不一致。');
        if (this.destroyed || version !== this.indicatorVersion || this.contract.id !== contractId) return;
        this.indicatorPayload = payload;
        this.indicatorHistoryPayload = null;
      } catch (error) {
        if (error.name === 'AbortError' || this.destroyed || version !== this.indicatorVersion) return;
        this.indicatorError = error.message || '指标读取失败，请重试。';
      } finally {
        if (!this.destroyed && version === this.indicatorVersion) {
          this.indicatorLoading = false;
          this.renderIndicators();
          this.syncIndicatorViewport(true);
        }
      }
    }
    indicatorViewport() {
      const visible = this.visible();
      if (!visible.items.length) return null;
      const daily = this.data['1d'] || [];
      const positions = new Map();
      const dates = [];
      let start = visible.items[0].trading_date;
      let end = visible.items[visible.items.length - 1].trading_date;
      if (this.period === '1h') {
        const finalHour = new Map();
        this.data['1h'].forEach((bar, index) => finalHour.set(bar.trading_date, index));
        finalHour.forEach((index, date) => {
          if (index >= visible.start && index < visible.end) {
            positions.set(date, index - visible.start + .5);
            dates.push(date);
          }
        });
      } else if (this.period === '1d') {
        visible.items.forEach((bar, index) => {positions.set(bar.trading_date, index + .5);dates.push(bar.trading_date);});
      } else {
        const groups = new Map();
        daily.forEach(bar => {
          const key = groupKey(bar.time, this.period);
          if (!groups.has(key)) groups.set(key, []);
          groups.get(key).push(bar);
        });
        visible.items.forEach((bar, index) => {
          const group = groups.get(groupKey(bar.time, this.period)) || [];
          group.forEach((day, dayIndex) => {
            positions.set(day.trading_date, index + (dayIndex + .5) / group.length);
            dates.push(day.trading_date);
          });
        });
        if (dates.length) {start = dates[0];end = dates[dates.length - 1];}
      }
      dates.sort();
      return {start, end, positions, dates, visible, count:visible.items.length,
              key:`${this.contract.id}|${this.period}|${visible.start}|${visible.end}|${this.stage.clientWidth}`};
    }
    syncIndicatorViewport(force = false) {
      if (this.destroyed || !this.indicatorNode) return;
      const viewport = this.indicatorViewport();
      if (!viewport) {this.drawIndicatorCharts();return;}
      if (force || viewport.key !== this.indicatorViewportKey) {
        this.indicatorViewportKey = viewport.key;
        this.indicatorHover.clear();
        this.indicatorNode.querySelectorAll('.indicator-chart-tooltip').forEach(node => {node.hidden = true;});
        this.drawIndicatorCharts();
      }
      if (this.indicatorLoading) return;
      const key = `${this.contract.id}|${viewport.start}|${viewport.end}`;
      if (key === this.indicatorHistoryRangeKey) return;
      this.cancelIndicatorHistory();
      this.indicatorHistoryRangeKey = key;
      this.indicatorHistoryError = null;
      const covered = this.indicatorHistoryRanges.some(range => range.start <= viewport.start && range.end >= viewport.end);
      this.indicatorHistoryLoading = !covered;
      if (covered) {this.drawIndicatorCharts();return;}
      const contractId = this.contract.id;
      const version = this.indicatorVersion;
      const historyVersion = this.indicatorHistoryVersion;
      const range = {start:viewport.start,end:viewport.end};
      this.indicatorHistoryTimer = setTimeout(() => {
        this.indicatorHistoryTimer = null;
        this.loadIndicatorHistory(contractId, version, historyVersion, range);
      }, 350);
    }
    mergeIndicatorHistory(payload) {
      const previousRows = new Map((this.indicatorHistoryPayload?.rows || []).map(row => [row.id, row]));
      const rows = payload.rows.map(row => {
        const previous = previousRows.get(row.id);
        const oldSeries = new Map((previous?.series || []).map(series => [series.id, series]));
        const series = (row.series || []).map(curve => {
          const points = new Map((oldSeries.get(curve.id)?.points || []).map(point => [point.date, point]));
          (curve.points || []).forEach(point => points.set(point.date, point));
          return {...curve,points:[...points.values()].sort((a,b) => a.time - b.time)};
        });
        return {...row,series};
      });
      this.indicatorHistoryPayload = {...payload,rows};
    }
    async loadIndicatorHistory(contractId, version, historyVersion, range) {
      const controller = new AbortController();
      this.indicatorHistoryController = controller;
      let pending = false;
      try {
        const query = new URLSearchParams({history:'1',start:range.start,end:range.end});
        const response = await fetch(`/api/market/${encodeURIComponent(contractId)}/indicators?${query}`, {signal:controller.signal,headers:{Accept:'application/json'}});
        const payload = await response.json();
        if (!response.ok) throw new Error(typeof payload.error === 'string' ? payload.error : `历史指标请求失败（${response.status}）`);
        if (payload.contract?.id !== contractId || !Array.isArray(payload.rows)) throw new Error('历史指标合约或数据格式与请求不一致。');
        if (this.destroyed || version !== this.indicatorVersion || historyVersion !== this.indicatorHistoryVersion || this.contract.id !== contractId) return;
        this.mergeIndicatorHistory(payload);
        this.indicatorPayload = payload;
        pending = Boolean(payload.history?.pending);
        if (!pending) this.indicatorHistoryRanges.push(range);
      } catch (error) {
        if (error.name === 'AbortError' || this.destroyed || version !== this.indicatorVersion || historyVersion !== this.indicatorHistoryVersion) return;
        this.indicatorHistoryError = error.message || '历史指标读取失败，请更新指标重试。';
      } finally {
        if (!this.destroyed && version === this.indicatorVersion && historyVersion === this.indicatorHistoryVersion) {
          this.indicatorHistoryLoading = pending;
          this.renderIndicators();
          if (pending) this.indicatorPollTimer = setTimeout(() => {
            this.indicatorPollTimer = null;
            this.loadIndicatorHistory(contractId, version, historyVersion, range);
          }, 1000);
        }
      }
    }
    indicatorEmptyText(identifier) {
      if (this.indicatorLoading || this.indicatorHistoryLoading) return '正在载入…';
      if (this.indicatorHistoryError || this.indicatorError) return '数据暂不可用';
      return {open_interest:'暂无可核对日期的持仓历史',member_ratio:'暂无会员持仓历史',member_net:'暂无会员持仓历史',pcr:'暂无同月期权历史',range_position:'日线数据不足',
        ma_trend:'日线不足以计算均线',returns:'日线不足以计算该周期收益',rsi14:'日线不足14个交易日',williams_r14:'日线不足14个交易日',
        adx14:'日线不足以计算ADX14',cci20:'日线不足20个交易日',atr14:'日线不足14个交易日',bollinger:'日线不足20个交易日',
        realized_vol20:'日线不足20个收益观测',oi_change:'缺少相邻交易日持仓量'}[identifier] || '当前范围暂无日频数据';
    }
    renderIndicators() {
      if (!this.indicatorNode) return;
      this.indicatorNode.setAttribute('aria-busy', String(this.indicatorLoading || this.indicatorHistoryLoading));
      const status = this.indicatorNode.querySelector('.indicator-status');
      status.classList.toggle('error', Boolean(this.indicatorError || this.indicatorHistoryError));
      status.textContent = this.indicatorError || this.indicatorHistoryError ? '部分指标暂不可用，可更新重试。' : '';
      status.hidden = !status.textContent;
      this.indicatorNode.querySelector('.indicator-rows').innerHTML = INDICATOR_LABELS.map(([id, label]) =>
        `<div class="indicator-row" data-indicator-row="${id}"><dt>${escapeHTML(label)}</dt><dd class="indicator-chart-cell"><div class="indicator-chart-stage${id === 'price_volume' ? ' paired' : ''}"><canvas class="indicator-canvas" data-indicator-chart="${id}" tabindex="0" role="img" aria-label="${escapeHTML(label)}，与K线显示范围一致。滚轮缩放，左右方向键查看数据。"></canvas><div class="indicator-chart-empty">${this.indicatorEmptyText(id)}</div><div class="indicator-chart-tooltip" role="status" hidden></div></div></dd></div>`
      ).join('');
      const refresh = this.indicatorNode.querySelector('[data-indicators-refresh]');
      refresh.disabled = this.indicatorLoading;
      refresh.textContent = this.indicatorLoading ? '更新中…' : this.indicatorError || this.indicatorHistoryError ? '重试指标' : '更新指标';
      this.drawIndicatorCharts();
    }
    indicatorSeries(identifier, viewport = this.indicatorViewport()) {
      if (!viewport) return [];
      const daily = this.data['1d'] || [];
      const priceUnit = this.contract.unit;
      const definition = identifier === 'price_volume'
        ? [{id:'price',label:'收盘价',unit:priceUnit,kind:'line'},{id:'volume',label:'成交量',unit:'手',kind:'bar'}]
        : ({
          ma_trend:[{id:'price',label:'收盘价',unit:priceUnit,kind:'line'},{id:'ma20',label:'MA20',unit:priceUnit,kind:'line'},{id:'ma60',label:'MA60',unit:priceUnit,kind:'line'},{id:'ema12',label:'EMA12',unit:priceUnit,kind:'line'},{id:'ema26',label:'EMA26',unit:priceUnit,kind:'line'}],
          returns:[{id:'return5',label:'5日收益',unit:'%',kind:'line'},{id:'return20',label:'20日收益',unit:'%',kind:'line'},{id:'return60',label:'60日收益',unit:'%',kind:'line'},{id:'return120',label:'120日收益',unit:'%',kind:'line'},{id:'return252',label:'252日收益',unit:'%',kind:'line'}],
          bollinger:[{id:'price',label:'收盘价',unit:priceUnit,kind:'line'},{id:'middle',label:'中轨 MA20',unit:priceUnit,kind:'line'},{id:'upper',label:'上轨',unit:priceUnit,kind:'line'},{id:'lower',label:'下轨',unit:priceUnit,kind:'line'}],
        }[identifier] || [{id:{price_trend:'price',volume:'volume'}[identifier] || identifier,
            label:INDICATOR_LABELS.find(item => item[0] === identifier)?.[1] || identifier,
            unit:identifier === 'price_trend' ? priceUnit : ['volume','open_interest','member_net','oi_change'].includes(identifier) ? '手' : ['range_position','realized_vol20','returns'].includes(identifier) ? '%' : '倍',
            kind:['volume','member_net','oi_change'].includes(identifier) ? 'bar' : 'line'}]);
      const historical = this.indicatorHistoryPayload?.rows?.find(row => row.id === identifier);
      const curves = new Map((historical?.series || []).map(series => [series.id, series]));
      return definition.map(template => {
        const curve = curves.get(template.id);
        const saved = new Map((curve?.points || []).map(point => [point.date, point]));
        const raw = new Map();
        daily.forEach((bar, index) => {
          let value = null;
          if (template.id === 'price') value = bar.close;
          else if (template.id === 'volume') value = bar.volume;
          else if (template.id === 'open_interest') value = bar.open_interest;
          else if (template.id === 'range_position' && index >= 9) {
            const window = daily.slice(index - 9, index + 1);
            const high = Math.max(...window.map(item => item.high)), low = Math.min(...window.map(item => item.low));
            value = high > low ? (bar.close - low) / (high - low) * 100 : null;
          }
          raw.set(bar.trading_date,{time:bar.time,date:bar.trading_date,value:typeof value === 'number' && Number.isFinite(value) ? value : null,session:bar.session});
        });
        return {...template,...curve,points:viewport.dates.map(date => {
          const base = raw.get(date), cached = saved.get(date);
          return {time:cached?.time ?? base?.time ?? Date.parse(date),date,value:cached?.value ?? base?.value ?? null,session:cached?.session ?? base?.session};
        })};
      });
    }
    indicatorNumber(value, unit = '') {
      if (typeof value !== 'number' || !Number.isFinite(value)) return '暂无数据';
      const text = Math.abs(value) >= 10000 ? volume(value) : value.toLocaleString('zh-CN', {maximumFractionDigits: unit === '手' ? 0 : 2});
      return `${text}${unit === '%' ? '%' : unit ? ` ${unit}` : ''}`;
    }
    drawIndicatorCharts() {
      if (this.destroyed || !this.indicatorNode) return;
      this.indicatorPlots.clear();
      const viewport = this.indicatorViewport();
      this.indicatorNode.querySelectorAll('[data-indicator-chart]').forEach(canvas => {
        const id = canvas.dataset.indicatorChart;
        const series = this.indicatorSeries(id, viewport);
        const points = series[0]?.points || [];
        const width = canvas.parentElement.clientWidth;
        const height = canvas.parentElement.clientHeight;
        if (!width || !height) return;
        const dpr = Math.max(1, global.devicePixelRatio || 1);
        if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
          canvas.width = Math.round(width * dpr);
          canvas.height = Math.round(height * dpr);
        }
        const ctx = canvas.getContext('2d');
        if (!ctx) return;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, width, height);
        const valid = value => typeof value === 'number' && Number.isFinite(value);
        const hasValues = series.some(item => item.points.some(point => valid(point.value)));
        const empty = canvas.parentElement.querySelector('.indicator-chart-empty');
        empty.hidden = hasValues;
        empty.textContent = this.indicatorEmptyText(id);
        if (!viewport) return;
        const left = 18, right = width - 72, top = 11, bottom = height - 24;
        const step = Math.max(1, (right - left) / viewport.count);
        const x = index => left + (viewport.positions.get(points[index]?.date) ?? 0) * step;
        const panes = id === 'price_volume' ? series.map(item => [item]) : [series];
        const paneGap = panes.length > 1 ? 18 : 0;
        const paneHeight = (bottom - top - paneGap * (panes.length - 1)) / panes.length;
        ctx.font = '10px -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif';
        ctx.textBaseline = 'middle';
        panes.forEach((pane, paneIndex) => {
          const paneTop = top + paneIndex * (paneHeight + paneGap);
          const paneBottom = paneTop + paneHeight;
          const values = pane.flatMap(item => item.points.map(point => point.value)).filter(valid);
          if (!values.length) return;
          const isBar = pane.some(item => item.kind === 'bar');
          let minimum = Math.min(...values), maximum = Math.max(...values);
          const reference = id === 'member_ratio' || id === 'pcr' ? 1 : null;
          if (isBar) {minimum = Math.min(0, minimum); maximum = Math.max(0, maximum);}
          if (reference !== null) {minimum = Math.min(reference, minimum); maximum = Math.max(reference, maximum);}
          if (id === 'range_position') {minimum = 0; maximum = 100;}
          else {
            const padding = Math.max((maximum - minimum) * .12, Math.abs(maximum) * .015, .01);
            if (!isBar || minimum < 0) minimum -= padding;
            if (!isBar || maximum > 0) maximum += padding;
            if (maximum === minimum) maximum = minimum + 1;
          }
          const y = value => paneBottom - (value - minimum) / (maximum - minimum) * paneHeight;
          const unit = pane[0].unit || '';
          [minimum, (minimum + maximum) / 2, maximum].forEach(value => {
            const py = y(value);
            ctx.strokeStyle = '#edf0e7'; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(left, py + .5); ctx.lineTo(right, py + .5); ctx.stroke();
            ctx.fillStyle = '#94a087'; ctx.textAlign = 'left';
            const axis = Math.abs(value) >= 10000 ? volume(value) : value.toLocaleString('zh-CN', {maximumFractionDigits: Math.abs(value) < 10 ? 2 : 1});
            ctx.fillText(axis + (unit === '%' ? '%' : ''), right + 12, py);
          });
          if (reference !== null || id === 'member_net') {
            const value = reference ?? 0;
            ctx.save(); ctx.setLineDash([3, 3]); ctx.strokeStyle = '#a8b39b';
            ctx.beginPath(); ctx.moveTo(left, y(value)); ctx.lineTo(right, y(value)); ctx.stroke(); ctx.restore();
            if (reference !== null) {ctx.fillStyle = '#8a977d';ctx.textAlign = 'right';ctx.fillText('1', right, y(value) - 7);}
          }
          pane.forEach(item => {
            const color = series.indexOf(item) ? '#89a293' : '#ae7b60';
            if (item.kind === 'bar') {
              const gaps = points.slice(1).map((point, index) => x(index + 1) - x(index)).filter(gap => gap > 0);
              const barWidth = clamp((gaps.length ? Math.min(...gaps) : step) * .55, .7, 16);
              item.points.forEach((point, index) => {
                if (!valid(point.value)) return;
                ctx.fillStyle = id === 'member_net' ? point.value >= 0 ? UP : DOWN : '#94aa86';
                const zero = y(0), py = y(point.value);
                ctx.fillRect(x(index) - barWidth / 2, Math.min(zero, py), barWidth, Math.max(point.value === 0 ? 0 : 1, Math.abs(zero - py)));
              });
            } else {
              ctx.strokeStyle = color; ctx.lineWidth = 1.7; ctx.beginPath();
              let connected = false;
              item.points.forEach((point, index) => {
                if (!valid(point.value)) {connected = false;return;}
                if (connected) ctx.lineTo(x(index), y(point.value));
                else ctx.moveTo(x(index), y(point.value));
                connected = true;
              });
              ctx.stroke();
              item.points.forEach((point, index) => {
                if (!valid(point.value)) return;
                ctx.fillStyle = color;ctx.beginPath();ctx.arc(x(index), y(point.value), 2, 0, Math.PI * 2);ctx.fill();
              });
            }
            const hover = this.indicatorHover.get(id);
            if (hover !== undefined && valid(item.points[hover]?.value)) {
              ctx.fillStyle = color;ctx.beginPath();ctx.arc(x(hover), y(item.points[hover].value), 3, 0, Math.PI * 2);ctx.fill();
            }
          });
        });
        const axisBars = viewport.visible.items;
        const tickCount = Math.max(2, Math.min(6, Math.floor((right - left) / 100)));
        const ticks = [...new Set(Array.from({length:tickCount}, (_, index) => Math.round(index / (tickCount - 1) * (axisBars.length - 1))))];
        ticks.forEach((index, tickIndex) => {
          ctx.fillStyle = '#94a087';ctx.textAlign = tickIndex === 0 ? 'left' : tickIndex === ticks.length - 1 ? 'right' : 'center';
          ctx.fillText(dateLabel(axisBars[index].time, this.period), left + (index + .5) * step, height - 9);
        });
        const hover = this.indicatorHover.get(id);
        if (hover !== undefined && points[hover]) {
          ctx.save();ctx.setLineDash([3, 3]);ctx.strokeStyle = '#acb89f';ctx.beginPath();ctx.moveTo(x(hover), top);ctx.lineTo(x(hover), bottom);ctx.stroke();ctx.restore();
        }
        this.indicatorPlots.set(id, {points, series, left, right, step, x, start:viewport.start, end:viewport.end});
      });
    }
    inspectIndicator(canvas, index) {
      const id = canvas.dataset.indicatorChart;
      const plot = this.indicatorPlots.get(id);
      if (!plot?.points[index]) return;
      this.indicatorHover.set(id, index);
      this.drawIndicatorCharts();
      const tooltip = canvas.parentElement.querySelector('.indicator-chart-tooltip');
      const point = plot.points[index];
      tooltip.innerHTML = `<strong>交易日 ${escapeHTML(point.date)}</strong>${plot.series.map(series => `<div><span>${escapeHTML(series.label || '')}</span><b>${escapeHTML(this.indicatorNumber(series.points[index]?.value, series.unit))}</b></div>`).join('')}<p class="indicator-session-time">${escapeHTML(sessionText(point.session, 'indicator'))}</p>`;
      tooltip.hidden = false;
      const width = tooltip.offsetWidth || 180;
      tooltip.style.left = `${clamp(plot.x(index) + 12, 4, Math.max(4, canvas.parentElement.clientWidth - width - 4))}px`;
      tooltip.style.top = '7px';
    }
    bars() { return this.data[this.period] || []; }
    visible() {
      const bars = this.bars();
      const count = Math.min(this.count,bars.length);
      this.offset = clamp(this.offset,0,Math.max(0,bars.length-count));
      const end = bars.length-this.offset;
      const start = Math.max(0,end-count);
      return {start,end,items:bars.slice(start,end)};
    }
    updateHeader() {
      const bars = this.bars();
      const last = bars[bars.length-1];
      const previous = bars[bars.length-2];
      const change = last && previous ? last.close-previous.close : null;
      const percent = previous?.close && change !== null ? change/Math.abs(previous.close)*100 : null;
      const signed = value => value === null ? '—' : `${value >= 0 ? '+' : ''}${price(value)}`;
      const base = this.period === '1h' ? '1h' : '1d';
      const payload = this.payloads[base];
      this.container.querySelector('.market-contract-title').textContent = `${this.contract.family.label} · ${this.contract.code}`;
      this.container.querySelector('.market-meta').textContent = `${this.contract.exchange} · ${this.contract.label} · ${this.contract.unit} · UTC+0`;
      this.container.querySelector('.quote-price').textContent = price(last?.close);
      const changeNode = this.container.querySelector('.quote-change');
      changeNode.className = `quote-change ${change === null ? '' : change >= 0 ? 'up' : 'down'}`;
      changeNode.textContent = last ? `${signed(change)}  (${signed(percent)}%) · ${PERIOD_LABELS[this.period]}周期变化` : (this.error ? '未能获取行情' : '正在抓取真实行情…');
      this.container.querySelector('.quote-metrics').innerHTML = [['开盘',price(last?.open)],['最高',price(last?.high)],['最低',price(last?.low)],['成交量',last?.volume === null || !last ? '—' : `${volume(last.volume)} 手`]].map(([label,value]) => `<div class="quote-metric"><span>${label}</span><strong>${value}</strong></div>`).join('');
      const source = this.container.querySelector('.chart-source');
      if (payload) {
        const name = payload.source?.name || '行情来源';
        const url = payload.source?.url || '';
        const link = /^https?:\/\//i.test(url) ? `<a href="${escapeHTML(url)}" target="_blank" rel="noopener noreferrer">${escapeHTML(name)}</a>` : escapeHTML(name);
        const notes = (payload.notes || []).filter(note => !/已跳过.*槽位|重复时间槽/.test(note)).join('；');
        source.innerHTML = `来源 ${link} · 历史起点 ${escapeHTML(payload.history_coverage?.available_start || this.bars()[0]?.trading_date || "—")} · 行情截至 ${escapeHTML(asOfLabel(payload.data_as_of,base))} · 抓取 ${escapeHTML(fetchedLabel(payload.fetched_at))} UTC+0${payload.cache?.stale ? ' · 来源暂不可用，显示上次成功缓存' : ''}${notes ? `<small>${escapeHTML(notes)}</small>` : ''}`;
      } else source.textContent = this.error ? '本合约暂未取得可展示的真实行情。' : '正在连接行情来源';
      this.container.querySelectorAll('[data-period]').forEach(button => {
        const active = button.dataset.period === this.period;
        button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));
      });
      this.container.querySelectorAll('[data-count]').forEach(button => {
        const active = Number(button.dataset.count) === this.count;
        button.classList.toggle('active',active);button.setAttribute('aria-pressed',String(active));
      });
      const refresh = this.container.querySelector('.chart-refresh');
      refresh.disabled = this.loading;
      refresh.textContent = this.loading ? '抓取中…' : '更新行情';
      this.canvas.setAttribute('aria-label',`${this.contract.code} ${PERIOD_LABELS[this.period]} 真实行情K线与成交量，时间为UTC+0。左右方向键查看K线，加减键缩放。`);
    }
    updateReadout(bar) {
      if (!bar) { this.readout.textContent = this.loading ? '正在读取行情' : '暂无K线数据'; return; }
      const dayLabel = bar.trading_date || dateLabel(bar.time, this.period, true);
      const timeLabel = this.period === '1h' ? `${dateLabel(bar.time, this.period, true)} UTC+0` : `${this.period === '1d' ? '交易日' : `${PERIOD_LABELS[this.period]}起始交易日`} ${dayLabel}`;
      const session = this.period === '1h' ? '' : `<span class="chart-session-readout">${escapeHTML(sessionText(bar.session, this.period === '1d' ? 'daily' : 'aggregate'))}</span>`;
      this.readout.innerHTML = `<span class="chart-bar-date">${escapeHTML(timeLabel)}</span><span>开<b>${price(bar.open)}</b></span><span>高<b>${price(bar.high)}</b></span><span>低<b>${price(bar.low)}</b></span><span>收<b>${price(bar.close)}</b></span><span>量<b>${volume(bar.volume)}</b></span>${session}`;
    }
    inspect(x,y) {
      const plot = this.plot;
      if (x < plot.left || x > plot.left+plot.width || y < plot.top || y > plot.bottom) {
        this.hover=null;this.tooltip.hidden=true;this.requestDraw();return;
      }
      const visible = this.visible();
      const index = clamp(Math.floor((x-plot.left)/plot.step),0,visible.items.length-1);
      this.hover = visible.start+index;
      this.cursorY = y;
      this.showTooltip(visible.items[index],x,y);
      this.requestDraw();
    }
    showTooltip(bar,x,y) {
      const timeLabel = this.period === '1h' ? `${dateLabel(bar.time, this.period, true)} UTC+0` : bar.trading_date || dateLabel(bar.time, this.period, true);
      this.tooltip.innerHTML = [[this.period === '1h' ? '时间' : this.period === '1d' ? '交易日' : '起始交易日',timeLabel],['开盘',price(bar.open)],['最高',price(bar.high)],['最低',price(bar.low)],['收盘',price(bar.close)],['成交量',`${volume(bar.volume)} 手`]].map(([label,value]) => `<div><span>${label}</span><strong>${escapeHTML(value)}</strong></div>`).join('') + (this.period === '1h' ? '' : `<p class="chart-session-time">${escapeHTML(sessionText(bar.session, this.period === '1d' ? 'daily' : 'aggregate'))}</p>`);
      this.tooltip.hidden = false;
      const width = this.tooltip.offsetWidth || 190;
      const height = this.tooltip.offsetHeight || 184;
      const left = x > this.stage.clientWidth*.6 ? x-width-16 : x+16;
      this.tooltip.style.left = `${clamp(left,8,Math.max(8,this.stage.clientWidth-width-8))}px`;
      this.tooltip.style.top = `${clamp(y-height*.35,8,Math.max(8,this.stage.clientHeight-height-8))}px`;
    }
    zoom(factor,fraction) {
      const bars = this.bars();
      const current = this.visible();
      const position = clamp(fraction,0,1);
      const anchor = current.start+position*current.items.length;
      const count = clamp(Math.round(this.count*factor),Math.min(12,bars.length),Math.min(366,bars.length));
      const start = clamp(Math.round(anchor-position*count),0,Math.max(0,bars.length-count));
      this.count = count;
      this.offset = Math.max(0,bars.length-count-start);
      this.hover = null;
      this.tooltip.hidden = true;
      this.updateHeader();
      this.requestDraw();
    }
    reset() {
      this.count=60;this.offset=0;this.hover=null;this.tooltip.hidden=true;
      this.updateHeader();this.requestDraw();
    }
    requestDraw() {
      if (this.destroyed || this.frame !== null) return;
      this.frame = requestAnimationFrame(() => {
        this.frame=null;
        if(!this.destroyed) {this.draw();this.syncIndicatorViewport();}
      });
    }
    draw() {
      const width = this.stage.clientWidth;
      const height = this.stage.clientHeight;
      if (!width || !height || !this.context) return;
      const dpr = Math.max(1,global.devicePixelRatio || 1);
      if (this.canvas.width !== Math.round(width*dpr) || this.canvas.height !== Math.round(height*dpr)) {
        this.canvas.width=Math.round(width*dpr);this.canvas.height=Math.round(height*dpr);
      }
      const ctx = this.context;
      ctx.setTransform(dpr,0,0,dpr,0,0);
      ctx.clearRect(0,0,width,height);
      ctx.fillStyle='#ffffff';ctx.fillRect(0,0,width,height);
      ctx.font='10px -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif';
      const visible = this.visible();
      const candles = visible.items;
      if (!candles.length) { this.plot = null; this.updateReadout(null); return; }
      const plot={left:18,right:width-72,top:20,bottom:height-34};
      plot.width=Math.max(10,plot.right-plot.left);
      plot.priceBottom=plot.top+(plot.bottom-plot.top)*.72;
      plot.volumeTop=plot.priceBottom+28;
      plot.step=plot.width/candles.length;
      this.plot=plot;
      const low=Math.min(...candles.map(bar=>bar.low));
      const high=Math.max(...candles.map(bar=>bar.high));
      const padding=Math.max((high-low)*.12,Math.max(Math.abs(high),Math.abs(low))*.002,.05);
      const minimum=low-padding,maximum=high+padding;
      const priceY=value=>plot.priceBottom-(value-minimum)/(maximum-minimum)*(plot.priceBottom-plot.top);
      const hasVolume=candles.some(bar=>bar.volume!==null && Number.isFinite(bar.volume));
      const maxVolume=Math.max(...candles.map(bar=>bar.volume || 0),1);
      const volumeHeight=plot.bottom-plot.volumeTop;
      ctx.textBaseline='middle';ctx.lineWidth=1;
      for(let index=0;index<=4;index++) {
        const y=plot.top+(plot.priceBottom-plot.top)*index/4;
        ctx.strokeStyle='#edf0e7';ctx.beginPath();ctx.moveTo(plot.left,y+.5);ctx.lineTo(plot.right,y+.5);ctx.stroke();
        ctx.fillStyle='#89957d';ctx.textAlign='left';ctx.fillText(price(maximum-(maximum-minimum)*index/4),plot.right+12,y);
      }
      ctx.fillStyle='#89957d';ctx.fillText('成交量',plot.left,plot.volumeTop-11);
      ctx.fillText(hasVolume?volume(maxVolume):'未提供',plot.right+12,plot.volumeTop+4);
      ctx.strokeStyle='#edf0e7';ctx.beginPath();ctx.moveTo(plot.left,plot.bottom+.5);ctx.lineTo(plot.right,plot.bottom+.5);ctx.stroke();
      const candleWidth=clamp(plot.step*.55,1,13);
      candles.forEach((bar,index)=>{
        const x=plot.left+(index+.5)*plot.step;
        const color=bar.close>=bar.open?UP:DOWN;
        ctx.strokeStyle=color;ctx.fillStyle=color;
        ctx.beginPath();ctx.moveTo(x,priceY(bar.high));ctx.lineTo(x,priceY(bar.low));ctx.stroke();
        const top=Math.min(priceY(bar.open),priceY(bar.close));
        ctx.fillRect(x-candleWidth/2,top,candleWidth,Math.max(1,Math.abs(priceY(bar.close)-priceY(bar.open))));
        if(bar.volume!==null){ctx.globalAlpha=.42;ctx.fillRect(x-candleWidth/2,plot.bottom-bar.volume/maxVolume*volumeHeight,candleWidth,bar.volume/maxVolume*volumeHeight);ctx.globalAlpha=1;}
      });
      const tickCount=Math.max(2,Math.min(6,Math.floor(plot.width/100)));
      for(let index=0;index<tickCount;index++) {
        const barIndex=Math.round(index/(tickCount-1)*(candles.length-1));
        const x=plot.left+(barIndex+.5)*plot.step;
        ctx.fillStyle='#89957d';ctx.textAlign=index===0?'left':index===tickCount-1?'right':'center';
        ctx.fillText(dateLabel(candles[barIndex].time,this.period),x,plot.bottom+19);
      }
      const latest=this.bars()[this.bars().length-1];
      if(latest.close>=minimum&&latest.close<=maximum) {
        const y=priceY(latest.close);
        ctx.save();ctx.setLineDash([3,4]);ctx.strokeStyle='#b7bfb0';ctx.beginPath();ctx.moveTo(plot.left,y);ctx.lineTo(plot.right,y);ctx.stroke();ctx.restore();
        ctx.fillStyle=latest.close>=latest.open?UP:DOWN;ctx.fillRect(plot.right+5,y-9,61,18);ctx.fillStyle='#fff';ctx.textAlign='left';ctx.fillText(price(latest.close),plot.right+11,y);
      }
      let selected=candles[candles.length-1];
      if(this.hover!==null && this.hover>=visible.start && this.hover<visible.end) {
        selected=this.bars()[this.hover];
        const x=plot.left+(this.hover-visible.start+.5)*plot.step;
        const y=clamp(this.cursorY || priceY(selected.close),plot.top,plot.bottom);
        ctx.save();ctx.setLineDash([3,3]);ctx.strokeStyle='#a7b29d';ctx.beginPath();ctx.moveTo(x,plot.top);ctx.lineTo(x,plot.bottom);ctx.moveTo(plot.left,y);ctx.lineTo(plot.right,y);ctx.stroke();ctx.restore();
      }
      this.updateReadout(selected);
      const hint=this.container.querySelector('.chart-view-hint');
      hint.textContent=`${candles.length} 根 · 红涨绿跌 · 悬停查看 · 滚轮缩放 · 拖动平移`;
    }
    setContract(contractId) {
      if(this.destroyed || this.contract.id===contractId) return;
      const next=resolveContract(contractId);
      if(!next) throw new Error('合约不存在');
      if(this.controller) this.controller.abort();
      this.requestVersion++;
      this.contract=next;
      this.data={'1h':[], '1d':[], '1w':[], '1M':[]};
      this.payloads={};this.offset=0;this.hover=null;this.tooltip.hidden=true;
      this.indicatorPayload=null;
      this.indicatorHistoryPayload=null;
      this.indicatorHover.clear();
      this.loadPeriod(this.period);
      this.loadIndicators();
    }
    reloadCachedData() {
      if (this.destroyed) return;
      this.data={'1h':[], '1d':[], '1w':[], '1M':[]};
      this.payloads={};
      this.loadPeriod(this.period);
      this.loadIndicators();
    }
    destroy() {
      if(this.destroyed) return;
      this.destroyed=true;
      this.requestVersion++;
      this.indicatorVersion++;
      this.cancelIndicatorHistory();
      if(this.controller) this.controller.abort();
      if(this.indicatorController) this.indicatorController.abort();
      if(this.indicatorHistoryController) this.indicatorHistoryController.abort();
      if(this.frame!==null) cancelAnimationFrame(this.frame);
      this.observer.disconnect();
      this.indicatorObserver.disconnect();
      this.listeners.forEach(remove=>remove());
      if(this.drag && this.canvas.hasPointerCapture(this.drag.id)) this.canvas.releasePointerCapture(this.drag.id);
      this.drag=null;
    }
  }

  global.OilContracts = {
    families,
    resolveContract,
    aggregateBars,
    mount(container, options) {
      if(!container) throw new Error('K线容器不存在');
      return new ContractChart(container, options);
    },
  };
})(window);
