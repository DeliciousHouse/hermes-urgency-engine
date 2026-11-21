import {host, useValue, useQuery, useQueryClient, gatewayActivationEpoch, useGatewayActivationEpoch,
  Button, Input, ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA, STATUSBAR_AREAS} from '@hermes/plugin-sdk';
import {useState, useEffect, useRef} from 'react';
import {jsx, jsxs} from 'react/jsx-runtime';

export function Progress({progress}) {
  const percent = progress?.percent;
  if (percent === null || percent === undefined) return jsx('span', {children: 'Insufficient evidence'});
  return jsxs('div', {children: [jsx('div', {role: 'progressbar', 'aria-label': 'Verified completion',
    'aria-valuemin': 0, 'aria-valuemax': 100, 'aria-valuenow': percent,
    style: {height: 8, background: 'var(--ui-stroke-secondary)'}, children: jsx('div', {
      style: {width: `${percent}%`, height: '100%', background: 'var(--ui-accent)'}})}),
    jsx('span', {children: `${percent}% verified`})]});
}

const levels = ['Unmeasured', 'Normal', 'Elevated', 'High', 'Critical', 'Deadline imminent', 'Overdue / recovery required'];
const date = value => value ? new Date(value).toLocaleString() : 'Unknown';
const risk = item => levels[item?.urgency?.level || 0] || 'Unknown';
const cell = children => jsx('td', {children});

function currentScope() {
  return {connection: host.state.connectionId.get(), profile: host.state.profile.get(), epoch: gatewayActivationEpoch()};
}
function sameScope(scope) {
  const now = currentScope();
  return scope.connection === now.connection && scope.profile === now.profile && scope.epoch === now.epoch;
}
export async function scopedRead(ctx, path, scope) {
  if (!sameScope(scope)) throw new Error('Connection changed. Reload this view.');
  const result = await ctx.rest(path, {timeoutMs: 15000});
  if (!sameScope(scope)) throw new Error('Connection changed. Reload this view.');
  if (!result.ok) throw new Error(result.error?.code || 'Read failed');
  return result.data;
}
function useScope() {
  const connection = useValue(host.state.connectionId);
  const profile = useValue(host.state.profile);
  const epoch = useGatewayActivationEpoch();
  return {connection, profile, epoch};
}
function useRead(ctx, path, scope) {
  const client = useQueryClient();
  const query = useQuery({queryKey: ['urgency', scope.connection, scope.profile, scope.epoch, path],
    queryFn: () => scopedRead(ctx, path, scope), retry: false, staleTime: 5000,
    refetchInterval: query => query.state.error ? 60000 : 10000, refetchIntervalInBackground: false});
  useEffect(() => ctx.onEvent('*', () => client.invalidateQueries({queryKey: ['urgency', scope.connection, scope.profile, scope.epoch]})),
    [ctx, client, scope.connection, scope.profile, scope.epoch]);
  return query;
}

export function Portfolio({items, onSelect}) {
  return jsx('div', {className: 'ue-scroll', children: jsxs('table', {children: [
    jsx('caption', {children: 'Projects · verified progress and deadline risk'}),
    jsx('thead', {children: jsx('tr', {children: ['Project', 'Verified progress', 'Urgency', 'Next commitment'].map(label => jsx('th', {scope: 'col', children: label}, label))})}),
    jsx('tbody', {children: items.map(item => jsxs('tr', {children: [
      cell(jsx(Button, {onClick: () => onSelect(item.project_id), children: item.title})),
      cell(jsx(Progress, {progress: item.progress})), cell(risk(item)),
      cell(item.commitments?.length ? `${item.commitments[0].kind} · ${date(item.commitments[0].target_at)}` : 'No recorded commitment')
    ]}, item.project_id))})]})});
}

const styles = `.ue {font:inherit;color:var(--ui-text-primary);padding:20px;overflow:auto;height:100%;font-size:16px;line-height:1.5}
.ue * {box-sizing:border-box}.ue h1{font-size:28px;margin:0 0 12px}.ue h2{font-size:22px;margin:24px 0 8px}
.ue button,.ue input,.ue select{min-height:44px}.ue button:focus-visible,.ue input:focus-visible,.ue select:focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.ue-scroll{overflow:auto}.ue table{width:100%;border-collapse:collapse;text-align:left}.ue th,.ue td{padding:12px 16px 12px 0;vertical-align:top;border-bottom:1px solid var(--ui-stroke-secondary)}
.ue caption{text-align:left;padding:12px 0}.ue-toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.ue-note{padding:12px 0}.ue dl{display:grid;grid-template-columns:minmax(120px,1fr) 3fr;gap:8px}.ue dd{margin:0;overflow-wrap:anywhere}
.ue [role=progressbar]{min-width:120px}.ue form{max-width:640px}.ue label{display:grid;gap:4px;margin:12px 0}.ue input,.ue select{width:100%;color:inherit;background:var(--ui-bg);border:1px solid var(--ui-stroke-secondary);padding:8px;font:inherit}
@media(max-width:640px){.ue{padding:12px}.ue th,.ue td{padding-right:12px}.ue dl{grid-template-columns:1fr}.ue dd{margin-bottom:8px}}`;

function Page({ctx}) {
  const scope = useScope();
  return jsx(ScopedPage, {ctx, scope}, JSON.stringify(scope));
}
function ScopedPage({ctx, scope}) {
  const [selected, setSelected] = useState(null);
  const [after, setAfter] = useState(null);
  const gateway = useValue(host.state.gateway);
  const path = selected ? `/projects/${encodeURIComponent(selected)}` : `/portfolio?limit=100${after ? '&after=' + encodeURIComponent(after) : ''}`;
  const query = useRead(ctx, path, scope);
  return jsxs('main', {className: 'ue', 'aria-label': 'Urgency Engine', children: [
    jsx('style', {children: styles}), jsx('h1', {children: 'Urgency Engine'}),
    jsx('p', {children: `${scope.connection || 'No connection'} / ${scope.profile} · ${gateway}`}),
    selected && jsx(Button, {onClick: () => setSelected(null), children: 'Back to portfolio'}),
    query.isPending ? jsx('p', {role: 'status', children: 'Loading verified measurements…'}) : null,
    query.isError && jsxs('div', {role: 'alert', children: ['Measurements unavailable or stale. ', query.error?.message,
      jsx(Button, {onClick: () => query.refetch(), children: 'Retry read'})]}),
    query.data && jsxs('section', {children: [
      jsx('p', {className: 'ue-note', children: `Observed ${date(query.data.observed_at)} · Coverage: ${query.data.coverage}. Artifact byte identity does not prove semantic acceptance.`}),
      selected ? jsxs('section', {children: [jsx('h2', {children: query.data.title}), jsx(Progress, {progress: query.data.progress}),
        jsx('p', {children: risk(query.data)}), jsx('p', {children: query.data.urgency?.reasons?.join('; ') || 'No risk measurement'}),
        jsx('h2', {children: 'Commitments'}), ...(query.data.commitments || []).map(commitment => jsxs('dl', {children: [
          jsx('dt', {children: `${commitment.kind} deadline`}), jsx('dd', {children: commitment.deadline_id}),
          jsx('dt', {children: 'Original'}), jsx('dd', {children: date(commitment.original_at)}),
          jsx('dt', {children: 'Current target'}), jsx('dd', {children: date(commitment.target_at)}),
          jsx('dt', {children: 'Forecast'}), jsx('dd', {children: date(commitment.forecast_at)}),
          jsx('dt', {children: 'Verified completion'}), jsx('dd', {children: date(commitment.actual_verified_at)})
        ]}, commitment.deadline_id))]}) : jsxs('section', {children: [
          jsx(Portfolio, {items: query.data.items, onSelect: setSelected}),
          !query.data.items.length && jsx('p', {children: 'No configured projects in this scope. Create a project to begin measuring verified work.'}),
          after && jsx(Button, {onClick: () => setAfter(null), children: 'First page'}),
          query.data.has_more && jsx(Button, {onClick: () => setAfter(query.data.next), children: 'Next projects'})]})]})]});
}

export default {
  id: 'hermes-urgency-engine', name: 'Urgency Engine',
  register(ctx) {
    ctx.register({id: 'page', area: ROUTES_AREA, data: {path: '/urgency'}, render: () => jsx(Page, {ctx})});
    ctx.register({id: 'nav', area: SIDEBAR_NAV_AREA, data: {path: '/urgency', label: 'Urgency Engine', codicon: 'watch'}});
    ctx.register({id: 'open', area: PALETTE_AREA, data: {id: 'urgency.open', label: 'Open Urgency Engine',
      keywords: ['deadline', 'progress', 'milestone'], run: () => host.navigate('/urgency')}});
    ctx.register({id: 'status', area: STATUSBAR_AREAS.right, render: () => jsx(Button, {
      onClick: () => host.navigate('/urgency'), children: 'Urgency'})});
  }
};
