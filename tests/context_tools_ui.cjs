// Synthetic host-bridge regression, not evidence of a Desktop owner connection.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const template = fs.readFileSync(path.join(__dirname, '../src/token_weather/assets/context.html'), 'utf8');
const script = template.split('<script>')[1].split('</script>')[0]
  .replace('__WEATHER_LIVE__', 'false').replace('__CONTEXT_MCP__', 'true').replace('__WEATHER_DATA__', '{}');
const stamp = Date.now() / 1000;
const reading = (thread = 'fixture-A') => ({schema_version: 1, thread_id: thread,
  available: true, tokens: 28286, observed_window: 475000, max_age_seconds: 30,
  last_request: {at: stamp, input: 28281, output: 5}});
const settle = () => new Promise(resolve => setImmediate(resolve));

function panel(initial, {firstPanelView, storage = new Map()} = {}) {
  class Element {
    constructor(tag) { this.tag = tag; this.attributes = {}; this.children = []; this.style = {setProperty(name,value) {this[name]=value;}}; this.dataset = {}; this.handlers = {}; this.value = ''; this.open = false; }
    setAttribute(name, value) { this.attributes[name] = value; }
    getAttribute(name) { return this.attributes[name]; }
    replaceChildren(...children) { this.children = children; }
    append(...children) { this.children.push(...children); }
    getAnimations() { entrances++; return []; }
    addEventListener(name, callback) { this.handlers[name] = callback; }
    click() { return this.handlers.click?.({preventDefault() {}}); }
  }
  const elements = new Map(), timers = [], intervals = [], calls = [], documentEvents = {};
  const byId = id => { if (!elements.has(id)) elements.set(id, new Element('div')); return elements.get(id); };
  let next = initial, onMessage, seconds = stamp, compactReply = null, entrances = 0;
  class Clock extends Date { static now() { return seconds * 1000; } }
  const parent = {postMessage(message) {
    calls.push(message);
    if (!message.id) return;
    const body = message.method === 'ui/initialize' ? {result: {}} : message.params?.name === 'token_weather_compact_session' ? {result: compactReply} :
      next === 'error' ? {error: {code: -1}} : {result: {_meta: {reading: structuredClone(next), first_panel_view:firstPanelView}}};
    queueMicrotask(() => onMessage({source: parent, data: {jsonrpc: '2.0', id: message.id, ...body}}));
  }};
  vm.runInNewContext(script, {Date: Clock, crypto: {randomUUID: require('node:crypto').randomUUID},
    document: {getElementById: byId, createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag),
      addEventListener(name, callback) { documentEvents[name] = callback; }},
    window: {parent, sessionStorage:{getItem:key => storage.get(key), setItem:(key,value) => storage.set(key,value)},
      addEventListener(name, callback) { onMessage = callback; }},
    setInterval(callback) { intervals.push(callback); },
    clearTimeout(callback) {
      const index = timers.indexOf(callback);
      if (index !== -1) timers.splice(index, 1);
    },
    setTimeout(callback, delay) {
      if (delay === 5000 || delay === 1000 || delay === 150) timers.push(callback);
      return callback;
    },
  });
  return {byId, calls, async refresh(value) { next = value; await timers.shift()(); },
    get entrances() { return entrances; }, visible() { documentEvents.visibilitychange(); },
    notify(meta) { onMessage({source:parent,data:{jsonrpc:'2.0',method:'ui/notifications/tool-result',params:{_meta:meta}}}); },
    setCompact(value) { compactReply = value; },
    advance(delta) { seconds += delta; intervals.forEach(callback => callback()); }};
}

(async () => {
  assert.match(template, /id="token-details" class="subpanel" open hidden>/);
  assert.match(template, /id="weather-label"[^>]*aria-expanded="true"/);
  assert.match(template, /id="weather-scale"[^>]*aria-label="Weather scale">/);
  assert.ok(!template.includes('id="forecast-chart"'));
  assert.doesNotMatch(template, /name="usage-scope"|id="live-indicator"|id="indicator"/);
  for (const [label, icon] of Object.entries({Clear:'sun', Fair:'cloud-sun', Cloudy:'cloud',
    Showers:'cloud-rain', Storm:'cloud-lightning'})) {
    assert.match(template, new RegExp('id="scale-' + label + '"[^>]*><i class="icon icon-' + icon + '"'));
  }
  assert.ok(template.indexOf('id="growth-bars"') < template.indexOf('id="request-details"'));
  assert.match(template, /id="compact-session"[^>]* disabled>Compact<\/button>/);
  assert.ok(!template.includes('id="catalog-dialog"'));
  assert.ok(!template.includes('Read Codex usage'));
  assert.doesNotMatch(template, /id="(?:native-links|plugins-link|skills-link)"/);

  const catalogs = reading();
  const storage = new Map();
  const first = panel(catalogs, {storage});
  await settle();
  assert.equal(first.entrances, 1);
  first.visible();
  await first.refresh(catalogs);
  first.notify({reading:catalogs});
  first.notify({entrance:'invocation',reading:reading('fixture-B')});
  assert.equal(first.entrances, 1);
  first.notify({entrance:'invocation',reading:catalogs});
  assert.equal(first.entrances, 2);
  const reopened = panel(catalogs, {storage});
  const nativeReopened = panel(catalogs, {firstPanelView:false});
  await settle();
  assert.equal(reopened.entrances, 0);
  assert.equal(nativeReopened.entrances, 0);
  const offline = panel('error');
  await settle();
  assert.equal(offline.entrances, 0);
  await offline.refresh(catalogs);
  assert.equal(offline.entrances, 1);
  const a = panel(catalogs), b = panel(reading('fixture-B'));
  await settle();
  assert.equal(a.byId('usage').textContent, '28.3k tokens');
  assert.doesNotMatch(template, /id="panel-refresh"/);
  await a.refresh({...catalogs, tokens:30000, last_request:{at:stamp,input:29995,output:5}});
  assert.equal(a.byId('usage').textContent, '30.0k tokens');
  const resetAt = Math.floor(stamp) + 3600;
  const expiryAt = resetAt + 3600;
  const formatLocal = seconds => new Intl.DateTimeFormat(undefined, {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  }).format(seconds * 1000);
  const limits = {kind:'codex_account_limits', thread_id:'fixture-A', observed_at:stamp,
    weekly_used_percent:28, weekly_resets_at:resetAt, banked_reset_available:2, banked_reset_expires_at:expiryAt};
  const quota = panel({...catalogs, account_limits: limits});
  await settle();
  assert.equal(quota.byId('weekly-usage').textContent, '28% used');
  assert.equal(quota.byId('weekly-reset').textContent, formatLocal(resetAt));
  assert.equal(quota.byId('banked-reset').textContent, '2 available · Expires ' + formatLocal(expiryAt));
  await quota.refresh({...catalogs, account_limits:{...limits, banked_reset_available:0, banked_reset_expires_at:null}});
  assert.equal(quota.byId('banked-reset').textContent, '0 available');
  quota.advance(76);
  assert.equal(quota.byId('account-limits').hidden, true);
  await quota.refresh({...catalogs, weekly_quota: {observed_at:stamp, used_percent:32.5, resets_at:resetAt}});
  assert.equal(quota.byId('weekly-usage').textContent, '32.5% used');
  assert.equal(quota.byId('weekly-label').textContent, 'Weekly usage · last reported');
  assert.equal(quota.byId('banked-row').hidden, true);
  quota.advance(3600);
  assert.equal(quota.byId('account-limits').hidden, true);
  await a.refresh(reading('fixture-B'));
  assert.equal(a.byId('request-details').hidden, true);
  assert.equal(a.byId('status').textContent, 'Chat binding changed');
  assert.equal(b.byId('usage').textContent, '28.3k tokens');
  await a.refresh(catalogs);
  await a.refresh('error');
  assert.equal(a.byId('usage').textContent, '28.3k tokens');
  await a.refresh(catalogs);
  assert.ok(a.calls.filter(call => call.params?.name === 'token_weather_context')
    .every(call => call.params.arguments.section === 'runtime'));

  const flowReading = {...reading(), tokens:100, last_request:{at:stamp, input:90, output:10,
    cached_input:60, cache_write_input:20, reasoning_output:8}, turn_reasoning:11,
    session_usage:{at:stamp,total:1000,input:960,output:40,cached_input:700,cache_write_input:10,reasoning_output:5}};
  const flows = panel(flowReading);
  await settle();
  assert.equal(flows.byId('cache-share').textContent, '66.7%');
  assert.equal(flows.byId('turn-reasoning').textContent, '11 tokens');
  await flows.refresh({...flowReading, turn_reasoning:0});
  assert.equal(flows.byId('quick-usage').hidden, true);
  const labels = id => flows.byId(id).children.map(row => row.children.map(child => child.textContent));
  assert.deepEqual(labels('counts'), [
    ['Fresh input','10','10.0%'], ['Cache read','60','60.0%'], ['Cache write','20','20.0%'],
    ['Other output','2','2.0%'], ['Reasoning output','8','8.0%'],
  ]);
  assert.equal(flows.byId('session-total').textContent, '1.0k tokens');
  assert.deepEqual(labels('session-counts')[1], ['Cache read','700','70.0%']);
  await flows.refresh({...flowReading, last_request:{...flowReading.last_request, cache_write_input:null}});
  assert.deepEqual(labels('counts')[0], ['Other input','30','30.0%']);
  assert.equal(labels('counts').some(row => row[0] === 'Cache write'), false);
  await flows.refresh({...flowReading,tokens:99});
  assert.equal(flows.byId('meter').hidden, true);
  assert.equal(flows.byId('session-meter').hidden, false);
  await flows.refresh({...flowReading, api_cost_estimate:{usd:0.0045, priced_tokens:700, observed_tokens:1000,
    partial:true, prices_as_of:'2026-11-07'}});
  assert.equal(flows.byId('local-cost').hidden, false);
  assert.equal(flows.byId('local-cost-usd').textContent, 'Unavailable');
  assert.ok(!template.includes('Estimated API cost (partial)'));
  assert.match(flows.byId('local-cost').title, /Estimate only, not actual charges/);
  assert.equal(flows.byId('local-cost-coverage').textContent, 'Incomplete usage or model records');
  await flows.refresh({...flowReading,
    session_usage:{...flowReading.session_usage, at:stamp+1, input:1160, total:1200},
    api_cost_estimate:{usd:0.0649662, priced_tokens:1200, observed_tokens:1200, partial:false, prices_as_of:'2026-11-07'},
    native_usage_supported:true});
  assert.equal(flows.byId('session-total').textContent, '1.2k tokens');
  assert.equal(flows.byId('local-cost-usd').textContent, '≈$0.0650 USD');
  assert.equal(flows.byId('local-cost-coverage').hidden, true);
  assert.ok(flows.calls.filter(call => call.params?.name === 'token_weather_context')
    .every(call => call.params.arguments.section === 'runtime'));

  const weather = {thread_id:'fixture-A', observed_at:stamp, kind:'turn_forecast', reference_limit:100000,
    reference_kind:'observed_window', growth_per_turn:2000, turns_left:null, paused:false,
    points:[{tokens:30000, epoch:0, completed:true}, {tokens:20000, epoch:1, completed:true, compaction:true},
      {tokens:22000, epoch:1, completed:true}, {tokens:24500, epoch:1, completed:false}], outlook:[]};
  const branded = panel({...catalogs, tokens:20000, observed_window:100000, forecast:weather});
  await settle();
  const scale = branded.byId('weather-scale');
  const weatherButton = branded.byId('weather-label');
  const capacity = branded.byId('forecast-meter');
  scale.hidden = true;
  weatherButton.click();
  assert.equal(scale.hidden, false);
  assert.equal(weatherButton.getAttribute('aria-expanded'), 'true');
  for (const [tokens, label] of [[34000,'Clear'], [35000,'Fair'], [55000,'Cloudy'], [70000,'Showers'], [85000,'Storm']]) {
    await branded.refresh({...catalogs, tokens, observed_window:100000, forecast:weather});
    assert.equal(branded.byId('weather-condition').textContent, label);
    assert.ok(Math.abs(parseFloat(capacity.style['--meter-position']) - tokens / 1000) < 1e-9);
  }
  const threshold = {...weather, reference_kind:'sampling_auto_compact', turns_left:120,
    outlook:[{step:1,tokens:42000},{step:2,tokens:44000}]};
  await branded.refresh({...catalogs, tokens:40000, observed_window:200000, forecast:threshold});
  assert.equal(branded.byId('forecast-context').textContent, '20.0% used');
  assert.equal(branded.byId('forecast-meter-reserve').style.flexGrow, 50);
  assert.equal(branded.byId('capacity-reserve-label').textContent, 'compact 50.0%');
  assert.equal(branded.byId('forecast-title').textContent, 'Plenty of room');
  assert.equal(branded.byId('forecast-caption').textContent, '99+ turns before auto-compact at 100.0k');
  assert.equal(branded.byId('growth-typical').textContent, '+2.0k');
  assert.equal(branded.byId('growth-peak').textContent, '+2.0k');
  const chart = branded.byId('growth-bars');
  assert.equal(chart.children.length, 3);
  assert.match(chart.getAttribute('aria-label'), /Compacted/);
  assert.equal(chart.children.filter(child => child.children[0]?.dataset.projected === 'true').length, 2);
  assert.equal(chart.children.filter(child => child.children[0]?.dataset.peak === 'true').length, 1);
  assert.equal(scale.hidden, false);
  weatherButton.click();
  assert.equal(scale.hidden, true);
  await branded.refresh({...catalogs, tokens:40000, observed_window:200000,
    forecast:{...threshold, turns_left:99}});
  assert.equal(branded.byId('forecast-title').textContent, '~99 turns left');
  await branded.refresh({...catalogs, tokens:40000, observed_window:200000,
    forecast:{...threshold, paused:true, outlook:[]}});
  assert.equal(branded.byId('forecast-title').hidden, true);
  assert.equal(branded.byId('growth-projected').hidden, true);
  await branded.refresh({...catalogs, forecast:{...threshold, thread_id:'fixture-B'}});
  assert.equal(branded.byId('forecast-hero').hidden, true);
  assert.equal(branded.byId('growth-bars').hidden, true);
  const withMemory = {...catalogs, memory_files: {thread_id: 'fixture-A', observed_at: stamp,
    source: 'thread_memory_sources', count: 2, files: [{path: '/synthetic/one.md'}, {path: '/synthetic/two.md'}]}};
  await a.refresh(withMemory);
  assert.equal(a.byId('memory-link-row').hidden, false);
  a.byId('memory-link').click(); assert.equal(a.byId('memory').open, true);
  assert.equal(a.byId('memory-link').getAttribute('aria-expanded'), 'true');
  a.byId('memory-link').click(); assert.equal(a.byId('memory').open, false);
  await a.refresh({...withMemory, memory_files: {...withMemory.memory_files, source: 'disk_scan'}});
  assert.equal(a.byId('memory').hidden, true);
  await a.refresh(catalogs);
  assert.equal(a.byId('memory-link-row').hidden, true);
  const runtime = {thread_id: 'fixture-A', observed_at: stamp, active: false, auto_compact: null,
    compaction: {supported: true, can_compact: true, phase: 'ready'}};
  const compactButton = a.byId('compact-session');
  const primary = a.byId('compact-primary');
  await a.refresh({...catalogs, runtime, runtime_supported:true});
  assert.equal(compactButton.disabled, false);
  for (const [tokens, urgent] of [[59999,false],[60000,true],[84999,true],[85000,true]]) {
    await a.refresh({...catalogs, runtime, tokens, observed_window:100000});
    assert.equal(a.byId('compact-advisory').hidden, !urgent);
    assert.equal(primary.hidden, !urgent);
    assert.equal(compactButton.hidden, urgent);
    assert.equal(a.byId('compact-hint').hidden, urgent);
  }
  const sweetSpot = {...catalogs, runtime, tokens:65000, observed_window:100000};
  await a.refresh({...sweetSpot, observed_window:200000, forecast:threshold});
  assert.equal(primary.hidden, false);
  await a.refresh({...sweetSpot, observed_window:200000, forecast:{...threshold, observed_at:stamp-30}});
  assert.equal(primary.hidden, true);
  await a.refresh({...sweetSpot, collection:{catching_up:true}});
  assert.equal(primary.disabled, true);
  await a.refresh({...sweetSpot, preview_mode:'read_only'});
  assert.equal(primary.hidden, true);
  assert.equal(a.byId('compact-urgent-fallback').hidden, false);
  await a.refresh({...sweetSpot, preview_mode:'demo'});
  await primary.click();
  assert.equal(a.byId('compact-urgent-status').textContent, 'Preview only');
  assert.equal(a.calls.filter(call => call.params?.name === 'token_weather_compact_session').length, 0);
  await a.refresh({...sweetSpot, tokens:28000});
  a.setCompact({_meta: {thread_id: 'fixture-A', compaction: {supported: true, can_compact: false, phase: 'accepted'}}});
  await Promise.all([a.byId('compact-session').click(), a.byId('compact-session').click()]);
  assert.equal(a.calls.filter(call => call.params?.name === 'token_weather_compact_session').length, 1);
  assert.equal(a.byId('compact-status').textContent, 'Compaction accepted');
  await a.refresh({...catalogs, tokens: null, last_request: null, runtime_supported: true,
    runtime: {...runtime, awaiting_usage: true, compaction: {supported: true, can_compact: false, phase: 'waiting_usage'}}});
  assert.equal(a.byId('status').textContent, 'Waiting for updated usage');
  assert.equal(a.byId('compact-session').disabled, true);
  assert.equal(a.byId('compact-status').hidden, false);
  await a.refresh({...sweetSpot, runtime: {...runtime, active: true}, runtime_supported: true});
  assert.equal(compactButton.disabled, true);
  assert.equal(primary.disabled, true);
  await a.byId('compact-session').click();
  assert.equal(a.calls.filter(call => call.params?.name === 'token_weather_compact_session').length, 1);
  await a.refresh(reading());
  assert.equal(a.byId('compact-session').disabled, true);
  for (const call of [...a.calls, ...b.calls]) {
    assert.ok(['ui/initialize', 'ui/notifications/initialized', 'ui/open-link', 'tools/call'].includes(call.method));
    if (call.method === 'tools/call') assert.ok(['token_weather_context', 'token_weather_compact_session'].includes(call.params.name));
  }
  console.log('PASS: compact layout, forecast, memory scope, compaction and thread isolation.');
})().catch(error => { console.error(error); process.exitCode = 1; });
