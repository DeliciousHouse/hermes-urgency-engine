"""Compare fixed-order and urgency-guided execution on synthetic work only."""
import argparse
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any

if __name__ == '__main__':
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from urgency_engine import ForecastInput, UrgencyLevel, calculate_forecast
from urgency_engine.ledger import Ledger
from urgency_engine.watchdog import DEFAULTS, Monitor

START = datetime(2030, 1, 1, tzinfo=timezone.utc)
STEP = 30
PROOF = b'synthetic deliverable\n'


@dataclass(frozen=True)
class Task:
    name: str
    deadline: int
    duration: int
    estimate: int | None
    history: tuple[int, ...] = ()
    held: bool = False
    claim_only: bool = False


@dataclass(frozen=True)
class Fault:
    at: int
    task: str
    # None supplies unavailable-worker telemetry; an integer delays in-flight work.
    delay: int | None = None


@dataclass(frozen=True)
class Scenario:
    name: str
    tasks: tuple[Task, ...]
    expected_selection: tuple[tuple[str, ...], tuple[str, ...]]
    expected_completion: tuple[tuple[bool | None, ...], tuple[bool | None, ...]]
    horizon: int = 1800
    faults: tuple[Fault, ...] = ()
    expected_actions: tuple[tuple[str, str, str, str], ...] = ()
    expected_latency: int | None = None


SCENARIOS = {
    'nominal': Scenario('nominal', (Task('normal', 3600, 120, 120),
                                    Task('claim', 3600, 60, 60, claim_only=True)),
                        (('normal', 'claim'), ('normal', 'claim')), ((True, None), (True, None)), horizon=240),
    'stalled': Scenario('stalled', (Task('delayed', 3600, 300, 300),
                                    Task('held', 3600, 120, 120, held=True),
                                    Task('unknown', 3600, 120, None)),
                        (('delayed',), ('delayed',)), ((True, None, None), (True, None, None)), horizon=2160,
                        faults=(Fault(60, 'delayed', 1800), Fault(60, 'held'), Fault(60, 'unknown')),
                        expected_actions=(('delayed', 'stalled', 'inspect_reestimate', 'unsupported'),
                            ('held', 'delegated_work_unavailable', 'prioritize_critical_path', 'held'),
                            ('unknown', 'delegated_work_unavailable', 'prioritize_critical_path', 'unknown')),
                        expected_latency=1770),
    'pressure': Scenario('pressure', (
        Task('routine', 3600, 900, 900, (720, 810, 900, 990, 1080)),
        Task('urgent', 600, 300, 300),
    ), (('routine', 'urgent'), ('urgent', 'routine')), ((True, False), (True, True))),
}


def _run(case: Scenario, start: datetime, enabled: bool) -> dict[str, Any]:
    now = start
    clock = lambda: now
    selection, trace, forecasts = [], [], {}
    actions, seen, latency = [], set(), None
    operations = dict(forecast=0, watchdog_tick=0, observations=0, dispatch=0,
                      verification=0, claims=0, recommendations=0)
    faults = {offset: [f for f in case.faults if f.at == offset] for offset in {f.at for f in case.faults}}
    injected_delays = {}

    def log(event, **facts):
        trace.append(dict(at=now.isoformat(), event=event, **facts))

    with ExitStack() as stack:
        directory = stack.enter_context(TemporaryDirectory(prefix='urgency-simulation-'))
        root = Path(directory).resolve(strict=True)
        ledger_path = root / 'ledger.db'
        monitor = stack.enter_context(Monitor.open(root / 'monitor.db', clock=clock)) if enabled else None
        with Ledger(sqlite3.connect(ledger_path), clock=clock, artifact_root=root) as ledger:
            ledger.create_project('demo', 'Synthetic work', request_id='project', actor='fixture')

            def update(task, revision, status):
                return ledger.upsert_task('demo', task.name, project_id='demo', goal=task.name,
                    expected_revision=revision, status=status, remaining_seconds=task.estimate,
                    reason='Simulation', request_id=f'{status}-{task.name}', actor='fixture')

            records = {}
            for task in case.tasks:
                seeded = update(task, 0, 'planned')
                records[task.name] = ledger.set_criteria('demo', task.name, [dict(
                    criterion_id='proof', description='Synthetic artifact', weight=1, required=True,
                    expected_sha256=sha256(PROOF).hexdigest())], expected_revision=seeded['revision'],
                    reason='Simulation', request_id=f'criteria-{task.name}', actor='fixture')
                if task.held:
                    records[task.name] = update(task, records[task.name]['revision'], 'blocked')

            waiting = [task for task in case.tasks if not task.held and task.estimate is not None]
            running, finish = None, 0
            for offset in range(0, case.horizon + 1, STEP):
                now = start + timedelta(seconds=offset)
                for index, fault in enumerate(faults.get(offset, ())):
                    applied = running is not None and fault.task == running.name and fault.delay is not None
                    if applied and fault.delay is not None:
                        finish += fault.delay
                        injected_delays.setdefault(fault.task, offset)
                    log('fault', task=fault.task, delay_seconds=fault.delay,
                        delay_applied=applied, worker_unavailable=fault.delay is None)
                    if monitor is not None and fault.delay is None:
                        monitor.observe(dict(request_id=f'fault-{offset}-{index}', reason='Simulation',
                            project_id='demo', board_id='demo', task_id=fault.task, kind='task_signal',
                            occurred_at=now.isoformat(), data={'agent_available': False}), ledger_path)
                        operations['observations'] += 1
                if running is not None and offset == finish:
                    revision = records[running.name]['revision']
                    if running.claim_only:
                        ledger.claim_complete('demo', running.name, expected_revision=revision,
                            reason='Simulation', request_id=f'claim-{running.name}', actor='fixture-worker')
                        operations['claims'] += 1
                        log('claimed', task=running.name)
                    else:
                        (root / 'proof').write_bytes(PROOF)
                        operations['verification'] += 1
                        ledger.verify_artifact('demo', running.name, 'proof', 'proof',
                            expected_revision=revision, reason='Simulation',
                            request_id=f'verify-{running.name}', actor='fixture-verifier')
                    record = ledger.get_task('demo', running.name)
                    if record['status'] == 'verified' and record['actual_verified_at']:
                        log('verified', task=running.name, ledger_at=record['actual_verified_at'])
                    running = None
                if monitor is not None and offset % 60 == STEP:
                    tick = monitor.tick(ledger_path, DEFAULTS, force=True)
                    operations['watchdog_tick'] += 1
                    if not tick['evaluated'] or not tick['scan_complete']:
                        raise RuntimeError('synthetic watchdog scan incomplete')
                    incident_page = monitor.page('incidents', active=True)
                    plan_page = monitor.page('plans', active=True)
                    incidents, plan_rows = incident_page['items'], plan_page['items']
                    if (incident_page['has_more'] or plan_page['has_more']
                            or not isinstance(incidents, list) or not isinstance(plan_rows, list)):
                        raise RuntimeError('synthetic watchdog evidence incomplete')
                    plans = {p['incident_id']: p for p in plan_rows}
                    for incident in incidents:
                        identity = incident['incident_id']
                        if identity in seen:
                            continue
                        seen.add(identity)
                        name, detector = incident['task_id'], incident['detector']
                        log('watchdog_detected', task=name, detector=detector, incident_id=identity,
                            facts=incident['explanation']['facts'])
                        if detector == 'stalled' and name in injected_delays and latency is None:
                            detected = datetime.fromisoformat(incident['first_seen'])
                            latency = (detected - start).total_seconds() - injected_delays[name]
                        task = ledger.get_task('demo', name)
                        fence = ('held' if task['status'] == 'blocked' else
                                 'unknown' if not task['remaining_known'] else None)
                        action = 'prioritize_critical_path' if fence else 'inspect_reestimate'
                        plan = plans.get(identity, {})
                        offered = [a['action'] for a in plan.get('actions', {}).get('actions', [])]
                        disposition = (fence or 'unsupported') if action in offered else 'unknown_recommendation'
                        log('watchdog_action', task=name, detector=detector, incident_id=identity,
                            plan_id=plan.get('plan_id'), offered_actions=offered,
                            action=action, disposition=disposition, applied=False)
                        actions.append((name, detector, action, disposition))
                if running is not None or not waiting or offset == case.horizon:
                    continue
                log('observation', ready=[task.name for task in waiting])
                chosen = waiting[0]
                if enabled:
                    ranks = {}
                    predictions = {}
                    for task in waiting:
                        prediction = calculate_forecast(ForecastInput(
                            start + timedelta(seconds=task.deadline), task.estimate,
                            completion_samples_seconds=task.history), now=now)
                        operations['forecast'] += 1
                        ranks[task.name] = prediction.level
                        predictions[task.name] = prediction
                        log('forecast', task=task.name, level=prediction.level.name,
                            evidence=prediction.evidence, slack_seconds=prediction.deadline_slack_seconds)
                    # Synthetic dispatch rule; equal urgency preserves fixed order.
                    chosen = max(waiting, key=lambda task: ranks[task.name])
                    prediction = predictions[chosen.name]
                    forecasts[chosen.name] = (now, prediction)
                    operations['recommendations'] += 1
                    log('recommendation', source='forecast', action='select', task=chosen.name,
                        fixed_order_next=waiting[0].name, level=ranks[chosen.name].name)
                waiting.remove(chosen)
                running, finish = chosen, offset + chosen.duration
                selection.append(chosen.name)
                operations['dispatch'] += 1
                records[chosen.name] = update(chosen, records[chosen.name]['revision'], 'running')
                log('selected', task=chosen.name)

            outcomes = []
            for task in case.tasks:
                record = ledger.get_task('demo', task.name)
                raw = record['actual_verified_at'] if record['status'] == 'verified' else None
                verified = datetime.fromisoformat(raw) if raw else None
                outcomes.append(dict(task=task.name, status=record['status'], verified_at=raw,
                    on_time=(verified <= start + timedelta(seconds=task.deadline)) if verified else None))
    quantiles = {}
    for key in ('p50', 'p90'):
        eligible = []
        for row in outcomes:
            name = row['task']
            if row['verified_at'] is None or name not in forecasts:
                continue
            selected_at, forecast = forecasts[name]
            predicted = getattr(forecast, f'{key}_seconds')
            if predicted is not None:
                actual = (datetime.fromisoformat(row['verified_at']) - selected_at).total_seconds()
                eligible.append(dict(task=name, predicted_seconds=predicted, actual_seconds=actual,
                                     within=actual <= predicted))
        quantiles[key] = dict(total=len(outcomes), missing=len(outcomes) - len(eligible), eligible=eligible)
    return dict(selection=selection, trace=trace, operations=operations, quantiles=quantiles,
                watchdog_actions=sorted(actions), detection_latency_seconds=latency, completion=dict(
        verified_on_time=sum(row['on_time'] is True for row in outcomes), total=len(outcomes),
        unverified=sum(row['verified_at'] is None for row in outcomes), tasks=outcomes))


def run_pair(baseline: Scenario, enabled: Scenario, *, clock=lambda: START) -> dict[str, Any]:
    if baseline != enabled:
        raise ValueError('paired inputs must be identical')
    times_on_grid = [baseline.horizon, *(t.duration for t in baseline.tasks),
                     *(f.at for f in baseline.faults),
                     *(f.delay for f in baseline.faults if f.delay is not None)]
    if any(type(n) is not int or n < 0 or n % STEP for n in times_on_grid):
        raise ValueError('simulation times must be nonnegative integers on the 30-second grid')
    if (baseline.horizon == 0 or any(t.duration == 0 for t in baseline.tasks)
            or any(f.at > baseline.horizon for f in baseline.faults)):
        raise ValueError('simulation requires positive durations and faults within the horizon')
    start = clock()
    if start.utcoffset() != timedelta(0):
        raise ValueError('simulation clock must supply UTC')
    arms, times = [], {}
    for index, name in enumerate(('baseline', 'enabled')):
        started = perf_counter()
        arm = _run(baseline, start, enabled=index == 1)
        times[name] = perf_counter() - started
        expected = list(baseline.expected_selection[index])
        actions = sorted(baseline.expected_actions) if index else []
        latency = baseline.expected_latency if index else None
        expected_completion = list(baseline.expected_completion[index])
        arms.append(dict(arm=name, **arm, correctness=dict(expected_selection=expected,
            expected_watchdog_actions=actions, expected_detection_latency_seconds=latency,
            expected_on_time_by_task=dict(zip((t.name for t in baseline.tasks), expected_completion)),
            correct=(arm['selection'] == expected and arm['watchdog_actions'] == actions
                     and arm['detection_latency_seconds'] == latency
                     and [row['on_time'] for row in arm['completion']['tasks']] == expected_completion))))
    return dict(scenario=baseline.name, arms=arms, correct=all(a['correctness']['correct'] for a in arms),
                wall_clock_seconds_nondeterministic=times)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', choices=SCENARIOS, help='Run one pair (default: all)')
    args = parser.parse_args()
    cases = (SCENARIOS[args.scenario],) if args.scenario else SCENARIOS.values()
    pairs = [run_pair(case, case) for case in cases]
    print(json.dumps(dict(schema_version=1, pairs=pairs), indent=2, allow_nan=False))
    return int(not all(pair['correct'] for pair in pairs))


if __name__ == '__main__':
    raise SystemExit(main())
