"""protects=source-specific persisted recovery; fails_when=absence resolves episodes;
why_new=positive detector tests missed false recovery; seam=SQLite/fixed clock;
no_claim=host delivery or independently reviewed acceptance.
"""
from datetime import timedelta
from itertools import product
import unittest
import test_watchdog as fixtures


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.WatchdogTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def state(self, detector):
        with self.f.monitor(True) as monitor:
            plans = {p['incident_id']: p['state'] for p in monitor.page('plans')['items']}
            return sorted((i['episode'], i['incident_id'], i['state'], plans[i['incident_id']],
                           i['notice_count'], i['next_notice_at'])
                          for i in monitor.page('incidents')['items'] if i['detector'] == detector)

    def signal(self, **data):
        self.serial = getattr(self, 'serial', 0) + 1
        return self.f.observe(data, event='matrix-' + str(self.serial))

    def status(self, status):
        with self.f.ledger() as ledger:
            task = ledger.get_task('board', 'task')
            ledger.upsert_task('board', 'task', project_id='demo', goal=task['goal'], status=status,
                remaining_seconds=600, expected_revision=task['revision'], reason='fixture state',
                request_id='status-' + str(task['revision']), actor='fixture')

    def advance(self, seconds):
        self.f.now += timedelta(seconds=seconds)

    def recovery_outcome(self, identity, outcome):
        self.serial = getattr(self, 'serial', 0) + 1
        self.f.observe({'incident_id': identity, 'outcome': outcome}, kind='recovery_outcome',
                       event='recovery-' + str(self.serial))

    def scenario(self, detector):
        f = self.f
        if detector == 'stalled':
            f.tick()
            self.advance(1801)
            def recur():
                self.status('running')
                f.tick()
                self.advance(1801)
            return lambda: self.status('blocked'), recur
        if detector == 'stale_claim':
            self.signal(heartbeat_at=(f.now - timedelta(seconds=901)).isoformat())
            return lambda: self.signal(heartbeat_at=f.now.isoformat()), lambda: self.advance(901)
        if detector == 'missed_checkpoint':
            self.signal(checkpoint_at=(f.now - timedelta(seconds=1)).isoformat())
            return lambda: self.signal(checkpoint_at=(f.now + timedelta(seconds=60)).isoformat()), lambda: self.advance(61)
        if detector == 'delegated_work_unavailable':
            self.signal(agent_available=False)
            return lambda: self.signal(agent_available=True), lambda: self.signal(agent_available=False)
        if detector == 'dependency_unreachable':
            self.signal(dependency_state='unreachable')
            return lambda: self.signal(dependency_state='available'), lambda: self.signal(dependency_state='unreachable')
        if detector == 'dependency_cycle':
            self.signal(dependencies=[['board', 'task']])
            return lambda: self.signal(dependencies=[]), lambda: self.signal(dependencies=[['board', 'task']])
        if detector == 'repeated_tool_failure':
            self.signal(tool_failures=3)
            return lambda: self.signal(tool_failures=0), lambda: self.signal(tool_failures=3)
        if detector == 'ineffective_recovery':
            self.signal(agent_available=False)
            identity = f.tick()['notices'][0]['incident_id']
            def recur():
                for _ in range(3):
                    self.recovery_outcome(identity, 'failed')
            recur()
            return lambda: self.recovery_outcome(identity, 'improved'), recur
        if detector in ('expired_deadline', 'forecast_miss'):
            with f.ledger() as ledger:
                ledger.set_deadline('due', project_id='demo', kind='hard',
                    at=(f.now + timedelta(seconds=0 if detector == 'expired_deadline' else 86400)).isoformat(),
                    timezone='UTC', owner='fixture', source='fixture', request_id='due', actor='fixture')
            f.tick()
            self.advance(601)
            def recover():
                with f.ledger() as ledger:
                    ledger.revise_deadline('due', at=(f.now + timedelta(seconds=600)).isoformat(), timezone='UTC',
                        intent='operator', expected_revision=1, reason='Explicit replan', request_id='replan', actor='fixture')
            return recover, lambda: self.advance(601)
        self.fail('Uncovered detector: ' + detector)

    def test_every_detector_persisted_transition_matrix(self):
        inventory = {'stalled', 'missed_checkpoint', 'expired_deadline', 'dependency_cycle',
                     'repeated_tool_failure', 'delegated_work_unavailable', 'stale_claim',
                     'dependency_unreachable', 'forecast_miss', 'ineffective_recovery'}
        seen = set()
        for detector in sorted(inventory):
            with self.subTest(detector=detector):
                self.setUp()
                f = self.f
                recover, recur = self.scenario(detector)
                f.tick()
                initial = self.state(detector)
                self.assertEqual(len(initial), 1)
                self.assertEqual(initial[0][2:5], ('active', 'recommended', 1))
                # Actual loss of an authoritative snapshot row, not a mocked detector.
                table = 'deadlines' if detector in ('expired_deadline', 'forecast_miss') else 'tasks'
                with f.ledger() as ledger, ledger.connection:
                    row = tuple(ledger.connection.execute('SELECT * FROM ' + table).fetchone())
                    ledger.connection.execute('DELETE FROM ' + table)
                self.advance(300)
                notices = f.tick()['notices']
                self.assertFalse(any(n['detector'] == detector for n in notices))
                self.assertEqual(self.state(detector), initial)
                with f.ledger() as ledger, ledger.connection:
                    ledger.connection.execute('INSERT INTO ' + table + ' VALUES(' + ','.join('?' for _ in row) + ')', row)
                f.tick()
                restored = self.state(detector)
                self.assertEqual(restored[0][1], initial[0][1])
                self.assertEqual(restored[0][4], 2)
                recover()
                f.tick()
                self.assertEqual(self.state(detector)[0][2:5], ('resolved', 'resolved', 2))
                f.tick()
                self.assertEqual(len(self.state(detector)), 1)
                recur()
                f.tick()
                final = self.state(detector)
                self.assertEqual([(r[0], r[2], r[3]) for r in final],
                                 [(1, 'resolved', 'resolved'), (2, 'active', 'recommended')])
                self.assertNotEqual(final[0][1], final[1][1])
                seen.add(detector)
        self.assertEqual(seen, inventory)

    def criteria(self):
        f = self.f
        with f.ledger() as ledger:
            revision = ledger.get_task('board', 'task')['revision']
            ledger.set_criteria('board', 'task', [dict(criterion_id=name, description=name, weight=1,
                required=True, expected_sha256=fixtures.sha256(name.encode()).hexdigest()) for name in ('one', 'two')],
                expected_revision=revision, reason='fixture', request_id='criteria', actor='fixture')
        for name in ('one', 'two'):
            (f.root / name).write_bytes(name.encode())
        self.status('running')

    def verify(self, name):
        with self.f.ledger() as ledger:
            revision = ledger.get_task('board', 'task')['revision']
            ledger.verify_artifact('board', 'task', name, name, expected_revision=revision,
                                   reason='fixture', request_id='verify-' + name, actor='fixture')

    def test_partial_verified_progress_only_clears_stalled(self):
        f = self.f
        self.criteria()
        self.signal(agent_available=False, dependency_state='unreachable', dependencies=[['board', 'task']],
                    heartbeat_at=f.now.isoformat(), checkpoint_at=f.now.isoformat(), tool_failures=3)
        f.tick()
        self.advance(1801)
        f.tick()
        protected = ('delegated_work_unavailable', 'dependency_unreachable', 'dependency_cycle',
                     'stale_claim', 'missed_checkpoint', 'repeated_tool_failure')
        before = {d: self.state(d) for d in protected}
        self.assertEqual(self.state('stalled')[0][2], 'active')
        self.verify('one')
        f.tick()
        self.assertEqual(self.state('stalled')[0][2:4], ('resolved', 'resolved'))
        for detector in protected:
            current = self.state(detector)
            self.assertEqual(current[0][1:4], before[detector][0][1:4], detector)
        with f.ledger() as ledger:
            task = ledger.get_task('board', 'task')
            ledger.claim_complete('board', 'task', expected_revision=task['revision'], reason='claim only',
                                  request_id='claim', actor='fixture')
        f.tick()
        for detector in protected:
            self.assertEqual(self.state(detector)[0][2:4], ('active', 'recommended'), detector)
        self.verify('two')
        f.tick()
        for detector in protected:
            self.assertEqual(self.state(detector)[0][2:4], ('resolved', 'resolved'), detector)
        with f.ledger() as ledger:
            task = ledger.get_task('board', 'task')
            ledger.reopen_task('board', 'task', expected_revision=task['revision'], reason='new work',
                               request_id='reopen', actor='fixture')
        f.tick()
        # Reopening is not permission to replay old unavailable observations.
        self.assertEqual(len(self.state('delegated_work_unavailable')), 1)
        self.signal(agent_available=False)
        f.tick()
        self.assertEqual(len(self.state('delegated_work_unavailable')), 2)

    def test_older_heartbeat_cannot_reopen_resolved_episode(self):
        # Older heartbeat timestamps in newer envelopes must not change persisted plans or notices.
        f = self.f
        stale = (f.now - timedelta(seconds=901)).isoformat()
        self.signal(heartbeat_at=stale)
        f.tick()
        self.assertEqual(self.state('stale_claim')[0][2:5], ('active', 'recommended', 1))
        self.advance(1)
        self.signal(heartbeat_at=f.now.isoformat())
        f.tick()
        resolved = self.state('stale_claim')
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0][2:5], ('resolved', 'resolved', 1))
        for missing in (False, True):
            if missing:
                self.advance(1)
                self.signal(heartbeat_at=None)
                self.advance(900)
                self.assertFalse(any(n['detector'] == 'stale_claim' for n in f.tick()['notices']))
            self.advance(1)
            self.assertFalse(self.signal(heartbeat_at=stale)['out_of_order'])
            self.assertFalse(any(n['detector'] == 'stale_claim' for n in f.tick()['notices']))
            self.assertEqual(self.state('stale_claim'), resolved)
        self.advance(1)
        self.signal(heartbeat_at=f.now.isoformat())
        f.tick()
        self.assertEqual(self.state('stale_claim'), resolved)
        self.advance(900)
        notices = [n for n in f.tick()['notices'] if n['detector'] == 'stale_claim']
        self.assertEqual(len(notices), 1)
        final = self.state('stale_claim')
        self.assertEqual(final[0], resolved[0])
        self.assertEqual((final[1][0], *final[1][2:5]), (2, 'active', 'recommended', 1))

    def test_null_heartbeat_cannot_reveal_an_older_healthy_claim(self):
        # protects=fallback freshness; fails_when=a cached claim clears the fault;
        # why_new=equal claim replay; seam=clock/SQLite; no_claim=worker health.
        f = self.f
        self.signal(heartbeat_at=(f.now - timedelta(seconds=901)).isoformat(), claim_at=f.now.isoformat())
        f.tick()
        before = self.state('stale_claim')
        self.signal(heartbeat_at=None)
        f.tick()
        self.assertEqual(self.state('stale_claim'), before)
        self.signal(claim_at=f.now.isoformat())
        f.tick()
        self.assertEqual(self.state('stale_claim'), before)
        self.advance(1)
        self.signal(claim_at=f.now.isoformat())
        f.tick()
        self.assertEqual(self.state('stale_claim')[0][2:4], ('resolved', 'resolved'))

    def test_timestamp_replay_cannot_restore_coverage_after_null(self):
        # protects=episode/plan/notice continuity; fails_when=equal sample remints proof;
        # why_new=claim path and equal recovery replay; seam=reopened SQLite; no_claim=delivery.
        for field in ('heartbeat_at', 'claim_at'):
            with self.subTest(field=field):
                self.setUp()
                f = self.f
                self.signal(**{field: (f.now - timedelta(seconds=901)).isoformat()})
                f.tick()
                self.advance(5)
                sample = f.now - timedelta(seconds=2)
                self.signal(**{field: sample.isoformat()})
                f.tick()
                resolved = self.state('stale_claim')
                self.assertEqual(resolved[0][2:5], ('resolved', 'resolved', 1))
                self.advance(1)
                self.signal(**{field: None})
                self.advance(900)
                for timestamp in (sample.isoformat().replace('+00:00', 'Z'),
                                  (sample - timedelta(seconds=1)).isoformat()):
                    self.assertFalse(self.signal(**{field: timestamp})['out_of_order'])
                    self.assertFalse(any(n['detector'] == 'stale_claim' for n in f.tick()['notices']))
                    self.assertEqual(self.state('stale_claim'), resolved)
                self.signal(**{field: (sample + timedelta(seconds=1)).isoformat()})
                self.assertEqual([n['detector'] for n in f.tick()['notices']], ['stale_claim'])
                final = self.state('stale_claim')
                self.assertEqual(final[0], resolved[0])
                self.assertEqual((final[1][0], *final[1][2:5]), (2, 'active', 'recommended', 1))

    def test_continuous_timestamp_ages_at_threshold_without_refresh(self):
        # protects=continuous aging; fails_when=equal delivery resets age or suppresses stale;
        # why_new=both timestamp sources at exact threshold; seam=clock/SQLite; no_claim=gateway health.
        for field in ('heartbeat_at', 'claim_at'):
            with self.subTest(field=field):
                self.setUp()
                f = self.f
                sample = (f.now - timedelta(seconds=3)).isoformat()
                self.signal(**{field: sample})
                f.tick()
                self.advance(896)
                self.signal(**{field: sample})
                f.tick()
                self.assertEqual(self.state('stale_claim'), [])
                self.advance(1)
                self.assertEqual([n['detector'] for n in f.tick()['notices']], ['stale_claim'])
                self.assertEqual(self.state('stale_claim')[0][2:5], ('active', 'recommended', 1))

    def test_switching_timestamp_sources_cannot_restore_lost_coverage(self):
        # protects=unknown across fallback; fails_when=another field relabels a lost sample;
        # why_new=no previously evaluated fault to fence the replay; seam=clock/SQLite; no_claim=remote delivery.
        for primary, alternate in (('heartbeat_at', 'claim_at'), ('claim_at', 'heartbeat_at')):
            with self.subTest(primary=primary):
                self.setUp()
                f = self.f
                sample = f.now
                self.signal(**{primary: sample.isoformat()})
                self.advance(1)
                self.signal(**{primary: None})
                self.advance(900)
                self.signal(**{alternate: sample.isoformat()})
                f.tick()
                self.assertEqual(self.state('stale_claim'), [])
                self.signal(**{alternate: (sample + timedelta(seconds=1)).isoformat()})
                self.assertIn('stale_claim', [n['detector'] for n in f.tick()['notices']])

    def test_repeated_null_does_not_move_cross_field_coverage_gap(self):
        # protects=episode/plan/notice continuity; fails_when=repeated null moves the gap;
        # why_new=repeat after alternate replay and after valid fallback; seam=SQLite; no_claim=delivery.
        for primary, alternate in (('claim_at', 'heartbeat_at'), ('heartbeat_at', 'claim_at')):
            with self.subTest(primary=primary):
                self.setUp()
                f = self.f
                self.signal(**{primary: (f.now - timedelta(seconds=901)).isoformat()})
                self.assertEqual([n['detector'] for n in f.tick()['notices']], ['stale_claim'])
                self.advance(1)
                sample = f.now.isoformat()
                self.signal(**{primary: sample})
                f.tick()
                resolved = self.state('stale_claim')
                self.assertEqual(len(resolved), 1)
                self.assertEqual(resolved[0][2:5], ('resolved', 'resolved', 1))

                self.advance(1)
                self.signal(**{primary: None})
                self.assertEqual(f.tick()['notices'], [])
                self.advance(900)
                self.signal(**{alternate: sample})
                self.assertEqual(f.tick()['notices'], [])
                for _ in range(2):
                    self.advance(1)
                    self.signal(**{primary: None})
                    self.assertEqual(f.tick()['notices'], [])
                    self.assertEqual(self.state('stale_claim'), resolved)

                self.advance(1)
                self.signal(**{alternate: f.now.isoformat()})
                self.assertEqual(f.tick()['notices'], [])
                self.advance(900)
                self.signal(**{primary: None})
                notices = [n for n in f.tick()['notices'] if n['detector'] == 'stale_claim']
                self.assertEqual(len(notices), 1)
                final = self.state('stale_claim')
                self.assertEqual(len(final), 2)
                self.assertEqual(final[0], resolved[0])
                self.assertEqual((final[1][0], *final[1][2:5]), (2, 'active', 'recommended', 1))

    def test_timestamp_coverage_does_not_reappear_after_retention(self):
        # protects=retained identity without retained coverage; fails_when=raw pruning permits replay;
        # why_new=retention with no intervening tick; seam=clock/reopened SQLite; no_claim=disk quota.
        for field in ('heartbeat_at', 'claim_at'):
            with self.subTest(field=field):
                self.setUp()
                f = self.f
                sample = f.now.isoformat()
                self.signal(**{field: sample})
                f.tick()
                self.advance(31 * 86400)
                self.assertFalse(self.signal(**{field: sample})['out_of_order'])
                f.tick()
                self.assertEqual(self.state('stale_claim'), [])
                monitor = f.monitor(True)
                assert monitor is not None
                with monitor:
                    self.assertEqual(monitor.db.execute('SELECT count(*) FROM observations').fetchone()[0], 1)
                self.signal(**{field: f.now.isoformat()})
                f.tick()
                self.advance(900)
                self.assertEqual([n['detector'] for n in f.tick()['notices'] if n['detector'] == 'stale_claim'],
                                 ['stale_claim'])

    def test_recurrence_keeps_distinct_incident_and_plan_ids_after_retention(self):
        for field, prune_on_observe in (('claim_at', True), ('heartbeat_at', False)):
            with self.subTest(field=field):
                self.setUp()
                f = self.f

                def notices():
                    return [n['incident_id'] for n in f.tick()['notices'] if n['detector'] == 'stale_claim']

                def plans():
                    monitor = f.monitor(True)
                    assert monitor is not None
                    with monitor:
                        items = monitor.page('plans')['items']
                        assert isinstance(items, list)
                        return items

                seen = set()
                for episode in (1, 2, 3):
                    self.signal(**{field: (f.now - timedelta(seconds=901)).isoformat()})
                    identity, = notices()
                    active, = self.state('stale_claim')
                    self.assertNotIn(identity, seen)
                    self.assertEqual(active[:5], (episode, identity, 'active', 'recommended', 1))
                    plan, = [p for p in plans() if p['incident_id'] == identity]
                    self.assertEqual(plan['plan_id'], identity)
                    seen.add(identity)
                    self.assertEqual(notices(), [])

                    self.advance(1)
                    recovered = f.now.isoformat()
                    self.signal(**{field: recovered})
                    notices()
                    resolved, = self.state('stale_claim')
                    self.assertEqual(resolved[:5], (episode, identity, 'resolved', 'resolved', 1))
                    self.advance(31 * 86400)
                    if not prune_on_observe:
                        self.assertEqual(notices(), [])
                    self.signal(**{field: None})
                    self.signal(**{field: recovered})
                    self.assertEqual(notices(), [])
                    self.assertEqual(self.state('stale_claim'), [])
                    self.assertFalse(any(p['incident_id'] in seen for p in plans()))

    def test_atomic_claim_family_delivery_matrix(self):
        # protects=episode/plan/notice stability; fails_when=delivery order mints evidence;
        # why_new=combined and split null/replay permutations; seam=clock/reopened SQLite;
        # no_claim=remote delivery. Oracle: relative to the recovered sample, older and
        # equal are unknown after loss; either strictly newer sample is known and ages.
        schedules = (('loss', 'sample'), ('sample', 'loss'), ('both',), ('reverse',))
        oracle = ((-1, False, False), (0, False, False), (1, True, True), (3, False, True))
        for primary, order, (delta, initially_stale, recurs), tick_between, repeat in product(
                ('claim_at', 'heartbeat_at'), schedules, oracle, (False, True), (False, True)):
            with self.subTest(primary=primary, order=order, delta=delta, tick=tick_between, repeat=repeat):
                self.setUp()
                f = self.f
                alternate = 'heartbeat_at' if primary == 'claim_at' else 'claim_at'
                self.signal(**{primary: (f.now - timedelta(seconds=901)).isoformat()})
                first, = f.tick()['notices']
                self.advance(1)
                recovered = f.now
                self.signal(**{primary: recovered.isoformat()})
                f.tick()
                baseline, = self.state('stale_claim')
                self.assertEqual(baseline[:5], (1, first['incident_id'], 'resolved', 'resolved', 1))

                # The +1 sample is already stale without ever sampling the old
                # source at that time. For split deliveries exercise an intervening
                # tick before either old source could age into a legitimate fault.
                late = len(order) == 1 and delta == 1 and not tick_between
                self.advance(901 if late else max(2, delta))
                candidate = (recovered + timedelta(seconds=delta)).isoformat()
                loss, sample = {primary: None}, {alternate: candidate}
                envelopes = {'loss': loss, 'sample': sample, 'both': {**loss, **sample},
                             'reverse': {**sample, **loss}}
                for index, phase in enumerate(order):
                    self.signal(**envelopes[phase])
                    if tick_between and index + 1 < len(order):
                        self.assertEqual(f.tick()['notices'], [])
                        self.assertEqual(self.state('stale_claim'), [baseline])
                if repeat:
                    self.signal(**loss)
                    self.signal(**sample)
                    self.signal(**loss)

                def notices():
                    return [n['incident_id'] for n in f.tick()['notices'] if n['detector'] == 'stale_claim']

                observed = notices()
                if not (late and initially_stale):
                    self.assertEqual(observed, [])
                    self.assertEqual(self.state('stale_claim'), [baseline])
                    self.advance(900)
                    observed = notices()
                rows = self.state('stale_claim')
                if recurs:
                    self.assertEqual(len(rows), 2)
                    self.assertEqual(len(observed), 1)
                    original, incident = rows
                    self.assertEqual(original, baseline)
                    self.assertNotEqual(incident[1], baseline[1])
                    self.assertEqual(incident, (2, observed[0], 'active', 'recommended', 1,
                                               (f.now + timedelta(seconds=300)).isoformat()))
                    self.assertEqual(observed, [incident[1]])
                else:
                    self.assertEqual(observed, [])
                    self.assertEqual(rows, [baseline])
                self.assertEqual(notices(), [])
                self.assertEqual(self.state('stale_claim'), rows)

    def test_old_explanation_recovers_when_retained_source_proves_its_origin(self):
        import json
        f = self.f
        self.signal(dependency_state='unreachable')
        f.tick()
        with f.monitor() as monitor, monitor.transaction():
            row = monitor.db.execute("SELECT * FROM incidents WHERE detector='dependency_unreachable'").fetchone()
            explanation = json.loads(row['explanation_json'])
            explanation.pop('sources')
            explanation['pending_sources'] = ['explicit_signal']
            monitor.db.execute('UPDATE incidents SET explanation_json=? WHERE incident_id=?',
                               (json.dumps(explanation), row['incident_id']))
        self.signal(dependency_state='unreachable')
        f.tick()
        self.signal(dependency_state='available')
        f.tick()
        self.assertEqual(self.state('dependency_unreachable')[0][2:4], ('resolved', 'resolved'))

    def test_unproven_historical_claim_metadata_is_unknown(self):
        # protects=upgrade continuity; fails_when=per-field legacy proof is trusted;
        # why_new=atomic admission has a new persisted marker; seam=SQLite; no_claim=host upgrade.
        f = self.f
        self.signal(heartbeat_at=(f.now - timedelta(seconds=901)).isoformat())
        f.tick()
        baseline = self.state('stale_claim')
        monitor = f.monitor()
        assert monitor is not None
        with monitor, monitor.transaction():
            key, = monitor.db.execute("SELECT key FROM monitor_meta WHERE key LIKE 'evidence:%'").fetchone()
            evidence = monitor.meta(key)
            evidence.pop('claim_gap', None)
            evidence.pop('claim_source', None)
            evidence['records']['heartbeat_at']['gap_sequence'] = None
            monitor.put(key, evidence)
        self.advance(300)
        self.assertEqual(f.tick()['notices'], [])
        self.assertEqual(self.state('stale_claim'), baseline)
        self.signal(heartbeat_at=f.now.isoformat())
        f.tick()
        self.assertEqual(self.state('stale_claim')[0][2:5], ('resolved', 'resolved', 1))

    def test_retention_is_unknown_and_new_source_recovery_still_works(self):
        f = self.f
        self.signal(agent_available=False, tool_failures=3)
        f.tick()
        before = {d: self.state(d) for d in ('delegated_work_unavailable', 'repeated_tool_failure')}
        self.advance(31 * 86400)
        f.tick()
        f.tick()
        for detector in before:
            self.assertEqual(self.state(detector), before[detector])
        self.signal(agent_available=True, tool_failures=0)
        f.tick()
        for detector in before:
            self.assertEqual(self.state(detector)[0][2:4], ('resolved', 'resolved'))
        with f.monitor(True) as monitor:
            self.assertGreater(monitor.health()['coverage']['evaluations']['unknown'], 0)

    def test_new_nonrunning_state_ends_claim_but_claim_complete_does_not(self):
        # protects=authoritative scope-end versus claim-complete; fails_when=old sample replays;
        # why_new=scope end is a coverage gap; seam=ledger/reopened SQLite; no_claim=Kanban reclaim.
        f = self.f
        sample = f.now - timedelta(seconds=901)
        self.signal(heartbeat_at=sample.isoformat())
        f.tick()
        self.status('blocked')
        f.tick()
        self.assertEqual(self.state('stale_claim')[0][2:4], ('resolved', 'resolved'))
        f.tick()
        self.assertEqual(len(self.state('stale_claim')), 1)
        self.status('running')
        self.signal(heartbeat_at=sample.isoformat())
        f.tick()
        self.assertEqual(len(self.state('stale_claim')), 1)
        self.advance(1)
        self.signal(heartbeat_at=(f.now - timedelta(seconds=901)).isoformat())
        f.tick()
        self.assertEqual(len(self.state('stale_claim')), 2)
        with f.ledger() as ledger:
            task = ledger.get_task('board', 'task')
            ledger.claim_complete('board', 'task', expected_revision=task['revision'], reason='claim only',
                                  request_id='claim', actor='fixture')
        f.tick()
        self.assertEqual(self.state('stale_claim')[-1][2:4], ('active', 'recommended'))

    def test_expired_failure_window_is_unknown_not_a_count_reset(self):
        f = self.f
        self.signal(tool_failures=3)
        f.tick()
        before = self.state('repeated_tool_failure')
        self.advance(901)
        self.signal(checkpoint_at=None)
        f.tick()
        self.assertEqual(self.state('repeated_tool_failure'), before)
        f.observe(dict(tool_name='read', outcome='failure', duration_seconds=0), kind='tool_outcome', event='one-failure')
        f.tick()
        self.assertEqual(self.state('repeated_tool_failure'), before)
        f.observe(dict(tool_name='read', outcome='success', duration_seconds=0), kind='tool_outcome', event='success')
        f.tick()
        self.assertEqual(self.state('repeated_tool_failure')[0][2:4], ('resolved', 'resolved'))

    def test_nullable_signals_preserve_whole_rows_and_notice_budget(self):
        cases = [('delegated_work_unavailable', 'agent_available', False, None, True),
                 ('dependency_unreachable', 'dependency_state', 'unreachable', 'unknown', 'available'),
                 ('missed_checkpoint', 'checkpoint_at', 'past', None, 'future')]
        for detector, field, active, unknown, clear in cases:
            with self.subTest(detector=detector):
                self.setUp()
                f = self.f
                active = (f.now - timedelta(seconds=1)).isoformat() if active == 'past' else active
                self.signal(**{field: active})
                f.tick()
                with f.monitor(True) as monitor:
                    before = monitor.page('incidents')['items'], monitor.page('plans')['items']
                self.advance(300)
                self.signal(**{field: unknown})
                self.signal()
                f.tick()
                with f.monitor(True) as monitor:
                    self.assertEqual((monitor.page('incidents')['items'], monitor.page('plans')['items']), before)
                self.signal(**{field: active})
                f.tick()
                self.assertEqual(self.state(detector)[0][4], 2)
                clear = (f.now + timedelta(seconds=1)).isoformat() if clear == 'future' else clear
                self.signal(**{field: clear})
                f.tick()
                self.assertEqual(self.state(detector)[0][2:4], ('resolved', 'resolved'))

    def test_deadline_replan_resolves_both_commitment_incidents(self):
        f = self.f
        with f.ledger() as ledger:
            ledger.set_deadline('due', project_id='demo', kind='hard', at=f.now.isoformat(), timezone='UTC',
                                owner='fixture', source='fixture', request_id='due', actor='fixture')
        f.tick()
        f.now += timedelta(seconds=601)
        f.tick()
        for detector in ('expired_deadline', 'forecast_miss'):
            self.assertEqual(self.state(detector)[0][2], 'active')
        with f.ledger() as ledger:
            ledger.revise_deadline('due', at=(f.now + timedelta(days=1)).isoformat(), timezone='UTC',
                intent='operator', expected_revision=1, reason='Explicit replan', request_id='replan', actor='fixture')
        f.tick()
        for detector in ('expired_deadline', 'forecast_miss'):
            self.assertEqual(self.state(detector)[0][2:4], ('resolved', 'resolved'))
        f.tick()
        self.assertEqual(len(self.state('forecast_miss')), 1, 'Old prediction replayed after replan')
        f.now += timedelta(seconds=601)
        f.tick()
        self.assertEqual([(r[0], r[2]) for r in self.state('forecast_miss')], [(1, 'resolved'), (2, 'active')])

    def test_ingestion_watermark_survives_pruning_before_first_tick(self):
        f = self.f
        f.observe({'agent_available': False})
        f.now += timedelta(days=31)
        f.tick()
        self.assertEqual(self.state('delegated_work_unavailable'), [])
        with f.monitor(True) as monitor:
            evidence = monitor.meta('evidence:' + fixtures.sha256(b'no').hexdigest())
            rows = list(monitor.db.execute("SELECT value_json FROM monitor_meta WHERE key LIKE 'evidence:%'"))
            self.assertEqual(len(rows), 1, 'Admitted source watermark must survive raw retention')
            self.assertIn('agent_available', rows[0][0])
            self.assertEqual(monitor.db.execute('SELECT count(*) FROM observations').fetchone()[0], 0)

    def test_null_claim_retains_episode_and_recovery_requires_new_heartbeat(self):
        # protects=notice budget; fails_when=equal replay restores coverage;
        # why_new=old assertion only compared incident ID; seam=reopened SQLite; no_claim=delivery.
        f = self.f
        stale = (f.now - timedelta(seconds=901)).isoformat()
        f.observe({'heartbeat_at': stale})
        f.tick()
        initial = self.state('stale_claim')
        f.now += timedelta(seconds=300)
        f.observe({'heartbeat_at': None}, event='lost')
        self.assertEqual(f.tick()['notices'], [])
        self.assertEqual(self.state('stale_claim'), initial)
        f.observe({'heartbeat_at': stale}, event='again')
        self.assertFalse(any(n['detector'] == 'stale_claim' for n in f.tick()['notices']))
        self.assertEqual(self.state('stale_claim'), initial)
        f.observe({'heartbeat_at': f.now.isoformat()}, event='recovered')
        f.tick()
        self.assertEqual(self.state('stale_claim')[0][2:4], ('resolved', 'resolved'))
        f.now += timedelta(seconds=901)
        f.tick()
        self.assertEqual([(r[0], r[2]) for r in self.state('stale_claim')], [(1, 'resolved'), (2, 'active')])


if __name__ == '__main__':
    unittest.main()
