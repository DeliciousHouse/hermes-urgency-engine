"""protects=request-profile public services and pre-writer redaction;
fails_when=private plugin internals select settings or unsafe text reaches a writer;
seam=synthetic Hermes/FastAPI modules with real Dashboard and Runtime;
no_claim=native host routing, authentication, or actual redactor quality.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import importlib.util
import json
import sqlite3
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class DashboardContextTests(unittest.TestCase):
    def setUp(self):
        fastapi = types.ModuleType('fastapi')
        class Router:
            def get(self, *args, **kwargs):
                return lambda function: function
            post = get
        fastapi.APIRouter = Router
        fastapi.Request = type('Request', (), {})
        responses = types.ModuleType('fastapi.responses')
        class JSONResponse:
            def __init__(self, content, status_code):
                self.content, self.status_code = content, status_code
        responses.JSONResponse = JSONResponse
        hermes = types.ModuleType('hermes_cli')
        hermes.__path__ = []
        private = types.ModuleType('hermes_cli.plugins')
        def deny(*args, **kwargs):
            raise AssertionError('private plugin context accessed')
        private.PluginContext = private.PluginManifest = private.get_plugin_manager = deny
        config = types.ModuleType('hermes_cli.config')
        self.config = {}
        config.load_config_readonly = lambda: self.config
        agent = types.ModuleType('agent')
        agent.__path__ = []
        redact = types.ModuleType('agent.redact')
        self.redact = Mock(side_effect=lambda text, **kwargs: '[redacted]' if text == 'sensitive' else text)
        redact.redact_sensitive_text = self.redact
        modules = {'fastapi': fastapi, 'fastapi.responses': responses,
                   'hermes_cli': hermes, 'hermes_cli.plugins': private, 'hermes_cli.config': config,
                   'agent': agent, 'agent.redact': redact}
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)
        spec = importlib.util.spec_from_file_location('dashboard_context_fixture',
            Path(__file__).resolve().parents[1] / 'dashboard' / 'plugin_api.py')
        self.api = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.api
        spec.loader.exec_module(self.api)

    def test_public_config_precedence_and_defaults(self):
        self.config = {'context_enabled': False, 'default_project': 'unrelated', 'plugins': {
            'entries': {'hermes-urgency-engine': {
                'settings': {'context_max_chars': 2200},
                'config': {'context_max_chars': 1800, 'default_project': 'legacy'}}}}}
        settings = self.api.scoped_runtime().settings()
        self.assertEqual(settings['context_max_chars'], 2200)
        self.assertEqual(settings['default_project'], 'legacy')
        self.assertEqual(settings['context_enabled'], True)

    def test_malformed_containers_match_native_defaults_and_legacy_fallback(self):
        plugin = 'hermes-urgency-engine'
        cases = [
            (None, 'default'), ([], 'default'),
            ({'plugins': None}, 'default'), ({'plugins': []}, 'default'),
            ({'plugins': {'entries': None}}, 'default'),
            ({'plugins': {'entries': {plugin: []}}}, 'default'),
            ({'plugins': {'entries': {plugin: {'settings': [], 'config': {'key': 'legacy'}}}}}, 'legacy'),
            ({'plugins': {'entries': {plugin: {'settings': {}, 'config': []}}}}, 'default'),
            ({'plugins': {'entries': {plugin: {'settings': {'key': None}, 'config': {'key': 'legacy'}}}}}, None),
            ({'plugins': {'entries': {plugin: {'settings': {}, 'config': {'key': None}}}}}, None),
        ]
        for config, expected in cases:
            with self.subTest(config=config):
                self.config = config
                self.assertEqual(self.api.scoped_runtime().ctx.get_config('key', 'default'), expected)

    def test_invalid_present_setting_is_not_replaced_by_legacy_or_default(self):
        self.config = {'plugins': {'entries': {'hermes-urgency-engine': {
            'settings': {'context_max_chars': None}, 'config': {'context_max_chars': 2000}}}}}
        with self.assertRaises(self.api._ledger.LedgerError) as caught:
            self.api.scoped_runtime().settings()
        self.assertEqual((caught.exception.code, caught.exception.field), ('INVALID_CONFIG', 'context_max_chars'))

    def test_each_request_resolves_new_config_without_mutating_it(self):
        first = {'plugins': {'entries': {'hermes-urgency-engine': {'settings': {'default_project': 'one'}}}}}
        second = {'plugins': {'entries': {'hermes-urgency-engine': {'settings': {'default_project': 'two'}}}}}
        self.config = first
        one = self.api.scoped_runtime()
        self.config = second
        two = self.api.scoped_runtime()
        self.assertEqual(one.settings()['default_project'], 'one')
        self.assertEqual(two.settings()['default_project'], 'two')
        self.assertEqual((first, second), (
            {'plugins': {'entries': {'hermes-urgency-engine': {'settings': {'default_project': 'one'}}}}},
            {'plugins': {'entries': {'hermes-urgency-engine': {'settings': {'default_project': 'two'}}}}}))

    def test_concurrent_requests_keep_profiles_and_configuration_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            homes = [Path(directory) / name for name in ('one', 'two')]
            for home in homes:
                root = home / 'plugin-data' / 'hermes-urgency-engine'
                root.mkdir(parents=True)
                with self.api._ledger.Ledger(sqlite3.connect(root / 'data.db'),
                        clock=lambda: datetime.now(timezone.utc), artifact_root=root) as ledger:
                    ledger.create_project(home.name, home.name, request_id='create', actor='operator')
            local = threading.local()
            barrier = threading.Barrier(2)
            constants = types.ModuleType('hermes_constants')
            constants.get_hermes_home = lambda: local.home
            def load_config():
                barrier.wait(timeout=10)
                return {'plugins': {'entries': {'hermes-urgency-engine': {
                    'settings': {'kanban_sync_enabled': local.enabled}}}}}
            config = sys.modules['hermes_cli.config']
            def request(index):
                local.home = homes[index]
                local.enabled = bool(index)
                runtime = self.api.scoped_runtime()
                result = self.api.portfolio(types.SimpleNamespace(query_params={}))
                return ([item['project_id'] for item in result['data']['items']],
                        runtime.kanban_config()['kanban_sync_enabled'])
            with patch.dict(sys.modules, {'hermes_constants': constants}), \
                 patch.object(config, 'load_config_readonly', side_effect=load_config), \
                 ThreadPoolExecutor(max_workers=2) as executor:
                self.assertEqual(list(executor.map(request, (0, 1))), [(['one'], False), (['two'], True)])

    def test_empty_portfolio_get_creates_no_profile_files(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'fresh'
            constants = types.ModuleType('hermes_constants')
            constants.get_hermes_home = lambda: home
            with patch.dict(sys.modules, {'hermes_constants': constants}):
                request = types.SimpleNamespace(query_params={})
                result = self.api.portfolio(request)
            self.assertEqual(result.status_code, 400)
            self.assertEqual(result.content['error']['field'], 'artifact_root')
            self.assertFalse(home.exists())
            root = home / 'plugin-data' / 'hermes-urgency-engine'
            root.mkdir(parents=True)
            with patch.dict(sys.modules, {'hermes_constants': constants}):
                result = self.api.portfolio(request)
            self.assertTrue(result['ok'])
            self.assertEqual(result['data']['items'], [])
            self.assertEqual(list(root.iterdir()), [])

    def test_command_writes_only_selected_profile_with_public_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            homes = [Path(directory) / 'one', Path(directory) / 'two']
            selected = [homes[0]]
            constants = types.ModuleType('hermes_constants')
            constants.get_hermes_home = lambda: selected[0]
            plugins = types.ModuleType('plugins')
            plugins.__path__ = []
            storage = types.ModuleType('plugins.plugin_storage')
            def plugin_db(plugin):
                self.assertEqual(plugin, 'hermes-urgency-engine')
                return sqlite3.connect(selected[0] / 'plugin-data' / plugin / 'data.db')
            storage.plugin_db = plugin_db
            payload = {'operation': 'create_project', 'request_id': 'request',
                       'arguments': {'project_id': 'project', 'title': 'safe'}}
            with patch.dict(sys.modules, {'hermes_constants': constants, 'plugins': plugins,
                                          'plugins.plugin_storage': storage}), \
                 patch.object(self.api._runtime.interventions, 'operator_only'), \
                 patch.object(self.api._runtime.interventions, 'host_stop') as stop:
                result = self.api.execute_command(json.dumps(payload).encode())
                self.assertEqual(result['title'], 'safe')
                stop.assert_called_once()
                self.assertFalse(homes[1].exists())
                request = types.SimpleNamespace(query_params={})
                self.assertEqual(self.api.portfolio(request)['data']['items'][0]['project_id'], 'project')
                selected[0] = homes[1]
                root = homes[1] / 'plugin-data' / 'hermes-urgency-engine'
                root.mkdir(parents=True)
                self.assertEqual(self.api.portfolio(request)['data']['items'], [])
                self.assertEqual(list(root.iterdir()), [])

    def test_config_failure_returns_existing_diagnostic_without_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'plugin-data' / 'hermes-urgency-engine'
            root.mkdir(parents=True)
            constants = types.ModuleType('hermes_constants')
            constants.get_hermes_home = lambda: Path(directory)
            config = sys.modules['hermes_cli.config']
            with patch.dict(sys.modules, {'hermes_constants': constants}), \
                 patch.object(config, 'load_config_readonly', side_effect=RuntimeError('unavailable')):
                result = self.api.portfolio(types.SimpleNamespace(query_params={}))
            self.assertEqual(result.status_code, 500)
            self.assertEqual(result.content['error']['code'], 'INTERNAL_ERROR')
            self.assertIn('diagnostic_id', result.content['error'])
            self.assertEqual(list(root.iterdir()), [])

    def test_redactor_exception_and_invalid_result_refuse_before_writer(self):
        payload = {'operation': 'create_project', 'request_id': 'request',
                   'arguments': {'project_id': 'project', 'title': 'safe'}}
        for failure in (RuntimeError('redactor unavailable'), None):
            with self.subTest(failure=type(failure).__name__), \
                 patch.object(self.api._runtime.interventions, 'operator_only'), \
                 patch.object(self.api._runtime.interventions, 'host_stop'), \
                 patch.object(self.api._runtime.Runtime, 'open') as writer:
                self.redact.side_effect = failure if isinstance(failure, Exception) else lambda *a, **k: None
                with self.assertRaises((RuntimeError, self.api._ledger.LedgerError)):
                    self.api.execute_command(json.dumps(payload).encode())
                writer.assert_not_called()

    def test_source_tasks_reads_only_existing_sync_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / 'plugin-data' / 'hermes-urgency-engine'
            root.mkdir(parents=True)
            binding = {'board': 'board', 'tenant': 'tenant', 'project_id': 'project'}
            values = {'kanban_sync_enabled': True, 'kanban_bindings': [binding]}
            self.config = {'plugins': {'entries': {'hermes-urgency-engine': {'settings': values}}}}
            class Source:
                def inventory(self, binding):
                    return [{'id': 'task', 'tenant': binding['tenant']}]
                def detail(self, binding, task_id):
                    return {'task': {'id': task_id, 'tenant': binding['tenant'], 'title': task_id,
                                     'status': 'todo'}, 'parents': []}
            now = lambda: datetime.now(timezone.utc)
            with self.api._ledger.Ledger(sqlite3.connect(root / 'data.db'), clock=now, artifact_root=root) as ledger, \
                 self.api._runtime.Sync(sqlite3.connect(root / 'kanban.db'), clock=now, redact=str) as sync:
                ledger.create_project('project', 'Project', request_id='create', actor='operator')
                scan = sync.scan(self.api._runtime.kanban_settings(values), ledger, None, root / 'data.db',
                                 Source(), request_id='scan', reason='Synthetic')
                self.assertEqual(scan['state'], 'complete')
            constants = types.ModuleType('hermes_constants')
            constants.get_hermes_home = lambda: home
            with patch.dict(sys.modules, {'hermes_constants': constants}), \
                 patch.object(self.api._runtime, 'NativeSource', side_effect=AssertionError('live source accessed')), \
                 patch.object(self.api._runtime.Sync, 'host', side_effect=AssertionError('writer accessed')):
                result = self.api.source_tasks('project', types.SimpleNamespace(query_params={}))
            self.assertTrue(result['ok'])
            self.assertEqual([item['task_id'] for item in result['data']['items']], ['task'])
            self.assertEqual(result['data']['sync_health']['storage'], 'available')
            self.assertFalse((root / 'watchdog.db').exists())

    def test_host_stop_refuses_before_redaction_or_writer(self):
        payload = {'operation': 'create_project', 'request_id': 'request',
                   'arguments': {'project_id': 'project', 'title': 'safe'}}
        error = self.api._ledger.LedgerError('PAUSED', 'desktop')
        with patch.object(self.api._runtime.interventions, 'operator_only'), \
             patch.object(self.api._runtime.interventions, 'host_stop', side_effect=error), \
             patch.object(self.api._runtime.Runtime, 'open') as writer:
            with self.assertRaises(self.api._ledger.LedgerError) as caught:
                self.api.execute_command(json.dumps(payload).encode())
            self.assertEqual(caught.exception.code, 'PAUSED')
            self.redact.assert_not_called()
            writer.assert_not_called()

    def test_command_refuses_changed_text_before_writer_with_strong_redaction(self):
        self.config = {'security': {'redact_sensitive_data': False}}
        payload = {'operation': 'create_project', 'request_id': 'request',
                   'arguments': {'project_id': 'project', 'title': 'sensitive'}}
        with patch.object(self.api._runtime.interventions, 'operator_only'), \
             patch.object(self.api._runtime.interventions, 'host_stop'), \
             patch.object(self.api._runtime.Runtime, 'open') as writer:
            with self.assertRaises(self.api._ledger.LedgerError) as caught:
                self.api.execute_command(json.dumps(payload).encode())
            self.assertEqual((caught.exception.code, caught.exception.field), ('INVALID_INPUT', 'secret_text'))
            writer.assert_not_called()
        self.assertTrue(self.redact.call_args_list)
        for call in self.redact.call_args_list:
            self.assertEqual(call.kwargs, dict(force=True, file_read=True, secret_file=True,
                                              redact_url_credentials=True))


if __name__ == '__main__':
    unittest.main()
