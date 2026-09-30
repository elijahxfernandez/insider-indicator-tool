/* Presentation only: all analytical values and classifications come from Python.
   Never turn null into zero, invent a chart, or put the Massive key in the browser. */
const $ = (id) => document.getElementById(id);
let loadedDefaults = false;
let report = null;
let timer = null;
let displayedVersion = null;
const numeric = (value) => typeof value === 'number' && Number.isFinite(value);
const money = (value, compact = false) => numeric(value) ? new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', notation: compact ? 'compact' : 'standard',
  maximumFractionDigits: 2,
}).format(value) : 'Unavailable';
const count = (value) => numeric(value) ? value.toLocaleString('en-US') : 'unknown';
const percent = (value) => numeric(value) ? `${value > 0 ? '+' : ''}${value.toFixed(2)}%` : 'Unavailable';
const text = (id, value) => { $(id).textContent = value; };
const el = (tag, content, className) => {
  const node = document.createElement(tag);
  if (content !== undefined) node.textContent = content;
  if (className) node.className = className;
  return node;
};
function notice(message, kind = '') {
  const node = $('notice');
  node.hidden = !message;
  node.className = `notice ${kind}`;
  node.textContent = message || '';
}
function status(layer = {}) {
  return {ok:'Available', incomplete:'Incomplete', unavailable:'Unavailable', not_configured:'Missing'}[layer.status] || 'Unavailable';
}
function signed(id, value, formatter) {
  const node = $(id);
  node.textContent = formatter(value);
  node.classList.toggle('down', numeric(value) && value < 0);
  node.classList.toggle('up', numeric(value) && value > 0);
  node.title = numeric(value) ? String(value) : 'No usable value';
}
function list(items) {
  const node = el('ul');
  items.forEach((item) => node.append(el('li', String(item))));
  return node;
}
function details(title, value) {
  const node = el('details', undefined, 'detail-card');
  node.append(el('summary', title), el('pre', JSON.stringify(value, null, 2)));
  return node;
}
function renderReport(r) {
  report = r;
  const c = r.classification || {}, f3 = r.form3 || {}, f4 = r.form4 || {};
  const inst = r.institutional || {}, price = r.price || {}, behavior = r.purchase_behavior || {};
  const partial = c.analysis_status === 'partial';
  const usable = c.signal && c.signal !== 'Insufficient data';
  const stale = c.uses_stale_data;
  text('coverage-badge', stale ? 'SAVED / STALE INPUTS' : partial ? 'PARTIAL ANALYSIS' : usable ? 'SELECTED SCOPE' : 'INSUFFICIENT DATA');
  $('coverage-badge').classList.toggle('partial', partial || stale || !usable);
  text('signal', usable ? c.signal : 'More evidence needed.');
  const observations = {
    'Confirmation': `Price and net insider activity both moved ${c.direction === 'upward' ? 'upward' : 'downward'}.`,
    'Positive divergence': 'Price declined while net reported insider buying was positive.',
    'Negative divergence': 'Price rose while net reported insider selling was negative.',
    'Quiet accumulation': 'Net reported insider buying was positive while price stayed within the flat band.',
    'Mixed/neutral': 'The measured directions do not meet an aligned-state rule.',
  };
  text('signal-description', usable
    ? `${observations[c.signal] || ''} ${partial ? 'Institutional evidence is missing, so this is a partial reading.' : 'The selected manager’s quarterly holdings are included.'}${stale ? ' Some inputs were not refreshed successfully or lack the requested coverage.' : ''}`
    : 'There is not enough usable price and insider evidence to classify this period. See the missing layers below.');
  text('price-direction', numeric(price.change_pct) ? Math.abs(price.change_pct) < 2 ? '→ Flat band' : price.change_pct > 0 ? '↗ Rising' : '↘ Falling' : 'Missing');
  text('insider-direction', numeric(f4.net_insider_value) ? f4.net_insider_value > 0 ? '↗ Net buying' : f4.net_insider_value < 0 ? '↘ Net selling' : '→ Net zero' : 'Missing');
  text('institution-direction', inst.status === 'ok' && numeric(inst.share_change) ? inst.share_change > 0 ? '↗ Increasing' : inst.share_change < 0 ? '↘ Decreasing' : '→ Unchanged' : 'Missing');
  const window = r.measurement_window || {};
  text('period-caption', `${window.after || '?'} → ${window.through || '?'} · Filings through ${r.as_of || '?'}`);
  signed('price-value', price.change_pct, percent);
  const savedInputs = (r.data_freshness || []).some((item) => ['cache','offline_cache','stale_cache'].includes(item.source));
  text('price-note', `${status(price)} · Quarter price movement`);
  signed('net-value', f4.net_insider_value, (value) => money(value, true));
  $('net-value').title = money(f4.net_insider_value);
  text('net-note', `${status(f4)} · P purchases minus S sales`);
  text('purchase-value', money(f4.purchases?.known_value_usd, true));
  $('purchase-value').title = money(f4.purchases?.known_value_usd);
  text('purchase-note', `${count(f4.purchases?.rows)} rows · ${count(f4.purchases?.unique_owner_ciks)} buyers · Known USD`);

  const coverage = $('coverage-list'); coverage.replaceChildren();
  [['Form 3','Ownership baseline',f3],['Form 4','Insider activity',f4],['13F','Institutional holdings',inst],['Price','Quarter context',price]].forEach(([name, caption, layer]) => {
    const row = el('div', undefined, 'coverage-row');
    const label = el('span', name); label.append(el('small', caption));
    row.append(label, el('span', status(layer), `status-pill ${layer.status === 'ok' ? 'good' : 'missing'}`));
    coverage.append(row);
  });
  text('coverage-note', partial ? 'Partial analysis. 13F remains missing; adding it can change the classification. Available does not mean every baseline was reconciled.' : c.scope || 'Coverage is limited to the returned records.');

  const feed = $('feed'); feed.replaceChildren();
  const items = [
    ['03', 'The starting position', 'Form 3', `${count(f3.owners)} initial reported owners. ${count(r.ownership_baseline?.reconciled_accounts)} direct accounts reconciled under the current rules. Unverified baseline changes remain unknown.`, status(f3)],
    ['04', 'What insiders reported', 'Form 4', `${count(f4.sales?.rows)} sale rows across ${count(f4.sales?.unique_owner_ciks)} identified sellers, totaling ${money(f4.sales?.known_value_usd)} in known value. Net reported activity: ${money(f4.net_insider_value)}.`, status(f4)],
    ['P', 'A closer look at purchases', 'Form 4 · Code P', f4.purchases?.rows === 0 && f4.status === 'ok' ? 'No P-purchase rows were found in the retrieved records for this quarter. That is a returned-record observation, not proof of insider intent.' : `${count(f4.purchases?.rows)} P-purchase rows, ${count(f4.purchases?.unique_owner_ciks)} identified buyers, and ${money(f4.purchases?.known_value_usd)} in known value. Explore purchase details for context.`, 'Direct & indirect ownership distinguished'],
    ['13F', 'The institutional piece', 'Quarterly holdings', inst.status === 'ok' ? `Selected manager ${inst.filer_cik}: ${count(inst.share_change)} reported share change. This is one manager’s quarterly position, not market-wide ownership or confirmed trades.` : `Institutional comparison is missing. ${inst.status === 'not_configured' ? 'An investing firm and verified security identifier have not been configured.' : inst.reason || 'The quarterly comparison is unavailable.'} This layer does not contribute to the current classification.`, status(inst)],
  ];
  items.forEach(([icon,title,source,body,tag]) => {
    const item = el('article', undefined, 'feed-item');
    const content = el('div', undefined, 'feed-body');
    const heading = el('div', undefined, 'feed-title');
    heading.append(el('h3',title),el('span',source));
    content.append(heading,el('p',body),el('span',tag,'feed-tag'));
    item.append(el('span',icon,'feed-avatar'),content);feed.append(item);
  });
  if (c.counterevidence?.length) {
    const card = el('div', undefined, 'panel-content');
    card.append(el('h3','Keep in view'),list(c.counterevidence));feed.append(card);
  }
  renderPurchases(behavior, f4, r.ownership_baseline);
  renderCoverage(r);
  text('feed-count', savedInputs ? 'Includes saved observations' : '4 evidence layers');
  $('download').disabled = false;
}
function renderPurchases(behavior, f4, baseline) {
  const target = $('purchases-content'); target.replaceChildren();
  target.append(el('h3','Purchase behavior, in context'));
  target.append(el('p','Code P includes open-market and private purchases. A purchase is not an inferred motive or a forecast.','fine-print'));
  if (behavior.observations?.length) target.append(list(behavior.observations));
  else target.append(el('p',behavior.reason || 'Purchase analysis is unavailable for this report.'));
  // Seven original evidence angles stay inspectable without re-scoring them.
  const angles = [
    ['1 · Who bought', behavior.who_bought],['2 · Purchase size and existing positions',behavior.relative_size],
    ['3 · Cluster buying',behavior.cluster_buying],['4 · Historical behavior',{history:behavior.historical_comparison,recurrence:behavior.routine_context}],
    ['5 · Price and time',behavior.price_and_time],['6 · Firm and liquidity context',behavior.firm_characteristics],
    ['7 · Direct and indirect ownership',behavior.ownership_type],
    ['Private-purchase review flags',behavior.private_purchase_review],
    ['Individual P records',f4.p_purchase_tracker || {status:'unavailable'}],
    ['Form 3 baseline reconciliation',baseline],
  ];
  angles.forEach(([title,value]) => target.append(details(title,value ?? {status:'unavailable'})));
}
function renderCoverage(r) {
  const target = $('coverage-content'); target.replaceChildren();
  target.append(el('h3','Source freshness'));
  if (!r.data_freshness?.length) target.append(el('p','No source freshness metadata is available.'));
  (r.data_freshness || []).forEach((source) => {
    const card = el('article',undefined,'detail-card');
    const name = source.endpoint?.includes('form-3') ? 'Form 3' : source.endpoint?.includes('form-4') ? 'Form 4' : source.endpoint?.includes('13-F') ? '13F' : 'Price';
    card.append(el('h3',`${name} · ${source.source || 'unknown source'}`));
    card.append(el('p',`Last successful refresh: ${source.refreshed_at || 'unknown'}`));
    card.append(el('p',`Filing coverage: ${source.data_through || 'See price observation dates'}`));
    if (source.stale || source.coverage_complete === false) card.append(el('p','Warning: stale inputs or incomplete cutoff coverage.'));
    if (source.refresh_error) card.append(el('p',`Refresh issue: ${source.refresh_error}`));
    target.append(card);
  });
  target.append(details('Classification inputs and rules',r.classification),details('Issuer matching',r.issuer_scope),details('Remaining limitations',r.not_implemented));
}
async function readResponse(response) {
  if (response.status === 401) throw new Error('Reviewer sign-in is required. Reload the page to sign in.');
  let payload;
  try { payload = await response.json(); }
  catch { throw new Error('The server could not return a report. Please try again.'); }
  if (!response.ok) throw new Error(payload.error || 'The request could not finish.');
  return payload;
}
async function syncState() {
  clearTimeout(timer);
  try {
    const state = await readResponse(await fetch('/api/state'));
    if (!loadedDefaults) {
      Object.entries(state.defaults).forEach(([name,value]) => { $(name).value = value; });
      loadedDefaults = true;
    }
    if (state.report && state.generated_at !== displayedVersion) {
      renderReport(state.report); displayedVersion = state.generated_at;
    }
    const running = state.job.status === 'running';
    $('refresh').disabled = running;
    $('refresh').textContent = running ? 'Updating analysis…' : '↻ Refresh analysis';
    if (running) {
      notice(`${state.job.message}. API pacing can take a few minutes.${state.report ? ' The previous report remains visible until this run finishes.' : ''}`, 'busy');
      timer = setTimeout(syncState, 2500);
    } else if (state.job.status === 'error') notice(state.job.message,'error');
    else if (!state.api_configured) notice(`${state.report ? 'Showing an existing report, not a new live retrieval. ' : 'The dashboard is ready. '}Add MASSIVE_API_KEY to the server environment to retrieve live data. Your key is never entered on this page.`);
    else if (state.report?.classification?.uses_stale_data) notice('This report includes stale or incomplete source coverage. Inspect Data coverage before interpreting the conclusion.','error');
    else if (state.report) notice(`Report generated ${new Date(state.generated_at).toLocaleString()}. Filing and price observations reflect the period shown below.`);
    else notice('Ready to analyze COIN. Choose your quarter and refresh to retrieve the evidence.');
  } catch (error) {
    notice(error.message || 'Connection lost. Retry the refresh when the server is available.','error');
    $('refresh').disabled = false;
    $('refresh').textContent = '↻ Refresh analysis';
  }
}
$('analysis-form').addEventListener('submit',async (event) => {
  event.preventDefault();
  $('refresh').disabled = true;
  notice('Starting the analysis…','busy');
  try {
    const payload = Object.fromEntries(new FormData(event.currentTarget));
    await readResponse(await fetch('/api/refresh', {method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'ownership-dashboard'},body:JSON.stringify(payload)}));
    await syncState();
  } catch (error) {
    $('refresh').disabled = false;
    notice(error.message,'error');
  }
});
$('download').addEventListener('click',async () => {
  try {
    const response = await fetch('/api/report');
    if (!response.ok) { await readResponse(response); return; }
    const url = URL.createObjectURL(await response.blob());
    const link = el('a');link.href=url;link.download='COIN-ownership-report.json';link.click();
    setTimeout(() => URL.revokeObjectURL(url),1000);
  } catch (error) { notice(error.message,'error'); }
});
function activateTab(tab) {
  document.querySelectorAll('[data-tab]').forEach((button) => {
    const selected = button === tab;
    button.classList.toggle('active',selected);button.setAttribute('aria-selected',String(selected));
    button.tabIndex=selected?0:-1;$(`panel-${button.dataset.tab}`).hidden=!selected;
  });
}
const tabs = [...document.querySelectorAll('[data-tab]')];
tabs.forEach((tab,index) => {
  tab.addEventListener('click',() => activateTab(tab));
  tab.addEventListener('keydown',(event) => {
    let next;
    if (event.key==='ArrowRight') next=tabs[(index+1)%tabs.length];
    if (event.key==='ArrowLeft') next=tabs[(index+tabs.length-1)%tabs.length];
    if (event.key==='Home') next=tabs[0];if (event.key==='End') next=tabs.at(-1);
    if(next){event.preventDefault();activateTab(next);next.focus();}
  });
});
syncState();
