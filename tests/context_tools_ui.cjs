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

function panel(initial) {
  class Element {
    constructor(tag) { this.tag = tag; this.attributes = {}; this.children = []; this.style = {setProperty(name,value) {this[name]=value;}}; this.dataset = {}; this.handlers = {}; this.value = ''; this.open = false; }
    setAttribute(name, value) { this.attributes[name] = value; }
    getAttribute(name) { return this.attributes[name]; }
    replaceChildren(...children) { this.children = children; }
    append(...children) { this.children.push(...children); }
    getAnimations() { return []; }
    addEventListener(name, callback) { this.handlers[name] = callback; }
    click() { return this.handlers.click?.({preventDefault() {}}); }
  }
  const elements = new Map(), timers = [], intervals = [], calls = [];
  const byId = id => { if (!elements.has(id)) elements.set(id, new Element('div')); return elements.get(id); };
  let next = initial, onMessage, seconds = stamp, compactReply = null;
  class Clock extends Date { static now() { return seconds * 1000; } }
  const parent = {postMessage(message) {
    calls.push(message);
    if (!message.id) return;
    const body = message.method === 'ui/initialize' ? {result: {}} : message.params?.name === 'token_weather_compact_session' ? {result: compactReply} :
      next === 'error' ? {error: {code: -1}} : {result: {_meta: {reading: structuredClone(next)}}};
    queueMicrotask(() => onMessage({source: parent, data: {jsonrpc: '2.0', id: message.id, ...body}}));
  }};
  vm.runInNewContext(script, {Date: Clock, crypto: {randomUUID: require('node:crypto').randomUUID},
    document: {getElementById: byId, createElement: tag => new Element(tag), createElementNS: (_, tag) => new Element(tag),
      addEventListener() {}},
    window: {parent, addEventListener(name, callback) { onMessage = callback; }},
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
    setCompact(value) { compactReply = value; },
    advance(delta) { seconds += delta; intervals.forEach(callback => callback()); }};
}

(async () => {
  assert.match(template, /id="request-details" class="subpanel" open>/);
  assert.match(template, /id="session-details" class="subpanel" open hidden>/);
  assert.doesNotMatch(template, /name="usage-scope"|id="live-indicator"|id="indicator"/);
  for (const [label, icon] of Object.entries({Clear:'sun', Fair:'cloud-sun', Cloudy:'cloud',
    Showers:'cloud-rain', Storm:'cloud-lightning'})) {
    assert.match(template, new RegExp('id="scale-' + label + '"[^>]*><i class="icon icon-' + icon + '"'));
  }
  assert.ok(template.indexOf('id="forecast-band"') < template.indexOf('id="request-details"'));
  assert.match(template, /class="session-action-row">\s*<button id="compact-session"[^>]* disabled>Compact session<\/button>\s*<\/div>/);
  assert.ok(!template.includes('id="catalog-dialog"'));
  assert.ok(!template.includes('Read Codex usage'));
  assert.doesNotMatch(template, /id="(?:native-links|plugins-link|skills-link)"/);

  const catalogs = reading();
  const a = panel(catalogs), b = panel(reading('fixture-B'));
  await settle();
  assert.equal(a.byId('usage').textContent, '28.29k tokens');
  assert.doesNotMatch(template, /id="panel-refresh"/);
  await a.refresh({...catalogs, tokens:30000, last_request:{at:stamp,input:29995,output:5}});
  assert.equal(a.byId('usage').textContent, '30k tokens');
  const resetAt = Math.floor(stamp) + 3600;
  const expiryAt = resetAt + 3600;
  const formatLocal = seconds => new Intl.DateTimeFormat(undefined, {
    month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit',
  }).format(seconds * 1000);
  const limits = {kind:'codex_account_limits', thread_id:'fixture-A', observed_at:stamp,
    weekly_used_percent:28, weekly_resets_at:resetAt, banked_reset_available:2, banked_reset_expires_at:expiryAt};
  const quota = panel({...catalogs, account_limits: limits});
  await settle();
  assert.equal(quota.byId('weekly-usage').textContent, '28% used · Resets ' + formatLocal(resetAt));
  assert.equal(quota.byId('banked-reset').textContent, '2 available · Expires ' + formatLocal(expiryAt));
  await quota.refresh({...catalogs, account_limits:{...limits, banked_reset_available:0, banked_reset_expires_at:null}});
  assert.equal(quota.byId('banked-reset').textContent, '0 available');
  quota.advance(76);
  assert.equal(quota.byId('weekly-usage').textContent, 'Unavailable');
  assert.equal(quota.byId('banked-reset').textContent, 'Unavailable');
  await a.refresh(reading('fixture-B'));
  assert.equal(a.byId('request-details').hidden, true);
  assert.equal(a.byId('status').textContent, 'Chat binding changed');
  assert.equal(b.byId('usage').textContent, '28.29k tokens');
  await a.refresh(catalogs);
  await a.refresh('error');
  assert.equal(a.byId('usage').textContent, '28.29k tokens');
  await a.refresh(catalogs);
  assert.ok(a.calls.filter(call => call.params?.name === 'token_weather_context')
    .every(call => call.params.arguments.section === 'runtime'));

  const flowReading = {...reading(), tokens:100, last_request:{at:stamp, input:90, output:10,
    cached_input:60, cache_write_input:20, reasoning_output:8},
    session_usage:{at:stamp,total:1000,input:960,output:40,cached_input:700,cache_write_input:10,reasoning_output:5}};
  const flows = panel(flowReading);
  await settle();
  const labels = id => flows.byId(id).children.map(row => row.children.map(child => child.textContent));
  assert.deepEqual(labels('counts'), [
    ['Fresh input','10','10.0%'], ['Cache read','60','60.0%'], ['Cache write','20','20.0%'],
    ['Other output','2','2.0%'], ['Reasoning output','8','8.0%'],
  ]);
  assert.equal(flows.byId('session-total').textContent, '1k tokens');
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

  const weather = {thread_id: 'fixture-A', observed_at: stamp, kind: 'turn_forecast', reference_limit: 100000,
    reference_kind: 'observed_window', growth_per_turn: 2000, growth_per_request: 1000, rate_basis: 2, last_turn_delta: 2000,
    last_turn_requests: 3, last_turn_output: 10, turns_left: null, paused: false,
    points: [{tokens: 20000, epoch: 0}, {tokens: 22000, epoch: 0}],
    outlook: [1,2,3,4,5].map(step => ({step, tokens: 22000 + step * 2000}))};
  const branded = panel({...catalogs, tokens: 20000, observed_window: 100000, forecast: weather});
  await settle();
  assert.match(template, /id="weather-scale"[^>]* hidden>/);
  const scale = branded.byId('weather-scale');
  const weatherButton = branded.byId('weather-label');
  const capacity = branded.byId('forecast-meter');
  scale.hidden = true;
  weatherButton.click();
  assert.equal(scale.hidden, false);
  assert.equal(weatherButton.getAttribute('aria-expanded'), 'true');
  for (const [tokens, label, colour] of [[34000, 'Clear', 'clear'], [35000, 'Fair', 'fair'], [55000, 'Cloudy', 'cloudy'], [70000, 'Showers', 'showers'], [85000, 'Storm', 'storm']]) {
    await branded.refresh({...catalogs, tokens, observed_window: 100000, forecast: weather});
    assert.equal(branded.byId('weather-condition').textContent, label);
    assert.equal(branded.byId('panel').style['--weather'], 'var(--weather-' + colour + ')');
    assert.ok(Math.abs(parseFloat(capacity.style['--meter-position']) - tokens / 1000) < 1e-9);
    assert.equal(branded.byId('forecast-details').hidden, false);
    assert.equal(branded.byId('compact-session').disabled, true);
  }
  await branded.refresh({...catalogs, tokens:40000, observed_window:200000,
    forecast:{...weather, reference_kind:'auto_compact'}});
  assert.equal(branded.byId('forecast-context').textContent, '20.0%');
  assert.equal(branded.byId('scale-Fair').getAttribute('aria-current'), 'true');
  assert.equal(capacity.style['--meter-position'], '20%');
  assert.equal(capacity.style['--clear-end'], '17.5%');
  assert.equal(capacity.style['--showers-end'], '42.5%');
  assert.equal(branded.byId('forecast-meter-reserve').style.width, '50%');
  assert.match(branded.byId('weather-basis').textContent, /40\.0% of the auto-compact threshold/);
  assert.equal(scale.hidden, false);
  weatherButton.click();
  assert.equal(scale.hidden, true);
  assert.equal(weatherButton.getAttribute('aria-expanded'), 'false');
  await branded.refresh({...catalogs, forecast: {...weather, thread_id: 'fixture-B'}});
  assert.equal(branded.byId('weather-condition').textContent, 'Waiting');
  assert.equal(branded.byId('panel').style['--weather'], 'var(--muted)');
  assert.equal(capacity.style['--clear-end'], '35%');
  await a.refresh({...catalogs, tokens: 22000, observed_window: 100000, forecast: weather});
  assert.equal(a.byId('weather-forecast').hidden, false);
  assert.equal(a.byId('forecast-growth').textContent, '+2k/turn');
  assert.equal(a.byId('forecast-sample').textContent, '+1k per request');
  assert.equal(a.byId('forecast-context').textContent, '22.0%');
  assert.equal(a.byId('forecast-details').hidden, false);
  assert.equal(a.byId('forecast-projection').textContent, '~32% in 5 turns');
  assert.equal(a.byId('forecast-band').getAttribute('aria-expanded'), 'true');
  assert.equal(a.byId('forecast-band').getAttribute('aria-label'), 'Hide forecast');
  assert.equal(a.byId('compact-session').disabled, true);
  a.byId('forecast-band').click();
  assert.equal(a.byId('forecast-details').hidden, true);
  assert.equal(a.byId('forecast-context').textContent, '22.0%');
  assert.equal(a.byId('compact-session').disabled, true);
  await a.refresh({...catalogs, tokens: 24000, observed_window: 100000, forecast: weather});
  assert.equal(a.byId('forecast-context').textContent, '24.0%');
  assert.equal(a.byId('forecast-details').hidden, true); // Polling preserves the user's view.
  a.byId('forecast-band').click();
  assert.equal(a.byId('forecast-remaining').textContent, '—');
  assert.match(a.byId('forecast-estimate').textContent, /In 5 turns: ~32% of the observed context window/);
  assert.equal(a.byId('forecast-note').hidden, true);
  const chart = a.byId('forecast-chart');
  assert.equal(chart.children.filter(c => c.getAttribute('class') === 'forecast-point').length, 5);
  assert.ok(chart.children.some(c => c.tag === 'circle'));
  assert.ok(chart.children.every(c => !['rect','polygon','defs'].includes(c.tag)));
  assert.equal(a.byId('forecast-key-projected').hidden, false);
  await a.refresh({...catalogs, forecast: {...weather, paused: true}});
  assert.equal(a.byId('forecast-note').hidden, true);
  assert.equal(a.byId('forecast-projection').hidden, true);
  assert.equal(a.byId('forecast-key').hidden, false);
  assert.equal(a.byId('forecast-key-projected').hidden, true);
  assert.equal(a.byId('forecast-chart').getAttribute('aria-label'), 'Context history. Forecast paused.');
  await a.refresh({...catalogs, forecast: weather});
  assert.equal(a.byId('forecast-projection').hidden, false);
  assert.equal(a.byId('forecast-key').hidden, false);
  await a.refresh({...catalogs, forecast: {...weather, thread_id: 'fixture-B'}});
  assert.equal(a.byId('weather-forecast').hidden, false);
  assert.equal(a.byId('forecast-details').hidden, true);
  assert.equal(a.byId('compact-session').disabled, true);
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
  await a.refresh({...catalogs, runtime, runtime_supported: true});
  assert.equal(a.byId('forecast-budget').hidden, true);
  assert.equal(compactButton.disabled, false);
  for (const [tokens, recommended] of [[59999,false], [60000,true], [84999,true], [85000,false]]) {
    await a.refresh({...catalogs, runtime, tokens, observed_window:100000});
    assert.equal(compactButton.dataset.recommended, String(recommended));
    assert.equal(compactButton.disabled, false);
  }
  const sweetSpot = {...catalogs, runtime, tokens:65000, observed_window:100000};
  const smallerBoundary = {...weather, reference_kind:'auto_compact', reference_limit:100000};
  await a.refresh({...sweetSpot, observed_window:200000, forecast:smallerBoundary});
  assert.equal(compactButton.dataset.recommended, 'true');
  await a.refresh({...sweetSpot, observed_window:200000,
    forecast:{...smallerBoundary, observed_at:stamp-30}});
  assert.equal(compactButton.dataset.recommended, 'false');
  await a.refresh({...sweetSpot, collection:{catching_up:true}});
  assert.equal(compactButton.disabled, true);
  assert.equal(compactButton.dataset.recommended, 'false');
  const budget = {thread_id: 'fixture-A', observed_at: stamp, measured_at: stamp, source: 'same_owner_engine_compaction_trace', kind: 'effective_runtime_remaining', active_context_tokens: 28286, remaining_tokens: 428093};
  await a.refresh({...catalogs, runtime: {...runtime, auto_compact: budget}});
  assert.equal(a.byId('forecast-remaining').textContent, '428.1k');
  await a.refresh({...catalogs, runtime: {...runtime, active: true, auto_compact: {...budget, kind: 'sampling_runtime_remaining'}}});
  assert.equal(a.byId('compact-session').disabled, true);
  assert.equal(a.byId('forecast-budget').hidden, false);
  assert.match(a.byId('forecast-remaining').title, /Turn-end policy/);
  await a.refresh({...catalogs, runtime: {...runtime, auto_compact: budget}});
  assert.ok(a.byId('forecast-remaining').title.includes('428,093'));
  await a.refresh({...catalogs, tokens: 28286, runtime: {...runtime, auto_compact: {...budget, active_context_tokens: 28286}}});
  assert.equal(a.byId('forecast-remaining').textContent, '428.1k'); // No history required for live counts.
  assert.equal(a.byId('forecast-details').hidden, true);
  assert.equal(compactButton.dataset.recommended, 'false');
  await a.refresh({...catalogs, runtime: {...runtime, auto_compact: budget}});
  for (const invalid of [{...budget, thread_id: 'fixture-B'}, {...budget, source: 'window_remainder'}, {...budget, observed_at: stamp - 10}]) {
    await a.refresh({...catalogs, runtime: {...runtime, auto_compact: invalid}});
    assert.equal(a.byId('forecast-budget').hidden, true);
  }

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
  assert.equal(compactButton.dataset.recommended, 'false');
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
