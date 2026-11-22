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
// Web Locks serialize same-origin Desktop windows; PluginStorage alone cannot
// compare-and-set. Unsupported hosts remain readable, never silently unlocked.
export function acquireWriter(scopeId, onChange, locks = globalThis.navigator?.locks) {
  let closed = false, release;
  onChange(false);
  if (scopeId && locks?.request) {
    const lifetime = new Promise(resolve => {release = resolve;});
    void locks.request(`hermes-urgency-engine:${scopeId}`, {ifAvailable: true}, async lock => {
      if (!lock || closed) return;
      onChange(true);
      await lifetime;
    }).catch(() => {if (!closed) onChange(false);});
  }
  return () => {closed = true; onChange(false); release?.();};
}
const activationInstance = crypto.randomUUID();
export function commandSender(ctx, scope, journal = null) {
  const scopeId = journal?.scopeId;
  const key = scopeId ? `pending:${scopeId}` : null;
  let record = key ? ctx.storage.get(key, null) : null;
  let retained = record?.payload || (record ? {unavailable: true} : null);
  let busy = false;
  function requireWriter() {
    if (journal?.writable && !journal.writable()) throw new Error('Read-only window. Open one management window for this scope.');
  }
  function bindingMatches() {
    return record?.version === 1 && record.scope_id === scopeId && record.profile === scope.profile
      && record.connection === scope.connection
      && (scope.connection !== null || (record.instance === activationInstance && record.epoch === scope.epoch));
  }
  function persist() {
    requireWriter();
    if (!journal) return;
    if (!scopeId) throw new Error('Initialize command recovery before editing.');
    if (!bindingMatches()) throw new Error('Retained scope is ambiguous or different from this scope. Nothing was sent.');
    if (!retained || retained.scope_id !== scopeId || typeof retained.request_id !== 'string'
      || !/^[A-Za-z0-9_.:-]{1,128}$/.test(retained.request_id) || typeof retained.operation !== 'string'
      || !retained.arguments || typeof retained.arguments !== 'object' || Array.isArray(retained.arguments)
      || Object.keys(retained).sort().join(',') !== 'arguments,operation,request_id,scope_id'
      || JSON.stringify(retained).length > 65536) throw new Error('Invalid retained payload. Nothing was sent.');
    const existing = ctx.storage.get(key, null);
    if (existing && JSON.stringify(existing) !== JSON.stringify(record)) throw new Error('Another retained command exists. Reload before reconciling.');
    ctx.storage.set(key, record);
    if (JSON.stringify(ctx.storage.get(key, null)) !== JSON.stringify(record)) throw new Error('Unable to persist command. Nothing was sent.');
  }
  function clear() {
    requireWriter();
    if (key) {
      if (JSON.stringify(ctx.storage.get(key, null)) !== JSON.stringify(record)) throw new Error('Stored command changed. Reload before reconciling.');
      ctx.storage.remove(key);
      if (ctx.storage.get(key, null) !== null) throw new Error('Unable to clear retained command. Reconcile the same request.');
    }
    retained = null; record = null;
  }
  async function transmit() {
    requireWriter();
    if (busy) throw new Error('Command already pending');
    if (!sameScope(scope)) throw new Error('Connection changed. Return to the original scope to reconcile.');
    busy = true;
    try {
      persist();
      if (journal) {
        const identity = await scopedRead(ctx, '/identity', scope);
        if (identity.scope_id !== scopeId) throw new Error('Backend identity changed. Retained request was not sent.');
        // Re-read after awaiting the network: another window may have changed
        // storage, or the user may have switched the selected gateway.
        persist();
      }
      if (!sameScope(scope)) throw new Error('Connection changed. Return to the original scope to reconcile.');
      const result = await ctx.rest('/commands', {method: 'POST', body: retained, timeoutMs: 15000});
      if (!sameScope(scope)) throw new Error('Connection changed. Return to the original scope to reconcile.');
      if (!result.ok) throw new Error(JSON.stringify(result));
      clear();
      return result.data;
    } catch (error) {
      // Only the host's structured no-effect receipt releases an intent. A
      // timeout, generic HTTP status or lost reply still requires exact replay.
      const message = String(error?.message || '');
      try {
        const envelope = JSON.parse(message.slice(message.indexOf('{')));
        if (envelope.ok === false && envelope.error?.side_effects === 'none' && envelope.error?.field !== 'scope_id') clear();
      } catch { /* An unstructured transport error has an unknown outcome. */ }
      throw error;
    } finally { busy = false; }
  }
  return {
    async send(operation, args) {
      requireWriter();
      if (retained) throw new Error('Reconcile the previous command before submitting another intent.');
      retained = JSON.parse(JSON.stringify({request_id: crypto.randomUUID(), operation, arguments: args,
        ...(journal ? {scope_id: scopeId} : {})}));
      record = {version: 1, scope_id: scopeId, connection: scope.connection, profile: scope.profile,
        instance: activationInstance, epoch: scope.epoch, payload: retained};
      return transmit();
    },
    async reconcile() {
      if (!retained) throw new Error('No command to reconcile');
      return transmit();
    },
    pending: () => !journal || !retained || bindingMatches() ? retained : {unavailable: true}
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
export function mergeInventory(canonical, measured, {measurementsComplete = true, canonicalAvailable = true} = {}) {
  const byId = new Map((measured || []).map(item => [item.project_id, item]));
  const rows = (canonical || []).map(project => {
    const match = byId.get(project.id) || null;
    byId.delete(project.id);
    return {key: project.id, canonical: project, measured: match,
      binding: match ? 'measured' : measurementsComplete ? 'not_configured' : 'measurement_not_loaded'};
  });
  for (const item of byId.values()) {
    rows.push({key: item.project_id, canonical: null, measured: item,
      binding: canonicalAvailable ? 'no_canonical_project' : 'canonical_unavailable'});
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
  measurement_not_loaded: 'Measurement not loaded on this page', canonical_unavailable: 'Canonical inventory unavailable',
  no_canonical_project: 'No canonical project in this scope'};

export function PortfolioFilters({onApply}) {
  const empty = {board: '', project: '', assignee: '', urgency: '', deadline_type: ''};
  const [draft, setDraft] = useState(empty);
  const fields = [['board', 'Board ID'], ['project', 'Project ID'], ['assignee', 'Assigned agent'],
    ['urgency', 'Urgency', [['', 'Any urgency'], ...levels.map((label, index) => [String(index), label])]],
    ['deadline_type', 'Deadline type', [['', 'Any deadline'], ['hard', 'Hard'], ['soft', 'Soft']]]];
  return jsxs('form', {className: 'ue-filters', onSubmit: event => {event.preventDefault(); onApply(draft);}, children: [
    ...fields.map(([name, label, options]) => jsxs('label', {children: [label,
      jsx(options ? 'select' : Input, {value: draft[name], maxLength: 128,
        onChange: event => setDraft(current => ({...current, [name]: event.target.value})),
        ...(options ? {children: options.map(([value, text]) => jsx('option', {value, children: text}, value))} : {})})]}, name)),
    jsx(Button, {type: 'submit', children: 'Apply filters'}),
    jsx(Button, {type: 'button', onClick: () => {setDraft(empty); onApply(empty);}, children: 'Clear filters'})]});
}

export function Portfolio({rows, onSelect, sort = 'risk'}) {
  const sorted = [...rows].sort((a, b) => {
    const name = row => row.canonical?.name || row.measured?.title || row.key;
    const value = row => sort === 'risk' ? -(row.measured?.urgency?.level || 0)
      : Date.parse(row.measured?.summary?.next_commitment?.target_at) || Infinity;
    return (sort === 'name' ? 0 : value(a) - value(b)) || name(a).localeCompare(name(b));
  });
  const fact = (label, value) => jsxs('div', {children: [jsx('strong', {children: `${label}: `}), value]});
  const commitment = item => item ? `${item.kind} · ${date(item.target_at)}` : 'None recorded / unknown';
  return jsx('div', {className: 'ue-scroll', children: jsxs('table', {className: 'ue-portfolio', children: [
    jsx('caption', {children: 'Projects · verified progress and deadline risk (sorted within this page)'}),
    jsx('thead', {children: jsx('tr', {children: ['Project', 'Verified progress', 'Urgency', 'Next commitment'].map(label => jsx('th', {scope: 'col', children: label}, label))})}),
    jsx('tbody', {children: sorted.map(row => {
      const item = row.measured, summary = item?.summary;
      const title = row.canonical?.name || item?.title;
      return jsxs('tr', {children: [
        cell(jsxs('div', {children: [item ? jsx(Button, {onClick: () => onSelect(item.project_id), children: title}) : title,
          fact('Measurement', `${bindings[row.binding]}${row.canonical?.archived ? ' · archived' : ''}`),
          summary && jsxs('details', {children: [jsx('summary', {children: 'Execution and forecast'}),
            fact('Recorded task states', Object.entries(summary.task_states || {}).map(([state, count]) => `${state}: ${count}`).join(' · ') || 'No tasks'),
            fact('Assigned', summary.assigned_agents?.join(', ') || 'None recorded'),
            fact('Recorded running', summary.recorded_running_agents?.join(', ') || 'None recorded'),
            fact('Forecast', date(summary.forecast_at)), fact('Forecast evidence', summary.forecast_evidence),
            fact('Hard deadline', commitment(summary.hard_deadline)), fact('Next milestone', commitment(summary.next_milestone)),
            fact('Last verified progress', date(item.progress?.last_verified_progress_at)),
            jsx('p', {children: 'Recorded Ledger state, not live worker liveness. Source tasks shows canonical run freshness.'}),
            summary.names_partial && jsx('p', {children: 'Only the first 100 board/agent names are listed.'})]})]})),
        cell(item ? jsxs('div', {children: [jsx(Progress, {progress: item.progress}),
          jsx('p', {children: `${item.progress?.verified_weight ?? 'Unknown'} verified of ${item.progress?.current_weight ?? 'unknown'} current weight`})]}) : 'Not measured'),
        cell(jsxs('div', {children: [item ? risk(item) : 'Unmeasured',
          fact('Schedule', summary?.schedule_status || 'unknown')]})),
        cell(commitment(summary?.next_commitment))
      ]}, row.key);
    })})]})});
}

export function PortfolioTimeline({projects, coordination, now, onSelect}) {
  const [page, setPage] = useState(0);
  const commitments = projects.flatMap(project => (project.commitments || [])
    .filter(item => item.kind === 'hard' || item.milestone_id)
    .map(item => ({...item, project_id: project.project_id, label: `${project.title} · ${item.deadline_id}`})));
  const offset = Math.min(page, Math.max(0, Math.ceil(commitments.length / 100) - 1)) * 100;
  return jsxs('section', {children: [
    jsx('h2', {children: 'Portfolio commitments and shared resources'}),
    jsx('p', {children: 'Loaded project and commitment pages only. Same UTC-day targets and shared assignments are potential contention, not proof of a capacity violation.'}),
    ...(coordination?.shared_agents || []).map(item => jsx('p', {children: `Shared agent ${item.agent}: ${item.projects.join(', ')}`}, item.agent)),
    ...(coordination?.same_day_targets || []).map(item => jsx('p', {children: `Deadline collision ${item.utc_day} (UTC): ${item.projects.join(', ')}`}, item.utc_day)),
    coordination?.truncated && jsx('p', {role: 'status', children: 'Coordination list truncated at 100 groups.'}),
    jsx(TimelineView, {commitments: commitments.slice(offset, offset + 100), now, onSelect: item => onSelect(item.project_id)}),
    offset > 0 && jsx(Button, {onClick: () => setPage(0), children: 'First portfolio commitments'}),
    offset + 100 < commitments.length && jsx(Button, {onClick: () => setPage(offset / 100 + 1), children: 'Next portfolio commitments'})]});
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
    base?.revision !== undefined && base.revision !== pinned?.revision && jsx('div', {className: 'ue-scroll',
      children: jsxs('table', {children: [jsx('caption', {children: 'Review changed fields before adopting the current revision'}),
        jsx('thead', {children: jsx('tr', {children: ['Field', 'Your draft', 'Current saved value'].map(label => jsx('th', {scope: 'col', children: label}, label))})}),
        jsx('tbody', {children: fields.filter(field => field.initial || field.name in base).map(field =>
          jsxs('tr', {children: [cell(field.label), cell(draft[field.name]),
            cell(field.initial ? field.initial(base) : base[field.name])]}, field.name))})]})}),
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

export function BaselineForm({project, sender, onSaved, disabled}) {
  return CommandForm({sender, onSaved, disabled, base: project, title: 'Record original scope',
    submitLabel: 'Record immutable baseline', fields: [{name: 'reason', label: 'Reason for baseline'}],
    preamble: [jsx('p', {children: 'This records the current weighted scope once. An existing baseline cannot be replaced.'})],
    build: (draft, pinned) => ['baseline_scope', {project_id: pinned.project_id,
      expected_revision: pinned.revision, reason: draft.reason}]});
}

export function CriteriaForm({task, sender, onSaved, disabled}) {
  return CommandForm({sender, onSaved, disabled, base: task, title: 'Edit acceptance criteria',
    submitLabel: 'Replace criteria and invalidate verification',
    preamble: [jsx('p', {children: 'Replacing criteria invalidates existing verification. This is not a completion control. Review the full replacement before saving.'}),
      jsx('p', {children: 'JSON array: each criterion needs criterion_id, description, positive weight, required (true/false), and expected_sha256 (a lowercase SHA-256 or null). Maximum 100 criteria; [] removes all criteria.'})],
    fields: [{name: 'criteria', label: 'Acceptance criteria (JSON)', multiline: true, maxLength: 65536,
      initial: base => JSON.stringify(base.criteria.map(item => ({criterion_id: item.criterion_id,
        description: item.description, weight: item.weight, required: !!item.required,
        expected_sha256: item.expected_sha256})), null, 2)},
      {name: 'reason', label: 'Reason for scope change'}],
    validate: draft => {
      try {if (Array.isArray(JSON.parse(draft.criteria))) return null;} catch { /* Show an inline input error. */ }
      return 'Acceptance criteria must be a JSON array. Nothing was sent.';
    },
    build: (draft, pinned) => ['set_criteria', {board_id: pinned.board_id, task_id: pinned.task_id,
      criteria: JSON.parse(draft.criteria), expected_revision: pinned.revision, reason: draft.reason}]});
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
  ['scope', 'Verified work'], ['source-tasks', 'Source tasks'], ['history', 'History'], ['interventions', 'Interventions']];

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
      jsx('td', {children: value === null || value === undefined ? 'Unknown' : value?.type ? value : String(value)}, columns[column]))}, index))})]}),
    query.data.has_more ? jsx(Button, {onClick: () => onMore?.(query.data.next),
      children: 'Next records'}) : jsx('p', {className: 'ue-note', children: 'End of recorded page.'})]});
}

export function Timeline({commitments, start, end, now, onSelect}) {
  const left = Date.parse(start), width = Date.parse(end) - left;
  const position = value => Math.max(0, Math.min(100, 100 * (Date.parse(value) - left) / width));
  const marker = (value, label, symbol, onClick, track = 0) => value && jsx('button', {type: 'button', onClick,
    title: `${label}: ${date(value)}`, 'aria-label': `${label}: ${date(value)}`,
    style: {position: 'absolute', left: `${position(value)}%`, transform: 'translateX(-50%)', top: track * 44,
      minWidth: 44, minHeight: 44, color: 'var(--ui-text-primary)'}, children: symbol});
  return jsxs('section', {'aria-label': 'Commitment timeline', children: [
    jsx('p', {children: '◆ Hard target · ● Soft target · ◇ Forecast · ■ Actual · │ Original. Dates outside the range appear at its edge; inspect for the exact date.'}),
    jsx('div', {className: 'ue-scroll', tabIndex: 0, 'aria-label': 'Timeline, scroll horizontally', children: jsxs('div', {
      style: {minWidth: 720, padding: '0 24px'}, children: [
        jsxs('div', {className: 'ue-toolbar', style: {justifyContent: 'space-between'}, children: [date(start), date(end)]}),
        jsx('div', {style: {position: 'relative', height: 48, marginLeft: 180}, children: marker(now, 'Current time', '↓', () => {})}),
        ...commitments.map(item => jsxs('div', {style: {display: 'grid', gridTemplateColumns: '180px 1fr', minHeight: 176,
          borderBottom: '1px solid var(--ui-stroke-secondary)'}, children: [
          jsx(Button, {onClick: () => onSelect(item), children: item.label || item.deadline_id}),
          jsxs('div', {style: {position: 'relative'}, children: [
            marker(item.original_at, 'Original', '│', () => onSelect(item)),
            marker(item.target_at, item.kind === 'hard' ? 'Hard target' : 'Soft target', item.kind === 'hard' ? '◆' : '●', () => onSelect(item), 1),
            marker(item.forecast_at, 'Forecast', '◇', () => onSelect(item), 2),
            marker(item.actual_verified_at, 'Actual completion', '■', () => onSelect(item), 3)]})]}, item.deadline_id))]})}),
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
export function TimelineView({commitments, now, onSelect}) {
  const [hours, setHours] = useState(168);
  const [center, setCenter] = useState(null);
  const anchor = center ?? Date.parse(now);
  const span = hours * 3600000;
  const zoom = factor => setHours(value => Math.max(1, Math.min(8784, value * factor)));
  function fit() {
    const times = commitments.flatMap(item => [item.original_at, item.target_at,
      item.forecast_at, item.actual_verified_at]).map(Date.parse).filter(Number.isFinite);
    if (!times.length) {setCenter(null); setHours(168); return;}
    const first = Math.min(...times), last = Math.max(...times);
    setCenter((first + last) / 2);
    setHours(Math.max(1, Math.min(8784, (last - first) / 3600000 * 1.1)));
  }
  return jsxs('section', {children: [
    jsxs('fieldset', {children: [jsx('legend', {children: 'Timeline range'}),
      jsx('div', {className: 'ue-toolbar', children: [
        ...RANGES.map(([value, label]) => jsx(Button, {'aria-pressed': hours === value,
          onClick: () => {setHours(value); setCenter(null);}, children: label}, value)),
        jsx(Button, {onClick: () => zoom(.5), children: 'Zoom in'}),
        jsx(Button, {onClick: () => zoom(2), children: 'Zoom out'}),
        jsx(Button, {onClick: () => setCenter(anchor - span / 2), children: 'Earlier'}),
        jsx(Button, {onClick: () => setCenter(anchor + span / 2), children: 'Later'}),
        jsx(Button, {onClick: fit, children: 'Fit commitments'})]})]}),
    jsx(Timeline, {commitments, now, onSelect, start: new Date(anchor - span / 2).toISOString(),
      end: new Date(anchor + span / 2).toISOString()})]});
}
const seconds = value => value === null || value === undefined ? 'Unknown'
  : `${(value / 3600).toFixed(1)} h`;

export function TaskScope({ctx, scope, sender, project, disabled, onSaved}) {
  const [after, setAfter] = useState(null);
  const [selected, setSelected] = useState(null);
  // Stable range key during polling. Undated task membership is not range-filtered.
  const [start] = useState(() => project.observed_at);
  const end = new Date(Date.parse(start) + 86400000).toISOString();
  const path = `/projects/${encodeURIComponent(project.project_id)}/timeline?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&limit=100${after ? '&after=' + encodeURIComponent(after) : ''}`;
  const query = useRead(ctx, path, scope);
  const task = query.data?.items.find(item => JSON.stringify([item.board_id, item.task_id]) === selected);
  const saved = () => {onSaved(); query.refetch();};
  return jsxs('section', {children: [
    jsx('h2', {children: 'Verified work and scope'}),
    query.isPending && jsx('p', {role: 'status', children: 'Loading task scope…'}),
    query.isError && jsx('p', {role: 'alert', children: 'Task scope unavailable or stale. Editing disabled.'}),
    ...(query.data?.items || []).map(item => jsx(Button, {
      onClick: () => setSelected(JSON.stringify([item.board_id, item.task_id])),
      children: `${item.board_id}/${item.task_id} · ${item.goal}`}, JSON.stringify([item.board_id, item.task_id]))),
    query.data?.items.length === 0 && jsx('p', {children: 'No measured tasks. Bind canonical work with the supported Kanban sync configuration.'}),
    after && jsx(Button, {onClick: () => {setAfter(null); setSelected(null);}, children: 'First tasks'}),
    query.data?.has_more && jsx(Button, {onClick: () => {setAfter(query.data.next); setSelected(null);}, children: 'Next tasks'}),
    task && jsxs('section', {children: [jsx('h3', {children: task.goal}),
      jsxs('dl', {children: [jsx('dt', {children: 'Recorded status'}), jsx('dd', {children: task.status}),
        jsx('dt', {children: 'Assigned agent'}), jsx('dd', {children: task.assignee || 'Unassigned'}),
        jsx('dt', {children: 'Measured execution'}), jsx('dd', {children: seconds(task.actual_seconds)}),
        jsx('dt', {children: 'Estimated remaining'}), jsx('dd', {children: seconds(task.remaining_seconds)}),
        jsx('dt', {children: 'Verified completion'}), jsx('dd', {children: date(task.actual_verified_at)})]}),
      jsx('h3', {children: 'Acceptance evidence'}),
      ...(task.criterion_evidence || []).map(item => jsxs('details', {children: [
        jsx('summary', {children: `${item.description} · ${item.verified ? 'Verified bytes' : 'Not verified'} · weight ${item.weight}${item.required ? ' · required' : ''}`}),
        jsxs('dl', {children: [jsx('dt', {children: 'Evidence receipt'}), jsx('dd', {children: item.evidence_id || 'None'}),
          jsx('dt', {children: 'Checked at'}), jsx('dd', {children: date(item.checked_at)}),
          jsx('dt', {children: 'Expected SHA-256'}), jsx('dd', {children: item.expected_sha256 || 'Unknown'}),
          jsx('dt', {children: 'Observed SHA-256'}), jsx('dd', {children: item.observed_sha256 || 'Unknown'})]})]}, item.criterion_id)),
      !task.criterion_evidence?.length && jsx('p', {children: 'No current acceptance evidence recorded.'}),
      jsx(CriteriaForm, {task, sender, disabled: disabled || query.isError || query.isPending, onSaved: saved}, selected)]}),
    jsx(BaselineForm, {project, sender, disabled, onSaved})]});
}

export function ProjectDetail({ctx, scope, sender, project, disabled, onSaved}) {
  const [tab, setTab] = useState('commitments');
  const [inspected, setInspected] = useState(null);
  const [cursor, setCursor] = useState(null);
  const [milestone, setMilestone] = useState(null);
  const base = `/projects/${encodeURIComponent(project.project_id)}`;
  // One hook, one scoped key per tab: switching tabs reads the selected surface
  // and never renders another tab's rows.
  const records = useRead(ctx, `${base}/${tab}?limit=100${cursor ? '&after=' + encodeURIComponent(cursor) : ''}`, scope, tab !== 'scope');
  const milestoneRead = useRead(ctx, `${base}/milestones/${encodeURIComponent(milestone || '')}`, scope, !!milestone);
  const commitments = tab === 'commitments' && records.data?.items
    ? records.data.items : cursor ? [] : project.commitments || [];
  const chosen = commitments.find(item => item.deadline_id === inspected) || null;
  const tabs = {
    scope: () => jsx(TaskScope, {ctx, scope, sender, project, disabled, onSaved}, project.project_id),
    milestones: () => jsx(RecordList, {query: records, empty: 'No milestones recorded.', onMore: setCursor,
      columns: ['Milestone', 'Name', 'Revision'], row: item => [item.milestone_id, item.title, item.revision]}),
    'source-tasks': () => jsxs('section', {children: [
      jsx('p', {className: 'ue-note', children: records.data
        ? `Canonical sync cadence ${records.data.sync_seconds}s, distinct from this page's 10s read. Sync health: ${JSON.stringify(records.data.sync_health)}`
        : 'Reading canonical source coverage…'}),
      jsx(RecordList, {query: records, empty: 'No canonical source tasks bound to this project.', onMore: setCursor,
        columns: ['Board', 'Task', 'Status', 'Assignee', 'Goal', 'Dependencies (same board)', 'Current run', 'Run start', 'Run end', 'Heartbeat', 'Source coverage'],
        row: item => [item.board_id, item.task_id, item.status, item.assignee, item.title,
          item.parents?.join(', ') || 'None recorded', item.worker?.run_id, item.worker?.started_at,
          item.worker?.ended_at, item.worker?.heartbeat_at,
          `${item.coverage} · ${item.stale ? 'stale' : 'observed'} ${item.observed_at}`]})]}),
    history: () => jsx(RecordList, {query: records, empty: 'No recorded changes.', onMore: setCursor,
      columns: ['Sequence', 'At', 'Actor', 'Operation', 'Reason', 'Change'],
      row: item => [item.sequence, item.occurred_at, item.actor, item.action, item.reason,
        jsxs('details', {children: [jsx('summary', {children: 'Inspect recorded change'}),
          jsx('pre', {style: {whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxWidth: 480},
            children: `Before: ${item.before_json ?? 'none'}\nAfter: ${item.after_json ?? 'none'}`})]})]}),
    interventions: () => jsxs('section', {children: [
      jsx('p', {className: 'ue-note', children: records.data
        ? `Recovery creation: ${records.data.recovery_creation}. ${records.data.next_action}`
        : 'Reading intervention receipts…'}),
      jsx(RecordList, {query: records, empty: 'No recorded interventions', onMore: setCursor,
        columns: ['Action', 'Kind', 'State', 'Requested', 'Observed', 'Attribution', 'Next action'],
        row: item => [item.action_id, item.kind, item.state, JSON.stringify(item.requested_effect),
          JSON.stringify(item.observed_effect), item.attribution, item.next_action]})]}),
    commitments: () => jsxs('section', {children: [
      jsx(TimelineView, {commitments, now: project.observed_at,
        onSelect: item => setInspected(item.deadline_id)}),
      cursor && jsx(Button, {onClick: () => {setCursor(null); setInspected(null);}, children: 'First commitments'}),
      records.data?.has_more && jsx(Button, {onClick: () => {setCursor(records.data.next); setInspected(null);}, children: 'Next commitments'}),
      project.commitments_has_more && jsx('p', {role: 'status', children:
        'Project risk covers only the summary page. Commitment pages do not establish a complete aggregate risk.'}),
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
    jsx(DetailTabs, {tab, query: tab === 'scope' ? null : records,
      onTab: value => {setCursor(null); setTab(value);}, children: tabs[tab]()}),
    chosen && jsx(ReviseForm, {sender, commitment: chosen, disabled: disabled || records.isError,
      onSaved: () => {onSaved(); records.refetch();}}, chosen.deadline_id),
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

export function RecoveryControls({ctx, scope, identity, sender, onSaved, readOnly = false}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const gateway = useValue(host.state.gateway);
  const lock = useRef(false);
  async function act(initialize) {
    if (lock.current || (!initialize && readOnly) || gateway !== 'open' || !sameScope(scope)) return;
    lock.current = true; setBusy(true); setError(null);
    try {
      if (initialize) {
        const result = await ctx.rest('/identity', {method: 'POST', timeoutMs: 15000});
        if (!sameScope(scope)) throw new Error('Connection changed. Read the current scope before continuing.');
        if (!result.ok) throw new Error(JSON.stringify(result));
        await identity.refetch();
      } else {
        await sender.reconcile();
        onSaved?.();
      }
    } catch (failure) {setError(commandFailure(failure).headline);}
    finally {lock.current = false; setBusy(false);}
  }
  const pending = sender.pending();
  return jsxs('section', {'aria-label': 'Command recovery', children: [
    identity.isError && jsx('p', {role: 'alert', children: 'Command recovery identity unavailable. Editing is disabled.'}),
    identity.data && !identity.data.scope_id && jsxs('div', {children: [
      jsx('p', {children: 'Enable command recovery before editing. Submitted command fields are retained in this Desktop app until their outcome is confirmed.'}),
      jsx(Button, {disabled: busy || gateway !== 'open', onClick: () => act(true), children: 'Enable command recovery'})]}),
    pending && jsxs('div', {children: [
      jsx('h2', {children: 'Retained command'}),
      pending.unavailable ? jsx('p', {role: 'alert', children: 'Retained scope is ambiguous or belongs to another gateway/profile. It has not been sent; return to the original scope.'})
        : jsxs('div', {children: [jsx('p', {children: 'Inspect current state and this exact request before replay. Nothing is sent automatically.'}),
          jsx('pre', {style: {whiteSpace: 'pre-wrap', overflowWrap: 'anywhere'}, children: JSON.stringify(pending, null, 2)}),
          jsx(Button, {disabled: readOnly || busy || gateway !== 'open' || identity.isError,
            onClick: () => act(false), children: 'Reconcile retained command'})]})]}),
    error && jsx('p', {role: 'alert', children: error})]});
}

function Page({ctx}) {
  const scope = useScope();
  const identity = useRead(ctx, '/identity', scope);
  const [writerKey, setWriterKey] = useState(null);
  const [attempt, setAttempt] = useState(0);
  const ownsWriter = useRef(false);
  const key = JSON.stringify([scope, identity.data?.scope_id || null]);
  useEffect(() => acquireWriter(identity.data?.scope_id, owns => {
    ownsWriter.current = owns;
    setWriterKey(owns ? key : null);
  }), [key, attempt]);
  const writable = writerKey === key;
  const sender = useRef(null);
  const senderKey = JSON.stringify([key, writable]);
  if (sender.current?.key !== senderKey) sender.current = {key: senderKey,
    value: commandSender(ctx, scope, {scopeId: identity.data?.scope_id || null,
      writable: () => ownsWriter.current && sameScope(scope)})};
  return jsx(ScopedPage, {ctx, scope, identity, sender: sender.current.value, writer: writable,
    requestWriter: () => setAttempt(value => value + 1)}, key);
}
function ScopedPage({ctx, scope, identity, sender, writer, requestWriter}) {
  const [selected, setSelected] = useState(null);
  const [after, setAfter] = useState(null);
  const [filters, setFilters] = useState({});
  const [sort, setSort] = useState('risk');
  const [inventoryPage, setInventoryPage] = useState(0);
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
  const filterQuery = new URLSearchParams(Object.entries(filters).filter(([, value]) => value)).toString();
  const path = selected ? `/projects/${encodeURIComponent(selected)}` : `/portfolio?limit=100${after ? '&after=' + encodeURIComponent(after) : ''}${filterQuery ? '&' + filterQuery : ''}`;
  const query = useRead(ctx, path, scope);
  const inventory = useQuery({queryKey: ['urgency', scope.connection, scope.profile, scope.epoch, 'canonical'],
    queryFn: () => canonicalProjects(scope), retry: false, staleTime: 5000, enabled: !selected && gateway === 'open',
    refetchInterval: state => state.state.error ? 60000 : 30000, refetchIntervalInBackground: false});
  const writable = writer && gateway === 'open' && !identity.isError && !!identity.data?.scope_id;
  const rows = selected ? [] : mergeInventory(inventory.data, query.data?.items,
    {measurementsComplete: !after && !query.data?.has_more && !filterQuery, canonicalAvailable: !inventory.isError && !inventory.isPending})
    .filter(row => row.measured || (!filters.assignee && !filters.urgency && !filters.deadline_type
      && (!filters.board || row.canonical?.board_slug === filters.board)
      && (!filters.project || row.key === filters.project)));
  return jsxs('main', {className: 'ue', 'aria-label': 'Urgency Engine', children: [
    jsx('style', {children: styles}), jsx('h1', {children: 'Urgency Engine'}),
    jsx('p', {children: `${connectionLabel(scope.connection, gateway)} / ${scope.profile} · ${gateway}`}),
    identity.data?.scope_id && !writer && jsxs('div', {role: 'status', children: [
      jsx('p', {children: 'Read-only window. Only one window can manage this scope. Close its Urgency page, then try here. Hosts without Web Locks remain read-only.'}),
      jsx(Button, {onClick: requestWriter, children: 'Try management in this window'})]}),
    jsx(RecoveryControls, {ctx, scope, identity, sender, readOnly: !writer, onSaved: () => query.refetch()}),
    jsxs('details', {children: [jsx('summary', {children: selected ? 'Edit project' : 'Create project'}),
      jsx(ProjectEditor, {selected, sender, project: selected ? query.data : null, disabled: !writable || query.isPending || query.isError,
        onSaved: () => query.refetch()}, selected || 'new')]}),
    selected && jsx(Button, {onClick: () => setSelected(null), children: 'Back to portfolio'}),
    query.isPending ? jsx('p', {role: 'status', children: 'Loading verified measurements…'}) : null,
    query.isError && jsxs('div', {role: 'alert', children: ['Measurements unavailable or stale. ', query.error?.message,
      jsx(Button, {onClick: () => query.refetch(), children: 'Retry read'})]}),
    query.data && jsxs('section', {children: [
      jsx('p', {className: 'ue-note', children: `Observed ${date(query.data.observed_at)} · Coverage: ${query.data.coverage}. Artifact byte identity does not prove semantic acceptance.`}),
      selected ? jsx(ProjectDetail, {ctx, scope, sender, project: query.data,
        disabled: !writable || query.isError || query.isPending, onSaved: () => query.refetch()}, selected) : jsxs('section', {children: [
          inventoryNotice(inventory) && jsx('p', {className: 'ue-note', role: 'status', children: inventoryNotice(inventory)}),
          jsx(PortfolioFilters, {onApply: value => {setFilters(value); setAfter(null); setInventoryPage(0);}}),
          jsx('p', {children: 'Filters use recorded measurements; unmeasured projects cannot match agent, urgency or deadline filters. Empty pages can have more matches. Clear filters to see the full canonical inventory.'}),
          jsxs('label', {children: ['Sort loaded page', jsx('select', {value: sort, onChange: event => setSort(event.target.value),
            children: [['risk', 'Urgency first'], ['name', 'Project name'], ['target', 'Earliest commitment']].map(([value, label]) => jsx('option', {value, children: label}, value))})]}),
          jsx(Portfolio, {rows: rows.slice(inventoryPage * 100, (inventoryPage + 1) * 100), sort, onSelect: setSelected}),
          inventoryPage > 0 && jsx(Button, {onClick: () => setInventoryPage(value => value - 1), children: 'Previous inventory rows'}),
          (inventoryPage + 1) * 100 < rows.length && jsx(Button, {onClick: () => setInventoryPage(value => value + 1), children: 'More inventory rows'}),
          jsx(PortfolioTimeline, {projects: query.data.items, coordination: query.data.coordination,
            now: query.data.observed_at, onSelect: setSelected}, path),
          !filterQuery && !query.data.has_more && !query.data.items.length && !inventory.data?.length && jsx('p', {children: 'No configured projects in this scope. Create a project to begin measuring verified work.'}),
          after && jsx(Button, {onClick: () => {setAfter(null); setInventoryPage(0);}, children: 'First page'}),
          query.data.has_more && jsx(Button, {onClick: () => {setAfter(query.data.next); setInventoryPage(0);}, children: 'Next projects'})]})]})]});
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
