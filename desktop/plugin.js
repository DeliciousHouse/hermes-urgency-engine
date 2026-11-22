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

export function connectionLabel(connection, gateway) {
  return connection || (gateway === 'open' ? 'Active gateway (connection identity unavailable)' : 'Gateway disconnected');
}
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
// Canonical discovery uses the host's own scoped RPC; this plugin never reads a
// board database or infers a project from a path.
export async function canonicalProjects(scope) {
  if (!sameScope(scope)) throw new Error('Connection changed. Reload this view.');
  const payload = await host.request('projects.list', {profile: scope.profile});
  if (!sameScope(scope)) throw new Error('Connection changed. Reload this view.');
  return payload?.projects || [];
}
export function commandSender(ctx, scope) {
  let retained = null;
  let busy = false;
  async function transmit() {
    if (busy) throw new Error('Command already pending');
    if (!sameScope(scope)) throw new Error('Connection changed. Return to the original scope to reconcile.');
    busy = true;
    try {
      const result = await ctx.rest('/commands', {method: 'POST', body: retained, timeoutMs: 15000});
      if (!sameScope(scope)) throw new Error('Connection changed. Return to the original scope to reconcile.');
      if (!result.ok) throw new Error(JSON.stringify(result));
      retained = null;
      return result.data;
    } catch (error) {
      // Only the host's structured no-effect receipt releases an intent. A
      // timeout, generic HTTP status or lost reply still requires exact replay.
      const message = String(error?.message || '');
      try {
        const envelope = JSON.parse(message.slice(message.indexOf('{')));
        if (envelope.ok === false && envelope.error?.side_effects === 'none') retained = null;
      } catch { /* An unstructured transport error has an unknown outcome. */ }
      throw error;
    } finally { busy = false; }
  }
  return {
    async send(operation, args) {
      if (retained) throw new Error('Reconcile the previous command before submitting another intent.');
      retained = JSON.parse(JSON.stringify({request_id: crypto.randomUUID(), operation, arguments: args}));
      return transmit();
    },
    async reconcile() {
      if (!retained) throw new Error('No command to reconcile');
      return transmit();
    },
    pending: () => retained
  };
}

function useScope() {
  const connection = useValue(host.state.connectionId);
  const profile = useValue(host.state.profile);
  const epoch = useGatewayActivationEpoch();
  return {connection, profile, epoch};
}
function useRead(ctx, path, scope, enabled = true) {
  const gateway = useValue(host.state.gateway);
  return useQuery({queryKey: ['urgency', scope.connection, scope.profile, scope.epoch, path],
    queryFn: () => scopedRead(ctx, path, scope), retry: false, staleTime: 5000, enabled: enabled && gateway === 'open',
    refetchInterval: query => query.state.error ? 60000 : 10000, refetchIntervalInBackground: false});
}

// Canonical identity only. Two projects sharing a name, slug, board or path are
// different projects; a measurement is shown against a canonical row solely when
// the Ledger project id IS that canonical project id, read in the same scope.
export function mergeInventory(canonical, measured) {
  const byId = new Map((measured || []).map(item => [item.project_id, item]));
  const rows = (canonical || []).map(project => {
    const match = byId.get(project.id) || null;
    byId.delete(project.id);
    return {key: project.id, canonical: project, measured: match,
      binding: match ? 'measured' : 'not_configured'};
  });
  for (const item of byId.values()) {
    rows.push({key: item.project_id, canonical: null, measured: item, binding: 'no_canonical_project'});
  }
  return rows;
}

export function inventoryNotice(query) {
  if (query.isError) return `Canonical project inventory unavailable (${query.error?.message || 'request failed'}). Only locally measured projects are listed; this is not an empty inventory.`;
  if (query.isPending) return 'Loading the canonical project inventory…';
  if (!query.data?.length) return 'No canonical projects in this scope. Measured projects below, if any, have no matching canonical project.';
  return null;
}

const bindings = {measured: 'Measured', not_configured: 'Not configured for urgency measurement',
  no_canonical_project: 'No canonical project in this scope'};

export function Portfolio({rows, onSelect}) {
  return jsx('div', {className: 'ue-scroll', children: jsxs('table', {children: [
    jsx('caption', {children: 'Projects · verified progress and deadline risk'}),
    jsx('thead', {children: jsx('tr', {children: ['Project', 'Measurement', 'Verified progress', 'Urgency', 'Next commitment'].map(label => jsx('th', {scope: 'col', children: label}, label))})}),
    jsx('tbody', {children: rows.map(row => {
      const item = row.measured;
      const title = row.canonical?.name || item?.title;
      return jsxs('tr', {children: [
        cell(item ? jsx(Button, {onClick: () => onSelect(item.project_id), children: title}) : title),
        cell(`${bindings[row.binding]}${row.canonical?.archived ? ' · archived canonical project' : ''}`),
        cell(item ? jsx(Progress, {progress: item.progress}) : 'Not measured'),
        cell(item ? risk(item) : 'Unmeasured'),
        cell(item?.commitments?.length ? `${item.commitments[0].kind} · ${date(item.commitments[0].target_at)}` : 'No recorded commitment')
      ]}, row.key);
    })})]})});
}

export function ProjectEditor({selected, project, ...props}) {
  if (selected && !project) return null;
  return jsx(ProjectForm, {...props, project}, selected || 'new');
}

// A local datetime control omits seconds; the Ledger parses a full civil time
// against the named zone. Never guess an offset here.
const civil = value => /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value || '') ? `${value}:00` : value;

// A transport failure can arrive as a long string with the backend envelope
// embedded in it. Summarise the part an operator can act on and keep the raw
// text available instead of dumping it into the page as prose.
export function commandFailure(error) {
  const message = String(error?.message || error || 'Command refused');
  const start = message.indexOf('{');
  if (start !== -1) {
    try {
      const envelope = JSON.parse(message.slice(start));
      const detail = envelope?.error || envelope;
      const code = detail?.code;
      if (code) {
        return {headline: [code, detail.operation && `during ${detail.operation}`,
          detail.next_action].filter(Boolean).join(' · '), detail: message};
      }
    } catch {
      // Not an envelope after all; fall through to the plain summary.
    }
    return {headline: message.slice(0, start).trim() || 'Command refused', detail: message};
  }
  return {headline: message, detail: null};
}

// One audited command path for every control: the submitted revision is the one
// the draft began on, an unknown outcome is retained rather than resent, and no
// form can name an actor or an authorization.
export function CommandForm({sender, title, submitLabel, fields, base, build, onSaved, disabled, preamble, validate}) {
  const [pinned, setPinned] = useState(base);
  const [draft, setDraft] = useState(() => Object.fromEntries(fields.map(field =>
    [field.name, field.initial ? field.initial(base) : base?.[field.name] ?? (field.options ? field.options[0][0] : '')])));
  const [saved, setSaved] = useState('');
  const [failure, setFailure] = useState(null);
  const [pending, setPending] = useState(false);
  const lock = useRef(false);
  const [dirty, setDirty] = useState({});
  const set = (name, value) => {
    setDirty(current => ({...current, [name]: true}));
    setDraft(current => ({...current, [name]: value}));
  };
  async function submit(reconcile = false) {
    if (lock.current || disabled) return;
    // Named inline before anything is sent, so a misread field never becomes a
    // backend error the operator has to decode.
    const invalid = reconcile ? null : validate?.(draft);
    if (invalid) {setSaved(''); setFailure({headline: invalid, detail: null}); return;}
    lock.current = true; setPending(true);
    try {
      const [operation, args] = build(draft, pinned);
      const result = reconcile ? await sender.reconcile() : await sender.send(operation, args);
      // Keep the pin when the result carries no revision of its own (milestone
      // operations are checked against their project's revision).
      if (base && result?.revision !== undefined) setPinned(result);
      setFailure(null); setSaved('Saved. Reading current state…'); onSaved(result);
    } catch (error) {
      setSaved(''); setFailure(commandFailure(error));
    } finally {lock.current = false; setPending(false);}
  }
  const control = field => field.options
    ? jsx('select', {value: draft[field.name], disabled: pending, 'aria-label': field.label,
      onChange: event => set(field.name, event.target.value),
      children: field.options.map(([value, label]) => jsx('option', {value, children: label}, value))})
    : jsx(field.multiline ? 'textarea' : Input, {value: draft[field.name], disabled: pending,
      required: field.required !== false, maxLength: field.maxLength || 4096, type: field.type,
      rows: field.multiline ? 6 : undefined, onChange: event => set(field.name, event.target.value)});
  return jsxs('form', {onSubmit: event => {event.preventDefault(); void submit();}, children: [
    jsx('h2', {children: title}), ...(preamble || []),
    ...fields.map(field => jsxs('label', {children: [field.label, control(field)]}, field.name)),
    base?.revision !== undefined && base.revision !== pinned?.revision && jsx('p', {role: 'status',
      children: `The record changed (draft revision ${pinned?.revision}, current ${base.revision}). Review current values before keeping your changes.`}),
    base?.revision !== undefined && base.revision !== pinned?.revision && jsx(Button, {type: 'button',
      disabled: disabled || pending || !!sender.pending?.(), onClick: () => {
        setPinned(base);
        setDraft(current => Object.fromEntries(fields.map(field => [field.name, dirty[field.name] ? current[field.name]
          : field.initial ? field.initial(base) : base?.[field.name] ?? (field.options ? field.options[0][0] : '')])));
        setFailure(null); setSaved('Current revision loaded. Draft edits retained; review before saving.');
      }, children: 'Use current revision and keep draft'}),
    jsx(Button, {type: 'submit', disabled: disabled || pending || !!sender.pending?.(), children: pending ? 'Saving…' : submitLabel}),
    failure && jsxs('div', {role: 'alert', className: 'ue-alert', children: [
      jsx('strong', {children: 'Save not confirmed.'}), ' ', failure.headline,
      jsx('p', {children: 'Read the current state before deciding. Nothing is resent automatically.'}),
      failure.detail && jsxs('details', {children: [jsx('summary', {children: 'Full response'}),
        jsx('pre', {style: {whiteSpace: 'pre-wrap', overflowWrap: 'anywhere'}, children: failure.detail})]})]}),
    sender.pending?.() && jsxs('div', {children: [
      jsx('p', {children: 'This request was kept because its outcome is unknown. Reconciling resends the identical request, so the Ledger treats a duplicate as a no-op instead of a second change.'}),
      jsx(Button, {type: 'button', disabled: disabled || pending, onClick: () => submit(true), children: 'Reconcile same request'})]}),
    saved && jsx('p', {role: 'status', children: saved})]});
}

export function ProjectForm({sender, project, onSaved, disabled}) {
  const fields = project
    ? [{name: 'title', label: 'Project name', initial: base => base?.title || ''}, {name: 'reason', label: 'Reason for change'}]
    : [{name: 'project_id', label: 'Project ID', maxLength: 128}, {name: 'title', label: 'Project name'}];
  return CommandForm({sender, onSaved, disabled, base: project, fields,
    title: project ? 'Edit project' : 'Create project', submitLabel: 'Save project',
    build: (draft, pinned) => project
      ? ['edit_project', {project_id: pinned.project_id, title: draft.title,
        expected_revision: pinned.revision, reason: draft.reason}]
      : ['create_project', {project_id: draft.project_id, title: draft.title}]});
}

export function DeadlineForm({sender, projectId, onSaved, disabled}) {
  return CommandForm({sender, onSaved, disabled, base: null, title: 'Add a deadline',
    submitLabel: 'Create deadline',
    fields: [{name: 'deadline_id', label: 'Deadline ID', maxLength: 128},
      {name: 'kind', label: 'Deadline type', options: [['hard', 'Hard (external commitment)'], ['soft', 'Soft (internal target)']]},
      {name: 'at', label: 'Target date and time', type: 'datetime-local'},
      {name: 'timezone', label: 'Time zone', initial: () => 'UTC'},
      {name: 'owner', label: 'Deadline owner'}, {name: 'source', label: 'Deadline source'},
      {name: 'milestone_id', label: 'Milestone ID (optional)', required: false}],
    build: draft => ['set_deadline', {deadline_id: draft.deadline_id, project_id: projectId,
      kind: draft.kind, at: civil(draft.at), timezone: draft.timezone, owner: draft.owner,
      source: draft.source, ...(draft.milestone_id ? {milestone_id: draft.milestone_id} : {})}]});
}

export function ReviseForm({sender, commitment, onSaved, disabled}) {
  return CommandForm({sender, onSaved, disabled, base: commitment,
    title: `Reschedule ${commitment.kind} deadline ${commitment.deadline_id}`,
    submitLabel: 'Save new target',
    preamble: [jsxs('dl', {children: [
      jsx('dt', {children: 'Original target'}), jsx('dd', {children: date(commitment.original_at)}),
      jsx('dt', {children: 'Current target'}), jsx('dd', {children: date(commitment.target_at)}),
      jsx('dt', {children: 'Preserved'}), jsx('dd', {children: 'The original target is never overwritten; this records an audited revision.'})]})],
    fields: [{name: 'at', label: 'New target date and time', type: 'datetime-local', initial: () => ''},
      {name: 'timezone', label: 'Time zone', initial: base => base?.timezone || 'UTC'},
      {name: 'reason', label: 'Reason for the change'}],
    build: (draft, pinned) => ['revise_deadline', {deadline_id: pinned.deadline_id, at: civil(draft.at),
      timezone: draft.timezone, expected_revision: pinned.revision, reason: draft.reason}]});
}

// A milestone carries no revision of its own: the Ledger checks milestone edits
// and membership against the owning project's revision, so that is what these
// controls pin and submit.
export function MilestoneForm({sender, projectId, projectRevision, milestone, onSaved, disabled}) {
  const fields = milestone
    ? [{name: 'title', label: 'Milestone name', initial: () => milestone.title || ''}, {name: 'reason', label: 'Reason for the change'}]
    : [{name: 'milestone_id', label: 'Milestone ID', maxLength: 128}, {name: 'title', label: 'Milestone name'}];
  return CommandForm({sender, onSaved, disabled, fields,
    base: milestone ? {milestone_id: milestone.milestone_id, revision: projectRevision} : null,
    title: milestone ? 'Edit milestone' : 'Add a milestone', submitLabel: 'Save milestone',
    build: (draft, pinned) => milestone
      ? ['edit_milestone', {milestone_id: pinned.milestone_id, title: draft.title,
        expected_revision: pinned.revision, reason: draft.reason}]
      : ['create_milestone', {milestone_id: draft.milestone_id, project_id: projectId, title: draft.title}]});
}

// Only the first separator splits, because a task id may itself contain one.
// A line that does not name both halves is reported back, never guessed at: an
// absent separator would otherwise silently become a wrong board/task pair.
export function memberTasks(text) {
  const tasks = [];
  const rejected = [];
  for (const line of String(text ?? '').split('\n').map(entry => entry.trim()).filter(Boolean)) {
    const at = line.indexOf('/');
    if (at <= 0 || at === line.length - 1) rejected.push(line);
    else tasks.push([line.slice(0, at), line.slice(at + 1)]);
  }
  return {tasks, rejected};
}

export function MembershipForm({sender, milestoneId, projectRevision, tasks = [], onSaved, disabled}) {
  return CommandForm({sender, onSaved, disabled,
    base: {milestone_id: milestoneId, revision: projectRevision, tasks},
    title: `Milestone membership ${milestoneId}`, submitLabel: 'Save membership',
    fields: [{name: 'tasks', label: 'Member tasks, one board/task per line', multiline: true,
      maxLength: 65536, required: false, initial: base => base.tasks.map(pair => pair.join('/')).join('\n')},
      {name: 'reason', label: 'Reason for the change'}],
    validate: draft => {
      const {rejected} = memberTasks(draft.tasks);
      return rejected.length
        ? `Write each member task as board_id/task_id. Not understood: ${rejected.join(', ')}`
        : null;
    },
    build: (draft, pinned) => ['set_milestone_tasks', {milestone_id: pinned.milestone_id,
      tasks: memberTasks(draft.tasks).tasks,
      expected_revision: pinned.revision, reason: draft.reason}]});
}

export function MilestoneEditor({query, sender, disabled, onSaved}) {
  const milestone = query.data;
  if (!milestone) return jsx('p', {role: query.isError ? 'alert' : 'status',
    children: query.isError ? 'Milestone unavailable. Check the ID and retry.' : 'Loading milestone…'});
  const props = {sender, disabled: disabled || query.isError, onSaved,
    projectRevision: milestone.project_revision};
  return jsxs('section', {children: [
    query.isError && jsx('p', {role: 'alert', children: 'Milestone data is stale; editing is disabled.'}),
    jsx(MilestoneForm, {...props, milestone}, `title:${milestone.milestone_id}`),
    jsx(MembershipForm, {...props, milestoneId: milestone.milestone_id, tasks: milestone.tasks}, `members:${milestone.milestone_id}`)]});
}

const TABS = [['commitments', 'Commitments'], ['milestones', 'Milestones'],
  ['source-tasks', 'Source tasks'], ['history', 'History'], ['interventions', 'Interventions']];

export function DetailTabs({tab, onTab, query, children}) {
  return jsxs('section', {children: [
    jsx('div', {className: 'ue-toolbar', role: 'tablist', 'aria-label': 'Project detail views',
      children: TABS.map(([value, label]) => jsx(Button, {role: 'tab', 'aria-selected': tab === value,
        onClick: () => onTab(value), children: label}, value))}),
    query?.isPending ? jsx('p', {role: 'status', children: 'Loading records…'}) : null,
    query?.isError ? jsx('p', {role: 'alert', children: `Records unavailable. ${query.error?.message || ''}`}) : null,
    children]});
}

export function RecordList({query, empty, columns, row, onMore}) {
  const items = query?.data?.items;
  if (!items) return null;
  if (!items.length) return jsx('p', {children: empty});
  return jsxs('div', {className: 'ue-scroll', children: [jsxs('table', {children: [
    jsx('thead', {children: jsx('tr', {children: columns.map(label => jsx('th', {scope: 'col', children: label}, label))})}),
    jsx('tbody', {children: items.map((item, index) => jsx('tr', {children: row(item).map((value, column) =>
      jsx('td', {children: value === null || value === undefined ? 'Unknown' : String(value)}, columns[column]))}, index))})]}),
    query.data.has_more ? jsx(Button, {onClick: () => onMore?.(query.data.next),
      children: 'Next records'}) : jsx('p', {className: 'ue-note', children: 'End of recorded page.'})]});
}

export function Timeline({commitments, start, end, now, onSelect}) {
  const left = Date.parse(start), width = Date.parse(end) - left;
  const position = value => Math.max(0, Math.min(100, 100 * (Date.parse(value) - left) / width));
  const marker = (value, label, symbol, onClick) => value && jsx('button', {type: 'button', onClick,
    title: `${label}: ${date(value)}`, 'aria-label': `${label}: ${date(value)}`,
    style: {position: 'absolute', left: `${position(value)}%`, transform: 'translateX(-50%)', top: 0,
      minWidth: 44, minHeight: 44, color: 'var(--ui-text-primary)'}, children: symbol});
  return jsxs('section', {'aria-label': 'Commitment timeline', children: [
    jsx('p', {children: '◆ Hard target · ● Soft target · ◇ Forecast · ■ Actual · │ Original. Dates outside the range appear at its edge; inspect for the exact date.'}),
    jsx('div', {className: 'ue-scroll', tabIndex: 0, 'aria-label': 'Timeline, scroll horizontally', children: jsxs('div', {
      style: {minWidth: 720, padding: '0 24px'}, children: [
        jsxs('div', {className: 'ue-toolbar', style: {justifyContent: 'space-between'}, children: [date(start), date(end)]}),
        jsx('div', {style: {position: 'relative', height: 48, marginLeft: 180}, children: marker(now, 'Current time', '↓', () => {})}),
        ...commitments.map(item => jsxs('div', {style: {display: 'grid', gridTemplateColumns: '180px 1fr', minHeight: 52,
          borderBottom: '1px solid var(--ui-stroke-secondary)'}, children: [
          jsx(Button, {onClick: () => onSelect(item), children: item.deadline_id}),
          jsxs('div', {style: {position: 'relative'}, children: [
            marker(item.original_at, 'Original', '│', () => onSelect(item)),
            marker(item.target_at, item.kind === 'hard' ? 'Hard target' : 'Soft target', item.kind === 'hard' ? '◆' : '●', () => onSelect(item)),
            marker(item.forecast_at, 'Forecast', '◇', () => onSelect(item)),
            marker(item.actual_verified_at, 'Actual completion', '■', () => onSelect(item))]})]}, item.deadline_id))]})}),
    !commitments.length && jsx('p', {children: 'No commitments recorded. Add a hard deadline or soft target to begin scheduling.'})]});
}

const styles = `.ue {font:inherit;color:var(--ui-text-primary);padding:20px;overflow:auto;height:100%;font-size:16px;line-height:1.5}
.ue * {box-sizing:border-box}.ue h1{font-size:28px;margin:0 0 12px}.ue h2{font-size:22px;margin:24px 0 8px}
.ue button,.ue input,.ue select{min-height:44px}.ue button:focus-visible,.ue input:focus-visible,.ue select:focus-visible{outline:2px solid var(--ui-accent);outline-offset:2px}
.ue-alert{border:1px solid var(--ui-stroke-secondary);border-left-width:4px;padding:12px;margin:12px 0}.ue-alert pre{font:inherit;margin:8px 0 0}.ue-scroll{overflow:auto}.ue table{width:100%;border-collapse:collapse;text-align:left}.ue th,.ue td{padding:12px 16px 12px 0;vertical-align:top;border-bottom:1px solid var(--ui-stroke-secondary)}
.ue caption{text-align:left;padding:12px 0}.ue-toolbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.ue-note{padding:12px 0}.ue dl{display:grid;grid-template-columns:minmax(120px,1fr) 3fr;gap:8px}.ue dd{margin:0;overflow-wrap:anywhere}
.ue [role=progressbar]{min-width:120px}.ue form{max-width:640px}.ue label{display:grid;gap:4px;margin:12px 0}.ue input,.ue select{width:100%;color:inherit;background:var(--ui-bg);border:1px solid var(--ui-stroke-secondary);padding:8px;font:inherit}
@media(max-width:640px){.ue{padding:12px}.ue th,.ue td{padding-right:12px}.ue dl{grid-template-columns:1fr}.ue dd{margin-bottom:8px}}`;

const RANGES = [[6, '6 hours'], [24, '24 hours'], [168, '7 days'], [720, '30 days'], [2160, '90 days']];
const seconds = value => value === null || value === undefined ? 'Unknown'
  : `${(value / 3600).toFixed(1)} h`;

export function ProjectDetail({ctx, scope, sender, project, disabled, onSaved}) {
  const [tab, setTab] = useState('commitments');
  const [hours, setHours] = useState(168);
  const [inspected, setInspected] = useState(null);
  const [cursor, setCursor] = useState(null);
  const [milestone, setMilestone] = useState(null);
  const base = `/projects/${encodeURIComponent(project.project_id)}`;
  // One hook, one scoped key per tab: switching tabs reads the selected surface
  // and never renders another tab's rows.
  const records = useRead(ctx, tab === 'commitments' ? base
    : `${base}/${tab}?limit=100${cursor ? '&after=' + encodeURIComponent(cursor) : ''}`, scope);
  const milestoneRead = useRead(ctx, `${base}/milestones/${encodeURIComponent(milestone || '')}`, scope, !!milestone);
  const commitments = project.commitments || [];
  const chosen = commitments.find(item => item.deadline_id === inspected) || null;
  const tabs = {
    milestones: () => jsx(RecordList, {query: records, empty: 'No milestones recorded.', onMore: setCursor,
      columns: ['Milestone', 'Name', 'Revision'], row: item => [item.milestone_id, item.title, item.revision]}),
    'source-tasks': () => jsxs('section', {children: [
      jsx('p', {className: 'ue-note', children: records.data
        ? `Canonical sync cadence ${records.data.sync_seconds}s, distinct from this page's 10s read. Sync health: ${JSON.stringify(records.data.sync_health)}`
        : 'Reading canonical source coverage…'}),
      jsx(RecordList, {query: records, empty: 'No canonical source tasks bound to this project.', onMore: setCursor,
        columns: ['Board', 'Task', 'Status', 'Assignee', 'Goal'],
        row: item => [item.board_id, item.task_id, item.status, item.assignee, item.goal]})]}),
    history: () => jsx(RecordList, {query: records, empty: 'No recorded changes.', onMore: setCursor,
      columns: ['Sequence', 'At', 'Actor', 'Operation', 'Reason'],
      row: item => [item.sequence, item.occurred_at, item.actor, item.action, item.reason]}),
    interventions: () => jsxs('section', {children: [
      jsx('p', {className: 'ue-note', children: records.data
        ? `Recovery creation: ${records.data.recovery_creation}. ${records.data.next_action}`
        : 'Reading intervention receipts…'}),
      jsx(RecordList, {query: records, empty: 'No recorded interventions', onMore: setCursor,
        columns: ['Requested', 'Observed', 'Attributed effect', 'At'],
        row: item => [item.requested ?? item.action, item.observed, item.attributed_effect ?? item.outcome, item.at]})]}),
    commitments: () => jsxs('section', {children: [
      jsxs('fieldset', {children: [jsx('legend', {children: 'Timeline range'}), jsx('div', {className: 'ue-toolbar', children: RANGES.map(([value, label]) =>
        jsx(Button, {'aria-pressed': hours === value, onClick: () => setHours(value), children: label}, value))})]}),
      jsx(Timeline, {commitments, now: project.observed_at,
        start: new Date(Date.parse(project.observed_at) - hours * 360000).toISOString(),
        end: new Date(Date.parse(project.observed_at) + hours * 3240000).toISOString(),
        onSelect: item => setInspected(item.deadline_id)}),
      project.commitments_has_more && jsx('p', {role: 'status', children:
        `More commitments exist beyond the first ${commitments.length}; project risk is reported as ${project.risk_coverage}. Use the supported CLI to page the remainder.`}),
      ...commitments.map(commitment => jsxs('dl', {children: [
        jsx('dt', {children: `${commitment.kind} deadline`}), jsx('dd', {children: commitment.deadline_id}),
        jsx('dt', {children: 'Owner and source'}), jsx('dd', {children: `${commitment.owner} · ${commitment.source}`}),
        jsx('dt', {children: 'Original'}), jsx('dd', {children: date(commitment.original_at)}),
        jsx('dt', {children: 'Current target'}), jsx('dd', {children: date(commitment.target_at)}),
        jsx('dt', {children: 'Forecast'}), jsx('dd', {children: date(commitment.forecast_at)}),
        jsx('dt', {children: 'Verified completion'}), jsx('dd', {children: date(commitment.actual_verified_at)}),
        jsx('dt', {children: 'Variance against original'}), jsx('dd', {children: seconds(commitment.baseline_variance_seconds)}),
        jsx('dt', {children: 'Variance against target'}), jsx('dd', {children: seconds(commitment.target_variance_seconds)}),
        jsx('dt', {children: 'Urgency'}), jsx('dd', {children: `${levels[commitment.forecast?.level || 0]} · ${commitment.forecast?.reasons?.join('; ') || 'no recorded reason'}`}),
        jsx('dt', {children: 'Reschedule'}), jsx('dd', {children: jsx(Button, {
          'aria-pressed': inspected === commitment.deadline_id,
          onClick: () => setInspected(inspected === commitment.deadline_id ? null : commitment.deadline_id),
          children: inspected === commitment.deadline_id ? 'Close reschedule' : 'Reschedule this deadline'})})
      ]}, commitment.deadline_id))]})};
  return jsxs('section', {children: [
    jsx('h2', {children: project.title}), jsx(Progress, {progress: project.progress}),
    jsx('p', {children: `${risk(project)} · risk coverage ${project.risk_coverage}`}),
    jsx('p', {children: project.urgency?.reasons?.join('; ') || 'No single authoritative project risk measurement for this read.'}),
    jsx('dl', {children: [jsx('dt', {children: 'Weighted scope'}),
      jsx('dd', {children: `${project.progress?.verified_weight ?? 'unknown'} verified of ${project.progress?.current_weight ?? 'unknown'} current (baseline ${project.progress?.baseline_weight ?? 'none recorded'})`}),
      jsx('dt', {children: 'Last verified progress'}), jsx('dd', {children: date(project.progress?.last_verified_progress_at)})]}),
    jsx(DetailTabs, {tab, query: tab === 'commitments' ? null : records,
      onTab: value => {setCursor(null); setTab(value);}, children: tabs[tab]()}),
    chosen && jsx(ReviseForm, {sender, commitment: chosen, disabled, onSaved}, chosen.deadline_id),
    jsxs('details', {children: [jsx('summary', {children: 'Add a deadline'}),
      jsx(DeadlineForm, {sender, projectId: project.project_id, disabled, onSaved})]}),
    jsxs('details', {children: [jsx('summary', {children: 'Add a milestone'}),
      jsx(MilestoneForm, {sender, projectId: project.project_id, disabled, onSaved})]}),
    jsxs('details', {children: [jsx('summary', {children: 'Edit milestone membership'}),
      jsxs('label', {children: ['Milestone ID to edit', jsx(Input, {value: milestone || '',
        onChange: event => setMilestone(event.target.value)})]}),
      milestone
        ? jsx(MilestoneEditor, {sender, query: milestoneRead, disabled,
          onSaved: () => {onSaved(); milestoneRead.refetch(); records.refetch();}}, milestone)
        : jsx('p', {children: 'Name a milestone from the Milestones tab to edit its membership.'})]})]});
}

function Page({ctx}) {
  const scope = useScope();
  const senders = useRef(new Map());
  const key = JSON.stringify(scope);
  if (!senders.current.has(key)) senders.current.set(key, commandSender(ctx, scope));
  return jsx(ScopedPage, {ctx, scope, sender: senders.current.get(key)}, key);
}
function ScopedPage({ctx, scope, sender}) {
  const [selected, setSelected] = useState(null);
  const [after, setAfter] = useState(null);
  const gateway = useValue(host.state.gateway);
  const client = useQueryClient();
  useEffect(() => {
    let last = -Infinity;
    return ctx.onEvent('*', () => {
      if (!sameScope(scope) || host.state.gateway.get() !== 'open' || Date.now() - last < 5000) return;
      last = Date.now();
      // Mark stale, but let the bounded visible polling cadence perform I/O.
      // A busy gateway's event stream must not become one read per token.
      client.invalidateQueries({queryKey: ['urgency', scope.connection, scope.profile, scope.epoch], refetchType: 'none'});
    });
  }, [ctx, client, scope.connection, scope.profile, scope.epoch]);
  const path = selected ? `/projects/${encodeURIComponent(selected)}` : `/portfolio?limit=100${after ? '&after=' + encodeURIComponent(after) : ''}`;
  const query = useRead(ctx, path, scope);
  const inventory = useQuery({queryKey: ['urgency', scope.connection, scope.profile, scope.epoch, 'canonical'],
    queryFn: () => canonicalProjects(scope), retry: false, staleTime: 5000, enabled: !selected && gateway === 'open',
    refetchInterval: state => state.state.error ? 60000 : 30000, refetchIntervalInBackground: false});
  return jsxs('main', {className: 'ue', 'aria-label': 'Urgency Engine', children: [
    jsx('style', {children: styles}), jsx('h1', {children: 'Urgency Engine'}),
    jsx('p', {children: `${connectionLabel(scope.connection, gateway)} / ${scope.profile} · ${gateway}`}),
    jsxs('details', {children: [jsx('summary', {children: selected ? 'Edit project' : 'Create project'}),
      jsx(ProjectEditor, {selected, sender, project: selected ? query.data : null, disabled: gateway !== 'open' || query.isPending || query.isError,
        onSaved: () => query.refetch()}, selected || 'new')]}),
    selected && jsx(Button, {onClick: () => setSelected(null), children: 'Back to portfolio'}),
    query.isPending ? jsx('p', {role: 'status', children: 'Loading verified measurements…'}) : null,
    query.isError && jsxs('div', {role: 'alert', children: ['Measurements unavailable or stale. ', query.error?.message,
      jsx(Button, {onClick: () => query.refetch(), children: 'Retry read'})]}),
    query.data && jsxs('section', {children: [
      jsx('p', {className: 'ue-note', children: `Observed ${date(query.data.observed_at)} · Coverage: ${query.data.coverage}. Artifact byte identity does not prove semantic acceptance.`}),
      selected ? jsx(ProjectDetail, {ctx, scope, sender, project: query.data,
        disabled: gateway !== 'open' || query.isError || query.isPending, onSaved: () => query.refetch()}, selected) : jsxs('section', {children: [
          inventoryNotice(inventory) && jsx('p', {className: 'ue-note', role: 'status', children: inventoryNotice(inventory)}),
          jsx(Portfolio, {rows: mergeInventory(inventory.data, query.data.items), onSelect: setSelected}),
          !query.data.items.length && !inventory.data?.length && jsx('p', {children: 'No configured projects in this scope. Create a project to begin measuring verified work.'}),
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
