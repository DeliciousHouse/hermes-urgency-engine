// protects=SDK registration and evidence-only progress; fails_when=claims earn credit;
// why_new=first renderer; seam=SDK/React module boundary; no_claim=native host or pixels.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import test from 'node:test';
import vm from 'node:vm';

async function load() {
  const context = vm.createContext({URLSearchParams, Date, JSON, Math, Number, String, Error, crypto: globalThis.crypto});
  const navigate = [];
  const atom = value => ({get: () => value});
  const sdk = {host: {state: {profile: atom('default'), connectionId: atom('local'), gateway: atom('open')}, navigate: path => navigate.push(path)},
    ROUTES_AREA: 'routes', SIDEBAR_NAV_AREA: 'nav', PALETTE_AREA: 'palette', STATUSBAR_AREAS: {right: 'status'},
    Button: 'button', Input: 'input', useValue: atom => atom.get(), gatewayActivationEpoch: () => 1,
    useGatewayActivationEpoch: () => 1, useQuery: () => ({isPending: true}), useQueryClient: () => ({invalidateQueries() {}})};
  const react = {useState: value => [typeof value === 'function' ? value() : value, () => {}], useEffect: () => {}, useRef: value => ({current: value})};
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
