/* =========================================================
   RFM 客户价值分析可视化大屏 - 前端逻辑
   四页报表 + 全局筛选器（参考 DS04 手册第5步四页结构）
   ========================================================= */
/* 墨金终端主题：炭黑底 + 金色单强调（#C9A227）+ 语义色（好绿/差红/预警橙），客群色与页1饼图严格一致 */
const PALETTE = ['#C9A227','#6E8CA0','#4E8574','#B0725C','#8E7BA8','#A98B5B','#5C7F92','#9A6B7C','#6B8F7A','#8A7A5C'];
const CHARTS = {};
const AXIS_STYLE = {
  axisLine: { lineStyle: { color: '#2E343D' } },
  axisLabel: { color: '#8E96A0', fontSize: 12 },
  splitLine: { lineStyle: { color: 'rgba(142,150,160,.28)' } },
  axisTick: { show: false }
};
const TOOLTIP = {
  backgroundColor: 'rgba(22,26,32,.98)', borderColor: '#2E343D', borderWidth: 1,
  textStyle: { color: '#E7E4DC', fontSize: 13 }, confine: true,
  extraCssText: 'box-shadow: 0 4px 16px rgba(0,0,0,.5); border-radius: 6px;'
};
const BLUE_GRAD = { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
  colorStops: [{ offset: 0, color: '#C9A227' }, { offset: 1, color: 'rgba(201,162,39,.35)' }] };

/* ---------- 联动状态（图表点击 cross-filter） ---------- */
const LINK_STATE = { seg: '', category: '', strat: '' };
const LINK_LABELS = { seg: '客群', category: '品类', strat: '策略组' };
let LAST_DATA = null;
let FC_DATA = null;      /* ④ GMV 预测：/api/forecast 返回体，与主图同一次加载 */
let KPI_META = {};       /* ⑧ KPI 口径追溯元数据：/api/dashboard 的 metric_meta */

/* 自动洞察卡类型标签（type 取值仅 good/bad/warn，后端保证） */
const INSIGHT_TAGS = { good: '向好', bad: '回落', warn: '预警' };

/* HTML 转义：文件名/品类名/客群名等用户可控字符串进入 innerHTML 前必须调用（防存储型 XSS） */
function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/* ---------- 二级下钻上下文 ---------- */
let DEEP_CTX = null;      // {type:'seg'|'month', ...params}
let DEEP_FROM = 'p0';     // 打开抽屉时的来源页签

/* ---------- 大屏自适应缩放 ---------- */
function resize() {
  const s = document.getElementById('screen');
  const scale = Math.min(window.innerWidth / 1920, window.innerHeight / 1080);
  s.style.transform = 'translate(-50%, -50%) scale(' + scale + ')';
}
window.addEventListener('resize', resize);

/* ---------- 时钟 ---------- */
function tick() {
  const d = new Date();
  const p = n => String(n).padStart(2, '0');
  document.getElementById('clock').innerHTML =
    d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    ' <b>' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds()) + '</b>';
}
setInterval(tick, 1000); tick();

/* ---------- 图表实例管理 ---------- */
function getChart(id) {
  if (!CHARTS[id]) {
    const el = document.getElementById(id);
    CHARTS[id] = echarts.init(el, null, { renderer: 'canvas' });
  }
  return CHARTS[id];
}
function disposeChart(id) {
  /* 容器 DOM 将被 innerHTML 重建时，旧 ECharts 实例必须显式释放，否则绑定在已脱离文档的节点上 */
  if (CHARTS[id]) {
    CHARTS[id].dispose();
    delete CHARTS[id];
  }
}
function resizeAll() {
  setTimeout(() => { Object.values(CHARTS).forEach(c => c.resize()); }, 60);
}

/* ---------- 面板内多图并排布局 ---------- */
/* ---------- 页签切换 ---------- */
function switchPage(p) {
  if (p === 'p6') { openDeepPage(); return; }  /* 兼容 URL ?page=p6 直接打开下钻覆盖页 */
  if (document.getElementById('p6').classList.contains('show')) closeDeepPage();  /* 覆盖页打开时点其他页签先关闭 */
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.page').forEach(x => x.classList.remove('active'));
  const t = document.querySelector('.tab[data-page="' + p + '"]');
  if (t) t.classList.add('active');
  document.getElementById(p).classList.add('active');
  resizeAll();
}
document.querySelectorAll('.tab').forEach(t => {
  t.addEventListener('click', () => switchPage(t.dataset.page));
});

/* ---------- 筛选器 ---------- */
const FILTER_KEYS = ['tier', 'gender', 'age', 'city', 'year', 'month'];
const FILTER_LABELS = { tier: '客群', gender: '性别', age: '年龄组', city: '城市等级', year: '年份', month: '月份' };

function initFilters() {
  fetch('/api/filters').then(r => r.json()).then(f => {
    FILTER_KEYS.forEach(k => {
      const sel = document.getElementById('f-' + k);
      (f[k] || []).forEach(v => {
        const o = document.createElement('option');
        o.value = v;
        o.textContent = (k === 'month') ? v + '月' : v;
        sel.appendChild(o);
      });
    });
  });
}

function currentQuery() {
  const q = new URLSearchParams();
  FILTER_KEYS.forEach(k => {
    const v = document.getElementById('f-' + k).value;
    if (v) q.set(k, v);
  });
  Object.keys(LINK_STATE).forEach(k => {
    if (LINK_STATE[k]) q.set(k, LINK_STATE[k]);
  });
  return q.toString();
}

function updateFilterInfo() {
  const parts = FILTER_KEYS
    .map(k => [k, document.getElementById('f-' + k).value])
    .filter(x => x[1])
    .map(x => FILTER_LABELS[x[0]] + '=' + x[1]);
  document.getElementById('fb-info').textContent =
    parts.length ? '已筛选: ' + parts.join(' | ') : '已筛选: 全部数据';
}

/* ---------- 数据加载：150ms 防抖 + 请求序号（丢弃过期响应） + 全局加载遮罩 ---------- */
let LOAD_SEQ = 0;      // 请求序号：快速连点筛选时旧响应直接丢弃
let LOAD_TIMER = null;
const LOAD_DEBOUNCE_MS = 150;

/* 顶部细进度条（方案二）：有反馈但不遮挡内容。
   显示时金线从 0 推进到 72%，完成时补到 100% 再淡出；短请求表现为顶部一闪。 */
let LB_TIMER = null;

function showLoading() {
  const b = document.getElementById('loading-bar');
  if (!b) return;
  clearTimeout(LB_TIMER);
  b.classList.remove('fin', 'reset');
  b.classList.add('on');
}
function hideLoading() {
  const b = document.getElementById('loading-bar');
  if (!b) return;
  clearTimeout(LB_TIMER);
  if (!b.classList.contains('on')) return;   /* 从未显示：无需收尾 */
  b.classList.remove('on');
  b.classList.add('fin');
  LB_TIMER = setTimeout(() => {
    b.classList.remove('fin');
    b.classList.add('reset');
    void b.offsetWidth;        /* 强制回流，保证下次从 0 起跑 */
    b.classList.remove('reset');
  }, 480);
}

/* 主四页底部指标口径说明（后端 /api/dashboard 的 metric_notes 一并返回） */
function renderMetricNotes(notes) {
  Object.keys(notes || {}).forEach(k => {
    const el = document.getElementById(k + '-note');
    if (el) el.textContent = '口径说明：' + notes[k];
  });
}

function loadData() {
  updateFilterInfo();
  renderLinkBar();
  clearTimeout(LOAD_TIMER);
  LOAD_TIMER = setTimeout(() => {
    const seq = ++LOAD_SEQ;
    showLoading();
    /* 主数据 + 预测并行请求：一次渲染完成，避免主图二次重绘造成闪烁 */
    const q = currentQuery();
    Promise.all([
      fetch('/api/dashboard?' + q).then(r => r.json()),
      fetch('/api/forecast?' + q).then(r => r.json()).catch(() => null),
    ]).then(([d, fc]) => {
      if (seq !== LOAD_SEQ) return;   /* 已有更新的请求：丢弃本次过期响应 */
      LAST_DATA = d;
      FC_DATA = fc;
      KPI_META = d.metric_meta || {};
      document.getElementById('data-range').textContent = d.data_range;
      renderMetricNotes(d.metric_notes);
      renderOverview(d);
      renderP1(d.p1);
      renderP2(d.p2);
      renderP3(d.p3);
      renderP4(d.p4);
      resizeAll();
      hideLoading();
      /* 沙盘抽屉开着时：筛选变化后基线同步刷新 */
      if (document.getElementById('sim-modal').classList.contains('show')) simRequest();
    }).catch(e => {
      if (seq === LOAD_SEQ) hideLoading();
      console.error('数据加载失败', e);
    });
    /* 自动洞察：同一套筛选参数独立请求，共享请求序号防抖（旧响应直接丢弃） */
    fetch('/api/insights?' + currentQuery()).then(r => r.json()).then(list => {
      if (seq !== LOAD_SEQ) return;
      renderInsights(list);
    }).catch(e => {
      if (seq === LOAD_SEQ) console.error('洞察加载失败', e);
    });
  }, LOAD_DEBOUNCE_MS);
}

/* ---------- 联动状态管理 ---------- */
function toggleLink(key, value) {
  LINK_STATE[key] = (LINK_STATE[key] === value) ? '' : value;
  loadData();
}

function renderLinkBar() {
  const bar = document.getElementById('link-bar');
  const chips = Object.keys(LINK_STATE).filter(k => LINK_STATE[k])
    .map(k => '<span class="link-chip">' + LINK_LABELS[k] + ': <b>' + escapeHtml(LINK_STATE[k]) + '</b>' +
      '<span class="x" data-link="' + k + '">&times;</span></span>').join('');
  document.getElementById('link-chips').innerHTML = chips;
  bar.classList.toggle('hidden', !chips);
  document.querySelectorAll('[data-link]').forEach(el => {
    el.onclick = () => { LINK_STATE[el.dataset.link] = ''; loadData(); };
  });
}
document.getElementById('lb-clear').addEventListener('click', () => {
  Object.keys(LINK_STATE).forEach(k => LINK_STATE[k] = '');
  loadData();
});

/* 客群/性别/年龄饼图点击 -> 同步到全局筛选器下拉（toggle 语义） */
function toggleGlobalFilter(key, value) {
  const sel = document.getElementById('f-' + key);
  sel.value = (sel.value === value) ? '' : value;
  loadData();
}

/* ---------- 异常归因抽屉（样式复用下钻抽屉，逻辑独立） ---------- */
function openAnomalyDrawer(ym) {
  document.getElementById('anomaly-modal').classList.add('show');
  document.getElementById('anomaly-title').textContent = ym + ' · 异常归因';
  /* 归因体将重建图表容器 DOM，先释放旧实例（否则二次打开时图表空白） */
  disposeChart('anom-seg');
  disposeChart('anom-cat');
  document.getElementById('anomaly-body').innerHTML = '<div class="p6-empty">归因分析加载中…</div>';
  fetch('/api/anomaly?ym=' + encodeURIComponent(ym)).then(r => r.json()).then(d => {
    if (d.error) {
      document.getElementById('anomaly-body').innerHTML =
        '<div class="p6-empty">' + escapeHtml(d.error) + '</div>';
      return;
    }
    renderAnomalyAttribution(d);
  }).catch(e => {
    console.error('归因加载失败', e);
    document.getElementById('anomaly-body').innerHTML = '<div class="p6-empty">归因加载失败，请重试</div>';
  });
}

function closeAnomalyDrawer() {
  document.getElementById('anomaly-modal').classList.remove('show');
}

document.getElementById('anomaly-close').addEventListener('click', closeAnomalyDrawer);
document.getElementById('anomaly-modal').addEventListener('click', e => {
  if (e.target.id === 'anomaly-modal') closeAnomalyDrawer();
});

function renderAnomalyAttribution(d) {
  const body = document.getElementById('anomaly-body');
  const wan = v => (v / 10000).toFixed(1) + ' 万';
  const pctTxt = p => p == null ? '—' : (p >= 0 ? '+' : '') + p + '%';
  const pctCls = p => p == null ? '' : (p >= 0 ? ' up' : ' down');
  body.innerHTML =
    '<div class="modal-summary">' +
    '<div class="ms-item"><div class="ms-label">当月 GMV</div><div class="ms-value">' + wan(d.gmv) + '</div></div>' +
    '<div class="ms-item"><div class="ms-label">环比上月</div><div class="ms-value' + pctCls(d.pct) + '">' + pctTxt(d.pct) + '</div></div>' +
    '<div class="ms-item"><div class="ms-label">异常分值 |z|</div><div class="ms-value">' + Math.abs(d.z).toFixed(2) + '</div></div>' +
    '</div>' +
    '<div class="modal-section-title">客群贡献 · 较上月变化（按绝对变化排序）</div>' +
    '<div class="anom-chart" id="anom-seg"></div>' +
    '<div class="modal-section-title">品类贡献 · 较上月变化（按绝对变化排序）</div>' +
    '<div class="anom-chart" id="anom-cat"></div>' +
    '<div class="p6-note">口径：变化量 = 当月销售额 − 上月销售额（与总览页同月数据同口径，金额转万展示）；' +
    '绿 = 增长、红 = 下降；数值来自 /api/anomaly 真实聚合。</div>';
  renderAnomalyBars('anom-seg', d.seg_drivers);
  renderAnomalyBars('anom-cat', d.cat_drivers);
  resizeAll();
}

/* 归因横向条形：变化量（万），正=好绿 #57A574，负=差红 #CF5B52，最多展示前 10 组 */
function renderAnomalyBars(chartId, drivers) {
  const ch = getChart(chartId);
  if (!drivers || !drivers.length) {
    ch.setOption({ graphic: [{ type: 'text', left: 'center', top: 'middle',
      style: { text: '无数据', fill: '#6A7280', fontSize: 14 } }] }, true);
    return;
  }
  const top = drivers.slice(0, 10);
  const names = top.map(v => v.name).reverse();
  const data = top.map(v => ({
    value: +(v.delta / 10000).toFixed(2),
    itemStyle: { color: v.delta >= 0 ? '#57A574' : '#CF5B52' }
  })).reverse();
  ch.setOption({
    tooltip: Object.assign({ trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: ps => {
        const v = top[top.length - 1 - ps[0].dataIndex];
        return escapeHtml(v.name) + '<br/>本月: ' + (v.cur / 10000).toFixed(1) + ' 万' +
          '<br/>上月: ' + (v.prev / 10000).toFixed(1) + ' 万' +
          '<br/>变化: ' + (v.delta >= 0 ? '+' : '') + (v.delta / 10000).toFixed(1) + ' 万' +
          (v.pct == null ? '' : '（' + (v.pct >= 0 ? '+' : '') + v.pct + '%）');
      } }, TOOLTIP),
    grid: { left: 96, right: 46, top: 8, bottom: 24 },
    xAxis: Object.assign({ type: 'value', name: '变化(万)', nameTextStyle: { color: '#8E96A0' } }, AXIS_STYLE),
    yAxis: Object.assign({ type: 'category', data: names,
      axisLabel: { color: '#E7E4DC', fontSize: 11.5 } }, AXIS_STYLE),
    series: [{ type: 'bar', barWidth: 12, data: data,
      label: { show: true, position: 'right', color: '#9AA3AE', fontSize: 10.5,
        formatter: p => (p.value >= 0 ? '+' : '') + p.value } }]
  }, true);
}

/* ---------- 下钻抽屉（一级） ---------- */
function openDrawer() {
  document.getElementById('drill-modal').classList.add('show');
}
function closeDrawer() {
  document.getElementById('drill-modal').classList.remove('show');
}
document.getElementById('drill-close').addEventListener('click', closeDrawer);
document.getElementById('drill-modal').addEventListener('click', e => {
  if (e.target.id === 'drill-modal') closeDrawer();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    /* ⑦ 问答答案卡在最上层，优先关闭 */
    if (document.getElementById('ask-card').classList.contains('show')) { closeAskCard(); return; }
    if (document.getElementById('drill-modal').classList.contains('show')) closeDrawer();
    else if (document.getElementById('anomaly-modal').classList.contains('show')) closeAnomalyDrawer();
    else if (document.getElementById('fc-modal').classList.contains('show')) closeFcDrawer();
    else if (document.getElementById('sim-modal').classList.contains('show')) closeSim();
    else if (document.getElementById('p6').classList.contains('show')) closeDeepPage();
  }
});

/* 记录打开抽屉时的来源页签 */
function markSource() {
  const a = document.querySelector('.page.active');
  DEEP_FROM = a ? a.id : 'p0';
}

function openDrillModal(seg) {
  markSource();
  MONTH_REPORT_YM = null;   /* 客群下钻不显示月报按钮（仅月度下钻有） */
  setMonthReportBtn(null);
  const fy = document.getElementById('f-year').value;
  const fm = document.getElementById('f-month').value;
  DEEP_CTX = { type: 'seg', seg: seg, year: fy, month: fm, category: LINK_STATE.category };
  const q = new URLSearchParams({ seg: seg });
  if (fy) q.set('year', fy);   /* 抽屉数字与页面 KPI 同口径：把年份/月份筛选带进下钻 */
  if (fm) q.set('month', fm);
  if (LINK_STATE.category) q.set('category', LINK_STATE.category);
  fetch('/api/drill?' + q.toString()).then(r => r.json()).then(d => {
    const s = d.summary;
    document.getElementById('drill-title').textContent =
      '客群下钻 - ' + s.seg + ' · ' + (s.scope || '全时段') + '（' + s.cnt.toLocaleString() + ' 人）';
    const tierTags = Object.entries(s.tier_dist)
      .map(x => '<span class="tt">' + escapeHtml(x[0]) + ': ' + x[1] + '人</span>').join('');
    const rows = d.customers.slice(0, 5).map((c, i) =>
      '<tr><td>' + (i + 1) + '</td><td>CUST-' + c.key + '</td><td>' + c.f + '</td>' +
      '<td>' + c.m.toLocaleString() + '</td><td>' + c.r + '</td><td>' + c.score + '</td>' +
      '<td>' + (c.awaken ? '<span class="tag-awake">已唤醒</span>' : '<span class="tag-norm">未唤醒</span>') + '</td></tr>').join('');
    document.getElementById('drill-body').innerHTML =
      '<div class="modal-summary">' +
      '<div class="ms-item"><div class="ms-label">客群人数</div><div class="ms-value">' + s.cnt.toLocaleString() + '</div></div>' +
      '<div class="ms-item"><div class="ms-label">贡献销售(万)</div><div class="ms-value">' + s.total_sales + '</div></div>' +
      '<div class="ms-item"><div class="ms-label">笔单价(元)</div><div class="ms-value">' + s.avg_m.toLocaleString() + '</div></div>' +
      '<div class="ms-item"><div class="ms-label">人均订单F</div><div class="ms-value">' + s.avg_f + '</div></div>' +
      '<div class="ms-item"><div class="ms-label">平均R值(天)</div><div class="ms-value">' + s.avg_r + '</div></div>' +
      '<div class="ms-item"><div class="ms-label">唤醒客户数</div><div class="ms-value">' + s.awaken_cnt + '</div></div>' +
      '</div>' +
      '<div class="modal-section-title">营销层级构成（CustomerTier 口径）</div>' +
      '<div class="tier-tags">' + tierTags + '</div>' +
      '<div class="modal-section-title">Top 5 客户速览</div>' +
      '<table class="drill-table"><thead><tr>' +
      '<th>排名</th><th>客户ID</th><th>F</th><th>M(元)</th>' +
      '<th>R(天)</th><th>RFM总分</th><th>唤醒状态</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>';
    openDrawer();
  });
}

/* 月度下钻（一级）：单月摘要 + 品类 Top5 + 商品 Top5 */
function openMonthDrill(ym) {
  markSource();
  DEEP_CTX = { type: 'month', ym: ym, seg: LINK_STATE.seg, category: LINK_STATE.category };
  MONTH_REPORT_YM = ym;
  setMonthReportBtn(ym, true, '加载中…');
  const q = new URLSearchParams({ type: 'month', ym: ym });
  if (LINK_STATE.seg) q.set('seg', LINK_STATE.seg);
  if (LINK_STATE.category) q.set('category', LINK_STATE.category);
  fetch('/api/drill?' + q.toString()).then(r => r.json()).then(d => {
    if (d.empty) {
      document.getElementById('drill-title').textContent = '月度下钻 - ' + ym;
      document.getElementById('drill-body').innerHTML = '<div class="p6-empty">当前筛选条件下无数据</div>';
      /* 无数据月份：月报按钮置灰并提示，不硬凑 */
      setMonthReportBtn(ym, true, '本月无数据');
      openDrawer();
      return;
    }
    const s = d.summary;
    const awakePct = s.order_cnt ? (s.awake_cnt / s.order_cnt * 100).toFixed(1) : '0.0';
    const cats = d.top_category.slice(0, 5).map(c =>
      '<div class="cat-item"><div class="cat-name">' + escapeHtml(c.name) + '</div>' +
      '<div class="cat-bar"><i style="width:' + Math.max(2, c.pct) + '%"></i></div>' +
      '<div class="cat-val">' + c.sales + ' 万 · ' + c.pct + '%</div></div>').join('');
    const rows = d.top_product.slice(0, 5).map((p, i) =>
      '<tr><td>' + (i + 1) + '</td><td>' + escapeHtml(p.name) + '</td><td>' + escapeHtml(p.category) + '</td>' +
      '<td>' + p.sales + ' 万</td></tr>').join('');
    document.getElementById('drill-title').textContent = '月度下钻 - ' + s.ym;
    document.getElementById('drill-body').innerHTML =
      '<div class="modal-summary">' +
      '<div class="ms-item"><div class="ms-label">销售金额(万)</div><div class="ms-value">' + s.total_sales + '</div></div>' +
      '<div class="ms-item"><div class="ms-label">订单数</div><div class="ms-value">' + s.order_cnt.toLocaleString() + '</div></div>' +
      '<div class="ms-item"><div class="ms-label">购买客户数</div><div class="ms-value">' + s.cust_cnt.toLocaleString() + '</div></div>' +
      '<div class="ms-item"><div class="ms-label">笔单价(元)</div><div class="ms-value">' + s.aov.toLocaleString() + '</div></div>' +
      '<div class="ms-item"><div class="ms-label">唤醒订单数</div><div class="ms-value">' + s.awake_cnt.toLocaleString() + '</div></div>' +
      '<div class="ms-item"><div class="ms-label">唤醒占比</div><div class="ms-value">' + awakePct + '%</div></div>' +
      '</div>' +
      '<div class="modal-section-title">品类构成 Top 5</div>' +
      '<div class="cat-list">' + cats + '</div>' +
      '<div class="modal-section-title">Top 5 商品</div>' +
      '<table class="drill-table"><thead><tr>' +
      '<th>排名</th><th>商品</th><th>品类</th><th>销售额(万)</th></tr></thead>' +
      '<tbody>' + rows + '</tbody></table>';
    setMonthReportBtn(ym, false, '生成本月月报');
    openDrawer();
  });
}

/* ---------- 经营月报（模板叙事，月度下钻抽屉内生成与复制） ---------- */
let MONTH_REPORT_YM = null;   // 当前抽屉对应月份；客群下钻时置 null 隐藏月报按钮

function setMonthReportBtn(ym, disabled, text) {
  const btn = document.getElementById('btn-month-report');
  if (ym === null) { btn.style.display = 'none'; return; }
  btn.style.display = '';
  btn.disabled = !!disabled;
  btn.textContent = text || '生成本月月报';
  btn.title = disabled ? '该月无交易数据，无法生成月报' : '';
}

function copyReportText(text, btn) {
  /* 优先剪贴板 API，失败降级 document.execCommand（覆盖 file:// 等非安全上下文） */
  const done = () => { btn.textContent = '已复制'; setTimeout(() => { btn.textContent = '复制全文'; }, 2000); };
  const fail = () => { btn.textContent = '复制失败'; setTimeout(() => { btn.textContent = '复制全文'; }, 2000); };
  const fallback = () => {
    const ta = document.createElement('textarea');
    ta.value = text; ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0';
    document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0, text.length);
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    ok ? done() : fail();
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, fallback);
  } else fallback();
}

function generateMonthlyReport() {
  if (!MONTH_REPORT_YM) return;
  const ym = MONTH_REPORT_YM;
  setMonthReportBtn(ym, true, '生成中…');
  fetch('/api/monthly_report?ym=' + encodeURIComponent(ym)).then(r => r.json()).then(d => {
    setMonthReportBtn(ym, false, '重新生成本月月报');
    const body = document.getElementById('drill-body');
    if (d.empty || d.error) {
      /* 数据不足月返回空态提示，不硬凑 */
      body.insertAdjacentHTML('beforeend',
        '<div class="month-report"><div class="report-head"><span class="report-title">经营月报</span></div>' +
        '<div class="report-text">' + escapeHtml(d.reason || d.error || '该月数据不足，无法生成月报') + '</div></div>');
      return;
    }
    const full = d.text + '\n\n口径说明：' + d.metric_note;
    body.insertAdjacentHTML('beforeend',
      '<div class="month-report"><div class="report-head">' +
      '<span class="report-title">经营月报 · ' + escapeHtml(d.ym) + '</span>' +
      '<button class="btn-copy" id="copy-report">复制全文</button></div>' +
      '<div class="report-text">' + escapeHtml(d.text) + '</div>' +
      '<div class="report-note">口径说明：' + escapeHtml(d.metric_note) + '</div></div>');
    const copyBtn = document.getElementById('copy-report');
    if (copyBtn) copyBtn.addEventListener('click', () => copyReportText(full, copyBtn));
    body.scrollTop = body.scrollHeight;   /* 面板追加后滚到底部 */
  }).catch(() => {
    setMonthReportBtn(ym, false, '生成失败，点击重试');
  });
}
document.getElementById('btn-month-report').addEventListener('click', generateMonthlyReport);

/* 抽屉底部「查看完整分析」：关闭抽屉并打开覆盖页（不切页签） */
document.getElementById('drawer-full').addEventListener('click', () => {
  if (!DEEP_CTX) return;
  closeDrawer();
  openDeepPage();
});

/* ---------- 二级分析页：客群深挖 ---------- */
function disposeP6() {
  Object.keys(CHARTS).forEach(id => {
    if (id.indexOf('p6-') === 0) { CHARTS[id].dispose(); delete CHARTS[id]; }
  });
}

function p6Empty(msg) {
  document.getElementById('p6-kpi').innerHTML = '';
  document.getElementById('p6-content').innerHTML =
    '<div class="p6-empty">' + (msg || '当前筛选条件下无数据') + '</div>';
  const note = document.getElementById('p6-note');
  note.style.display = 'none';
}

function showP6Note(text) {
  const note = document.getElementById('p6-note');
  if (text) { note.textContent = '口径说明：' + text; note.style.display = 'block'; }
  else note.style.display = 'none';
}

/* ---------- 二级分析覆盖页：客群深挖（不切页签，仅下钻弹出） ---------- */
function openDeepPage() {
  document.getElementById('p6').classList.add('show');
  openDeepDive();
}

function closeDeepPage() {
  document.getElementById('p6').classList.remove('show');
  disposeP6();
  resizeAll();
}

function openDeepDive() {
  if (!DEEP_CTX) { p6Empty('请从下钻抽屉进入完整分析'); return; }
  disposeP6();
  const crumb = document.getElementById('p6-crumb');
  if (DEEP_CTX.type === 'seg') {
    crumb.innerHTML = '客群深挖<span class="cb-sep">/</span><b style="color:#C9A227">' + escapeHtml(DEEP_CTX.seg) + '</b>';
    const q = new URLSearchParams({ seg: DEEP_CTX.seg });
    if (DEEP_CTX.year) q.set('year', DEEP_CTX.year);
    if (DEEP_CTX.month) q.set('month', DEEP_CTX.month);
    if (DEEP_CTX.category) q.set('category', DEEP_CTX.category);
    fetch('/api/segment_profile?' + q.toString()).then(r => r.json()).then(renderSegProfile);
  } else {
    crumb.innerHTML = '月度深挖<span class="cb-sep">/</span><b style="color:#C9A227">' + escapeHtml(DEEP_CTX.ym) + '</b>';
    const q = new URLSearchParams({ ym: DEEP_CTX.ym });
    if (DEEP_CTX.seg) q.set('seg', DEEP_CTX.seg);
    if (DEEP_CTX.category) q.set('category', DEEP_CTX.category);
    fetch('/api/month_profile?' + q.toString()).then(r => r.json()).then(renderMonthProfile);
  }
}

/* 客群画像小环形图 */
function miniPie(id, data, title) {
  const chart = getChart(id);
  chart.setOption({
    tooltip: Object.assign({ trigger: 'item', formatter: '{b}: {c} ({d}%)' }, TOOLTIP),
    series: [{
      type: 'pie', radius: ['42%', '68%'], center: ['50%', '54%'],
      data: data.map((v, i) => ({ name: v.name, value: v.value, itemStyle: { color: PALETTE[i % PALETTE.length] } })),
      label: { color: '#8E96A0', fontSize: 11, formatter: '{b}\n{d}%' },
      labelLine: { length: 6, length2: 5 }, itemStyle: { borderColor: '#15181D', borderWidth: 2 }
    }]
  }, true);
}

let top20SortState = 'm';
function renderTop20Table(list) {
  const rows = list.map((c, i) =>
    '<tr><td>' + (i + 1) + '</td><td>CUST-' + c.key + '</td><td>' + c.f + '</td>' +
    '<td>' + c.m.toLocaleString() + '</td><td>' + c.r + '</td><td>' + c.score + '</td>' +
    '<td>' + escapeHtml(c.gender) + '</td><td>' + escapeHtml(c.city) + '</td><td>' + c.last_month + '</td>' +
    '<td>' + (c.awaken ? '<span class="tag-awake">已唤醒</span>' : '<span class="tag-norm">未唤醒</span>') + '</td></tr>').join('');
  return '<table class="drill-table"><thead><tr>' +
    '<th>#</th><th>客户ID</th><th>F</th><th>M(元)</th><th>R(天)</th><th>RFM</th>' +
    '<th>性别</th><th>城市</th><th>最近购买</th><th>唤醒</th></tr></thead>' +
    '<tbody>' + rows + '</tbody></table>';
}

function renderSegProfile(d) {
  if (d.empty) { p6Empty(); showP6Note(d.metric_note); return; }
  renderKPI('p6-kpi', d.kpi);
  document.getElementById('p6-content').innerHTML =
    '<div class="p6-panel full"><div class="pp-title">客户画像（性别 / 年龄段 / 城市层级）</div>' +
    '<div class="p6-profile-row">' +
    '<div class="p6-chart" id="p6-gender"></div>' +
    '<div class="p6-chart" id="p6-age"></div>' +
    '<div class="p6-chart" id="p6-city"></div></div></div>' +
    '<div class="p6-panel full"><div class="pp-title">月度销售迷你趋势（全时段）</div>' +
    '<div class="p6-chart" id="p6-trend"></div></div>' +
    '<div class="p6-panel"><div class="pp-title">品类偏好 Top 3</div><div id="p6-catpref"></div></div>' +
    '<div class="p6-panel"><div class="pp-title">该客群贡献占比（销售额）</div><div class="p6-chart" id="p6-cattr"></div></div>' +
    '<div class="p6-panel full"><div class="pp-title">Top 20 客户全表</div>' +
    '<div class="p6-sort-bar">' +
    '<button class="p6-sort" data-sort="m">按 M 消费</button>' +
    '<button class="p6-sort" data-sort="f">按 F 频次</button>' +
    '<button class="p6-sort" data-sort="r">按最近购买</button></div>' +
    '<div id="p6-top20" style="overflow:auto"></div></div>';

  miniPie('p6-gender', d.profile.gender);
  miniPie('p6-age', d.profile.age);
  miniPie('p6-city', d.profile.city);

  /* 趋势：裁掉前导 0（全时段无交易月） */
  const nz = d.trend.findIndex(v => v.sales > 0);
  const tr = nz > 0 ? d.trend.slice(nz) : d.trend;
  const note = nz > 0 ? (d.trend[nz].ym + ' 起（此前该客群无交易）') : '';
  getChart('p6-trend').setOption({
    title: { text: note, left: 'center', top: 0,
      textStyle: { color: '#8E96A0', fontSize: 11, fontWeight: 'normal' } },
    tooltip: Object.assign({ trigger: 'axis', formatter: ps => tr[ps[0].dataIndex].ym + '<br/>销售额: ' + ps[0].value + ' 万' }, TOOLTIP),
    grid: { left: 56, right: 24, top: 30, bottom: 40 },
    xAxis: Object.assign({ type: 'category', data: tr.map(v => v.ym),
      axisLabel: { color: '#8E96A0', fontSize: 10, interval: 2, rotate: 35 } }, AXIS_STYLE),
    yAxis: Object.assign({ type: 'value', name: '万元', nameTextStyle: { color: '#8E96A0' } }, AXIS_STYLE),
    series: [{ name: '月度销售(万)', type: 'line', smooth: true, symbol: 'circle', symbolSize: 5,
      lineStyle: { width: 2.5, color: '#57A574', shadowColor: 'rgba(87,165,116,.25)', shadowBlur: 10 },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
        colorStops: [{ offset: 0, color: 'rgba(87,165,116,.14)' }, { offset: 1, color: 'rgba(87,165,116,.02)' }] } },
      data: tr.map(v => v.sales) }]
  }, true);

  /* 品类偏好 Top3 列表 */
  document.getElementById('p6-catpref').innerHTML = d.category_pref.map(c =>
    '<div class="cat-item"><div class="cat-name">' + escapeHtml(c.name) + '</div>' +
    '<div class="cat-bar"><i style="width:' + Math.max(2, c.pct) + '%"></i></div>' +
    '<div class="cat-val">' + c.sales + ' 万 · ' + c.pct + '%</div></div>').join('') ||
    '<div class="p6-empty">无品类交易数据</div>';

  /* 品类占比环形（用 category_pref 的 pct 补全其他） */
  const pieData = d.category_pref.map((c, i) => ({ name: c.name, value: c.sales }));
  getChart('p6-cattr').setOption({
    tooltip: Object.assign({ trigger: 'item', formatter: '{b}: {c} 万 ({d}%)' }, TOOLTIP),
    legend: { orient: 'vertical', right: 6, top: 'middle', itemWidth: 12, itemHeight: 12,
      textStyle: { color: '#8E96A0', fontSize: 11 } },
    series: [{ type: 'pie', radius: ['42%', '66%'], center: ['38%', '52%'],
      data: pieData.map((v, i) => Object.assign({}, v, { itemStyle: { color: PALETTE[i % PALETTE.length] } })),
      label: { color: '#8E96A0', fontSize: 11, formatter: '{b}\n{d}%' },
      itemStyle: { borderColor: '#15181D', borderWidth: 2 } }]
  }, true);

  /* Top20 表 + 排序切换 */
  let segList = d.top_customers.slice();
  const render = () => {
    document.getElementById('p6-top20').innerHTML = renderTop20Table(segList);
    document.querySelectorAll('.p6-sort').forEach(b =>
      b.classList.toggle('active', b.dataset.sort === top20SortState));
  };
  const applySort = key => {
    top20SortState = key;
    segList = d.top_customers.slice().sort((a, b) => {
      if (key === 'r') return a.r - b.r;              /* R 越小越近 */
      return b[key] - a[key];                          /* m / f 降序 */
    });
    render();
  };
  document.querySelectorAll('.p6-sort').forEach(b =>
    b.addEventListener('click', () => applySort(b.dataset.sort)));
  applySort('m');

  showP6Note(d.metric_note);
  resizeAll();
}

function renderMonthProfile(d) {
  if (d.empty) { p6Empty(); showP6Note(d.metric_note); return; }
  renderKPI('p6-kpi', d.kpi);
  const fmtMom = v => v == null ? '<span class="flat">—</span>'
    : '<b class="' + (v >= 0 ? 'up' : 'down') + '">' + (v >= 0 ? '+' : '') + v + '%</b>';
  document.getElementById('p6-content').innerHTML =
    '<div class="p6-panel full"><div class="pp-title">环比上一月（' + (d.mom.prev_ym || '—') + '）</div>' +
    '<div class="p6-mom">' +
    '<div class="mm">销售金额环比' + fmtMom(d.mom.sales) + '</div>' +
    '<div class="mm">订单数环比' + fmtMom(d.mom.orders) + '</div></div></div>' +
    '<div class="p6-panel"><div class="pp-title">客户复购构成（当月）</div>' +
    '<div class="p6-chart" id="p6-repurchase"></div></div>' +
    '<div class="p6-panel"><div class="pp-title">品类构成（全量）</div>' +
    '<div class="p6-chart" id="p6-catpie"></div></div>' +
    '<div class="p6-panel full"><div class="pp-title">Top 10 商品</div>' +
    '<table class="drill-table"><thead><tr><th>#</th><th>品名</th><th>品类</th>' +
    '<th>销量</th><th>销售金额(万)</th><th>金额占比</th></tr></thead><tbody>' +
    d.top_product.map((p, i) =>
      '<tr><td>' + (i + 1) + '</td><td>' + escapeHtml(p.name) + '</td><td>' + escapeHtml(p.category) + '</td>' +
      '<td>' + p.qty.toLocaleString() + '</td><td>' + p.sales + '</td><td>' + p.pct + '%</td></tr>').join('') +
    '</tbody></table></div>' +
    '<div class="p6-panel full"><div class="pp-title">当月 Top 10 消费客户</div>' +
    '<table class="drill-table"><thead><tr><th>#</th><th>客户ID</th><th>客群</th>' +
    '<th>订单数</th><th>消费金额(万)</th></tr></thead><tbody>' +
    d.top_customers.map((c, i) =>
      '<tr><td>' + (i + 1) + '</td><td>CUST-' + c.key + '</td><td>' + escapeHtml(c.seg) + '</td>' +
      '<td>' + c.order_cnt + '</td><td>' + c.amount + '</td></tr>').join('') +
    '</tbody></table></div>';

  /* 复购对比柱 */
  getChart('p6-repurchase').setOption({
    tooltip: Object.assign({ trigger: 'axis', axisPointer: { type: 'shadow' } }, TOOLTIP),
    grid: { left: 48, right: 24, top: 24, bottom: 30 },
    xAxis: Object.assign({ type: 'category', data: ['下单1次', '下单≥2次'],
      axisLabel: { color: '#E7E4DC', fontSize: 13 } }, AXIS_STYLE),
    yAxis: Object.assign({ type: 'value', name: '客户数', nameTextStyle: { color: '#8E96A0' } }, AXIS_STYLE),
    series: [{ type: 'bar', barWidth: 48, data: [
      { value: d.repurchase.once, itemStyle: { color: '#6E7F91' } },
      { value: d.repurchase.multi, itemStyle: { color: '#57A574' } }],
      label: { show: true, position: 'top', color: '#E7E4DC', fontSize: 13 } }]
  }, true);

  /* 品类环形 */
  getChart('p6-catpie').setOption(pieOption(d.category_pie.map(v => ({ name: v.name, value: v.value })), {
    radius: ['40%', '64%'], center: ['40%', '52%'] }), true);

  showP6Note(d.metric_note);
  resizeAll();
}

/* 二级页返回按钮：关闭覆盖页（不切页签，回到来源页面） */
document.getElementById('p6-back').addEventListener('click', closeDeepPage);

FILTER_KEYS.forEach(k => {
  document.getElementById('f-' + k).addEventListener('change', loadData);
});
document.getElementById('btn-reset').addEventListener('click', () => {
  FILTER_KEYS.forEach(k => document.getElementById('f-' + k).value = '');
  loadData();
});

/* ---------- 轻量提示条（toast） ---------- */
let TOAST_TIMER = null;
function showToast(text, isErr) {
  const t = document.getElementById('toast');
  t.textContent = text;
  t.classList.toggle('err', !!isErr);
  t.classList.add('show');
  clearTimeout(TOAST_TIMER);
  TOAST_TIMER = setTimeout(() => t.classList.remove('show'), 3200);
}

/* ---------- 客群名单导出（分析 → 行动闭环，⑥） ---------- */
document.getElementById('btn-export').addEventListener('click', async () => {
  const btn = document.getElementById('btn-export');
  const q = currentQuery();
  /* 全量导出（无任何筛选）时二次确认 */
  if (!q && !window.confirm('当前未设置任何筛选，将导出全部客户名单（最多 5000 行），是否继续？')) return;
  btn.disabled = true;
  showToast('正在生成名单…');
  try {
    const r = await fetch('/api/export/segment' + (q ? '?' + q : ''));
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      showToast(d.error || '导出失败，请稍后重试', true);
      return;
    }
    const blob = await r.blob();
    /* 解析文件名（RFC 5987 filename*=UTF-8''...）与行数，用于反馈 */
    const cd = r.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename\*=UTF-8''([^;\s]+)/);
    const fname = m ? decodeURIComponent(m[1]) : 'segment_export.csv';
    const text = await blob.text();
    const rows = Math.max(0, text.replace(/^\uFEFF/, '').trim().split('\n').length - 1);
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    showToast('名单已导出：' + rows.toLocaleString() + ' 名客户（' + fname + '）');
  } catch (e) {
    console.error('导出失败', e);
    showToast('导出失败：' + e.message, true);
  } finally {
    btn.disabled = false;
  }
});

/* ---------- What-if 营销沙盘（⑤） ---------- */
const SIM = { seq: 0, timer: null };
const SIM_DEBOUNCE_MS = 150;
const SIM_FORMAT = { minimumFractionDigits: 2, maximumFractionDigits: 2 };

function fmtWan(v) { return Number(v).toLocaleString('zh-CN', SIM_FORMAT); }

function openSim() {
  document.getElementById('sim-modal').classList.add('show');
  simRequest();   /* 打开即按当前滑块（默认全 0 = 基线）请求一次 */
}
function closeSim() { document.getElementById('sim-modal').classList.remove('show'); }

function simQuery() {
  const q = new URLSearchParams();
  q.set('rebuy_pp', document.getElementById('sim-rebuy').value);
  q.set('awaken_pp', document.getElementById('sim-awaken').value);
  q.set('recover_n', document.getElementById('sim-recover').value);
  /* 客户池沿用全局筛选（客群/性别/年龄/城市）；年份/月份不参与——基线固定为最近完整月 */
  ['tier', 'gender', 'age', 'city'].forEach(k => {
    const v = document.getElementById('f-' + k).value;
    if (v) q.set(k, v);
  });
  return q.toString();
}

function simRequest() {
  const seq = ++SIM.seq;
  clearTimeout(SIM.timer);
  SIM.timer = setTimeout(() => {
    fetch('/api/simulate?' + simQuery()).then(r => r.json()).then(d => {
      if (seq !== SIM.seq) return;
      if (d && d.error) { simShowError(d.error); return; }
      simRender(d);
    }).catch(e => {
      if (seq === SIM.seq) simShowError('模拟请求失败：' + e.message);
    });
  }, SIM_DEBOUNCE_MS);
}

function simShowError(msg) {
  document.getElementById('sim-lead').textContent = msg;
  ['sim-inc', 'sim-roi', 'sim-total'].forEach(id => {
    document.getElementById(id).textContent = '--';
  });
  document.getElementById('sim-table').innerHTML = '';
}

function simRender(d) {
  const rb = document.getElementById('sim-rebuy');
  const aw = document.getElementById('sim-awaken');
  const rc = document.getElementById('sim-recover');
  /* 沉睡池人数随筛选变化：动态同步挽回滑块上限，越界值收敛 */
  const rcMax = Math.max(1, d.limits.recover_n);
  if (+rc.max !== rcMax) rc.max = rcMax;
  if (+rc.value > rcMax) { rc.value = rcMax; simSyncLabels(); simRequest(); return; }
  rc.disabled = d.limits.recover_n <= 0;

  document.getElementById('sim-lead').textContent =
    '基线月 ' + d.ym + '：GMV ' + fmtWan(d.baseline.gmv) + ' 万 · ' +
    d.baseline.cust.toLocaleString() + ' 名客户 · ' + d.baseline.orders.toLocaleString() +
    ' 笔。数字全部来自真实交易聚合，拖动滑块 150ms 防抖实时测算。';

  document.getElementById('sim-rebuy-hint').textContent =
    '当前复购率 ' + d.current.hv_rebuy_rate + '% · 高价值客群 ' + d.current.hv_n.toLocaleString() +
    ' 人（新增人数以未购买 ' + d.limits.rebuy_persons_cap.toLocaleString() + ' 人封顶）';
  document.getElementById('sim-awaken-hint').textContent =
    '当前唤醒率 ' + d.current.sl_awaken_rate + '% · 沉睡客群 ' + d.current.sl_n.toLocaleString() +
    ' 人（未唤醒 ' + d.limits.awaken_persons_cap.toLocaleString() + ' 人）';
  document.getElementById('sim-recover-hint').textContent =
    d.limits.recover_n <= 0 ? '当前筛选下无沉睡客户可挽回'
      : '上限 ' + d.limits.recover_n.toLocaleString() + ' 人（与唤醒档共用沉睡池，自动去重）';

  const up = d.lift_pct > 0 ? ' up' : (d.lift_pct < 0 ? ' down' : '');
  document.getElementById('sim-inc').textContent = fmtWan(d.inc_gmv) + ' 万';
  document.getElementById('sim-inc-sub').textContent =
    (d.lift_pct >= 0 ? '+' : '') + d.lift_pct + '% vs 基线';
  document.getElementById('sim-inc-sub').className = 'sub' + up;
  document.getElementById('sim-roi').textContent =
    d.roi == null ? '--' : d.roi.toFixed(2);
  document.getElementById('sim-roi-sub').textContent =
    '投入 ' + fmtWan(d.cost.total) + ' 万 = 让利 ' + fmtWan(d.cost.discount) +
    ' + 触达 ' + fmtWan(d.cost.reach);
  document.getElementById('sim-total').textContent = fmtWan(d.total_gmv) + ' 万';
  document.getElementById('sim-total-sub').textContent =
    '基线 ' + fmtWan(d.baseline.gmv) + ' 万 · 触达 ' + d.cost.persons.toLocaleString() + ' 人';

  document.getElementById('sim-table').innerHTML = d.levers.map(l =>
    '<tr><td>' + escapeHtml(l.label) + '</td><td class="num">' + l.persons.toLocaleString() +
    '</td><td class="num">' + l.per_capita.toLocaleString('zh-CN') +
    '</td><td class="num">' + fmtWan(l.inc) + '</td></tr>').join('') +
    '<tr><td>合计</td><td class="num">' + d.cost.persons.toLocaleString() +
    '</td><td class="num">--</td><td class="num">' + fmtWan(d.inc_gmv) + '</td></tr>';

  document.getElementById('sim-assume').innerHTML =
    d.assumptions.map(a => '<li>' + escapeHtml(a) + '</li>').join('');
  document.getElementById('sim-note').textContent = d.note;
}

function simSyncLabels() {
  document.getElementById('sim-rebuy-val').textContent =
    '+' + document.getElementById('sim-rebuy').value + ' pp';
  document.getElementById('sim-awaken-val').textContent =
    '+' + document.getElementById('sim-awaken').value + ' pp';
  document.getElementById('sim-recover-val').textContent =
    document.getElementById('sim-recover').value + ' 人';
}

/* 滑块：标签即时更新，结果 150ms 防抖请求 */
['sim-rebuy', 'sim-awaken', 'sim-recover'].forEach(id => {
  document.getElementById(id).addEventListener('input', () => {
    simSyncLabels();
    simRequest();
  });
});
document.getElementById('btn-sim').addEventListener('click', openSim);
document.getElementById('sim-close').addEventListener('click', closeSim);
document.getElementById('sim-ok').addEventListener('click', closeSim);
document.getElementById('sim-reset').addEventListener('click', () => {
  document.getElementById('sim-rebuy').value = 0;
  document.getElementById('sim-awaken').value = 0;
  document.getElementById('sim-recover').value = 0;
  simSyncLabels();
  simRequest();
});
document.getElementById('sim-modal').addEventListener('click', e => {
  if (e.target.id === 'sim-modal') closeSim();
});
/* 支持 ?sim=1 直达沙盘（验收/截图用，不影响正常使用）；可带 rebuy/awaken/recover 预置滑块 */
if (new URLSearchParams(location.search).get('sim') === '1') {
  const qs = new URLSearchParams(location.search);
  if (qs.get('rebuy')) document.getElementById('sim-rebuy').value = Math.min(20, +qs.get('rebuy') || 0);
  if (qs.get('awaken')) document.getElementById('sim-awaken').value = Math.min(20, +qs.get('awaken') || 0);
  if (qs.get('recover')) document.getElementById('sim-recover').value = +qs.get('recover') || 0;
  simSyncLabels();
  openSim();
}

/* ---------- KPI 卡片（⑧ 每张卡右上角 ⓘ 打开口径追溯） ---------- */
function renderKPI(containerId, list) {
  const html = list.map(k => {
    const meta = k.kpi_id ? KPI_META[k.kpi_id] : null;
    return '<div class="kpi-card"' + (meta ? ' data-kpi="' + escapeHtml(k.kpi_id) + '"' : '') + '>' +
      '<div class="label">' + k.label + '</div>' +
      '<div class="value">' + k.value + '</div>' +
      (meta ? '<span class="kpi-i" role="button" tabindex="0" aria-label="查看该指标的口径追溯">i</span>' : '') +
      '</div>';
  }).join('');
  document.getElementById(containerId).innerHTML = html;
  /* ?kpi=<kpi_id> 直达口径气泡（验收/截图用，不影响正常使用） */
  const demo = new URLSearchParams(location.search).get('kpi');
  if (demo && !window.__kpiDemoDone) {
    const badge = document.querySelector('.kpi-card[data-kpi="' + demo + '"] .kpi-i');
    if (badge) {
      window.__kpiDemoDone = true;
      setTimeout(() => { KPI_POP_PINNED = true; showKpiPop(demo, badge); }, 120);
    }
  }
}

/* ---------- ⑧ 口径追溯气泡：点/悬停 ⓘ 显示公式、来源、计算步骤、口径 ---------- */
let KPI_POP_ID = null;
let KPI_POP_PINNED = false;
let KPI_POP_TIMER = null;

function showKpiPop(id, anchor) {
  const m = KPI_META[id];
  const pop = document.getElementById('kpi-pop');
  if (!m || !pop) return;
  const row = (k, v) => '<div class="kpi-pop-row"><div class="kp-k">' + k +
    '</div><div class="kp-v">' + escapeHtml(v) + '</div></div>';
  pop.innerHTML =
    '<div class="kpi-pop-head"><span class="kp-title">' + escapeHtml(m.label) + '</span>' +
    '<span class="kp-page">' + escapeHtml(m.page || '') + '</span></div>' +
    row('公式', m.formula || '—') +
    row('来源', m.source || '—') +
    row('计算步骤', m.calc_path || '—') +
    row('口径', m.note || '—') +
    '<div class="kpi-pop-foot">数字由右侧公式实时计算，非人工录入</div>';
  pop.classList.add('show');
  KPI_POP_ID = id;
  /* 定位：默认贴在角标下方右对齐，越界自动翻转到上方 / 贴边 */
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth;
  const h = pop.offsetHeight;
  let left = r.right - w;
  if (left < 12) left = 12;
  if (left + w > window.innerWidth - 12) left = window.innerWidth - w - 12;
  let top = r.bottom + 8;
  if (top + h > window.innerHeight - 12) top = r.top - h - 8;
  pop.style.left = Math.round(left) + 'px';
  pop.style.top = Math.round(Math.max(12, top)) + 'px';
}

function hideKpiPop() {
  clearTimeout(KPI_POP_TIMER);
  const pop = document.getElementById('kpi-pop');
  if (pop) pop.classList.remove('show');
  KPI_POP_ID = null;
}

function kpiBadgeOf(el) {
  return (el && el.closest) ? el.closest('.kpi-i') : null;
}

/* 悬停即预览，移出 260ms 后收起（移入气泡可取消收起） */
document.addEventListener('mouseover', e => {
  const b = kpiBadgeOf(e.target);
  if (!b) return;
  clearTimeout(KPI_POP_TIMER);
  showKpiPop(b.parentNode.getAttribute('data-kpi'), b);
});
document.addEventListener('mouseout', e => {
  const b = kpiBadgeOf(e.target);
  if (!b || KPI_POP_PINNED) return;
  clearTimeout(KPI_POP_TIMER);
  KPI_POP_TIMER = setTimeout(hideKpiPop, 260);
});

/* 点击角标：固定/取消固定；点其他地方关闭 */
document.addEventListener('click', e => {
  const b = kpiBadgeOf(e.target);
  if (b) {
    e.stopPropagation();
    e.preventDefault();
    const id = b.parentNode.getAttribute('data-kpi');
    if (KPI_POP_PINNED && KPI_POP_ID === id) {
      KPI_POP_PINNED = false;
      hideKpiPop();
    } else {
      KPI_POP_PINNED = true;
      showKpiPop(id, b);
    }
    return;
  }
  if (!e.target.closest || !e.target.closest('#kpi-pop')) {
    KPI_POP_PINNED = false;
    hideKpiPop();
  }
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && KPI_POP_ID) { KPI_POP_PINNED = false; hideKpiPop(); }
});

(function () {
  const pop = document.getElementById('kpi-pop');
  if (!pop) return;
  pop.addEventListener('mouseenter', () => clearTimeout(KPI_POP_TIMER));
  pop.addEventListener('mouseleave', () => {
    if (!KPI_POP_PINNED) KPI_POP_TIMER = setTimeout(hideKpiPop, 200);
  });
})();

/* ---------- 自动洞察卡（对标 Tableau Pulse） ---------- */
function renderInsights(list) {
  const box = document.getElementById('p0-insights');
  if (!box) return;
  if (!list || !list.length) {
    box.innerHTML = '<div class="insight-empty">当前筛选条件下暂无洞察</div>';
    return;
  }
  /* 文案与联动值均经 escapeHtml 防注入；type 仅 good/bad/warn 三值 */
  box.innerHTML = list.map(it =>
    '<div class="insight-card ' + (it.type || 'warn') + '"' +
    (it.link ? ' data-key="' + escapeHtml(it.link.key) + '" data-val="' +
      escapeHtml(String(it.link.value)) + '"' : '') + '>' +
    '<div class="ic-head"><span class="ic-tag">' + (INSIGHT_TAGS[it.type] || '洞察') + '</span>' +
    '<span class="ic-delta">' + escapeHtml(it.delta) + '</span></div>' +
    '<div class="ic-text">' + escapeHtml(it.text) + '</div></div>').join('');
  box.querySelectorAll('.insight-card').forEach(el => {
    el.onclick = () => {
      const k = el.dataset.key, v = el.dataset.val;
      if (!k) return;
      if (k === 'seg' || k === 'category') toggleLink(k, v);      /* 客群/品类写入联动 */
      else toggleGlobalFilter(k, v);                               /* 月份写入全局筛选 */
    };
  });
}

/* ---------- 空数据清屏（P0-01）：KPI 提示卡 + 图表覆盖提示文字，不允许残留旧数字 ---------- */
function clearPageCards(prefix, msg) {
  msg = msg || '当前筛选条件下暂无数据';
  /* KPI 容器渲染一张全宽提示卡，替换旧指标 */
  document.getElementById(prefix + '-kpi').innerHTML =
    '<div class="kpi-card"><div class="label">数据提示</div>' +
    '<div class="value" style="font-size:22px;color:#8E96A0;text-shadow:none">' + msg + '</div></div>';
  /* 该页所有已创建的图表用 graphic 文字覆盖旧图（notMerge=true 彻底清空 series） */
  Object.keys(CHARTS).forEach(id => {
    if (id.indexOf(prefix + '-') === 0) {
      CHARTS[id].off('click');
      CHARTS[id].setOption({ graphic: [{ type: 'text', left: 'center', top: 'middle',
        style: { text: msg, fill: '#8E96A0', fontSize: 16 } }] }, true);
    }
  });
}

/* ---------- 饼图通用 ---------- */
function pieOption(data, opts) {
  opts = opts || {};
  return {
    tooltip: Object.assign({ trigger: 'item', formatter: '{b}: {c} ({d}%)' }, TOOLTIP),
    legend: opts.legend === false ? undefined : {
      orient: 'vertical', right: 6, top: 'middle', itemWidth: 12, itemHeight: 12,
      textStyle: { color: '#8E96A0', fontSize: 11 } },
    series: [{
      type: 'pie', radius: opts.radius || ['40%', '64%'],
      center: opts.center || ['42%', '52%'],
      roseType: opts.rose || false,
      data: data.map((v, i) => Object.assign({ name: v.name, value: v.value },
        opts.noColor ? {} : { itemStyle: { color: PALETTE[i % PALETTE.length] } })),
      label: { color: '#8E96A0', fontSize: 10.5, formatter: '{b}\n{d}%' },
      labelLine: { length: 8, length2: 6 },
      itemStyle: { borderColor: '#15181D', borderWidth: 2 }
    }]
  };
}

/* ==================== 总览页: 主图 + 年度ROI + 双迷你趋势卡 ==================== */
/* 拆图原则：一张图只讲一件事——
   ① 主图：客群销售构成（月度堆叠面积，单击色块联动客群+下钻）
   ② 年度ROI 独立成图（年轴柱状 + 盈亏线 1.0，达标藏蓝/未达标警示红，单击联动年份）
   ③/④ 高价值客户销售 / 沉睡唤醒订单 降级为右下两张迷你趋势卡（单击查当月明细） */
/* ==================== ④ GMV 预测：提示条 / 方法抽屉 / 主图叠加 ==================== */
function renderFcNote(fc, filtered) {
  const el = document.getElementById('p0-fc-note');
  if (!el) return;
  if (filtered) {
    el.innerHTML = '<span class="fc-chip">预测已收起</span>' +
      '<span>预测基于全量月度序列，筛选视图下不显示；点「重 置」可查看</span>';
    return;
  }
  if (!fc) { el.innerHTML = ''; return; }
  if (!fc.available) {
    el.innerHTML = '<span class="fc-chip">预测不可用</span><span class="fc-warn">' +
      escapeHtml(fc.reason || '当前条件下无法预测') + '</span>';
    return;
  }
  const f1 = fc.forecast[0];
  const bt = fc.backtest || {};
  const one = (bt.one_step || {});
  el.innerHTML =
    '<span class="fc-chip">GMV 预测</span>' +
    '<span>' + escapeHtml(f1.ym) + ' 点估计 <b>' + f1.point.toFixed(2) + ' 万</b></span>' +
    '<span>80% 区间 <b>' + f1.lo.toFixed(1) + ' ~ ' + f1.hi.toFixed(1) + ' 万</b></span>' +
    (one.mape != null ? '<span>· 一步滚动回测 MAPE <b>' + one.mape + '%</b>（' + one.n + ' 个月）</span>' : '') +
    '<span class="fc-link" id="fc-open">方法与精度 &rarr;</span>' +
    (fc.warn ? '<span class="fc-warn">· ' + escapeHtml(fc.warn) + '</span>' : '');
  const link = document.getElementById('fc-open');
  if (link) link.addEventListener('click', openFcDrawer);
  /* ?fc=1 直达预测方法抽屉（验收/截图用，不影响正常使用） */
  if (!window.__fcOpened && new URLSearchParams(location.search).get('fc') === '1') {
    window.__fcOpened = true;
    setTimeout(openFcDrawer, 80);
  }
}

function openFcDrawer() {
  document.getElementById('fc-modal').classList.add('show');
  const body = document.getElementById('fc-body');
  const fc = FC_DATA;
  if (!fc) { body.innerHTML = '<div class="p6-empty">预测数据未加载</div>'; return; }
  if (!fc.available) {
    body.innerHTML = '<div class="p6-empty">' + escapeHtml(fc.reason || '当前条件下无法预测') + '</div>';
    return;
  }
  const p = fc.params || {};
  const bt = fc.backtest || {};
  const one = bt.one_step || {};
  const hold = bt.holdout || {};
  const f1 = fc.forecast[0];
  document.getElementById('fc-title').textContent =
    'GMV 预测 · ' + fc.base_ym + ' 之后 ' + fc.forecast.length + ' 个月';

  let html = '<div class="modal-summary">' +
    '<div class="ms-item"><div class="ms-label">' + escapeHtml(f1.ym) + ' 点估计</div>' +
    '<div class="ms-value">' + f1.point.toFixed(2) + ' 万</div></div>' +
    '<div class="ms-item"><div class="ms-label">80% 区间</div>' +
    '<div class="ms-value">' + f1.lo.toFixed(1) + '–' + f1.hi.toFixed(1) + ' 万</div></div>' +
    '<div class="ms-item"><div class="ms-label">一步滚动 MAPE</div>' +
    '<div class="ms-value">' + (one.mape != null ? one.mape + '%' : '—') + '</div></div>' +
    '</div>';

  html += '<div class="fc-kv">' +
    '<div><span>方法：</span><b>' + escapeHtml(fc.method || '') + '</b></div>' +
    '<div><span>基线月：</span><b>' + escapeHtml(fc.base_ym) + '</b></div>' +
    '<div><span>参与拟合月数：</span><b>' + fc.n_months + ' 个月</b></div>' +
    '<div><span>置信水平：</span><b>' + Math.round((fc.confidence || 0.8) * 100) + '%</b></div>' +
    '<div><span>α（水平平滑）：</span><b>' + (p.alpha != null ? p.alpha : '—') + '</b></div>' +
    '<div><span>β（趋势平滑）：</span><b>' + (p.beta != null ? p.beta : '—') + '</b></div>' +
    '<div><span>φ（阻尼系数）：</span><b>' + (p.phi != null ? p.phi : '—') + '</b></div>' +
    '<div><span>残差 σ（对数）：</span><b>' + (p.sigma_log != null ? p.sigma_log : '—') + '</b></div>' +
    '</div>';

  html += '<div class="sim-sec-title">预测明细（万元）</div>' +
    '<table class="sim-table"><thead><tr><th>月份</th><th class="num">点估计</th>' +
    '<th class="num">下界</th><th class="num">上界</th><th class="num">区间半宽</th></tr></thead><tbody>' +
    fc.forecast.map(f => '<tr><td>' + escapeHtml(f.ym) + '</td><td class="num">' + f.point.toFixed(2) +
      '</td><td class="num">' + f.lo.toFixed(2) + '</td><td class="num">' + f.hi.toFixed(2) +
      '</td><td class="num">±' + (f.width_pct != null ? f.width_pct : '—') + '%</td></tr>').join('') +
    '</tbody></table>';

  if (hold.detail && hold.detail.length) {
    html += '<div class="sim-sec-title">留出回测（最后 ' + hold.h + ' 个月，MAPE ' +
      (hold.mape != null ? hold.mape + '%' : '—') + '）</div>' +
      '<table class="sim-table"><thead><tr><th>月份</th><th class="num">预测</th>' +
      '<th class="num">实际</th><th class="num">误差</th></tr></thead><tbody>' +
      hold.detail.map(r => '<tr><td>' + escapeHtml(r.ym) + '</td><td class="num">' + r.pred.toFixed(1) +
        '</td><td class="num">' + r.actual.toFixed(1) + '</td><td class="num">' +
        (r.ape != null ? r.ape + '%' : '—') + '</td></tr>').join('') +
      '</tbody></table>';
  }

  html += '<div class="sim-sec-title">方法与口径</div>' +
    '<p class="fc-para">' + escapeHtml(fc.method_note || '') + '</p>' +
    '<div class="sim-note">' +
    '使用边界：预测只描述「按现有趋势外推」的结果，不含促销计划、预算与季节性活动信息；' +
    '2025-11、2025-12 两个月出现远超趋势的跃升（环比 +61%、+88%），留出回测误差因此明显大于一步滚动误差，' +
    '请以区间而非单点做决策。区间为对数空间对称后指数还原，故下界永不为负。' +
    (fc.warn ? '<br/><span style="color:#D9A03C">' + escapeHtml(fc.warn) + '</span>' : '') +
    '</div>';
  body.innerHTML = html;
}

function closeFcDrawer() { document.getElementById('fc-modal').classList.remove('show'); }
document.getElementById('fc-close').addEventListener('click', closeFcDrawer);
document.getElementById('fc-modal').addEventListener('click', e => {
  if (e.target.id === 'fc-modal') closeFcDrawer();
});

function renderOverview(d) {
  const safe = (obj, i, fb) => (obj && !obj.empty && obj.kpi && obj.kpi[i])
    ? obj.kpi[i] : { label: fb[0], value: fb[1] };
  const kpi = [safe(d.p1, 0, ['购买客户数', '-']), safe(d.p1, 2, ['销售金额(万)', '-']),
    safe(d.p2, 0, ['高价值客户复购率', '-']), safe(d.p3, 2, ['沉睡客户唤醒率', '-']),
    safe(d.p4, 0, ['整体ROI', '-']), safe(d.p4, 3, ['ROI提升倍数', '-'])];
  if (d.p2 && !d.p2.empty && d.p2.grp_name) kpi[2].label = d.p2.grp_name + '复购率';
  renderKPI('p0-kpi', kpi);
  document.getElementById('p0-hv-name').textContent =
    (d.p2 && d.p2.grp_name) ? d.p2.grp_name : '高价值客户';

  const chart = getChart('p0-fusion');
  const roiChart = getChart('p0-roi');
  const hvChart = getChart('p0-hv');
  const awChart = getChart('p0-aw');
  const months = (d.p0 && !d.p0.empty && d.p0.months) ? d.p0.months : [];
  if (!months.length) {
    const noteEl = document.getElementById('p0-fc-note');
    if (noteEl) noteEl.innerHTML = '';
    [chart, roiChart, hvChart, awChart].forEach(c => c.setOption({ graphic: [
      { type: 'text', left: 'center', top: 'middle',
        style: { text: '当前筛选条件下暂无数据', fill: '#6A7280', fontSize: 15 } }] }, true));
    return;
  }

  /* ---------- ② 年度ROI：年轴柱状 + 盈亏线（不再借用月轴，消除第三隐藏轴） ---------- */
  const yr = (d.p4 && !d.p4.empty && d.p4.yearly_roi) ? d.p4.yearly_roi : null;
  if (yr && yr.length) {
    roiChart.setOption({
      tooltip: Object.assign({ trigger: 'axis', formatter: ps => {
          const v = yr[ps[0].dataIndex];
          return v.year + '年 · 整体ROI: ' + v.roi +
            '<br/><span style="color:#8E96A0">单击联动年份筛选，再点取消</span>'; } }, TOOLTIP),
      grid: { left: 44, right: 20, top: 30, bottom: 30 },
      xAxis: Object.assign({ type: 'category', data: yr.map(v => v.year + '年') }, AXIS_STYLE),
      yAxis: Object.assign({ type: 'value', name: 'ROI' }, AXIS_STYLE),
      series: [{ name: '年度ROI', type: 'bar', barWidth: '38%',
        data: yr.map(v => ({ value: v.roi,
          itemStyle: { color: v.roi >= 1 ? '#C9A227' : '#CF5B52', borderRadius: [3, 3, 0, 0] } })),
        label: { show: true, position: 'top', color: '#D7D3C9', fontSize: 11, fontWeight: 600 },
        markLine: { data: [{ yAxis: 1 }], symbol: 'none',
          lineStyle: { color: '#A87320', type: 'dashed', width: 1.2 },
          label: { formatter: '盈亏线 1.0', color: '#A87320', fontSize: 10, position: 'insideEndTop' } } }]
    }, true);
    roiChart.off('click').on('click', p => {
      if (p.componentType === 'series' && yr[p.dataIndex]) toggleGlobalFilter('year', String(yr[p.dataIndex].year));
    });
  } else {
    roiChart.setOption({ graphic: [{ type: 'text', left: 'center', top: 'middle',
      style: { text: '当前筛选条件下暂无ROI数据', fill: '#6A7280', fontSize: 14 } }] }, true);
  }

  /* ---------- ③ 高价值客户月度销售（迷你趋势卡） ---------- */
  const hvRaw = (d.p2 && !d.p2.empty && d.p2.trend && d.p2.trend.length) ? d.p2.trend : null;
  /* 裁掉前导零月（该客群前期无交易），避免一条贴零平线占满卡片 */
  let hv = null, hvNote = '';
  if (hvRaw) {
    const nz = hvRaw.findIndex(v => v.sales > 0);
    hv = nz > 0 ? hvRaw.slice(nz) : hvRaw;
    if (nz > 0) hvNote = hv[0].ym + ' 起';
  }
  if (hv) {
    hvChart.setOption({
      title: { text: hvNote, right: 10, top: 2,
        textStyle: { color: '#6A7280', fontSize: 10, fontWeight: 'normal' } },
      tooltip: Object.assign({ trigger: 'axis', formatter: ps =>
          hv[ps[0].dataIndex].ym + '<br/>销售额: ' + ps[0].value + ' 万' +
          '<br/><span style="color:#8E96A0">单击查看该月明细</span>' }, TOOLTIP),
      grid: { left: 46, right: 14, top: 22, bottom: 38 },
      xAxis: Object.assign({ type: 'category', data: hv.map(v => v.ym),
        axisLabel: { color: '#8E96A0', fontSize: 9.5, interval: 3, rotate: 35 } }, AXIS_STYLE),
      yAxis: Object.assign({ type: 'value' }, AXIS_STYLE),
      series: [{ name: '月度销售(万)', type: 'line', smooth: true, symbol: 'circle', symbolSize: 4,
        lineStyle: { width: 2.5, color: '#C9A227' }, itemStyle: { color: '#C9A227' },
        areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: 'rgba(201,162,39,.18)' }, { offset: 1, color: 'rgba(201,162,39,.02)' }] } },
        data: hv.map(v => v.sales) }]
    }, true);
    hvChart.off('click').on('click', p => { if (hv[p.dataIndex]) openMonthDrill(hv[p.dataIndex].ym); });
  } else {
    hvChart.setOption({ graphic: [{ type: 'text', left: 'center', top: 'middle',
      style: { text: '暂无该客群交易数据', fill: '#6A7280', fontSize: 14 } }] }, true);
  }

  /* ---------- ④ 沉睡唤醒订单（迷你趋势卡） ---------- */
  const awRaw = (d.p3 && !d.p3.empty && d.p3.awaken_trend && d.p3.awaken_trend.length) ? d.p3.awaken_trend : null;
  let aw = null, awNote = '';
  if (awRaw) {
    const nz = awRaw.findIndex(v => v.cnt > 0);
    aw = nz > 0 ? awRaw.slice(nz) : awRaw;
    if (nz > 0) awNote = aw[0].ym + ' 起';
  }
  if (aw) {
    awChart.setOption({
      title: { text: awNote, right: 10, top: 2,
        textStyle: { color: '#6A7280', fontSize: 10, fontWeight: 'normal' } },
      tooltip: Object.assign({ trigger: 'axis', formatter: ps =>
          aw[ps[0].dataIndex].ym + '<br/>唤醒订单: ' + ps[0].value + ' 笔' +
          '<br/><span style="color:#8E96A0">单击查看该月明细</span>' }, TOOLTIP),
      grid: { left: 46, right: 14, top: 22, bottom: 38 },
      xAxis: Object.assign({ type: 'category', data: aw.map(v => v.ym),
        axisLabel: { color: '#8E96A0', fontSize: 9.5, interval: 3, rotate: 35 } }, AXIS_STYLE),
      yAxis: Object.assign({ type: 'value' }, AXIS_STYLE),
      series: [{ name: '唤醒订单(笔)', type: 'bar', barWidth: '45%',
        itemStyle: { color: '#D97B4A', borderRadius: [3, 3, 0, 0] },
        data: aw.map(v => v.cnt) }]
    }, true);
    awChart.off('click').on('click', p => { if (aw[p.dataIndex]) openMonthDrill(aw[p.dataIndex].ym); });
  } else {
    awChart.setOption({ graphic: [{ type: 'text', left: 'center', top: 'middle',
      style: { text: '暂无唤醒订单数据', fill: '#6A7280', fontSize: 14 } }] }, true);
  }

  /* ---------- ① 主图：客群销售构成 ---------- */
  /* 筛选器激活时切换「精简模式」：只显示筛选后总销售额单线，图表干净 */
  const f = d.filters || {};
  const filtered = !!(f.tier || f.gender || f.age || f.city || f.year || f.month);
  if (filtered) {
    renderFcNote(FC_DATA, true);   /* 筛选视图不叠加预测，仅在提示条说明原因 */
    const total = months.map((m, i) => {
      let s = 0;
      (d.p0.seg_series || []).forEach(seg => { s += (seg.data[i] || 0); });
      return Math.round(s * 100) / 100;
    });
    let idx = months.map((_, i) => i);
    if (f.year) idx = idx.filter(i => months[i].indexOf(f.year) === 0);
    if (f.month) idx = idx.filter(i => months[i].slice(5) === String(f.month).padStart(2, '0'));
    if (!f.year && !f.month) {
      const nz = idx.filter(i => total[i] > 0);
      if (nz.length) idx = idx.slice(Math.min(...nz), Math.max(...nz) + 1);
    }
    if (!idx.length) {
      chart.setOption({ graphic: [{ type: 'text', left: 'center', top: 'middle',
        style: { text: '当前筛选条件下暂无数据', fill: '#6A7280', fontSize: 15 } }] }, true);
      return;
    }
    const xs = idx.map(i => months[i]);
    const ys = idx.map(i => total[i]);
    chart.setOption({
      tooltip: Object.assign({ trigger: 'axis',
        formatter: ps => xs[ps[0].dataIndex] + '<br/>筛选后销售额: ' + ps[0].value + ' 万<br/><span style="color:#8E96A0">单击折线查看当月明细</span>' }, TOOLTIP),
      grid: { left: 64, right: 44, top: 34, bottom: 58 },
      xAxis: Object.assign({ type: 'category', data: xs,
        axisLabel: { color: '#8E96A0', fontSize: 10.5, interval: 0, rotate: 35 } }, AXIS_STYLE),
      yAxis: Object.assign({ type: 'value', name: '销售额(万)', nameTextStyle: { color: '#8E96A0' } }, AXIS_STYLE),
      series: [{ name: '筛选后销售额(万)', type: 'line', smooth: true, symbol: 'circle', symbolSize: 6,
        lineStyle: { width: 3, color: '#C9A227' },
        itemStyle: { color: '#C9A227' },
        areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
          colorStops: [{ offset: 0, color: 'rgba(201,162,39,.16)' }, { offset: 1, color: 'rgba(201,162,39,.02)' }] } },
        data: ys }]
    }, true);
    chart.off('click').on('click', p => {
      if (p.componentType === 'series' && p.dataIndex != null) openMonthDrill(xs[p.dataIndex]);
    });
    return;
  }

  /* 默认：四客群月度销售额堆叠面积图（比堆叠柱更适合读「构成随时间变化」，色系与页1饼图一致） */
  const segSeries = (d.p0.seg_series || []).map((s, i) => ({
    name: s.name, type: 'line', stack: 'seg', yAxisIndex: 0,
    smooth: false, symbol: 'none',
    lineStyle: { width: 1, color: 'rgba(255,255,255,.12)' },
    areaStyle: { opacity: .92, color: PALETTE[i % PALETTE.length] },
    emphasis: { focus: 'series' },
    data: s.data }));

  /* 异常月警示标记：滚动窗口 z-score（|z|>2，后端 /api/dashboard 的 anomalies 字段），
     红色 pin（语义差色 #CF5B52），点击打开归因抽屉；正常月点击行为保持不变 */
  const anomalies = d.anomalies || [];
  if (anomalies.length && segSeries.length) {
    segSeries[0].markPoint = {
      symbol: 'pin', symbolSize: 22, symbolOffset: [0, 8],
      itemStyle: { color: '#CF5B52', shadowColor: 'rgba(207,91,82,.45)', shadowBlur: 6 },
      label: { show: true, formatter: '!', color: '#FFFFFF', fontSize: 9, fontWeight: 700 },
      data: anomalies.map(a => ({
        xAxis: a.ym, yAxis: +(a.gmv / 10000).toFixed(2), ym: a.ym, z: a.z })),
      tooltip: { formatter: p => {
          const v = p.data;
          return escapeHtml(v.ym) + ' 异常月（z=' + v.z + '）' +
            '<br/><span style="color:#CF5B52">点击查看归因</span>';
        } }
    };
  }

  /* ---------- ④ 预测段：虚线 + 80% 置信带叠在真实数据右侧 ---------- */
  renderFcNote(FC_DATA, false);
  const fcRows = (FC_DATA && FC_DATA.available) ? (FC_DATA.forecast || []) : [];
  const segNames = (d.p0.seg_series || []).map(s => s.name);
  let allMonths = months;
  if (fcRows.length) {
    allMonths = months.concat(fcRows.map(r => r.ym));
    /* 最后一个月真实合计（虚线起点必须接在真实序列末端，不能悬空） */
    let lastTotal = 0;
    (d.p0.seg_series || []).forEach(s => { lastTotal += (s.data[months.length - 1] || 0); });
    lastTotal = Math.round(lastTotal * 100) / 100;
    const pad = new Array(months.length - 1).fill(null);
    /* 置信带：ECharts 无原生区间带，用「下界 + (上界-下界)」两条同 stack 面积系列叠加实现 */
    segSeries.push(
      { name: '__fc_lo', type: 'line', stack: 'fcband', symbol: 'none', silent: true,
        lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 }, tooltip: { show: false },
        legendHoverLink: false,
        data: pad.concat([lastTotal]).concat(fcRows.map(r => r.lo)) },
      { name: '__fc_band', type: 'line', stack: 'fcband', symbol: 'none', silent: true,
        lineStyle: { opacity: 0 }, areaStyle: { opacity: 1, color: 'rgba(201,162,39,.13)' },
        tooltip: { show: false }, legendHoverLink: false,
        data: pad.concat([0]).concat(fcRows.map(r => +(r.hi - r.lo).toFixed(2))) },
      { name: '预测', type: 'line', symbol: 'circle', symbolSize: 5, smooth: false, z: 6,
        lineStyle: { width: 2, type: 'dashed', color: '#E3C15F' },
        itemStyle: { color: '#E3C15F', borderColor: '#0B0D10', borderWidth: 1 },
        data: pad.concat([lastTotal]).concat(fcRows.map(r => r.point)) }
    );
  }

  chart.setOption({
    tooltip: Object.assign({ trigger: 'axis', axisPointer: { type: 'line' },
      formatter: ps => {
        const i = ps[0].dataIndex;
        const isFc = i >= months.length;
        let html = '<b>' + escapeHtml(allMonths[i]) + '</b>' +
          (isFc ? ' <span style="color:#E3C15F">预测</span>' : '');
        ps.forEach(p => {
          if (p.value == null || String(p.seriesName).indexOf('__fc') === 0) return;
          if (p.seriesName === '预测') {
            html += '<br/>' + p.marker + ' 预测: ' + p.value + ' 万';
          } else {
            html += '<br/>' + p.marker + ' ' + escapeHtml(p.seriesName) + ': ' + p.value + ' 万';
          }
        });
        if (isFc && fcRows[i - months.length]) {
          const r = fcRows[i - months.length];
          html += '<br/><span style="color:#8E96A0">80% 区间 ' + r.lo + ' ~ ' + r.hi + ' 万' +
            '（±' + r.width_pct + '%）</span>' +
            '<br/><span style="color:#C9A227">单击查看预测方法与精度</span>';
        } else {
          html += '<br/><span style="color:#8E96A0">单击色块联动客群并查看明细</span>';
        }
        return html;
      } }, TOOLTIP),
    legend: { top: 4, left: 'center', itemWidth: 14, itemHeight: 10,
      textStyle: { color: '#8E96A0', fontSize: 11.5 },
      data: segNames.concat(fcRows.length ? ['预测'] : []) },
    grid: { left: 64, right: 30, top: 52, bottom: 58 },
    xAxis: Object.assign({ type: 'category', data: allMonths, boundaryGap: false,
      axisLabel: { color: '#8E96A0', fontSize: 10.5, interval: 2, rotate: 35,
        /* 预测段月份标签换金色，让「这一段是预测」在坐标轴上也看得出来 */
        formatter: v => (months.indexOf(v) === -1 ? '{fc|' + v + '}' : v),
        rich: { fc: { color: '#C9A227', fontSize: 10.5, fontWeight: 600 } } } }, AXIS_STYLE),
    yAxis: Object.assign({ type: 'value', name: '销售额(万)', nameTextStyle: { color: '#8E96A0' } }, AXIS_STYLE),
    series: segSeries
  }, true);

  chart.off('click').on('click', p => {
    /* 异常标记：打开归因抽屉（markPoint 仅存在于异常月位置） */
    if (p.componentType === 'markPoint' && p.data && p.data.ym) {
      openAnomalyDrawer(p.data.ym);
      return;
    }
    if (p.componentType !== 'series' || p.seriesType !== 'line') return;
    /* 预测段：打开方法抽屉，绝不下钻到尚不存在的月份 */
    if (p.seriesName === '预测' || p.dataIndex >= months.length) {
      if (fcRows.length) openFcDrawer();
      return;
    }
    if (p.seriesName && (d.p0.seg_series || []).some(s => s.name === p.seriesName)) {
      toggleLink('seg', p.seriesName);
      openDrillModal(p.seriesName);
    } else {
      const m = months[p.dataIndex];
      if (m) openMonthDrill(m);
    }
  });
}

/* ==================== 页面1: RFM 客群分层总览 ==================== */
function renderP1(d) {
  if (d.empty) { clearPageCards('p1'); return; }
  renderKPI('p1-kpi', d.kpi);

  /* 客群结构占比（点击联动 + 弹下钻明细） */
  const segChart = getChart('p1-segpie');
  segChart.setOption(Object.assign(pieOption(d.seg_pie, {
    radius: ['42%', '66%'], center: ['46%', '54%'], rose: false }), {
    tooltip: Object.assign({ trigger: 'item',
      formatter: p => escapeHtml(p.name) + ': ' + p.value + ' 人 (' + p.percent + '%)\n单击联动筛选，再点取消',
      extraCssText: '' }, TOOLTIP) }), true);
  segChart.off('click').on('click', p => {
    toggleLink('seg', p.name);
    openDrillModal(p.name);
  });

  /* 各群平均 F/M（点击柱形联动客群 + 下钻明细） */
  const segfmChart = getChart('p1-segfm');
  segfmChart.setOption({
    tooltip: Object.assign({ trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: ps => { const v = d.seg_avg_fm[ps[0].dataIndex];
        return escapeHtml(v.name) + '<br/>平均订单数(F): ' + v.f + '<br/>平均消费(M): ' + v.m + ' 元' +
          '<br/><span style="color:#8E96A0">单击柱形联动客群并查看明细</span>'; } }, TOOLTIP),
    legend: { top: 2, right: 6, textStyle: { color: '#8E96A0', fontSize: 11 }, itemWidth: 14 },
    grid: { left: 48, right: 58, top: 34, bottom: 24 },
    xAxis: Object.assign({ type: 'category', data: d.seg_avg_fm.map(v => v.name),
      axisLabel: { color: '#8E96A0', fontSize: 10.5, interval: 0, rotate: 14 } }, AXIS_STYLE),
    /* F(次) 与 M(元) 量级相差约 1000 倍，双 Y 轴避免 M 柱被压成 1px */
    yAxis: [
      Object.assign({ type: 'value', name: '平均F(次)', nameTextStyle: { color: '#C9A227', fontSize: 10 },
        axisLabel: { color: '#C9A227', fontSize: 10 } }, AXIS_STYLE),
      Object.assign({ type: 'value', name: '平均M(元)', nameTextStyle: { color: '#D9903F', fontSize: 10 },
        axisLabel: { color: '#D9903F', fontSize: 10 } }, AXIS_STYLE)
    ],
    series: [
      { name: '平均F(订单数)', type: 'bar', barWidth: 12, yAxisIndex: 0,
        data: d.seg_avg_fm.map(v => ({ value: v.f, itemStyle: { color: '#C9A227' } })),
        label: { show: true, position: 'top', color: '#C9A227', fontSize: 10,
          formatter: p => p.value } },
      { name: '平均M(元)', type: 'bar', barWidth: 12, yAxisIndex: 1,
        data: d.seg_avg_fm.map(v => ({ value: v.m, itemStyle: { color: '#D9903F' } })),
        label: { show: true, position: 'top', color: '#D9903F', fontSize: 10,
          formatter: p => p.value } }
    ]
  }, true);
  segfmChart.off('click').on('click', p => {
    toggleLink('seg', p.name);
    openDrillModal(p.name);
  });

  /* R/F/M 三维散点（点击散点联动客群 + 下钻明细） */
  const segs = [...new Set(d.scatter.map(x => x.seg))];
  const scChart = getChart('p1-scatter');
  scChart.setOption({
    tooltip: Object.assign({ trigger: 'item',
      formatter: p => escapeHtml(p.seriesName) + '<br/>最近购买: ' + p.value[0] + ' 天<br/>订单数: ' +
        p.value[1] + '<br/>消费额: ' + p.value[2] + ' 元' +
        '<br/><span style="color:#8E96A0">单击散点联动客群并查看明细</span>' }, TOOLTIP),
    legend: { top: 2, right: 6, textStyle: { color: '#8E96A0', fontSize: 11 }, itemWidth: 13 },
    grid: { left: 48, right: 16, top: 34, bottom: 40 },
    xAxis: Object.assign({ type: 'value', name: 'R_days', nameTextStyle: { color: '#8E96A0' } }, AXIS_STYLE),
    yAxis: Object.assign({ type: 'value', name: 'F 频次', nameTextStyle: { color: '#8E96A0' } }, AXIS_STYLE),
    series: segs.map((s, i) => ({
      name: s, type: 'scatter',
      data: d.scatter.filter(x => x.seg === s).map(x => [x.r, x.f, x.m]),
      symbolSize: v => Math.max(4, Math.min(26, Math.sqrt(v[2]) / 14)),
      itemStyle: { color: PALETTE[i % PALETTE.length], opacity: .62 },
      emphasis: { itemStyle: { opacity: 1 } }
    }))
  }, true);
  scChart.off('click').on('click', p => {
    toggleLink('seg', p.seriesName);
    openDrillModal(p.seriesName);
  });
}

/* ==================== 页面2: 高价值客户分析 ==================== */
function renderP2(d) {
  if (d.empty) { clearPageCards('p2'); return; }
  renderKPI('p2-kpi', d.kpi);
  document.getElementById('p2-grpname').textContent = d.grp_name;

  /* 月度销售额趋势（点击月份查看当月明细）
     高价值客户在 2023-2024 年无交易，前段全 0 会把曲线压成一条直线，
     因此只从有交易的月份起画，并在图内注明实际起点 */
  const _nzIdx = d.trend.findIndex(v => v.sales > 0);
  const _trend = _nzIdx > 0 ? d.trend.slice(_nzIdx) : d.trend;
  const _note = _nzIdx > 0 ? (d.trend[_nzIdx].ym + ' 起（此前该客群无交易）') : '';
  const trendChart = getChart('p2-trend');
  trendChart.setOption({
    title: { text: _note, left: 'center', top: 0,
      textStyle: { color: '#8E96A0', fontSize: 11, fontWeight: 'normal' } },
    tooltip: Object.assign({ trigger: 'axis', formatter: ps => {
        const v = _trend[ps[0].dataIndex];
        return v.ym + '<br/>' + ps[0].seriesName + ': ' + ps[0].value + ' 万' +
          '<br/><span style="color:#8E96A0">单击查看该月明细</span>'; } }, TOOLTIP),
    grid: { left: 60, right: 24, top: 34, bottom: 40 },
    xAxis: Object.assign({ type: 'category', data: _trend.map(v => v.ym),
      axisLabel: { color: '#8E96A0', fontSize: 10, interval: 2, rotate: 35 } }, AXIS_STYLE),
    yAxis: Object.assign({ type: 'value', name: '万元', nameTextStyle: { color: '#8E96A0' } }, AXIS_STYLE),
    series: [{ name: d.grp_name + '月度销售(万)', type: 'line', smooth: true, symbol: 'circle', symbolSize: 5,
      lineStyle: { width: 2.5, color: '#57A574', shadowColor: 'rgba(87,165,116,.25)', shadowBlur: 10 },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
        colorStops: [{ offset: 0, color: 'rgba(87,165,116,.14)' }, { offset: 1, color: 'rgba(87,165,116,.02)' }] } },
      data: _trend.map(v => v.sales) }]
  }, true);
  trendChart.off('click').on('click', p => openMonthDrill(_trend[p.dataIndex].ym));

  /* 各品类销售金额排名（点击联动品类） */
  const tc = d.top_category.slice().reverse();
  const topCatChart = getChart('p2-topcat');
  topCatChart.setOption({
    tooltip: Object.assign({ trigger: 'axis', axisPointer: { type: 'shadow' } }, TOOLTIP),
    grid: { left: 88, right: 52, top: 14, bottom: 24 },
    xAxis: Object.assign({ type: 'value', name: '万元' }, AXIS_STYLE),
    yAxis: Object.assign({ type: 'category', data: tc.map(v => v.name),
      axisLabel: { color: '#E7E4DC', fontSize: 13 } }, AXIS_STYLE),
    series: [{ type: 'bar', barWidth: 18, data: tc.map((v, i) => ({
        value: v.sales, itemStyle: { color: PALETTE[i % PALETTE.length] } })),
      label: { show: true, position: 'right', color: '#D9903F', fontSize: 12,
        formatter: p => p.value + '万' } }]
  }, true);
  topCatChart.off('click').on('click', p => toggleLink('category', p.name));
}

/* ==================== 页面3: 沉睡客户唤醒 ==================== */
function renderP3(d) {
  if (d.empty) { clearPageCards('p3'); return; }
  renderKPI('p3-kpi', d.kpi);

  /* 沉睡客户唤醒订单月度趋势（点击月份查看当月明细） */
  const awakenTrendChart = getChart('p3-trend');
  awakenTrendChart.setOption({
    tooltip: Object.assign({ trigger: 'axis', formatter: ps => {
        const v = d.awaken_trend[ps[0].dataIndex];
        return v.ym + '<br/>唤醒订单: ' + v.cnt + ' 笔' +
          '<br/><span style="color:#8E96A0">单击查看该月明细</span>'; } }, TOOLTIP),
    grid: { left: 52, right: 24, top: 20, bottom: 40 },
    xAxis: Object.assign({ type: 'category', data: d.awaken_trend.map(v => v.ym),
      axisLabel: { color: '#8E96A0', fontSize: 10, interval: 2, rotate: 35 } }, AXIS_STYLE),
    yAxis: Object.assign({ type: 'value', name: '唤醒订单数', nameTextStyle: { color: '#8E96A0' } }, AXIS_STYLE),
    series: [{ name: '月度唤醒订单', type: 'line', smooth: true, symbol: 'circle',
      symbolSize: 4, lineStyle: { width: 2.5, color: '#D97B4A', shadowColor: 'rgba(217,123,74,.25)', shadowBlur: 10 },
      areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
        colorStops: [{ offset: 0, color: 'rgba(217,123,74,.14)' }, { offset: 1, color: 'rgba(217,123,74,.02)' }] } },
      data: d.awaken_trend.map(v => v.cnt) }]
  }, true);
  awakenTrendChart.off('click').on('click', p => openMonthDrill(d.awaken_trend[p.dataIndex].ym));

  /* 唤醒订单与未唤醒订单对比（按月，点击查看当月明细） */
  const stackChart = getChart('p3-stack');
  stackChart.setOption({
    tooltip: Object.assign({ trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: ps => d.stack[ps[0].dataIndex].ym + '<br/>' +
        ps.map(p => p.marker + p.seriesName + ': ' + p.value + ' 笔').join('<br/>') +
        '<br/><span style="color:#8E96A0">单击查看该月明细</span>' }, TOOLTIP),
    legend: { top: 2, right: 6, textStyle: { color: '#8E96A0', fontSize: 11 }, itemWidth: 14 },
    grid: { left: 52, right: 20, top: 34, bottom: 40 },
    xAxis: Object.assign({ type: 'category', data: d.stack.map(v => v.ym),
      axisLabel: { color: '#8E96A0', fontSize: 10, interval: 2, rotate: 35 } }, AXIS_STYLE),
    yAxis: Object.assign({ type: 'value', name: '订单数', nameTextStyle: { color: '#8E96A0' } }, AXIS_STYLE),
    series: [
      { name: '唤醒订单', type: 'bar', stack: 's', barWidth: '55%',
        data: d.stack.map(v => v.awake), itemStyle: { color: '#D97B4A' } },
      { name: '未唤醒订单', type: 'bar', stack: 's',
        data: d.stack.map(v => v.normal), itemStyle: { color: 'rgba(142,150,160,.45)' } }
    ]
  }, true);
  stackChart.off('click').on('click', p => openMonthDrill(d.stack[p.dataIndex].ym));
}

/* ==================== 页面4: 营销产出分析 ==================== */
function renderP4(d) {
  if (d.empty) { clearPageCards('p4'); return; }
  renderKPI('p4-kpi', d.kpi);

  /* P0-05：本页两张对比图不因联动裁掉数据，只做高亮；未选中的柱/点保持完整 */
  const selStrat = LINK_STATE.strat;
  const selYear = document.getElementById('f-year').value;

  /* 各营销策略组成本与收入对比（点击联动策略组；strat 联动只高亮选中柱，不过滤本图） */
  const stratChart = getChart('p4-strategy');
  stratChart.setOption({
    tooltip: Object.assign({ trigger: 'axis', axisPointer: { type: 'shadow' },
      formatter: ps => { const v = d.strategy[ps[0].dataIndex];
        return escapeHtml(v.name) + '<br/>成本: ' + v.cost + ' 万<br/>收入: ' + v.rev + ' 万<br/>ROI: ' + v.roi +
          (selStrat && v.name === selStrat ? '<br/><span style="color:#D9903F">已联动该策略组，再点取消</span>' : '');
      } }, TOOLTIP),
    legend: { top: 2, right: 6, textStyle: { color: '#8E96A0', fontSize: 11 }, itemWidth: 14 },
    grid: { left: 58, right: 24, top: 36, bottom: 46 },
    xAxis: Object.assign({ type: 'category', data: d.strategy.map(v => v.name),
      axisLabel: { color: p => (selStrat && p === selStrat ? '#D9903F' : '#E7E4DC'),
        fontSize: 13, interval: 0 } }, AXIS_STYLE),
    yAxis: Object.assign({ type: 'value', name: '万元', nameTextStyle: { color: '#8E96A0' } }, AXIS_STYLE),
    series: [
      { name: '成本(万)', type: 'bar', barWidth: 26, data: d.strategy.map(v => ({
          value: v.cost,
          itemStyle: { color: (selStrat && v.name !== selStrat) ? 'rgba(142,150,160,.4)' : '#6E7F91' } })) },
      { name: '收入(万)', type: 'bar', barWidth: 26, data: d.strategy.map(v => ({
          value: v.rev,
          itemStyle: { color: (selStrat && v.name !== selStrat) ? 'rgba(142,150,160,.4)' : '#D97B4A' },
          /* ROI 标签红绿标色：ECharts 的 label.color 不支持函数回调，需在 data 里逐条设置静态颜色 */
          label: { color: (selStrat && v.name !== selStrat)
            ? 'rgba(142,150,160,.65)'
            : (v.roi >= 1 ? '#57A574' : '#CF5B52') } })),
        label: { show: true, position: 'top', fontSize: 12,
          formatter: p => 'ROI ' + d.strategy[p.dataIndex].roi } }
    ]
  }, true);
  stratChart.off('click').on('click', p => toggleLink('strat', p.name));

  /* 整体ROI逐年变化（点击年份联动全局年份筛选；year 筛选只高亮该年，多年序列不裁点） */
  const yearRoiChart = getChart('p4-yearroi');
  yearRoiChart.setOption({
    tooltip: Object.assign({ trigger: 'axis', formatter: ps => {
        const v = d.yearly_roi[ps[0].dataIndex];
        return v.year + '年<br/>整体ROI: ' + v.roi +
          '<br/><span style="color:#8E96A0">单击联动年份筛选，再点取消；其余年份保留对比</span>'; } }, TOOLTIP),
    grid: { left: 48, right: 24, top: 20, bottom: 32 },
    xAxis: Object.assign({ type: 'category', data: d.yearly_roi.map(v => v.year + '年'),
      axisLabel: { color: p => (selYear && p === selYear + '年' ? '#D9903F' : '#E7E4DC') } }, AXIS_STYLE),
    yAxis: Object.assign({ type: 'value', name: 'ROI', nameTextStyle: { color: '#8E96A0' } }, AXIS_STYLE),
    series: [{ name: '年度ROI', type: 'line', smooth: true, symbolSize: 8,
      lineStyle: { width: 3, color: '#57A574', shadowColor: 'rgba(87,165,116,.25)', shadowBlur: 10 },
      itemStyle: { color: '#D9903F' },
      data: d.yearly_roi.map(v => (selYear && String(v.year) === selYear)
        ? { value: v.roi, symbolSize: 13, itemStyle: { color: '#D9903F', borderColor: '#F2D9A6',
            borderWidth: 2, shadowColor: 'rgba(217,144,63,.9)', shadowBlur: 16 },
            label: { color: '#D9903F', fontWeight: 'bold', fontSize: 13 } }
        : v.roi),
      label: { show: true, position: 'top', color: '#E7E4DC', fontSize: 12 },
      markLine: { data: [{ yAxis: 1 }], symbol: 'none',
        lineStyle: { color: '#D9903F', type: 'dashed' },
        label: { formatter: '盈亏线 1.0', color: '#A87320', fontSize: 10, position: 'insideEndTop' } },
      markPoint: { silent: true, data: [
        { type: 'max', name: '峰值', label: { color: '#57A574' } },
        { type: 'min', name: '谷值', label: { color: '#CF5B52' } }] } }]
  }, true);
  yearRoiChart.off('click').on('click', p => {
    if (p.componentType !== 'series') return;
    toggleGlobalFilter('year', String(d.yearly_roi[p.dataIndex].year));
  });

  /* 联动提示条：说明本页两图仅高亮、不裁数据 */
  const hint = document.getElementById('p4-hint');
  const hints = [];
  if (selStrat) hints.push('策略组「' + selStrat + '」联动中：对比图仅高亮该柱');
  if (selYear) hints.push('年份「' + selYear + '」筛选：逐年ROI仅高亮该年、多年序列不裁点');
  if (hints.length) {
    hint.textContent = '联动说明：' + hints.join('；') +
      (selStrat ? '（逐年ROI为「' + selStrat + '」口径）' : '');
  } else {
    hint.textContent = '';  /* 仅清空文本：元素恒占位（:empty 时 visibility:hidden），避免显隐导致 KPI 行跳动 */
  }
}

/* ==================== 页面5: EDA 报告生成 ==================== */
const EDA_SECTIONS = [
  { key: 'quality', name: '数据质量检查' },
  { key: 'trend', name: '销售趋势分析' },
  { key: 'product', name: '产品维度分析' },
  { key: 'customer', name: '客户画像分析' },
  { key: 'rfm', name: 'RFM分布与唤醒' },
  { key: 'marketing', name: '营销活动ROI' },
  { key: 'findings', name: '关键发现与建议' }
];
const EDA_CHECKED = { segments: [], categories: [], years: [],
  sections: EDA_SECTIONS.map(s => s.key) };
let EDA_GENERATING = false;

function renderCkGroup(containerId, items, key, labelFn) {
  const box = document.getElementById(containerId);
  box.innerHTML = items.map(v => {
    const vstr = String(v);
    const label = escapeHtml(labelFn ? labelFn(vstr) : vstr);
    const note = labelFn ? '<span class="ck-note">' + escapeHtml(vstr) + '</span>' : '';
    return '<label class="p5-ck"><input type="checkbox" value="' + escapeHtml(vstr) + '">' +
      '<span>' + label + '</span>' + note + '</label>';
  }).join('');
  box.querySelectorAll('input').forEach(ck => {
    ck.checked = EDA_CHECKED[key].map(String).includes(ck.value);
    ck.addEventListener('change', () => {
      const set = new Set(EDA_CHECKED[key].map(String));
      if (ck.checked) set.add(ck.value); else set.delete(ck.value);
      EDA_CHECKED[key] = Array.from(set);
      updateEdaSummary();
    });
  });
}

function initEdaPage() {
  fetch('/api/eda/options').then(r => r.json()).then(o => {
    renderCkGroup('ck-segments', o.segments, 'segments');
    renderCkGroup('ck-categories', o.categories, 'categories');
    renderCkGroup('ck-years', o.years, 'years');
    // 章节固定列表，默认全选（value 存 key，显示中文名）
    renderCkGroup('ck-sections', EDA_SECTIONS.map(s => s.key), 'sections',
      key => EDA_SECTIONS.find(s => s.key === key).name);
  }).catch(e => console.error('EDA选项加载失败', e));
  bindEdaButtons();
  updateEdaSummary();
  loadEdaHistory();
}

function bindEdaButtons() {
  const bind = (btnId, key, all) => {
    document.getElementById(btnId).addEventListener('click', () => {
      const box = document.getElementById('ck-' + key);
      const vals = Array.from(box.querySelectorAll('input')).map(ck => ck.value);
      EDA_CHECKED[key] = all ? vals : [];
      box.querySelectorAll('input').forEach(ck => { ck.checked = all; });
      updateEdaSummary();
    });
  };
  bind('btn-all-seg', 'segments', true);
  bind('btn-none-seg', 'segments', false);
  bind('btn-all-cat', 'categories', true);
  bind('btn-none-cat', 'categories', false);
  bind('btn-all-year', 'years', true);
  bind('btn-none-year', 'years', false);
  bind('btn-all-sec', 'sections', true);
  bind('btn-none-sec', 'sections', false);
}

function updateEdaSummary() {
  const txt = k => EDA_CHECKED[k].length ? escapeHtml(EDA_CHECKED[k].join('、')) : '<b>全部</b>';
  const secNames = EDA_CHECKED.sections.length ?
    EDA_CHECKED.sections.map(k =>
      (EDA_SECTIONS.find(s => s.key === k) || { name: k }).name).join('、') :
    '<b>未勾选</b>';
  document.getElementById('p5-summary').innerHTML =
    '数据范围<br/>客群: <b>' + txt('segments') + '</b><br/>品类: <b>' + txt('categories') +
    '</b><br/>年份: <b>' + txt('years') + '</b><br/>分析章节: <b>' + secNames + '</b>';
}

function setEdaStatus(cls, html) {
  const el = document.getElementById('p5-status');
  el.className = 'p5-status show ' + cls;
  el.innerHTML = html;
}

function generateEda() {
  if (EDA_GENERATING) return;
  if (!EDA_CHECKED.sections.length) {
    setEdaStatus('err', '请先勾选至少一个分析章节');
    return;
  }
  EDA_GENERATING = true;
  const btn = document.getElementById('btn-gen');
  btn.disabled = true;
  btn.textContent = '正在生成报告中…';
  setEdaStatus('ing', '正在分析勾选数据并生成 PDF 报告，约需数秒，请稍候…');

  fetch('/api/eda/generate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(EDA_CHECKED)
  }).then(r => r.json()).then(d => {
    EDA_GENERATING = false;
    btn.disabled = false;
    btn.textContent = '一键生成 EDA 报告';
    if (d.error) {
      setEdaStatus('err', '生成失败: ' + escapeHtml(d.error));
      return;
    }
    const m = d.meta;
    setEdaStatus('ok',
      '<span class="dl-link" onclick="window.open(\'' + d.url + '\')">点击下载 ' + d.filename + '</span>' +
      '已生成: ' + m.rows.toLocaleString() + ' 行交易 / ' +
      m.customers.toLocaleString() + ' 位客户 / ' +
      (m.total_sales / 10000).toFixed(1) + ' 万元<br/>范围: ' + m.scope);
    loadEdaHistory();
  }).catch(e => {
    EDA_GENERATING = false;
    btn.disabled = false;
    btn.textContent = '一键生成 EDA 报告';
    setEdaStatus('err', '生成失败，请检查后端服务是否正常');
    console.error(e);
  });
}

function loadEdaHistory() {
  fetch('/api/eda/list').then(r => r.json()).then(d => {
    const box = document.getElementById('p5-history');
    if (!d.reports || !d.reports.length) {
      box.innerHTML = '<div class="his-empty">暂无已生成的报告</div>';
      return;
    }
    box.innerHTML = d.reports.map(x => {
      const t = new Date(x.mtime * 1000);
      const p = n => String(n).padStart(2, '0');
      const time = t.getFullYear() + '-' + p(t.getMonth() + 1) + '-' + p(t.getDate()) +
        ' ' + p(t.getHours()) + ':' + p(t.getMinutes());
      const size = x.size > 1048576 ? (x.size / 1048576).toFixed(1) + 'MB'
        : Math.max(1, Math.round(x.size / 1024)) + 'KB';
      return '<div class="his-item">' +
        '<span class="his-name">' + escapeHtml(x.name) + '</span>' +
        '<span class="his-time">' + time + '</span>' +
        '<span class="his-size">' + size + '</span>' +
        '<span class="his-dl" onclick="window.open(\'/api/eda/download/' + x.name + '\')">下载</span></div>';
    }).join('');
  }).catch(e => console.error('EDA历史加载失败', e));
}

document.getElementById('btn-gen').addEventListener('click', generateEda);
initEdaPage();

/* 初始加载 */
initFilters();
/* URL 参数支持: ?page=p2 指定初始页签(用于截图/分享) */
(function () {
  const q = new URLSearchParams(location.search);
  const p = q.get('page');
  if (p && document.querySelector('.tab[data-page="' + p + '"]')) {
    switchPage(p);
  }
  if (q.get('seg')) LINK_STATE.seg = q.get('seg');
  if (q.get('category')) LINK_STATE.category = q.get('category');
  if (q.get('strat')) LINK_STATE.strat = q.get('strat');
})();
loadData();
resize();

/* ==================== 页面7: 自定义数据（拖入实时更新） ==================== */
function setUploadStatus(cls, html) {
  const el = document.getElementById('p7-upload-status');
  el.className = 'p7-upload-status show ' + cls;
  el.innerHTML = html;
}

function uploadFile(file) {
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  setUploadStatus('ing', '正在解析并清洗「' + escapeHtml(file.name) + '」…');
  fetch('/api/upload/preview', { method: 'POST', body: fd }).then(r => r.json()).then(d => {
    if (d.error) { setUploadStatus('err', '上传失败: ' + escapeHtml(d.error)); return; }
    renderPreview(d);
  }).catch(e => { setUploadStatus('err', '上传失败，请检查后端服务'); console.error(e); });
}

function uploadPaste() {
  const text = document.getElementById('p7-paste').value.trim();
  if (!text) { setUploadStatus('err', '请先粘贴表格文本'); return; }
  setUploadStatus('ing', '正在解析粘贴的表格…');
  fetch('/api/upload/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: text, name: '粘贴文本' }) }).then(r => r.json()).then(d => {
    if (d.error) { setUploadStatus('err', '上传失败: ' + escapeHtml(d.error)); return; }
    renderPreview(d);
  }).catch(e => { setUploadStatus('err', '上传失败，请检查后端服务'); console.error(e); });
}

function bindP7Dropzone() {
  const dz = document.getElementById('p7-dropzone');
  const fileInput = document.getElementById('p7-file');
  dz.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', () => {
    uploadFile(fileInput.files[0]);
    fileInput.value = '';
  });
  ['dragover', 'dragenter'].forEach(ev => dz.addEventListener(ev, e => {
    e.preventDefault(); e.stopPropagation(); dz.classList.add('over');
  }));
  ['dragleave', 'drop'].forEach(ev => dz.addEventListener(ev, e => {
    e.preventDefault(); e.stopPropagation(); dz.classList.remove('over');
  }));
  dz.addEventListener('drop', e => {
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) uploadFile(f);
  });
  document.getElementById('p7-paste-btn').addEventListener('click', uploadPaste);
}

function loadCustomState() {
  fetch('/api/custom/state').then(r => r.json()).then(d => {
    const box = document.getElementById('p7-batches');
    if (!d.batches || !d.batches.length) {
      box.innerHTML = '<div class="his-empty">尚未上传数据（拖入文件或粘贴表格，预览清洗结果并确认后并入大屏）</div>';
      return;
    }
    box.innerHTML = d.batches.map(b =>
      '<div class="p7-batch-item">' +
      '<span class="bt-name" title="' + escapeHtml(b.name) + '">' + escapeHtml(b.name) + '</span>' +
      '<span class="bt-meta">' + b.rows.toLocaleString() + ' 行 · ' + b.time + '</span>' +
      '<span class="bt-del" data-batch="' + b.id + '">删除</span></div>').join('');
    box.querySelectorAll('.bt-del').forEach(el => {
      el.addEventListener('click', () => {
        fetch('/api/custom/delete', { method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ batch_id: el.dataset.batch }) }).then(r => r.json()).then(() => {
          refreshAllCustom();
        });
      });
    });
  }).catch(e => console.error('自定义批次加载失败', e));
}

function loadCustomAnalyze() {
  fetch('/api/custom/analyze').then(r => r.json()).then(d => {
    document.getElementById('p7-note').textContent = d.metric_note
      ? '口径说明：' + d.metric_note : '';
    const statsBox = document.getElementById('p7-stats');
    const trendChart = getChart('p7-trend');
    const pieChart = getChart('p7-pie');
    const tableBox = document.getElementById('p7-table');
    if (d.empty) {
      statsBox.innerHTML = '';
      const emptyOpt = { graphic: [{ type: 'text', left: 'center', top: 'middle',
        style: { text: '尚未上传数据', fill: '#8E96A0', fontSize: 15 } }] };
      trendChart.setOption(emptyOpt, true);
      pieChart.setOption(emptyOpt, true);
      tableBox.innerHTML = '<div class="his-empty">拖入数据后此处展示明细表（前 100 行）</div>';
      return;
    }
    renderKPI('p7-stats', d.stats);

    /* 月度趋势折线 */
    if (d.trend && d.trend.length) {
      trendChart.setOption({
        tooltip: Object.assign({ trigger: 'axis', formatter: ps => d.trend[ps[0].dataIndex].ym + '<br/>金额: ' + ps[0].value + ' 万' }, TOOLTIP),
        grid: { left: 56, right: 24, top: 20, bottom: 40 },
        xAxis: Object.assign({ type: 'category', data: d.trend.map(v => v.ym),
          axisLabel: { color: '#8E96A0', fontSize: 10, interval: 2, rotate: 35 } }, AXIS_STYLE),
        yAxis: Object.assign({ type: 'value', name: '万元', nameTextStyle: { color: '#8E96A0' } }, AXIS_STYLE),
        series: [{ name: '上传金额(万)', type: 'line', smooth: true, symbol: 'circle', symbolSize: 5,
          lineStyle: { width: 2.5, color: '#C9A227', shadowColor: 'rgba(201,162,39,.25)', shadowBlur: 10 },
          areaStyle: { color: { type: 'linear', x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [{ offset: 0, color: 'rgba(201,162,39,.14)' }, { offset: 1, color: 'rgba(201,162,39,.02)' }] } },
          data: d.trend.map(v => v.value) }]
      }, true);
    } else {
      trendChart.setOption({ graphic: [{ type: 'text', left: 'center', top: 'middle',
        style: { text: '上传数据无日期列，无法生成趋势', fill: '#8E96A0', fontSize: 14 } }] }, true);
    }

    /* 品类构成饼图 */
    if (d.category_pie && d.category_pie.length) {
      pieChart.setOption(pieOption(d.category_pie, {
        radius: ['40%', '64%'], center: ['40%', '52%'] }), true);
    } else {
      pieChart.setOption({ graphic: [{ type: 'text', left: 'center', top: 'middle',
        style: { text: '上传数据无品类列', fill: '#8E96A0', fontSize: 14 } }] }, true);
    }

    /* 明细表（脏数据着色：红=无效/丢弃，橙=已修正/兜底；预览与自动分析共用） */
    renderDirtyTable(tableBox, d);
    resizeAll();
  }).catch(e => console.error('自定义分析加载失败', e));
}

/* 明细表渲染（脏数据着色：红=无效/丢弃，橙=已修正/兜底；预览与自动分析共用） */
function renderDirtyTable(box, d) {
  if (d.table && d.table.length) {
    const head = d.columns.map(c => '<th>' + escapeHtml(c) + '</th>').join('') + '<th class="th-note">行状态</th>';
    const rows = d.table.map(r => {
      const tds = d.columns.map(c => {
        const v = (r.cells && r.cells[c] !== undefined && r.cells[c] !== null) ? r.cells[c] : '';
        const fl = r.flags && r.flags[c];
        const cls = fl ? (fl.level === 'bad' ? ' class="cell-bad"' : ' class="cell-warn"') : '';
        const tip = fl ? ' title="' + escapeHtml(fl.msg) + '"' : '';
        return '<td' + cls + tip + '>' + escapeHtml(v) + '</td>';
      }).join('');
      const rowCls = r.row_status === 'dropped' ? ' class="row-dropped"'
        : (r.row_status === 'fixed' ? ' class="row-fixed"' : '');
      return '<tr' + rowCls + '>' + tds + '<td class="cell-note">' + escapeHtml(r.row_msg || '') + '</td></tr>';
    }).join('');
    let legend = '';
    if (d.dirty_summary && d.dirty_summary.total > 0) {
      const okCnt = d.dirty_summary.total - d.dirty_summary.dropped - d.dirty_summary.fixed;
      legend = '<div class="p7-legend">' +
        '<span class="lg"><i class="sw sw-bad"></i>无效/已丢弃 ' + d.dirty_summary.dropped + ' 行</span>' +
        '<span class="lg"><i class="sw sw-warn"></i>已修正/兜底 ' + d.dirty_summary.fixed + ' 行</span>' +
        '<span class="lg"><i class="sw sw-ok"></i>正常 ' + okCnt + ' 行</span>' +
        '</div>';
    }
    box.innerHTML = legend +
      '<table class="p7-table"><thead><tr>' + head + '</tr></thead><tbody>' + rows + '</tbody></table>';
  } else {
    box.innerHTML = '<div class="his-empty">暂无明细数据</div>';
  }
}

/* ---------- 清洗预览：勾选后确认并入大屏 ---------- */
let PENDING_ID = '';

function setPvStatus(cls, html) {
  const el = document.getElementById('p7-pv-status');
  el.className = 'p7-pv-status' + (cls ? ' ' + cls : '');
  el.innerHTML = html;
}

function renderPreview(d) {
  PENDING_ID = d.pending_id || '';
  document.getElementById('p7-pv-name').textContent = '「' + (d.name || '上传数据') + '」';
  renderKPI('p7-pv-stats', d.stats);
  renderDirtyTable(document.getElementById('p7-pv-table'), d);
  document.getElementById('p7-pv-commit').checked = true;
  document.getElementById('p7-pv-confirm').textContent = '确认导入';
  setPvStatus('', '红色=无效/已丢弃，橙色=已修正/兜底。确认后清洗数据集将并入大屏。');
  document.getElementById('p7-preview').classList.add('show');
}

function confirmPreview(commit) {
  if (!PENDING_ID) return;
  const btn = document.getElementById('p7-pv-confirm');
  btn.disabled = true;
  fetch('/api/upload/confirm', { method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ pending_id: PENDING_ID, commit: commit }) }).then(r => r.json()).then(d => {
    btn.disabled = false;
    if (d.error) { setPvStatus('err', escapeHtml(d.error)); return; }
    PENDING_ID = '';
    document.getElementById('p7-preview').classList.remove('show');
    document.getElementById('p7-paste').value = '';
    if (d.committed) {
      setUploadStatus('ok', '已并入大屏「' + escapeHtml(d.batch.name) + '」：' + d.batch.rows.toLocaleString() + ' 行（清洗后），全屏已刷新。');
      refreshAllCustom();
    } else {
      setUploadStatus('ok', '已放弃本次数据，未并入大屏。');
    }
  }).catch(e => { btn.disabled = false; setPvStatus('err', '操作失败，请检查后端服务'); console.error(e); });
}

document.getElementById('p7-pv-confirm').addEventListener('click', () => {
  confirmPreview(document.getElementById('p7-pv-commit').checked);
});
document.getElementById('p7-pv-cancel').addEventListener('click', () => confirmPreview(false));
document.getElementById('p7-pv-commit').addEventListener('change', e => {
  document.getElementById('p7-pv-confirm').textContent = e.target.checked ? '确认导入' : '确认放弃';
});

function refreshAllCustom() {
  loadData();          /* 全屏实时更新（新数据已合并入星型模型） */
  loadCustomState();
  loadCustomAnalyze();
}

document.getElementById('p7-clear').addEventListener('click', () => {
  if (!document.querySelector('#p7-batches .p7-batch-item')) return;
  fetch('/api/custom/clear', { method: 'POST' }).then(r => r.json()).then(() => {
    setUploadStatus('ok', '已清空全部上传数据，恢复基线。');
    refreshAllCustom();
  });
});

bindP7Dropzone();
loadCustomState();
loadCustomAnalyze();

/* ==================== ⑦ 中文问答查数（规则版 ChatBI，离线） ==================== */
let ASK_SEQ = 0;      /* 请求序号：连续提问只渲染最后一次 */
let ASK_DATA = null;  /* 当前展示的答案，供「在图中查看」使用 */

function openAskCard() {
  document.getElementById('ask-mask').classList.add('show');
  document.getElementById('ask-card').classList.add('show');
}
function closeAskCard() {
  document.getElementById('ask-mask').classList.remove('show');
  document.getElementById('ask-card').classList.remove('show');
}

function askSubmit(q) {
  q = (q || '').trim();
  if (!q) { document.getElementById('ask-input').focus(); return; }
  document.getElementById('ask-input').value = q;
  openAskCard();
  document.getElementById('ask-q').textContent = q;
  document.getElementById('ask-body').innerHTML = '<div class="ask-empty">正在按当前数据计算…</div>';
  const seq = ++ASK_SEQ;
  fetch('/api/ask?q=' + encodeURIComponent(q)).then(r => r.json()).then(d => {
    if (seq !== ASK_SEQ) return;
    ASK_DATA = d;
    renderAsk(d);
  }).catch(e => {
    if (seq !== ASK_SEQ) return;
    console.error('问答请求失败', e);
    document.getElementById('ask-body').innerHTML =
      '<div class="ask-empty">查询失败：后端未响应。请确认大屏服务正在运行（双击「启动大屏.bat」）。</div>';
  });
}

function askChips(list, label) {
  if (!list || !list.length) return '';
  return '<div class="ask-suggest"><span class="as-label">' + label + '</span>' +
    list.map(s => '<button class="ask-chip" data-ask="' + escapeHtml(s) + '">' +
      escapeHtml(s) + '</button>').join('') + '</div>';
}

function renderAsk(d) {
  const body = document.getElementById('ask-body');
  document.getElementById('ask-q').textContent = d.question || document.getElementById('ask-input').value;
  paintAskMode(d);

  /* 没听懂 / 无数据：只给结论与示例，不给假数字 */
  if (!d.ok) {
    body.innerHTML = '<div class="ask-empty">' + escapeHtml(d.answer || '') + '</div>' +
      askChips(d.examples || d.suggest, '试试这样问') +
      (d.note ? '<div class="ask-note">' + escapeHtml(d.note) + '</div>' : '');
    bindAskChips(body);
    return;
  }

  const hasVal = d.value !== null && d.value !== undefined && d.value !== '';
  let html = '<div class="ask-hero">' +
    '<div class="ask-value">' + escapeHtml(hasVal ? d.value : '—') +
    '<span class="ask-unit">' + escapeHtml(d.unit || '') + '</span></div>' +
    (d.metric_label ? '<div class="ask-badge">' + escapeHtml(d.metric_label) + '</div>' : '') +
    '</div>' +
    '<div class="ask-answer">' + escapeHtml(d.answer || '') + '</div>';

  if (d.understood && d.understood.length) {
    html += '<div class="ask-understood"><b>我按这些条件理解：</b>' +
      d.understood.map(escapeHtml).join(' · ') + '</div>';
  }

  if (d.detail && d.detail.rows && d.detail.rows.length) {
    const head = d.detail.columns.map(c => '<th>' + escapeHtml(c) + '</th>').join('');
    const rows = d.detail.rows.map(r => '<tr>' +
      r.map(c => '<td>' + escapeHtml(c == null ? '' : c) + '</td>').join('') + '</tr>').join('');
    html += '<table class="ask-table"><thead><tr>' + head + '</tr></thead><tbody>' + rows + '</tbody></table>';
  }

  const canFollow = d.follow && ((d.follow.filters && Object.keys(d.follow.filters).length) ||
    (d.follow.link && Object.keys(d.follow.link).length));
  html += '<div class="ask-foot">' +
    '<button class="ask-goto" id="ask-goto"' + (canFollow ? '' : ' disabled') + '>在图中查看</button>' +
    '<span class="as-label" style="font-size:11.5px;color:#6A7280">' +
    (canFollow ? '按解析出的维度联动筛选与图表' : '该问题无对应图表维度') + '</span></div>';

  if (d.note) html += '<div class="ask-note">' + escapeHtml(d.note) + '</div>';
  html += askChips(d.suggest, '继续问');
  body.innerHTML = html;

  const goto = document.getElementById('ask-goto');
  if (goto && canFollow) goto.addEventListener('click', applyAskFollow);
  bindAskChips(body);
}

function bindAskChips(root) {
  root.querySelectorAll('[data-ask]').forEach(el => {
    el.addEventListener('click', () => askSubmit(el.dataset.ask));
  });
}

/* 目标页：按指标落到最相关的那一页，避免「联动了但看不到」 */
function askTargetPage(d) {
  const m = d.metric;
  if (m === 'roi') return 'p4';
  if (m === 'awaken_rate') return 'p3';
  if (m === 'repurchase') return 'p2';
  if (d.slots && d.slots.dim === 'category') return 'p2';
  if (m === 'customers') return 'p1';
  return 'p0';
}

/* 在图中查看：先清空现有筛选，再套用问句解析出的维度，只触发一次数据加载 */
function applyAskFollow() {
  const d = ASK_DATA;
  if (!d) return;
  const f = (d.follow && d.follow.filters) || {};
  const l = (d.follow && d.follow.link) || {};
  FILTER_KEYS.forEach(k => {
    const sel = document.getElementById('f-' + k);
    if (sel) sel.value = (f[k] === undefined || f[k] === null) ? '' : String(f[k]);
  });
  Object.keys(LINK_STATE).forEach(k => { LINK_STATE[k] = l[k] || ''; });
  closeAskCard();
  switchPage(askTargetPage(d));
  loadData();
  showToast('已按「' + (d.question || '') + '」联动图表');
}

document.getElementById('ask-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); askSubmit(e.target.value); }
});
document.getElementById('ask-close').addEventListener('click', closeAskCard);
document.getElementById('ask-mask').addEventListener('click', closeAskCard);

/* 支持 ?q=... 直达答案（验收 / 截图用，不影响正常使用） */
const ASK_Q = new URLSearchParams(location.search).get('q');
/* ?llm=1 先自动开启本地 LLM 再提问（验收/截图用），因此提交要稍晚一点 */
const ASK_Q_DELAY = new URLSearchParams(location.search).get('llm') === '1' ? 1400 : 400;
if (ASK_Q) setTimeout(() => askSubmit(ASK_Q), ASK_Q_DELAY);

/* ==================== ⑨ 解析模式：规则解析 ↔ 本地 LLM ==================== */
/* 服务端是唯一权威：开关默认关闭，打开后若 Ollama 不可用会自动回退，
   前端只负责显示当前模式与最近一次解析走了哪条路。 */
let LLM_STATE = { enabled: false, locked: false, available: null, model: 'qwen2.5:7b', reason: '' };

function paintAskMode(d) {
  const el = document.getElementById('ask-mode');
  if (!el) return;
  const m = d && d.parse_mode;
  el.className = 'am' + (m === 'llm' ? ' llm' : '');
  el.textContent = m === 'llm' ? '本地LLM解析'
    : (m === 'rule-fallback' ? '规则解析（回退）' : '');
  el.title = (d && d.parse_note) || '';
}

function paintLlmSwitch() {
  const btn = document.getElementById('llm-sw');
  const lbl = document.getElementById('llm-sw-lbl');
  if (!btn || !lbl) return;
  lbl.textContent = LLM_STATE.enabled ? '本地LLM' : '规则解析';
  btn.classList.toggle('on', !!LLM_STATE.enabled);
  btn.disabled = !!LLM_STATE.locked;
  let tip;
  if (LLM_STATE.locked) {
    tip = '开关被启动环境变量 RFM_ENABLE_LLM 锁定';
  } else if (!LLM_STATE.enabled) {
    tip = '当前：规则解析（完全离线，不需要任何模型服务）。点击切换为本地 LLM 意图解析';
  } else if (LLM_STATE.available === false) {
    tip = '已开启，但本地 Ollama 不可用（' + (LLM_STATE.reason || '未运行') +
      '）：问答会自动回退规则解析，不影响出数';
  } else {
    tip = '当前：本地 LLM 解析意图（' + LLM_STATE.model +
      '，仅本机回环地址），数字仍由规则聚合实时计算';
  }
  btn.title = tip;
}

function applyLlmStatus(st) {
  if (!st || typeof st !== 'object') return;
  LLM_STATE = Object.assign(LLM_STATE, {
    enabled: !!st.enabled,
    locked: !!st.locked,
    available: (st.available === undefined ? LLM_STATE.available : st.available),
    model: st.model || LLM_STATE.model,
    reason: st.reason || ''
  });
  paintLlmSwitch();
}

function refreshLlmStatus() {
  fetch('/api/llm/status').then(r => r.json()).then(applyLlmStatus).catch(() => {});
}

/* 切换接口：本机 + 自定义请求头（服务端会校验） */
function setLlmEnabled(on) {
  return fetch('/api/llm/toggle?on=' + (on ? 1 : 0), {
    method: 'POST', headers: { 'X-RFM-Action': 'toggle-llm' }
  }).then(r => r.json().then(d => ({ ok: r.ok, d })));
}

(function bindLlmSwitch() {
  const btn = document.getElementById('llm-sw');
  if (!btn) return;
  btn.addEventListener('click', () => {
    if (btn.disabled) return;
    const next = !LLM_STATE.enabled;
    btn.disabled = true;
    setLlmEnabled(next).then(({ ok, d }) => {
      btn.disabled = false;
      if (!ok) {
        showToast(d && d.error ? d.error : '切换失败');
        refreshLlmStatus();
        return;
      }
      applyLlmStatus(d);
      if (d.enabled && d.available === false) {
        showToast('已开启本地 LLM，但 Ollama 未连接：本次仍走规则解析');
      } else {
        showToast(d.enabled ? '已切换为本地 LLM 意图解析' : '已切回规则解析（完全离线）');
      }
    }).catch(() => {
      btn.disabled = false;
      showToast('切换失败：后端未响应');
    });
  });

  refreshLlmStatus();
  /* ?llm=1：直达验收用，先开启再让 askSubmit 跑 */
  if (new URLSearchParams(location.search).get('llm') === '1') {
    setLlmEnabled(true).then(({ d }) => applyLlmStatus(d)).catch(() => {});
  }
})();
