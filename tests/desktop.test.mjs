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
  const sdk = {host: {state: {profile: atom('default'), connectionId: atom('local'), gateway: atom('open')}, navigate: path => navigate.push(path)},
    ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'nav', PALETTE_AREA: 'palette', STATUSBAR_AREAS: {right: 'status'},
    Button: 'button', Input: 'input', useValue: atom => atom.get(), gatewayActivationEpoch: () => 1,
    useGatewayActivationEpoch: () => 1, useQuery: () => ({isPending: true}), useQueryClient: () => ({invalidateQueries() {}})};
  const react = {useState: value => [typeof value === 'function' ? value() : value, () => {}], useEffect: () => {}, useRef: value => ({current: value})};
  Object.assign(react, overrides);
  const jsx = (type, props, key) => ({type, props: props || {}, key});
  const modules = {'@hermes/plugin-sdk': sdk, react, 'react/jsx-runtime': {jsx, jsxs: jsx}};
  const source = new vm.SourceTextModule(await readFile(new URL('../desktop/plugin.js', import.meta.url), 'utf8'), {context});
  await source.link(name => {
    assert.ok(modules[name], `Forbidden import ${name}`);
    return new vm.SyntheticModule(Object.keys(modules[name]), function() {
      for (const [key, value] of Object.entries(modules[name])) this.setExport(key, value);
    }, {context});
  });
  await source.evaluate();
  return {api: source.namespace, navigate};
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

test('portfolio renders authoritative project names and risk without status-derived progress', async () => {
  const {api} = await load();
  const page = api.Portfolio({items: [{project_id: 'a', title: 'Synthetic alpha',
    progress: {percent: null}, urgency: {level: 5, reasons: ['insufficient work data']}, commitments: []}], onSelect() {}});
  const text = JSON.stringify(page);
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

test('registered page mounts creation controls and native reads', async () => {
  const {api} = await load();
  const contributions = [];
  const ctx = {register: c => contributions.push(c), onEvent() {}, onDispose() {}};
  api.default.register(ctx);
  function expand(node) {
    if (Array.isArray(node)) return node.map(expand);
    if (!node || typeof node !== 'object') return node;
    if (typeof node.type === 'function') return expand(node.type(node.props));
    return {...node, props: {...node.props, children: expand(node.props?.children)}};
  }
  const tree = JSON.stringify(expand(contributions.find(c => c.area === 'routes').render()));
  assert.match(tree, /Create project/);
  assert.match(tree, /Loading verified measurements/);
});

test('edit form waits for loaded data and pins its draft revision across refresh', async () => {
  const values = []; let index = 0;
  const {api} = await load({useState(initial) {
    const slot = index++;
    if (!(slot in values)) values[slot] = typeof initial === 'function' ? initial() : initial;
    return [values[slot], value => {values[slot] = value;}];
  }});
  assert.equal(api.ProjectEditor({selected: 'a', project: undefined}), null);
  const sent = [];
  const props = {sender: {send: async (op, args) => {sent.push(args); return args;}}, onSaved() {},
    project: {project_id: 'a', title: 'Loaded title', revision: 4}};
  let tree = api.ProjectForm(props);
  const name = tree.props.children.find(n => n?.type === 'label' && n.props.children[0] === 'Project name').props.children[1];
  assert.equal(name.props.value, 'Loaded title');
  name.props.onChange({target: {value: 'My draft'}});
  index = 0;
  tree = api.ProjectForm({...props, project: {...props.project, title: 'Other editor', revision: 5}});
  tree.props.onSubmit({preventDefault() {}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sent[0].title, 'My draft');
  assert.equal(sent[0].expected_revision, 4);
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
