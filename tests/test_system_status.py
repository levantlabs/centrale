"""Hermetic machine resource observations (task-242)."""
import json
import unittest
from unittest import mock
import system_status as S

class SystemStatusTests(unittest.TestCase):
    def test_missing_and_malformed_fields(self):
        row = S.parse_worker('box-b', '{"ramTotal":100,"ramAvailable":-1,"load1":"bad","cores":0,"diskFree":null}')
        self.assertTrue(row['reachable'])
        self.assertEqual(row['ramTotal'], 100)
        for key in ('ramAvailable', 'load1', 'cores', 'diskFree'):
            self.assertIsNone(row[key])
        self.assertTrue(S.parse_worker('box-b', 'garbage')['reachable'])
        self.assertTrue(S.parse_worker('box-b', '[]')['reachable'])

    def test_hub_reads_without_subprocess(self):
        with mock.patch.object(S, 'read_meminfo', return_value={'MemTotal':1000, 'MemAvailable':200, 'SwapTotal':300, 'SwapFree':100}), mock.patch.object(S.os, 'statvfs', return_value=mock.Mock(f_blocks=100, f_bavail=25, f_frsize=10)), mock.patch.object(S.os, 'getloadavg', return_value=(2,3,4)), mock.patch.object(S.os, 'cpu_count', return_value=4):
            row = S.read_local('/projects')
        self.assertEqual((row['ramTotal'],row['ramAvailable'],row['swapUsed'],row['swapTotal']), (1000,200,200,300))
        self.assertEqual((row['diskTotal'],row['diskFree'],row['load1'],row['cores']), (1000,250,2,4))

    def test_cache_and_unreachable(self):
        cache = S.Cache()
        config = {'projects':[{'path':'/projects'}], 'machines':[{'name':'box-b','worktreeRoot':'~/work'}]}
        import machines
        with mock.patch.object(S, 'read_local', return_value=S.empty('hub', True)), mock.patch.object(machines, 'run_on_machine', return_value=machines.MachineResult(machines.UNREACHED,None,'','')) as run:
            cache.get(config)
            cache.thread.join(timeout=10)
            rows = cache.get(config)['machines']
            self.assertFalse(rows[1]['reachable'])
            cache.get(config)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.kwargs['timeout'], 5)
            with mock.patch.object(S.time, 'monotonic', return_value=cache.updated + 31):
                cache.get(config)
                cache.thread.join(timeout=10)
            self.assertEqual(run.call_count, 2)

from js_harness import requires_node, run_driver, LOAD_SOURCES_JS

@requires_node
class MachineFrontendTests(unittest.TestCase):
    def test_threshold_boundaries_and_rendering(self):
        result = run_driver(self, LOAD_SOURCES_JS + r'''
var C = loadFrontend(['state.js', 'dom.js', 'sessions.js']);
var cases = [['ram',20,100],['ram',19,100],['ram',10,100],['ram',9,100],
 ['disk',15,100],['disk',14,100],['disk',5,100],['disk',4,100],
 ['load',8,8],['load',8.1,8],['load',12,8],['load',12.1,8]];
console.log(JSON.stringify({levels:cases.map(function(x){return C.machineLevel.apply(null,x);}),
 unknown:C.machineMetrics({reachable:false}).textContent,
 checking:C.machineMetrics({reachable:null}).textContent,
 checkingClass:C.machineMetrics({reachable:null}).className,
 missing:C.machineMetrics({reachable:true}).textContent}));
''')
        self.assertEqual(result['levels'], ['', 'amber','amber','red','','amber','amber','red','','amber','amber','red'])
        self.assertEqual(result['unknown'], 'unreachable')
        self.assertEqual(result['checking'], 'checking…')
        self.assertIn('unavailable', result['checkingClass'])
        self.assertIn('RAM n/a', result['missing'])
        self.assertIn('disk n/a', result['missing'])
        self.assertIn('load n/a', result['missing'])

    def test_polling_shares_sidebar_and_fleet_figures(self):
        result = run_driver(self, LOAD_SOURCES_JS + r'''
var C = loadFrontend(['state.js','dom.js','sessions.js']);
var list = document.getElementById('machines-list');
var group = C.h('section',{attrs:{'data-machine':'box-b'}});
var head = C.h('div',{className:'fleet-machine-head'}); group.appendChild(head); document.getElementById('board').appendChild(group);
var requests = [], timers = [];
global.setInterval = function(fn, ms) {timers.push({fn:fn,ms:ms}); return 1;};
global.fetch = function(url) {requests.push(url); return Promise.resolve({ok:true,json:function(){return Promise.resolve({machines:[{name:'box-b',reachable:true,ramTotal:100,ramAvailable:9,diskTotal:100,diskFree:5,load1:12.1,cores:8,swapUsed:0}]});}});};
C.startSystemPolling(); C.startSystemPolling();
settle().then(function(){
 console.log(JSON.stringify({requests:requests,timers:timers.map(function(t){return t.ms;}), sidebar:list.textContent, fleet:head.textContent,
 red:head.querySelectorAll('.red').length, amber:head.querySelectorAll('.amber').length}));
});
''')
        self.assertEqual(result['requests'], ['/api/system'])
        self.assertEqual(result['timers'], [30000])
        self.assertEqual(result['sidebar'], 'box-b ' + result['fleet'])
        self.assertEqual((result['red'], result['amber']), (2,1))

    def test_swap_is_always_shown_and_coloured_by_share_used(self):
        # Owner, 2026-10-09: swap on the line always, not only on hover.
        result = run_driver(self, LOAD_SOURCES_JS + r'''
var C = loadFrontend(['state.js','dom.js','sessions.js']);
function line(m) { var row = C.machineMetrics(m); return {text: row.textContent, red: row.querySelectorAll('.red').length, amber: row.querySelectorAll('.amber').length}; }
var base = {name:'box-b',reachable:true,ramTotal:100,ramAvailable:90,diskTotal:100,diskFree:90,load1:0.1,cores:8};
console.log(JSON.stringify({
 none: line(Object.assign({}, base, {swapUsed:0, swapTotal:0})),
 half: line(Object.assign({}, base, {swapUsed:5e9, swapTotal:1e10})),
 amber: line(Object.assign({}, base, {swapUsed:6e9, swapTotal:1e10})),
 red: line(Object.assign({}, base, {swapUsed:9e9, swapTotal:1e10})),
 missing: line(Object.assign({}, base, {swapUsed:null, swapTotal:null}))}));
''')
        self.assertIn('swap 0.0 GB used', result['none']['text'])
        self.assertEqual((result['none']['red'], result['none']['amber']), (0, 0))
        self.assertEqual((result['half']['red'], result['half']['amber']), (0, 0))
        self.assertEqual((result['amber']['red'], result['amber']['amber']), (0, 1))
        self.assertIn('swap 9.0 GB used', result['red']['text'])
        self.assertEqual((result['red']['red'], result['red']['amber']), (1, 0))
        self.assertIn('swap n/a', result['missing']['text'])


class BackgroundResourceTests(unittest.TestCase):
    def test_hub_is_available_while_worker_is_blocked(self):
        import threading
        entered, release = threading.Event(), threading.Event()
        def worker(config, machine):
            entered.set()
            release.wait(2)
            return S.empty('box-b', True)
        cache = S.Cache()
        config = {'projects': [], 'machines':[{'name':'box-b','worktreeRoot':'~/work'}]}
        with mock.patch.object(S, 'read_worker', side_effect=worker):
            try:
                first = cache.get(config)
                self.assertTrue(entered.wait(1))
                self.assertTrue(first['machines'][0]['reachable'])
                self.assertIsNone(first['machines'][1]['reachable'])
                self.assertTrue(cache.get(config)['machines'][0]['reachable'])
            finally:
                release.set()
                cache.thread.join(3)
            self.assertTrue(cache.get(config)['machines'][1]['reachable'])

    def test_worker_is_checking_until_failed_read_finishes(self):
        import threading
        import machines
        entered, release = threading.Event(), threading.Event()
        def transport(*args, **kwargs):
            entered.set()
            release.wait(2)
            return machines.MachineResult(machines.UNREACHED, None, "", "")
        cache = S.Cache()
        config = {"projects": [], "machines": [{"name": "box-b", "worktreeRoot": "~/work"}]}
        with mock.patch.object(machines, "run_on_machine", side_effect=transport):
            try:
                first = cache.get(config)
                self.assertTrue(entered.wait(1))
                self.assertIsNone(first["machines"][1]["reachable"])
                self.assertIsNone(cache.get(config)["machines"][1]["reachable"])
            finally:
                release.set()
                cache.thread.join(3)
            self.assertIs(cache.get(config)["machines"][1]["reachable"], False)

    def test_missing_platform_figures_do_not_lose_reachability(self):
        with mock.patch.object(S, 'read_meminfo', return_value={}), mock.patch.object(S.os, 'statvfs', side_effect=OSError), mock.patch.object(S.os, 'getloadavg', side_effect=OSError), mock.patch.object(S.os, 'cpu_count', return_value=None):
            self.assertEqual(S.read_local('/projects'), dict.fromkeys(S.FIELDS))

    def test_worker_success_and_missing_reader(self):
        import machines
        config = {'machines':[{'name':'box-b','worktreeRoot':'~/work'}]}
        worker = config['machines'][0]
        with mock.patch.object(machines, 'run_on_machine', return_value=machines.MachineResult(machines.ANSWERED,0,'{"load1":3.2,"cores":8}','')):
            self.assertEqual(S.read_worker(config, worker)['load1'], 3.2)
        with mock.patch.object(machines, 'run_on_machine', return_value=machines.MachineResult(machines.ANSWERED,127,'','')):
            row = S.read_worker(config, worker)
            self.assertTrue(row['reachable'])
            self.assertIsNone(row['ramTotal'])

    def test_api_returns_all_machine_fields(self):
        import server
        handler = object.__new__(server.Handler)
        handler.path = '/api/system'
        handler.server = mock.Mock(centrale_config={'projects':[], 'machines':[]})
        with mock.patch.object(handler, '_refuse_untrusted_request', return_value=False), mock.patch.object(handler, '_send_json') as send, mock.patch.object(S, 'cache', S.Cache()):
            handler.do_GET()
        status, payload = send.call_args.args
        self.assertEqual(status, 200)
        self.assertEqual(payload['machines'][0]['name'], 'hub')
        self.assertTrue(payload['machines'][0]['reachable'])
        self.assertEqual(set(payload['machines'][0]), {'name','reachable',*S.FIELDS})

    def test_configuration_change_invalidates_observations(self):
        cache = S.Cache()
        with mock.patch.object(S, 'read_local', side_effect=lambda path: dict(S.empty('hub', True), diskFree=1 if path == '/a' else 2)):
            self.assertEqual(cache.get({'projects':[{'path':'/a'}]})['machines'][0]['diskFree'], 1)
            self.assertEqual(cache.get({'projects':[{'path':'/b'}]})['machines'][0]['diskFree'], 2)
