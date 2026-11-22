// protects=SDK registration and evidence-only progress; fails_when=claims earn credit;
// why_new=first renderer; seam=SDK/React module boundary; no_claim=native host or pixels.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

async function load(overrides = {}) {
  const context = vm.createContext({URLSearchParams, Date, JSON, Math, Number, String, Error, crypto: globalThis.crypto});
  const navigate = [];
  const atom = value => ({get: () => value, set: next => {value = next;}});
  const requests = [];
  const sdk = {host: {state: {profile: atom('default'), connectionId: atom('local'), gateway: atom('open')},
    navigate: path => navigate.push(path),
    request: async (method, params) => {requests.push([method, params]); return overrides.rpc?.(method, params) ?? {projects: []};}},
    ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'nav', PALETTE_AREA: 'palette', STATUSBAR_AREAS: {right: 'status'},
    Button: 'button', Input: 'input', useValue: atom => atom.get(), gatewayActivationEpoch: () => 1,
    useGatewayActivationEpoch: () => 1, useQuery: () => ({isPending: true}), useQueryClient: () => ({invalidateQueries() {}})};
  // Real hook semantics: slots persist across a render pass and a functional
  // updater receives the current value, so a draft actually survives a re-render.
  const slots = [];
  let slot = 0;
  // reset() re-renders the same component instance; mount() is a new instance,
  // which in React gets its own hook slots.
  const reset = () => {slot = 0;};
  const mount = () => {slots.length = 0; slot = 0;};
  const react = {
    useState(initial) {
      const index = slot++;
      if (!(index in slots)) slots[index] = typeof initial === 'function' ? initial() : initial;
      return [slots[index], next => {slots[index] = typeof next === 'function' ? next(slots[index]) : next;}];
    },
    useEffect: () => {}, useRef: value => ({current: value})};
  Object.assign(react, overrides);
  const jsx = (type, props, key) => ({type, props: props || {}, key});
  Object.assign(sdk, overrides.sdk || {});
  const modules = {'@hermes/plugin-sdk': sdk, react, 'react/jsx-runtime': {jsx, jsxs: jsx}};
  const source = new vm.SourceTextModule(await readFile(new URL('../desktop/plugin.js', import.meta.url), 'utf8'), {context});
  await source.link(name => {
    assert.ok(modules[name], `Forbidden import ${name}`);
    return new vm.SyntheticModule(Object.keys(modules[name]), function() {
      for (const [key, value] of Object.entries(modules[name])) this.setExport(key, value);
    }, {context});
  });
  await source.evaluate();
  return {api: source.namespace, sdk, navigate, requests, reset, mount};
}

function expand(node) {
  if (Array.isArray(node)) return node.map(expand);
  if (!node || typeof node !== 'object') return node;
  if (typeof node.type === 'function') return expand(node.type(node.props));
  return {...node, props: {...node.props, children: expand(node.props?.children)}};
}

test('reads reject results after a gateway activation changes', async () => {
  const {api} = await load();
  let resolve;
  const ctx = {rest: () => new Promise(done => {resolve = done;})};
  const pending = api.scopedRead(ctx, '/portfolio', {connection: 'other', profile: 'default', epoch: 1});
  await assert.rejects(pending, /Connection changed/);
  const valid = api.scopedRead({rest: async () => ({ok: true, data: {items: []}})}, '/portfolio',
    {connection: 'local', profile: 'default', epoch: 1});
  assert.equal((await valid).items.length, 0);
});

// protects=bounded live reads and stale-write refusal; fails_when=every event
// starts I/O or disconnected scopes keep polling; seam=SDK hooks; no_claim=native routing.
test('event bursts invalidate once and disconnected reads are disabled', async () => {
  const effects = [], queries = [], invalidations = [], callbacks = [];
  const {api, sdk} = await load({useEffect: callback => effects.push(callback), sdk: {
    useQuery: options => {queries.push(options); return {isPending: true};},
    useQueryClient: () => ({invalidateQueries: value => invalidations.push(value)})}});
  sdk.host.state.gateway.set('closed');
  const contributions = [];
  const ctx = {register: c => contributions.push(c), onEvent: (name, fn) => {callbacks.push(fn); return () => {};}};
  api.default.register(ctx);
  expand(contributions.find(c => c.area === 'routes').render());
  assert.ok(queries.length >= 2);
  assert.ok(queries.every(q => q.enabled === false), 'disconnected queries must not read');
  effects.forEach(fn => fn());
  for (let i = 0; i < 50; i++) callbacks.forEach(fn => fn());
  assert.equal(invalidations.length, 0, 'events cannot refresh a disconnected scope');
});

test('live event bursts do not bypass the polling interval', async () => {
  const effects = [], invalidations = [], callbacks = [];
  const {api} = await load({useEffect: callback => effects.push(callback), sdk: {
    useQueryClient: () => ({invalidateQueries: value => invalidations.push(value)})}});
  const contributions = [];
  const ctx = {register: c => contributions.push(c), onEvent: (name, fn) => {callbacks.push(fn); return () => {};}};
  api.default.register(ctx);
  expand(contributions.find(c => c.area === 'routes').render());
  effects.forEach(fn => fn());
  for (let i = 0; i < 50; i++) callbacks.forEach(fn => fn());
  assert.equal(invalidations.length, 1);
});

test('stale detail measurements disable every management control', async () => {
  const project = {project_id: 'a', title: 'A', revision: 1, observed_at: '2030-01-01T00:00:00Z'};
  const {api} = await load({useState: initial => [initial === null ? 'a' : initial, () => {}],
    sdk: {useQuery: () => ({data: project, isError: true, error: new Error('offline')})}});
  const contributions = [];
  api.default.register({register: c => contributions.push(c)});
  const page = contributions.find(c => c.area === 'routes').render();
  const scoped = page.type(page.props);
  const tree = scoped.type(scoped.props);
  let detail;
  (function walk(node) {
    if (Array.isArray(node)) return node.forEach(walk);
    if (!node || typeof node !== 'object') return;
    if (node.type?.name === 'ProjectDetail') detail = node;
    walk(node.props?.children);
  })(tree);
  assert.ok(detail);
  assert.equal(detail.props.disabled, true);
});

test('deadline and membership editors keep their identity across background revisions', async () => {
  let currentRevision = 1;
  const {api} = await load({useState: initial => [initial === null ? 'd' : initial, () => {}],
    sdk: {useQuery: () => ({data: {items: [{deadline_id: 'd', kind: 'soft', revision: currentRevision}]}})}});
  function keys(revision) {
    currentRevision = revision;
    const project = {project_id: 'a', revision, title: 'A', observed_at: '2030-01-01T00:00:00Z',
      commitments: [{deadline_id: 'd', revision, kind: 'soft'}]};
    const tree = api.ProjectDetail({ctx: {}, scope: {}, sender: {}, project});
    const editors = [];
    (function walk(node) {
      if (Array.isArray(node)) return node.forEach(walk);
      if (!node || typeof node !== 'object') return;
      if (['ReviseForm', 'MilestoneEditor'].includes(node.type?.name)) editors.push(node.key);
      walk(node.props?.children);
    })(tree);
    return editors;
  }
  assert.equal(keys(1).length, 2);
  assert.deepEqual(keys(1), keys(2), 'server revisions must not unmount unsaved drafts');
});

// protects=usable filtered portfolio; fails_when=controls do not reach scoped reads;
// why_new=portfolio management; seam=React tree; no_claim=native input or styling.
test('portfolio filters apply exact values and the summary exposes recorded facts', async () => {
  const {api, reset} = await load(); const applied = [];
  let tree = api.PortfolioFilters({onApply: value => applied.push(value)});
  for (const [label, value] of [['Board ID', 'b'], ['Project ID', 'p'], ['Assigned agent', 'builder'],
    ['Urgency', '4'], ['Deadline type', 'hard']]) labelled(tree, label).props.onChange({target: {value}});
  reset(); tree = api.PortfolioFilters({onApply: value => applied.push(value)});
  tree.props.onSubmit({preventDefault() {}});
  assert.deepEqual(JSON.parse(JSON.stringify(applied[0])), {board: 'b', project: 'p', assignee: 'builder', urgency: '4', deadline_type: 'hard'});
  const rows = api.mergeInventory([], [{project_id: 'p', title: 'Launch', progress: {percent: 25, verified_weight: 1, current_weight: 4},
    urgency: {level: 4}, summary: {task_states: {running: 1}, assigned_agents: ['builder'], recorded_running_agents: ['builder'],
      schedule_status: 'at risk', forecast_at: '2030-01-02T00:00:00Z', forecast_evidence: 'insufficient_data',
      next_commitment: {kind: 'hard', target_at: '2030-01-01T00:00:00Z'}}}]);
  reset(); const text = JSON.stringify(expand(api.Portfolio({rows, onSelect() {}})));
  for (const value of ['builder', 'at risk', 'insufficient_data', '1 verified of 4', 'Recorded running']) assert.ok(text.includes(value), value);
});

test('applied portfolio filters enter the scoped REST key and preserve canonical inventory', async () => {
  const queries = [], contributions = [];
  const {api, reset, mount} = await load({sdk: {useQuery: options => {
    queries.push(options.queryKey.at(-1)); return {data: options.queryKey.at(-1) === 'canonical' ? [] : {items: [], coverage: 'local_ledger_only'}};
  }}});
  api.default.register({register: c => contributions.push(c)});
  const page = contributions.find(c => c.area === 'routes').render();
  const scoped = page.type(page.props);
  mount();
  let tree = scoped.type(scoped.props);
  let filter;
  (function walk(node) {
    if (Array.isArray(node)) return node.forEach(walk);
    if (!node || typeof node !== 'object') return;
    if (node.type?.name === 'PortfolioFilters') filter = node;
    walk(node.props?.children);
  })(tree);
  assert.ok(filter, 'filters must be reachable from the portfolio');
  filter.props.onApply({board: 'board-a', assignee: 'builder', urgency: '4', project: '', deadline_type: 'hard'});
  reset(); scoped.type(scoped.props);
  assert.ok(queries.some(path => path.includes('board=board-a') && path.includes('assignee=builder') && path.includes('urgency=4') && path.includes('deadline_type=hard')));
});

test('portfolio timeline pages major commitments and opens their owning project', async () => {
  const {api, reset} = await load(), opened = [];
  const projects = [{project_id: 'a', title: 'Alpha', commitments: Array.from({length: 101}, (_, index) =>
    ({deadline_id: `d${index}`, kind: 'hard', target_at: '2030-02-01T00:00:00Z'}))}];
  const props = {projects, now: '2030-01-01T00:00:00Z', onSelect: id => opened.push(id),
    coordination: {shared_agents: [{agent: 'builder', projects: ['a', 'b']}], same_day_targets: [{utc_day: '2030-02-01', projects: ['a', 'b']}]}};
  let tree = api.PortfolioTimeline(props);
  const timeline = node => node.props.children.find(n => n?.type?.name === 'TimelineView');
  assert.equal(timeline(tree).props.commitments.length, 100);
  timeline(tree).props.onSelect(timeline(tree).props.commitments[0]);
  assert.deepEqual(opened, ['a']);
  assert.match(JSON.stringify(tree), /builder/);
  const next = tree.props.children.find(n => n?.props?.children === 'Next portfolio commitments');
  next.props.onClick(); reset(); tree = api.PortfolioTimeline(props);
  assert.equal(timeline(tree).props.commitments.length, 1);
});

test('portfolio renders authoritative project names and risk without status-derived progress', async () => {
  const {api} = await load();
  const rows = api.mergeInventory([], [{project_id: 'a', title: 'Synthetic alpha',
    progress: {percent: null}, urgency: {level: 5, reasons: ['insufficient work data']}, commitments: []}]);
  const text = JSON.stringify(api.Portfolio({rows, onSelect() {}}));
  assert.match(text, /Synthetic alpha/);
  assert.match(text, /Deadline imminent/);
  assert.match(text, /table/);
});

test('command sender retains exact payload after lost reply and refuses another intent', async () => {
  const {api} = await load();
  const sent = [];
  const sender = api.commandSender({rest: async (path, options) => {
    sent.push(options.body);
    if (sent.length === 1) throw new Error('lost reply');
    return {ok: true, data: {project_id: 'a'}};
  }}, {connection: 'local', profile: 'default', epoch: 1});
  await assert.rejects(sender.send('create_project', {project_id: 'a', title: 'Alpha'}), /lost reply/);
  await assert.rejects(sender.send('create_project', {project_id: 'b', title: 'Beta'}), /Reconcile/);
  await sender.reconcile();
  assert.equal(sent.length, 2);
  assert.equal(sent[0], sent[1]);
  assert.equal(sent[0].arguments.project_id, 'a');
});

// protects=correction after a confirmed no-effect refusal; fails_when=409 traps the
// operator in endless replay; seam=real sender with SDK transport replaced;
// why_new=first reusable command path; no_claim=host authentication.
test('confirmed no-effect refusal releases intent, unknown outcomes stay retained', async () => {
  const {api} = await load();
  let response = {ok: false, error: {code: 'REVISION_CONFLICT', side_effects: 'none', next_action: 'Reload current state'}};
  const sender = api.commandSender({rest: async () => response}, {connection: 'local', profile: 'default', epoch: 1});
  await assert.rejects(sender.send('edit_project', {project_id: 'a'}), /REVISION_CONFLICT/);
  assert.equal(sender.pending(), null);
  response = {ok: false, error: {code: 'INTERNAL_ERROR', side_effects: 'unknown'}};
  await assert.rejects(sender.send('edit_project', {project_id: 'a'}), /INTERNAL_ERROR/);
  assert.ok(sender.pending());
});

test('SDK thrown envelope releases only a structured no-effect refusal', async () => {
  const {api} = await load();
  let error = new Error('Error invoking remote method: 409: ' + JSON.stringify({ok: false,
    error: {code: 'REVISION_CONFLICT', side_effects: 'none', next_action: 'Reload'}}));
  const sender = api.commandSender({rest: async () => {throw error;}}, {connection: 'local', profile: 'default', epoch: 1});
  await assert.rejects(sender.send('edit_project', {}), /REVISION_CONFLICT/);
  assert.equal(sender.pending(), null);
  error = new Error('409 REVISION_CONFLICT connection lost');
  await assert.rejects(sender.send('edit_project', {}), /connection lost/);
  assert.ok(sender.pending(), 'unstructured text cannot establish no effects');
});

// protects=restart-safe exact intent bound to authenticated scope;
// fails_when=reload resends automatically or another gateway acquires the intent;
// why_new=durable recovery; seam=SDK storage/transport; no_claim=native host routing.
function journalContext() {
  const values = new Map(), sent = [];
  let identity = 'a'.repeat(32), lose = true;
  const ctx = {
    storage: {get: (key, fallback) => values.has(key) ? structuredClone(values.get(key)) : fallback,
      set: (key, value) => values.set(key, structuredClone(value)), remove: key => values.delete(key)},
    rest: async (path, options) => {
      if (path === '/identity') return {ok: true, data: {scope_id: identity}};
      sent.push(structuredClone(options.body));
      if (lose) throw new Error('lost reply');
      return {ok: true, data: {project_id: 'a'}};
    }
  };
  return {ctx, values, sent, setIdentity: value => {identity = value;}, succeed: () => {lose = false;}};
}

// protects=one writer and preserved unknown intent; fails_when=a second window
// overwrites retained work; why_new=single-window contract; seam=Web Locks protocol
// with a shared in-memory manager; no_claim=native cross-window lock availability.
test('writer ownership is exclusive, releases on close and fails closed without Web Locks', async () => {
  const {api} = await load();
  const held = new Set();
  const locks = {request: async (key, options, callback) => {
    assert.equal(options.ifAvailable, true);
    if (held.has(key)) return callback(null);
    held.add(key);
    try {return await callback({name: key});} finally {held.delete(key);}
  }};
  const first = [], second = [];
  const close = api.acquireWriter('scope', value => first.push(value), locks);
  api.acquireWriter('scope', value => second.push(value), locks);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(first.at(-1), true);
  assert.equal(second.at(-1), false);
  close();
  await new Promise(resolve => setImmediate(resolve));
  const third = [];
  const closeThird = api.acquireWriter('scope', value => third.push(value), locks);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(third.at(-1), true);
  closeThird();
  const unavailable = [];
  api.acquireWriter('scope', value => unavailable.push(value), null);
  assert.equal(unavailable.at(-1), false);
});

test('read-only window cannot persist or transmit and new owner restores unresolved intent', async () => {
  const {api} = await load(), probe = journalContext();
  const scope = {connection: 'local', profile: 'default', epoch: 1};
  let writable = false;
  const sender = api.commandSender(probe.ctx, scope, {scopeId: 'a'.repeat(32), writable: () => writable});
  await assert.rejects(sender.send('create_project', {project_id: 'a', title: 'Alpha'}), /read.only/i);
  assert.equal(probe.values.size, 0);
  assert.equal(probe.sent.length, 0);
  assert.equal(sender.pending(), null);
  writable = true;
  await assert.rejects(sender.send('create_project', {project_id: 'a', title: 'Alpha'}), /lost reply/);
  writable = false;
  await assert.rejects(sender.reconcile(), /read.only/i);
  const next = api.commandSender(probe.ctx, scope, {scopeId: 'a'.repeat(32), writable: () => true});
  await assert.rejects(next.send('create_project', {project_id: 'b'}), /previous command/i);
  probe.succeed(); await next.reconcile();
  assert.deepEqual(probe.sent[0], probe.sent[1]);
});

test('closing the writer during a request preserves the intent for the next owner', async () => {
  const {api} = await load(), probe = journalContext();
  const scope = {connection: 'local', profile: 'default', epoch: 1};
  let writable = true, reply;
  probe.ctx.rest = async path => path === '/identity' ? {ok: true, data: {scope_id: 'a'.repeat(32)}}
    : new Promise(resolve => {reply = resolve;});
  const sender = api.commandSender(probe.ctx, scope, {scopeId: 'a'.repeat(32), writable: () => writable});
  const pending = sender.send('create_project', {project_id: 'a', title: 'Alpha'});
  await new Promise(resolve => setImmediate(resolve));
  writable = false; reply({ok: true, data: {project_id: 'a'}});
  await assert.rejects(pending, /read.only/i);
  assert.equal(probe.values.size, 1);
});

test('durable sender restores the exact request without automatic transmission', async () => {
  const {api} = await load();
  const probe = journalContext(), scope = {connection: 'local', profile: 'default', epoch: 1};
  const sender = api.commandSender(probe.ctx, scope, {scopeId: 'a'.repeat(32)});
  await assert.rejects(sender.send('create_project', {project_id: 'a', title: 'Alpha'}), /lost reply/);
  assert.equal(probe.values.size, 1, 'intent must survive unload');
  const fresh = await load();
  const restored = fresh.api.commandSender(probe.ctx, scope, {scopeId: 'a'.repeat(32)});
  assert.equal(probe.sent.length, 1, 'activation must not resend');
  assert.equal(restored.pending().request_id, sender.pending().request_id);
  probe.succeed();
  await restored.reconcile();
  assert.deepEqual(probe.sent[1], probe.sent[0]);
  assert.equal(probe.values.size, 0);
});

test('durable recovery refuses changed identity and copied identity on another gateway', async () => {
  const {api} = await load();
  const probe = journalContext(), scope = {connection: 'local', profile: 'default', epoch: 1};
  const sender = api.commandSender(probe.ctx, scope, {scopeId: 'a'.repeat(32)});
  await assert.rejects(sender.send('create_project', {project_id: 'a', title: 'Alpha'}));
  probe.setIdentity('b'.repeat(32));
  await assert.rejects(sender.reconcile(), /identity changed/i);
  assert.equal(probe.sent.length, 1);
  probe.setIdentity('a'.repeat(32));
  const fresh = await load();
  fresh.sdk.host.state.connectionId.set('other');
  const copied = fresh.api.commandSender(probe.ctx, {...scope, connection: 'other'}, {scopeId: 'a'.repeat(32)});
  await assert.rejects(copied.reconcile(), /scope.*ambiguous|different.*scope/i);
  assert.equal(probe.sent.length, 1);
  assert.equal(probe.values.size, 1);
});

test('anonymous route can reconcile in-place but cannot replay after module restart', async () => {
  const first = await load(); first.sdk.host.state.connectionId.set(null);
  const probe = journalContext(), scope = {connection: null, profile: 'default', epoch: 1};
  const sender = first.api.commandSender(probe.ctx, scope, {scopeId: 'a'.repeat(32)});
  await assert.rejects(sender.send('create_project', {project_id: 'a', title: 'Alpha'}));
  const fresh = await load(); fresh.sdk.host.state.connectionId.set(null);
  const restored = fresh.api.commandSender(probe.ctx, scope, {scopeId: 'a'.repeat(32)});
  await assert.rejects(restored.reconcile(), /scope.*ambiguous/i);
  assert.equal(probe.sent.length, 1);
  probe.succeed();
  await sender.reconcile();
  assert.equal(probe.sent.length, 2);
});

test('silent persistence failure prevents a command from leaving the renderer', async () => {
  const {api} = await load(); const probe = journalContext();
  probe.ctx.storage.set = () => {};
  const sender = api.commandSender(probe.ctx, {connection: 'local', profile: 'default', epoch: 1}, {scopeId: 'a'.repeat(32)});
  await assert.rejects(sender.send('create_project', {project_id: 'a', title: 'Alpha'}), /persist/i);
  assert.equal(probe.sent.length, 0);
});

test('recovery controls initialize explicitly and expose the retained request before replay', async () => {
  const {api, reset} = await load();
  const sent = [], refreshed = [];
  const scope = {connection: 'local', profile: 'default', epoch: 1};
  const props = {ctx: {rest: async (path, options) => {sent.push([path, options]); return {ok: true, data: {scope_id: 'a'.repeat(32)}};}},
    scope, identity: {data: {scope_id: null}, refetch: () => refreshed.push(true)}, sender: {pending: () => null}};
  let tree = api.RecoveryControls(props);
  const find = (node, label) => Array.isArray(node) ? node.map(n => find(n, label)).find(Boolean)
    : !node || typeof node !== 'object' ? null : node.props?.children === label ? node : find(node.props?.children, label);
  const initialize = find(tree, 'Enable command recovery');
  assert.ok(initialize);
  assert.equal(sent.length, 0);
  await initialize.props.onClick();
  assert.equal(sent[0][0], '/identity');
  assert.equal(sent[0][1].method, 'POST');
  assert.equal(refreshed.length, 1);
  reset();
  let reconciled = 0;
  tree = api.RecoveryControls({...props, identity: {...props.identity, data: {scope_id: 'a'.repeat(32)}},
    sender: {pending: () => ({request_id: 'retained', operation: 'edit_project', arguments: {title: 'Draft'}}),
      reconcile: async () => {reconciled++;}}});
  assert.match(JSON.stringify(tree), /Draft/);
  assert.equal(reconciled, 0);
  await find(tree, 'Reconcile retained command').props.onClick();
  assert.equal(reconciled, 1);
});

test('scope refusal preserves the original intent and malformed retained payload is not replayed', async () => {
  const {api} = await load(); const probe = journalContext();
  const scope = {connection: 'local', profile: 'default', epoch: 1}, journal = {scopeId: 'a'.repeat(32)};
  const sender = api.commandSender(probe.ctx, scope, journal);
  await assert.rejects(sender.send('create_project', {project_id: 'a', title: 'Alpha'}));
  probe.ctx.rest = async path => path === '/identity' ? {ok: true, data: {scope_id: journal.scopeId}}
    : {ok: false, error: {code: 'SOURCE_DENIED', field: 'scope_id', side_effects: 'none'}};
  await assert.rejects(sender.reconcile(), /SOURCE_DENIED/);
  assert.ok(sender.pending(), 'scope refusal cannot resolve the original scope outcome');
  const key = [...probe.values.keys()][0], record = probe.values.get(key);
  delete record.payload.request_id;
  probe.values.set(key, record);
  const restored = api.commandSender(probe.ctx, scope, journal);
  await assert.rejects(restored.reconcile(), /invalid.*payload/i);
});

test('open legacy gateway without a registry id is not labeled disconnected', async () => {
  const {api} = await load();
  assert.equal(api.connectionLabel(null, 'open'), 'Active gateway (connection identity unavailable)');
  assert.equal(api.connectionLabel('remote-a', 'open'), 'remote-a');
  assert.equal(api.connectionLabel(null, 'closed'), 'Gateway disconnected');
});

test('project form exposes named fields and never accepts an actor', async () => {
  const {api} = await load();
  const tree = JSON.stringify(api.ProjectForm({sender: {}, onSaved() {}, disabled: false}));
  assert.match(tree, /Project ID/);
  assert.match(tree, /Project name/);
  assert.match(tree, /Create project/);
  assert.doesNotMatch(tree, /actor/);
});

test('timeline exposes distinct target forecast actual and original labels', async () => {
  const {api} = await load();
  const tree = JSON.stringify(api.Timeline({commitments: [{deadline_id: 'd', kind: 'hard',
    original_at: '2030-01-01T00:00:00Z', target_at: '2030-01-02T00:00:00Z',
    forecast_at: '2030-01-03T00:00:00Z', actual_verified_at: null}],
    start: '2030-01-01T00:00:00Z', end: '2030-01-04T00:00:00Z', now: '2030-01-02T00:00:00Z', onSelect() {}}));
  for (const label of ['Original', 'Hard target', 'Forecast', 'Current time']) assert.match(tree, new RegExp(label));
  assert.doesNotMatch(tree, /Actual completion/);
});

// protects=coincident commitments remain keyboard/pointer reachable;
// fails_when=44px markers overlay each other; seam=render tree; no_claim=pixel QA.
test('coincident timeline markers occupy separate tracks', async () => {
  const {api} = await load();
  const at = '2030-01-01T00:00:00Z';
  const tree = expand(api.Timeline({commitments: [{deadline_id: 'd', kind: 'hard',
    original_at: at, target_at: at, forecast_at: at, actual_verified_at: at}],
    start: at, end: '2030-01-02T00:00:00Z', now: at, onSelect() {}}));
  const markers = [];
  (function walk(node) {
    if (Array.isArray(node)) return node.forEach(walk);
    if (!node || typeof node !== 'object') return;
    if (/^(Original|Hard target|Forecast|Actual completion):/.test(node.props?.['aria-label'] || '')) markers.push(node);
    walk(node.props?.children);
  })(tree);
  assert.equal(markers.length, 4);
  const tops = markers.map(node => node.props.style.top).sort((a,b) => a-b);
  for (let i = 1; i < tops.length; i++) assert.ok(tops[i] - tops[i-1] >= 44);
});

// protects=hours-to-months keyboard navigation; fails_when=zoom/pan/Fit are inert;
// why_new=interactive timeline controls; seam=React hooks; no_claim=native pixels.
test('timeline zoom, pan and Fit change the axis without changing targets', async () => {
  const {api, reset} = await load();
  const props = {now: '2030-01-01T00:00:00Z', commitments: [{deadline_id: 'd',
    original_at: '2030-02-01T00:00:00Z', target_at: '2030-03-01T00:00:00Z'}], onSelect() {}};
  const original = JSON.stringify(props.commitments);
  const find = (node, label) => {
    if (Array.isArray(node)) return node.map(n => find(n, label)).find(Boolean);
    if (!node || typeof node !== 'object') return null;
    return node.props?.children === label ? node : find(node.props?.children, label);
  };
  let tree = api.TimelineView(props);
  const axis = node => node.props.children.find(n => n?.type?.name === 'Timeline').props;
  const initial = axis(tree);
  find(tree, 'Zoom in').props.onClick(); reset(); tree = api.TimelineView(props);
  assert.ok(Date.parse(axis(tree).end) - Date.parse(axis(tree).start) < Date.parse(initial.end) - Date.parse(initial.start));
  const beforePan = axis(tree).start;
  find(tree, 'Later').props.onClick(); reset(); tree = api.TimelineView(props);
  assert.ok(Date.parse(axis(tree).start) > Date.parse(beforePan));
  find(tree, 'Fit commitments').props.onClick(); reset(); tree = api.TimelineView(props);
  assert.ok(Date.parse(axis(tree).start) <= Date.parse(props.commitments[0].original_at));
  assert.ok(Date.parse(axis(tree).end) >= Date.parse(props.commitments[0].target_at));
  assert.equal(JSON.stringify(props.commitments), original);
});

test('registered page mounts creation controls and native reads', async () => {
  const {api} = await load();
  const contributions = [];
  const ctx = {register: c => contributions.push(c), onEvent() {}, onDispose() {}};
  api.default.register(ctx);
  const tree = JSON.stringify(expand(contributions.find(c => c.area === 'routes').render()));
  assert.match(tree, /Create project/);
  assert.match(tree, /Loading verified measurements/);
});

test('edit form waits for loaded data and pins its draft revision across refresh', async () => {
  const {api, reset} = await load();
  assert.equal(api.ProjectEditor({selected: 'a', project: undefined}), null);
  const sent = [];
  const props = {sender: {send: async (op, args) => {sent.push(args); return args;}}, onSaved() {},
    project: {project_id: 'a', title: 'Loaded title', revision: 4}};
  let tree = api.ProjectForm(props);
  const name = tree.props.children.find(n => n?.type === 'label' && n.props.children[0] === 'Project name').props.children[1];
  assert.equal(name.props.value, 'Loaded title');
  name.props.onChange({target: {value: 'My draft'}});
  reset();
  tree = api.ProjectForm({...props, project: {...props.project, title: 'Other editor', revision: 5}});
  tree.props.onSubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sent[0].title, 'My draft');
  assert.equal(sent[0].expected_revision, 4);
});

test('explicit reload adopts the current revision without dropping the draft', async () => {
  const {api, reset} = await load();
  const sent = [];
  const props = {sender: {send: async (op, args) => {sent.push(args); return args;}}, onSaved() {},
    project: {project_id: 'a', title: 'Loaded title', revision: 4}};
  let tree = api.ProjectForm(props);
  labelled(tree, 'Project name').props.onChange({target: {value: 'My draft'}});
  reset();
  const current = {...props, project: {...props.project, title: 'Other editor', revision: 5}};
  tree = api.ProjectForm(current);
  const reload = tree.props.children.find(n => n?.props?.children === 'Use current revision and keep draft');
  assert.ok(reload, 'a revision conflict needs a usable recovery control');
  reload.props.onClick();
  reset();
  tree = api.ProjectForm(current);
  tree.props.onSubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sent[0].title, 'My draft');
  assert.equal(sent[0].expected_revision, 5);
});

test('conflict review displays current field values beside retained draft before adoption', async () => {
  const {api, reset} = await load();
  const props = {sender: {}, onSaved() {}, project: {project_id: 'a', title: 'Original', revision: 1}};
  let tree = api.ProjectForm(props);
  labelled(tree, 'Project name').props.onChange({target: {value: 'My draft'}});
  reset();
  tree = api.ProjectForm({...props, project: {...props.project, title: 'External rename', revision: 2}});
  const text = JSON.stringify(tree);
  assert.match(text, /External rename/);
  assert.match(text, /My draft/);
  assert.match(text, /Current saved value/);
});

test('status bar shows measured urgency but never certifies a partial or disconnected portfolio', async () => {
  let data = {items: [{urgency: {level: 4}}, {urgency: {level: 6}}], has_more: false};
  const {api, sdk} = await load({sdk: {useQuery: () => ({data})}});
  let text = JSON.stringify(api.StatusChip({ctx: {}}));
  assert.match(text, /Overdue \/ recovery required/);
  data = {...data, has_more: true};
  assert.match(JSON.stringify(api.StatusChip({ctx: {}})), /partial/i);
  sdk.host.state.gateway.set('closed');
  assert.match(JSON.stringify(api.StatusChip({ctx: {}})), /disconnected/i);
});

test('unified plugin registers page, sidebar, palette and status', async () => {
  const {api, navigate} = await load();
  const contributions = [];
  api.default.register({register: c => contributions.push(c), onEvent() {}, onDispose() {}});
  assert.equal(api.default.id, 'hermes-urgency-engine');
  assert.deepEqual(contributions.map(c => c.area).sort(), ['nav', 'palette', 'routes', 'status']);
  contributions.find(c => c.area === 'palette').data.run();
  assert.equal(navigate[0], '/urgency');
});

test('progress uses the backend percentage and explicitly labels absent evidence', async () => {
  const {api} = await load();
  const known = api.Progress({progress: {percent: 25, verified_weight: 2, current_weight: 8}});
  const text = JSON.stringify(known);
  assert.match(text, /25/);
  assert.match(text, /progressbar/);
  assert.match(text, /aria-valuenow/);
  const unknown = JSON.stringify(api.Progress({progress: {percent: null}}));
  assert.match(unknown, /Insufficient evidence/);
  assert.doesNotMatch(unknown, /aria-valuenow/);
});

// protects=explicit canonical identity binding; fails_when=name/board inference joins
// foreign measurements; why_new=decision to discover canonical projects in the renderer;
// seam=gateway RPC boundary; no_claim=not proof of real multi-gateway routing.
test('canonical inventory joins measurements only on explicit project identity', async () => {
  const {api} = await load();
  const canonical = [{id: 'proj-alpha', slug: 'alpha', name: 'Shared name', board_slug: 'b1'},
    {id: 'proj-beta', slug: 'beta', name: 'Beta', board_slug: 'b1'}];
  const measured = [{project_id: 'proj-alpha', title: 'Shared name', progress: {percent: 50}},
    {project_id: 'ledger-only', title: 'Shared name', progress: {percent: 10}}];
  const rows = api.mergeInventory(canonical, measured);
  const alpha = rows.find(row => row.key === 'proj-alpha');
  assert.equal(alpha.measured.progress.percent, 50);
  assert.equal(alpha.binding, 'measured');
  const beta = rows.find(row => row.key === 'proj-beta');
  assert.equal(beta.measured, null);
  assert.equal(beta.binding, 'not_configured');
  const orphan = rows.find(row => row.key === 'ledger-only');
  assert.equal(orphan.binding, 'no_canonical_project');
  assert.equal(orphan.canonical, null);
  assert.equal(rows.length, 3);
});

test('an identically named canonical project in another scope is never joined', async () => {
  const {api} = await load();
  const rows = api.mergeInventory([{id: 'other-scope-id', slug: 'alpha', name: 'Shared name'}],
    [{project_id: 'proj-alpha', title: 'Shared name', progress: {percent: 50}}]);
  assert.equal(rows.find(row => row.key === 'other-scope-id').measured, null);
  assert.equal(rows.find(row => row.key === 'proj-alpha').canonical, null);
});

test('partial inventory never labels an off-page measurement unconfigured or missing', async () => {
  const {api} = await load();
  const row = api.mergeInventory([{id: 'later', name: 'Later'}], [], {measurementsComplete: false})[0];
  assert.equal(row.binding, 'measurement_not_loaded');
  const unavailable = api.mergeInventory([], [{project_id: 'a'}], {canonicalAvailable: false})[0];
  assert.equal(unavailable.binding, 'canonical_unavailable');
});

test('unavailable canonical inventory is not reported as an empty inventory', async () => {
  const {api} = await load();
  assert.match(api.inventoryNotice({isError: true, error: new Error('rpc down')}),
    /inventory unavailable/i);
  assert.match(api.inventoryNotice({isPending: true}), /Loading/i);
  assert.match(api.inventoryNotice({data: []}), /No canonical projects/i);
  assert.equal(api.inventoryNotice({data: [{id: 'a'}]}), null);
});

// protects=operator deadline and milestone control with audited revisions;
// fails_when=a reschedule drops the original target, the reason or the pinned
// revision; why_new=first management controls beyond project metadata;
// seam=command adapter boundary; no_claim=not forecast accuracy or native input.
function labelled(tree, text) {
  const found = [];
  (function walk(node) {
    if (Array.isArray(node)) return node.forEach(walk);
    if (!node || typeof node !== 'object') return;
    if (node.type === 'label' && node.props.children[0] === text) found.push(node.props.children[1]);
    walk(node.props?.children);
  })(tree);
  return found[0];
}

test('deadline control names hard and soft in text and records owner and source', async () => {
  const {api, reset} = await load();
  const sent = [];
  const props = {sender: {send: async (op, args) => {sent.push([op, args]); return args;}},
    projectId: 'proj-alpha', onSaved() {}, disabled: false};
  let tree = api.DeadlineForm(props);
  const text = JSON.stringify(tree);
  for (const field of ['Deadline ID', 'Deadline type', 'Target date and time', 'Time zone', 'Deadline owner', 'Deadline source']) {
    assert.ok(labelled(tree, field), `missing field ${field}`);
  }
  assert.match(text, /Hard \(external commitment\)/);
  assert.match(text, /Soft \(internal target\)/);
  assert.doesNotMatch(text, /actor/);
  labelled(tree, 'Deadline ID').props.onChange({target: {value: 'd1'}});
  labelled(tree, 'Target date and time').props.onChange({target: {value: '2030-06-01T12:00'}});
  labelled(tree, 'Deadline owner').props.onChange({target: {value: 'Client'}});
  labelled(tree, 'Deadline source').props.onChange({target: {value: 'Contract'}});
  reset();
  tree = api.DeadlineForm(props);
  tree.props.onSubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sent[0][0], 'set_deadline');
  assert.equal(sent[0][1].project_id, 'proj-alpha');
  assert.equal(sent[0][1].kind, 'hard');
  assert.equal(sent[0][1].at, '2030-06-01T12:00:00');
  assert.equal(sent[0][1].owner, 'Client');
});

test('rescheduling shows the original target and requires an explicit reason', async () => {
  const {api, reset} = await load();
  const sent = [];
  const commitment = {deadline_id: 'd1', kind: 'soft', revision: 3, timezone: 'UTC',
    original_at: '2030-01-01T00:00:00+00:00', target_at: '2030-02-01T00:00:00+00:00'};
  const props = {sender: {send: async (op, args) => {sent.push([op, args]); return args;}},
    commitment, onSaved() {}, disabled: false};
  let tree = api.ReviseForm(props);
  const text = JSON.stringify(tree);
  assert.match(text, /Original target/);
  assert.match(text, /Current target/);
  assert.ok(labelled(tree, 'Reason for the change'));
  labelled(tree, 'New target date and time').props.onChange({target: {value: '2030-03-01T09:30'}});
  labelled(tree, 'Reason for the change').props.onChange({target: {value: 'client moved the review'}});
  reset();
  tree = api.ReviseForm(props);
  tree.props.onSubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sent[0][0], 'revise_deadline');
  assert.equal(sent[0][1].deadline_id, 'd1');
  assert.equal(sent[0][1].expected_revision, 3);
  assert.equal(sent[0][1].reason, 'client moved the review');
  assert.equal(sent[0][1].intent, undefined);
});

test('milestone editor waits for membership and mounts both editing controls', async () => {
  const {api} = await load();
  const props = {sender: {}, onSaved() {}, disabled: false};
  const pending = api.MilestoneEditor({...props, query: {isPending: true}});
  assert.match(JSON.stringify(pending), /Loading milestone/);
  const tree = expand(api.MilestoneEditor({...props, query: {data: {milestone_id: 'm',
    title: 'Loaded milestone', project_revision: 7, tasks: [['b', 't']]}}}));
  assert.equal(labelled(tree, 'Milestone name').props.value, 'Loaded milestone');
  assert.equal(labelled(tree, 'Member tasks, one board/task per line').props.value, 'b/t');
});

test('milestone control creates and edits membership through named commands', async () => {
  const {api, reset, mount} = await load();
  const sent = [];
  const sender = {send: async (op, args) => {sent.push([op, args]); return args;}};
  const createProps = {sender, projectId: 'proj-alpha', onSaved() {}, disabled: false};
  let create = api.MilestoneForm(createProps);
  labelled(create, 'Milestone ID').props.onChange({target: {value: 'm1'}});
  labelled(create, 'Milestone name').props.onChange({target: {value: 'Beta ready'}});
  reset();
  create = api.MilestoneForm(createProps);
  create.props.onSubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  // The sent payload is built inside the vm realm, so deepStrictEqual rejects a
  // structurally identical object on prototype identity alone. Comparing the
  // serialized value still checks every key and value.
  assert.equal(sent[0][0], 'create_milestone');
  assert.deepEqual(JSON.parse(JSON.stringify(sent[0][1])), {milestone_id: 'm1', project_id: 'proj-alpha', title: 'Beta ready'});
  mount();
  const memberProps = {sender, milestoneId: 'm1', projectRevision: 2, onSaved() {}, disabled: false};
  let members = api.MembershipForm(memberProps);
  const lines = ['b1/t1', 'b1/t2', ''].join(String.fromCharCode(10));
  labelled(members, 'Member tasks, one board/task per line').props.onChange({target: {value: lines}});
  labelled(members, 'Reason for the change').props.onChange({target: {value: 'scope change'}});
  reset();
  members = api.MembershipForm(memberProps);
  members.props.onSubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sent[1][0], 'set_milestone_tasks');
  assert.deepEqual(JSON.parse(JSON.stringify(sent[1][1].tasks)), [['b1', 't1'], ['b1', 't2']]);
  assert.equal(sent[1][1].expected_revision, 2,
    'milestone membership is checked against the project revision, not a milestone revision');
});

// protects=real intervention outcomes; fails_when=guessed fields hide receipts;
// why_new=first intervention table; seam=SDK read; no_claim=native authorization.
test('interventions render requested and observed effects without implying attribution', async () => {
  const {api} = await load({useState: initial => [initial === 'commitments' ? 'interventions' : initial, () => {}],
    sdk: {useQuery: () => ({data: {items: [{action_id: 'action-a', kind: 'priority', state: 'observed',
      requested_effect: {priority: 8}, observed_effect: {priority: 8}, attribution: 'not_attributed',
      next_action: 'Inspect concurrent change'}]}})}});
  const text = JSON.stringify(expand(api.ProjectDetail({ctx: {}, scope: {}, sender: {},
    project: {project_id: 'a', title: 'A', observed_at: '2030-01-01T00:00:00Z'}})));
  for (const value of ['action-a', 'priority', 'not_attributed', 'Inspect concurrent change']) assert.ok(text.includes(value), value);
  assert.ok(text.includes('priority\\\":8'), 'structured effects remain inspectable');
});

test('source task rows show canonical title, run times, dependencies and stale coverage', async () => {
  const {api} = await load({useState: initial => [initial === 'commitments' ? 'source-tasks' : initial, () => {}],
    sdk: {useQuery: () => ({data: {items: [{board_id: 'b', task_id: 't', title: 'Canonical deliverable',
      status: 'running', assignee: 'builder', parents: ['parent-task'], stale: true, coverage: 'unknown',
      observed_at: '2030-01-01T12:00:00Z', worker: {run_id: 9, state: 'running',
        started_at: '2030-01-01T10:00:00Z', heartbeat_at: '2030-01-01T11:59:00Z', ended_at: null}}]}})}});
  const text = JSON.stringify(expand(api.ProjectDetail({ctx: {}, scope: {}, sender: {},
    project: {project_id: 'a', title: 'A', observed_at: '2030-01-01T12:00:00Z'}})));
  for (const value of ['Canonical deliverable', 'parent-task', '2030-01-01T10:00:00Z', 'stale', 'unknown']) assert.ok(text.includes(value), value);
});

test('history renders the actual audit timestamp and action fields', async () => {
  const {api} = await load({useState: initial => [initial === 'commitments' ? 'history' : initial, () => {}],
    sdk: {useQuery: () => ({data: {items: [{sequence: 9, occurred_at: '2030-01-02T10:00:00Z',
      action: 'revise_deadline', actor: 'operator', reason: 'Approved new target',
      before_json: '{"target_at":"2030-01-03"}', after_json: '{"target_at":"2030-01-04"}'}]}})}});
  const project = {project_id: 'a', title: 'A', revision: 1, observed_at: '2030-01-01T00:00:00Z'};
  const text = JSON.stringify(expand(api.ProjectDetail({ctx: {}, scope: {}, sender: {}, project})));
  assert.match(text, /2030-01-02T10:00:00Z/);
  assert.match(text, /revise_deadline/);
  assert.match(text, /2030-01-03/);
  assert.match(text, /2030-01-04/);
  assert.match(text, /Inspect recorded change/);
});

// protects=commitments beyond the summary cap; fails_when=detail never reads next page;
// why_new=paged commitment UI; seam=SDK query; no_claim=installed input/navigation.
test('commitment page navigation reads the next page and renders its targets', async () => {
  const queries = [];
  const {api, reset} = await load({sdk: {useQuery: options => {
    queries.push(options);
    return {data: {items: [{deadline_id: 'later', kind: 'soft', target_at: '2030-01-02T00:00:00Z'}],
      has_more: true, next: 'later'}};
  }}});
  const props = {ctx: {}, scope: {}, sender: {}, project: {project_id: 'a', title: 'A',
    observed_at: '2030-01-01T00:00:00Z', commitments: [], commitments_has_more: true}};
  let tree = expand(api.ProjectDetail(props));
  assert.ok(queries.some(q => q.queryKey.at(-1) === '/projects/a/commitments?limit=100'));
  assert.match(JSON.stringify(tree), /later/);
  let next;
  (function walk(node) {
    if (Array.isArray(node)) return node.forEach(walk);
    if (!node || typeof node !== 'object') return;
    if (node.props?.children === 'Next commitments') next = node;
    walk(node.props?.children);
  })(tree);
  assert.ok(next);
  next.props.onClick();
  reset();
  api.ProjectDetail(props);
  assert.ok(queries.some(q => q.queryKey.at(-1) === '/projects/a/commitments?limit=100&after=later'));
});

// protects=audited scope editing; fails_when=hidden controls or discarded criteria;
// why_new=scope controls; seam=command sender boundary; no_claim=native validation.
test('criteria and baseline controls submit pinned revisions and preserve loaded scope', async () => {
  const {api, reset, mount} = await load();
  const sent = [];
  const sender = {send: async (op, args) => {sent.push([op, args]); return {};}};
  const task = {board_id: 'b', task_id: 't', revision: 4, criteria: [{criterion_id: 'test',
    description: 'Tests pass', weight: 3, required: 1, expected_sha256: null, active: 1, criterion_revision: 2}]};
  const props = {task, sender, onSaved() {}};
  let tree = api.CriteriaForm(props);
  assert.match(JSON.stringify(tree), /invalidates existing verification/);
  labelled(tree, 'Reason for scope change').props.onChange({target: {value: 'Clarify scope'}});
  reset(); tree = api.CriteriaForm(props);
  tree.props.onSubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sent[0][0], 'set_criteria');
  assert.equal(sent[0][1].expected_revision, 4);
  assert.deepEqual(JSON.parse(JSON.stringify(sent[0][1].criteria)), [{criterion_id: 'test',
    description: 'Tests pass', weight: 3, required: true, expected_sha256: null}]);
  mount();
  tree = api.BaselineForm({project: {project_id: 'p', revision: 7}, sender, onSaved() {}});
  tree.props.onSubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sent[1][0], 'baseline_scope');
  assert.equal(sent[1][1].expected_revision, 7);
});

test('verified work inspector exposes loaded criteria and scope controls on the detail page', async () => {
  const task = {board_id: 'b', task_id: 't', goal: 'Ship artifact', revision: 4, criteria: [],
    actual_seconds: 3600, remaining_seconds: null, status: 'running',
    criterion_evidence: [{criterion_id: 'bundle', description: 'Bundle matches', weight: 2,
      required: true, verified: true, evidence_id: 'evidence-one', checked_at: '2030-01-01T00:00:00Z',
      expected_sha256: 'a'.repeat(64), observed_sha256: 'a'.repeat(64)}]};
  const {api, reset} = await load({sdk: {useQuery: () => ({data: {items: [task], has_more: false}})}});
  const props = {ctx: {}, scope: {}, sender: {}, project: {project_id: 'a', revision: 1,
    observed_at: '2030-01-01T00:00:00Z'}, onSaved() {}};
  let tree = api.TaskScope(props);
  let select;
  (function walk(node) {
    if (Array.isArray(node)) return node.forEach(walk);
    if (!node || typeof node !== 'object') return;
    if (node.props?.children === 'b/t · Ship artifact') select = node;
    walk(node.props?.children);
  })(tree);
  assert.ok(select);
  select.props.onClick(); reset();
  tree = expand(api.TaskScope(props));
  const text = JSON.stringify(tree);
  assert.match(text, /Acceptance criteria \(JSON\)/);
  assert.match(text, /Measured execution/);
  assert.match(text, /Unknown/);
  assert.match(text, /Record immutable baseline/);
  assert.match(text, /Bundle matches/);
  assert.match(text, /Verified bytes/);
  assert.match(text, /evidence-one/);
});

test('detail tabs expose every audited read surface with its own state', async () => {
  const {api} = await load();
  const tree = JSON.stringify(api.DetailTabs({tab: 'history', onTab() {},
    query: {isPending: true}, children: null}));
  for (const label of ['Commitments', 'Milestones', 'Verified work', 'Source tasks', 'History', 'Interventions']) {
    assert.match(tree, new RegExp(label));
  }
  assert.match(tree, /aria-selected/);
  const records = JSON.stringify(api.RecordList({query: {data: {items: [], has_more: false}},
    empty: 'No recorded interventions', columns: ['at'], row: item => [item.at]}));
  assert.match(records, /No recorded interventions/);
});

// protects=detail wiring of every control and audited tab; fails_when=a control or
// read surface is built but unreachable from the project page; why_new=detail view
// extracted into its own component; seam=SDK module boundary; no_claim=not native pixels.
test('project detail mounts the timeline, every tab and every audited control', async () => {
  const {api} = await load({useQuery: () => ({data: {items: [{milestone_id: 'm1', title: 'Beta', revision: 1}]}})});
  const project = {project_id: 'proj-alpha', title: 'Synthetic alpha', revision: 2,
    observed_at: '2030-02-01T00:00:00+00:00', coverage: 'local_ledger_only',
    risk_coverage: 'recorded_commitments',
    progress: {percent: 40, verified_weight: 2, current_weight: 5, baseline_weight: 5,
      last_verified_progress_at: '2030-01-20T00:00:00+00:00'},
    urgency: {level: 3, reasons: ['verification time exceeds remaining slack']},
    commitments: [{deadline_id: 'd1', kind: 'hard', revision: 1, owner: 'Client', source: 'Contract',
      original_at: '2030-03-01T00:00:00+00:00', target_at: '2030-03-01T00:00:00+00:00',
      forecast_at: '2030-03-10T00:00:00+00:00', actual_verified_at: null,
      baseline_variance_seconds: 777600, target_variance_seconds: 777600,
      forecast: {level: 3, reasons: ['forecast crosses the hard target']}}],
    commitments_has_more: false};
  const tree = JSON.stringify(expand(api.ProjectDetail({ctx: {rest: async () => ({ok: true, data: {}}), onEvent() {}},
    scope: {connection: 'local', profile: 'default', epoch: 1}, sender: {}, project,
    disabled: false, onSaved() {}})));
  for (const label of ['Synthetic alpha', 'Hard target', 'Commitments', 'Milestones',
    'Source tasks', 'History', 'Interventions', 'Add a deadline', 'Add a milestone',
    'Edit milestone membership', 'Reschedule this deadline', 'Weighted scope',
    'Variance against original', 'High']) {
    assert.ok(tree.includes(label), `detail is missing ${label}`);
  }
  assert.match(tree, /verification time exceeds remaining slack/);
});

// protects=an actionable save failure; fails_when=a raw transport payload is dumped
// into the page as ordinary text, or a conflict is not announced as an alert;
// why_new=a vision pass on the installed page found the 409 shown as inline prose;
// seam=SDK module boundary; no_claim=not a native rendering check.
test('a save failure is summarised, announced and keeps its detail collapsed', async () => {
  const {api} = await load();
  const raw = "Error invoking remote method 'hermes:api': Error: 409: "
    + JSON.stringify({ok: false, error: {code: 'REVISION_CONFLICT', operation: 'edit_project',
      retryable: true, side_effects: 'none',
      next_action: 'Reload current state and reconcile the intended change.'}});
  const failure = api.commandFailure(new Error(raw));
  assert.match(failure.headline, /REVISION_CONFLICT/);
  assert.match(failure.headline, /Reload current state/);
  assert.ok(!failure.headline.includes('{'), 'the summary must not contain the raw payload');
  assert.ok(failure.detail.includes('hermes:api'), 'the raw text stays available as detail');
  const plain = api.commandFailure(new Error('lost reply'));
  assert.match(plain.headline, /lost reply/);
  assert.equal(plain.detail, null);
});

test('the command form announces a failure as an alert and explains reconcile', async () => {
  const {api, reset} = await load();
  const props = {sender: {send: async () => {throw new Error('lost reply');}, pending: () => true},
    projectId: 'proj-alpha', onSaved() {}, disabled: false};
  let tree = api.DeadlineForm(props);
  tree.props.onSubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  reset();
  tree = api.DeadlineForm(props);
  const text = JSON.stringify(tree);
  assert.match(text, /"role":"alert"/);
  assert.match(text, /lost reply/);
  assert.match(text, /Reconcile same request/);
  assert.match(text, /resends the identical request/i);
});

// protects=operator-typed task references; fails_when=a line without a separator is
// silently split into a wrong board/task pair; why_new=membership is the only control
// that parses free text; seam=SDK module boundary; no_claim=not Ledger validation.
test('member task lines must name a board and a task, or the form refuses', async () => {
  const {api} = await load();
  assert.deepEqual(JSON.parse(JSON.stringify(api.memberTasks('b1/t1\n  b1/t2  \n\n'))),
    {tasks: [['b1', 't1'], ['b1', 't2']], rejected: []});
  // A task id may contain a separator; only the first one splits.
  assert.deepEqual(JSON.parse(JSON.stringify(api.memberTasks('b1/group/t3'))),
    {tasks: [['b1', 'group/t3']], rejected: []});
  const bad = api.memberTasks('b1t1\nb1/\n/t2');
  assert.deepEqual(JSON.parse(JSON.stringify(bad.tasks)), []);
  assert.deepEqual(JSON.parse(JSON.stringify(bad.rejected)), ['b1t1', 'b1/', '/t2']);
});

test('membership refuses to send when a line is unparseable', async () => {
  const {api, reset} = await load();
  const sent = [];
  const props = {sender: {send: async (op, args) => {sent.push([op, args]); return args;}},
    milestoneId: 'm1', projectRevision: 2, onSaved() {}, disabled: false};
  let tree = api.MembershipForm(props);
  const field = (function find(node) {
    if (Array.isArray(node)) {for (const item of node) {const hit = find(item); if (hit) return hit;}  return null;}
    if (!node || typeof node !== 'object') return null;
    if (node.type === 'label' && String(node.props.children[0]).startsWith('Member tasks')) return node.props.children[1];
    return find(node.props?.children);
  })(tree);
  field.props.onChange({target: {value: 'b1t1'}});
  reset();
  tree = api.MembershipForm(props);
  tree.props.onSubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sent.length, 0, 'an unparseable line must not reach the backend');
  reset();
  const text = JSON.stringify(api.MembershipForm(props));
  assert.match(text, /b1t1/);
  assert.match(text, /board_id\/task_id/);
});
