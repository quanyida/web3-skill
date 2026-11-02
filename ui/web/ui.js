// token-research/web/ui.js — renders the screen from the API. No frameworks, no build step.
// Every figure printed here comes from a request in api.js: a null prints as a grey dash,
// a real zero prints as 0. Nothing on this page is hardcoded data.

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const DASH = '<span class="dash">—</span>';

// Money in millions with a fixed 2 decimals; dollars under $1M keep 3.
function m(v) {
  if (v == null) return null;
  if (Math.abs(v) >= 1000) return (v / 1000).toFixed(2) + 'B';
  if (Math.abs(v) >= 1) return v.toFixed(2) + 'M';
  return v.toFixed(3) + 'M';
}
// A ratio as a percentage with 2 decimals (TSR 1.96 → "196%").
function pct(v) {
  if (v == null) return null;
  const n = v * 100;
  if (Math.abs(n) >= 100) return n.toFixed(0) + '%';
  if (Math.abs(n) >= 1) return n.toFixed(2) + '%';
  return n.toFixed(3) + '%';
}
function count(v) {
  if (v == null) return null;
  return v.toLocaleString('zh-CN');
}
function cell(v, fmt) {
  const s = v == null ? null : fmt(v);
  return s == null ? DASH : `<span class="num">${s}</span>`;
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
// Raw dollars → a number of millions. Formatting is `m`'s job, so a caller never
// holds a string where it expects a number (that mismatch blanked the whole page).
const usdM = (v) => (v == null ? null : v / 1e6);

// ---------- verdict → label + class ----------
function verdictMeta(o) {
  const v = (o.latest_report || {}).verdict;
  if (v === 'narrative') return { cls: 'is-narrative', text: '叙事定价' };
  if (v === 'mixed') return { cls: 'is-mixed', text: '部分真实' };
  if (v === 'real') return { cls: 'is-real', text: '真实业务' };
  if (!o.snapshots) return { cls: 'is-none', text: '未抓取' };
  return { cls: 'is-none', text: '未判定' };
}

// ---------- shared state ----------
const STATE = { objects: [], stats: null, selected: null, reportId: null };

// ---------- error surface ----------
function showError(el, label, err) {
  if (!el) return;
  const status = err && err.status ? `HTTP ${err.status} · ` : '';
  el.innerHTML = `<p class="empty">${esc(label)} 读取失败：${esc(status)}${esc((err && err.message) || err)}</p>`;
}

// ---------- KPI strip ----------
function renderStats(s) {
  $('#kTotal').textContent = s.total;
  $('#kJudged').textContent = s.judged;
  $('#kNarr').textContent = s.narrative;
  $('#kHold').textContent = s.with_holders;
  $('#tvlTotal').textContent = m(s.total_tvl_m) || '—';
  $('#subCount').textContent = s.total;
  $('#footStamp').textContent = s.latest_captured_day || '—';
}

// ---------- The finding, computed from the rows ----------
function renderFinding(rows) {
  const withBoth = rows.filter((o) => o.tsr != null);
  const narrative = rows.filter((o) => (o.latest_report || {}).verdict === 'narrative').length;
  const judged = rows.filter((o) => (o.latest_report || {}).verdict).length;
  const el = $('#finding');
  if (!withBoth.length) {
    el.innerHTML = `库里还没有同时有 TVL 与市值的对象，暂无可比比值。当前 ${judged} / ${rows.length} 个对象完成祛魅判定。`;
    return;
  }
  const lowest = withBoth.reduce((a, b) => (a.tsr <= b.tsr ? a : b));
  const highest = withBoth.reduce((a, b) => (a.tsr >= b.tsr ? a : b));
  el.innerHTML =
    `<strong>${esc(lowest.name)}</strong> 的 TVL 只有市值的 <span class="f-num">${pct(lowest.tsr)}</span>` +
    `（$${usdM(lowest.tvl_usd).toFixed(2)}M 对 $${usdM(lowest.market_cap_usd).toFixed(1)}M），全库最低；` +
    `反向看，<strong>${esc(highest.name)}</strong> 达 <span class="f-num">${pct(highest.tsr)}</span>，` +
    `这个比值只说明锁在链上的钱相对市值有多少，不代表代币捕获了这些钱。` +
    ` ${rows.length} 个研究对象里 <span class="f-num">${narrative}</span> 个判定为纯叙事，` +
    `<span class="f-num">${judged}</span> 个已完成判定。`;
}

// ---------- TSR chart: the signature element ----------
// A ranked bar list where the bar is TSR. TSR runs from 0.54% to 196% — 363×. On a linear
// scale the top bar fills the track and the bottom one collapses to a stub that reads as zero,
// so the bar is drawn on a LOG scale with its ticks labelled; the exact percentage stays in
// text at the row's end, so the precise value never depends on reading the bar.
function drawTsr(ranked) {
  const el = $('#tsrChart');
  if (!ranked.length) {
    el.innerHTML = '<p class="empty">没有可计算 TSR 的对象（TVL 或市值缺失）。</p>';
    return;
  }
  const W = Math.max(el.clientWidth || 760, 320);
  const LABEL = Math.min(150, Math.max(96, Math.round(W * 0.18)));
  const VALUE = 76;    // value column on the right
  const ROW = 34;
  const BAR = 14;
  const TOP = 22;      // room for the log ticks
  const H = ranked.length * ROW + TOP + 8;
  const plotW = Math.max(W - LABEL - VALUE, 60);
  const NS = 'http://www.w3.org/2000/svg';

  // Log scale over [0.001, 10] (0.1% .. 1000%): every bar keeps a readable length.
  const LO = 0.001, HI = 10;
  const xOf = (v) => LABEL + (Math.log10(Math.min(Math.max(v, LO), HI)) - Math.log10(LO)) /
    (Math.log10(HI) - Math.log10(LO)) * plotW;

  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label',
    'TVL 与市值之比排名（对数刻度，刻度 0.1% 到 1000%）：' +
    ranked.map((r) => `${r.name} ${pct(r.tsr)}`).join('，'));

  // Log ticks at 0.1% / 1% / 10% / 100% / 1000%, so the reader can see the scale is not linear.
  const ticks = [0.001, 0.01, 0.1, 1, 10];
  const tickLabel = (v) => (v >= 1 ? (v * 100).toFixed(0) + '%' : (v * 100).toFixed(1) + '%');
  ticks.forEach((tv) => {
    const x = xOf(tv);
    const line = document.createElementNS(NS, 'line');
    line.setAttribute('x1', x); line.setAttribute('x2', x);
    line.setAttribute('y1', TOP - 8); line.setAttribute('y2', H - 6);
    line.setAttribute('class', 'tsr-tick');
    line.setAttribute('opacity', tv === 1 ? '0.55' : '0.22');
    svg.appendChild(line);
    const t = document.createElementNS(NS, 'text');
    t.setAttribute('x', x);
    t.setAttribute('y', TOP - 12);
    t.setAttribute('text-anchor', 'middle');
    t.textContent = tickLabel(tv);
    svg.appendChild(t);
  });

  ranked.forEach((r, i) => {
    const y = TOP + i * ROW;

    const name = document.createElementNS(NS, 'text');
    name.setAttribute('x', 0);
    name.setAttribute('y', y + BAR);
    name.setAttribute('class', 'tsr-name');
    name.textContent = r.name;
    svg.appendChild(name);

    const track = document.createElementNS(NS, 'rect');
    track.setAttribute('x', LABEL);
    track.setAttribute('y', y + 1);
    track.setAttribute('width', plotW);
    track.setAttribute('height', BAR);
    track.setAttribute('class', 'tsr-track');
    track.setAttribute('rx', '2');
    svg.appendChild(track);

    const w = Math.max(xOf(r.tsr) - LABEL, 2);
    const fill = document.createElementNS(NS, 'rect');
    fill.setAttribute('x', LABEL);
    fill.setAttribute('y', y + 1);
    fill.setAttribute('width', w);
    fill.setAttribute('height', BAR);
    fill.setAttribute('class', 'tsr-fill' + (i === 0 ? ' is-lead' : ''));
    fill.setAttribute('rx', '2');
    svg.appendChild(fill);

    const val = document.createElementNS(NS, 'text');
    val.setAttribute('x', W);
    val.setAttribute('y', y + BAR);
    val.setAttribute('text-anchor', 'end');
    val.setAttribute('class', 'tsr-val');
    val.textContent = pct(r.tsr);
    svg.appendChild(val);
  });

  el.replaceChildren(svg);
}

const rankedTsr = (rows) => rows.filter((o) => o.tsr != null)
  .slice().sort((a, b) => b.tsr - a.tsr);

// ---------- Table ----------
function rowHtml(o) {
  const v = verdictMeta(o);
  const sym = o.symbol ? `<span class="sym">${esc(o.symbol)}</span>` : '';
  return `<tr tabindex="0" data-slug="${esc(o.slug)}"${STATE.selected === o.slug ? ' class="is-selected"' : ''}>
    <td class="col-name"><span class="name-cell"><span class="name-txt">${esc(o.name)}</span>${sym}</span></td>
    <td class="num" data-label="TVL">${cell(usdM(o.tvl_usd), m)}</td>
    <td class="num" data-label="市值">${cell(usdM(o.market_cap_usd), m)}</td>
    <td class="num" data-label="TSR">${o.tsr == null ? DASH : `<span class="score">${pct(o.tsr)}</span>`}</td>
    <td class="num" data-label="持币地址">${cell(o.holders, count)}</td>
    <td class="num" data-label="前10占比">${o.top10_pct == null ? DASH : `<span class="score${o.top10_pct >= 80 ? ' warn-cell' : ''}">${o.top10_pct.toFixed(2)}%</span>`}</td>
    <td class="col-verdict"><span class="verdict ${v.cls}">${v.text}</span></td>
  </tr>`;
}

function renderTable() {
  const q = ($('#q').value || '').trim().toLowerCase();
  const ranked = rankedTsr(STATE.objects);
  const rest = STATE.objects.filter((o) => o.tsr == null);
  const shown = ranked.concat(rest).filter((o) =>
    !q || o.name.toLowerCase().includes(q)
    || (o.symbol || '').toLowerCase().includes(q) || o.slug.includes(q));
  $('#tbody').innerHTML = shown.map(rowHtml).join('');
  $('#rowCount').textContent = shown.length + ' / ' + STATE.objects.length;
  $('#emptyRows').hidden = shown.length > 0;
}

// ---------- Detail ----------
function kvRows(d) {
  const o = d.object, s = d.snapshot, m2 = (s && s.metrics) || {};
  const hs = d.holder_structure;
  const dist = (hs && hs.distribution) || {};
  const rep = (d.reports || [])[0] || null;
  return [
    ['TVL', m2.tvl_usd == null ? null : '$' + m(m2.tvl_usd / 1e6)],
    ['市值', m2.market_cap_usd == null ? null : '$' + m(m2.market_cap_usd / 1e6)],
    ['FDV', m2.fdv_usd == null ? null : '$' + m(m2.fdv_usd / 1e6)],
    ['TSR', (m2.tvl_usd && m2.market_cap_usd) ? pct(m2.tvl_usd / m2.market_cap_usd) : null],
    ['持币地址', count(dist.holders)],
    ['前 10 占比', dist.top10_holding_pct == null ? null : dist.top10_holding_pct.toFixed(2) + '%'],
    ['快照', s ? s.captured_day : null],
    ['报告', rep ? rep.report_date : null],
  ];
}

function renderDetail(d) {
  const o = d.object, snap = d.snapshot;
  const rep = (d.reports || [])[0] || null;
  const dem = (rep && rep.demystify) || {};
  const panel = $('#detail');
  panel.hidden = false;

  const kindText = o.kind === 'chain' ? '公链' : o.kind === 'protocol' ? '协议' : '代币';
  const v = rep && rep.verdict
    ? (rep.verdict === 'narrative' ? { cls: 'is-narrative', text: '叙事定价' }
      : rep.verdict === 'mixed' ? { cls: 'is-mixed', text: '部分真实' }
        : rep.verdict === 'real' ? { cls: 'is-real', text: '真实业务' }
          : { cls: 'is-none', text: '未判定' })
    : { cls: 'is-none', text: '未判定' };

  $('#detailMeta').innerHTML =
    `<span class="verdict ${v.cls}">${v.text}</span> · ${kindText}` +
    ` · 快照 ${o.snapshot_id || d.snapshot.id}` +
    (o.last_snapshot_at ? ` · 抓取于 ${snap.captured_day}` : '');

  $('#detailLiner').textContent = (rep && rep.one_liner) || '这个对象已入库，但还没有登记研究报告。';

  const capture = dem.q5_value_capture || null;
  $('#detailCapture').textContent = capture ? '已评估' : (rep ? '报告未单列' : DASH);
  $('#detailCaptureNote').innerHTML = capture
    ? esc(capture)
    : (rep ? esc(rep.verdict_note || '报告没有单列「代币价值捕获」，以判定依据为准。')
      : '跑一遍 Step 2.5 + Step 5.5 后补上。');

  $('#detailProtocol').textContent = rep ? (dem.q1_who_uses ? '见报告 Q1' : '未记录') : '未记录';

  const missing = (snap && snap.missing) || [];
  $('#detailMissing').textContent = missing.length ? missing.join('、') : '无';

  $('#detailKv').innerHTML = kvRows(d).map(([k, val]) =>
    `<dt>${k}</dt><dd>${val == null ? DASH : esc(val)}</dd>`).join('');

  // Holder distribution — only the fields that are actually populated.
  const hs = d.holder_structure;
  const dist = (hs && hs.distribution) || {};
  const rows = [
    ['持币地址', count(dist.holders)],
    ['前 10 占比', dist.top10_holding_pct == null ? null : dist.top10_holding_pct.toFixed(2) + '%'],
    ['聪明钱', dist.smart_money_pct == null ? null : Number(dist.smart_money_pct).toFixed(2) + '%'],
    ['KOL', dist.kol_pct == null ? null : Number(dist.kol_pct).toFixed(2) + '%'],
    ['内部地址', dist.insider_pct == null ? null : Number(dist.insider_pct).toFixed(2) + '%'],
    ['狙击手', dist.sniper_pct == null ? null : Number(dist.sniper_pct).toFixed(2) + '%'],
    ['新钱包', dist.fresh_wallet_pct == null ? null : Number(dist.fresh_wallet_pct).toFixed(2) + '%'],
  ].filter(([, val]) => val != null);
  $('#detailHolders').innerHTML = rows.length
    ? rows.map(([k, val]) => `<dt>${k}</dt><dd>${esc(val)}</dd>`).join('')
    : `<dt>持有结构</dt><dd class="dash">未做 Step 2.5</dd>`;

  // History: a line only when there are at least two points; one point is a date, not a trend.
  const hist = d.history && d.history.points ? d.history.points : [];
  const vals = hist.map((p) => p.tvl_usd).filter((x) => x != null);
  const chart = $('#detailHistory');
  if (vals.length >= 2) {
    chart.innerHTML = '';
    window.Charts.line(chart, {
      labels: hist.map((p) => (p.captured_day || '').slice(5)),
      series: [{ name: 'TVL', values: hist.map((p) => p.tvl_usd) }],
      format: window.Charts.fmt.compact,
      height: 180,
      endLabel: true,
    });
    $('#detailHistoryNote').textContent = `${vals.length} 条快照 · TVL 走势`;
  } else {
    chart.replaceChildren();
    $('#detailHistoryNote').textContent = hist.length
      ? `仅 ${hist.length} 条快照（${hist[0].captured_day}），画不出走势`
      : '库里没有这个对象的快照';
  }

  // Reports linked to this object.
  const reps = d.reports || [];
  $('#detailReports').innerHTML = reps.length
    ? reps.map((r) => `<li><button type="button" class="link-btn" data-report="${r.id}">${esc(r.report_date)} · ${esc(r.title || r.report_path)}</button></li>`).join('')
    : '<li class="dash">还没有登记报告</li>';

  panel.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

async function select(slug) {
  STATE.selected = slug;
  renderTable();
  const panel = $('#detail');
  panel.hidden = false;
  $('#detailMeta').textContent = '读取中…';
  try {
    const d = await window.API.object(slug, { history: 24, with_protocols: 1 });
    renderDetail(d);
  } catch (err) {
    if (err.status === 404) {
      $('#detailMeta').textContent = err.message;
      $('#detailLiner').textContent = '这个对象在库里还没有快照，先跑 CLI 的 snapshot 再来看。';
      ['#detailCapture', '#detailCaptureNote', '#detailProtocol', '#detailMissing'].forEach((s) => { $(s).textContent = ''; });
      $('#detailKv').innerHTML = '';
      $('#detailHolders').innerHTML = '';
      $('#detailHistory').replaceChildren();
      $('#detailHistoryNote').textContent = '';
      $('#detailReports').innerHTML = '';
      return;
    }
    showError($('#detailLiner'), '对象详情', err);
    $('#detailMeta').textContent = '读取失败';
  }
}

// ---------- Reports: list drives the reader ----------
function renderReportList(reports) {
  const el = $('#reportList');
  const badge = $('#reportCount');
  if (badge) badge.textContent = reports.length ? reports.length + ' 份' : '';
  if (!reports.length) {
    el.innerHTML = '<li class="dash">库里还没有登记报告</li>';
    return;
  }
  el.innerHTML = reports.map((r) => {
    const v = r.verdict === 'narrative' ? 'is-narrative' : r.verdict === 'mixed' ? 'is-mixed'
      : r.verdict === 'real' ? 'is-real' : 'is-none';
    const label = r.verdict === 'narrative' ? '叙事' : r.verdict === 'mixed' ? '部分真实'
      : r.verdict === 'real' ? '真实业务' : '未判定';
    return `<li>
      <button type="button" class="report-item${STATE.reportId === r.id ? ' is-selected' : ''}" data-report="${r.id}">
        <span class="report-item-top">
          <span class="report-item-name">${esc(r.object_name || r.object_slug)}</span>
          <span class="verdict ${v}">${label}</span>
        </span>
        <span class="report-item-date num">${esc(r.report_date)}</span>
        <span class="report-item-liner">${esc(r.one_liner || r.title || '')}</span>
      </button>
    </li>`;
  }).join('');
}

async function openReport(id) {
  STATE.reportId = id;
  $$('[data-report]').forEach((b) => b.classList.toggle('is-selected', Number(b.dataset.report) === id));
  const body = $('#reportBody');
  body.innerHTML = '<p class="empty">读取中…</p>';
  try {
    const d = await window.API.report(id);
    const r = d.report;
    const head = `<header class="md-head">
        <h3>${esc(r.title || r.object_name + ' 报告')}</h3>
        <p class="md-meta">${esc(r.object_name)} · ${esc(r.report_date)} · <code>${esc(r.report_path)}</code></p>
      </header>`;
    if (d.html) {
      body.innerHTML = head + `<div class="md">${d.html}</div>`;
    } else if (d.raw) {
      body.innerHTML = head + `<div class="md"><pre>${esc(d.raw)}</pre></div>`;
    } else {
      body.innerHTML = head + `<p class="empty">正文文件不在知识库根下：<code>${esc(r.report_path)}</code>。把 <code>TOKEN_RESEARCH_ROOT</code> 指到正确目录后重试。</p>`;
    }
    body.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } catch (err) {
    showError(body, '报告正文', err);
  }
}

document.addEventListener('click', (e) => {
  const b = e.target.closest('[data-report]');
  if (b) openReport(Number(b.dataset.report));
});

// ---------- Rail: the active item follows what is on screen ----------
function watchSections() {
  const map = { overview: 'overview', tokens: 'tokens', reports: 'reports', method: 'method' };
  const links = $$('.rail-item');
  const setActive = (id) => links.forEach((a) => {
    const target = (a.getAttribute('href') || '').replace('#', '');
    a.classList.toggle('is-active', target === id);
    if (target === id) a.setAttribute('aria-current', 'true');
    else a.removeAttribute('aria-current');
  });
  if (!('IntersectionObserver' in window)) return;
  const obs = new IntersectionObserver((entries) => {
    const visible = entries.filter((x) => x.isIntersecting)
      .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (visible && map[visible.target.id]) setActive(visible.target.id);
  }, { rootMargin: '-20% 0px -60% 0px', threshold: [0.05, 0.4, 0.8] });
  Object.keys(map).forEach((id) => { const el = document.getElementById(id); if (el) obs.observe(el); });
}

// ---------- boot ----------
async function boot() {
  try {
    const [stats, objs, reports] = await Promise.all([
      window.API.stats(), window.API.objects({ with_reports: 1 }), window.API.reports({ limit: 50 }),
    ]);
    STATE.stats = stats;
    STATE.objects = objs.objects || [];
    renderStats(stats);
    renderFinding(STATE.objects);
    drawTsr(rankedTsr(STATE.objects));
    renderTable();
    renderReportList(reports.reports || []);
    watchSections();
  } catch (err) {
    const msg = err && err.message ? err.message : String(err);
    $('#finding').innerHTML = `<strong>连不上接口。</strong> 这个页面要由 <code>server/app.py</code> 提供；` +
      `直接双击打开 index.html 读不到库。用 <code>启动.bat</code> 启动，或运行 ` +
      `<code>python server/app.py</code>。错误：${esc(msg)}`;
    ['#tsrChart', '#tbody'].forEach((s) => { const el = $(s); if (el) el.innerHTML = ''; });
    $('#emptyRows').hidden = false;
    $('#emptyRows').textContent = '接口未连接，无数据可显示。';
    $$('.strip-value').forEach((el) => { el.textContent = '—'; });
    $('#tvlTotal').textContent = '—';
    $('#subCount').textContent = '—';
    renderReportList([]);
  }
}

$('#q').addEventListener('input', renderTable);
$('#tbody').addEventListener('click', (e) => {
  const tr = e.target.closest('tr[data-slug]');
  if (tr) select(tr.dataset.slug);
});
$('#tbody').addEventListener('keydown', (e) => {
  const tr = e.target.closest('tr[data-slug]');
  if (tr && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); select(tr.dataset.slug); }
});

let rz;
addEventListener('resize', () => {
  clearTimeout(rz);
  rz = setTimeout(() => drawTsr(rankedTsr(STATE.objects)), 140);
});

// The chart is drawn from its measured width, so redraw when the panel actually
// takes its width (the API response and the first layout can race) and when the
// web font lands. Without this a bar list is drawn for a 320px box inside a
// 1400px panel, and the right half of the card stays empty.
(function watchTsrWidth() {
  const el = $('#tsrChart');
  if (!el || !('ResizeObserver' in window)) return;
  let last = 0;
  new ResizeObserver(() => {
    const w = el.clientWidth;
    if (w && Math.abs(w - last) > 8) {
      last = w;
      drawTsr(rankedTsr(STATE.objects));
    }
  }).observe(el);
  if (document.fonts && document.fonts.ready) {
    document.fonts.ready.then(() => drawTsr(rankedTsr(STATE.objects)));
  }
})();

boot();
