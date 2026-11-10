import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import stack_config as config
import stack_manage as manage


def settings(root):
    return {'STRATA_RPC_'+k: str(v) for k,v in {
        'REMOTE': 'coordinator.example', 'COORDINATOR_HOME': root/'other user',
        'WORKER_ROOT':root/'worker space', 'WORKER_MODEL_ROOT':root/'worker data',
        'COORDINATOR_MODEL_ROOT':root/'remote data',
        'WORKER_BIND':'192.0.2.20:51056','WORKER_PEER':'192.0.2.10',
        'COORDINATOR_BIND':'0.0.0.0:8180','COORDINATOR_URL':'http://192.0.2.10:8180',
        'PROXY_BIND':'192.0.2.10:8181','PROXY_URL':'http://192.0.2.10:8181',
        'WORKER_MONITOR_BIND':'192.0.2.20:51057','MONITOR_BIND':'192.0.2.10:8186',
        'MONITOR_URL':'http://192.0.2.10:8186/metrics','INTERFACE':'eth2',
    }.items()}


class StackConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.env=settings(self.root)
        self.cfg=config.configuration(self.env)

    def test_missing_environment_fails_before_ssh_or_writes(self):
        with mock.patch.dict(os.environ,{},clear=True),mock.patch.object(sys,'argv',['stack_manage','activate']),mock.patch.object(manage,'run') as run,mock.patch.object(manage,'remote') as remote:
            with self.assertRaises(SystemExit) as error: manage.main()
        self.assertEqual(error.exception.code,2)
        run.assert_not_called();remote.assert_not_called()

    def test_distinct_host_paths_and_native_override(self):
        self.env['STRATA_RPC_WORKER_NATIVE_ROOT']=str(self.root/'network mount')
        self.env['STRATA_RPC_PROFILE_NAME']='unsloth-q8-v0212'
        cfg=config.configuration(self.env)
        worker=config.profile_for(cfg,'worker')
        args=worker['args']
        self.assertTrue(args[args.index('--native')+1].startswith(str(self.root/'network mount')))
        self.assertTrue(args[args.index('--ple-gguf')+1].startswith(str(self.root/'worker data')))
        co=config.profile_for(cfg,'coordinator')
        self.assertEqual((co['host'],co['port']),('0.0.0.0',8180))
        self.assertNotEqual(cfg['hosts']['worker']['env']['TLS_DIR'],cfg['hosts']['coordinator']['env']['TLS_DIR'])

    def test_all_profiles_expand_without_machine_defaults(self):
        for p in (config.ROOT/'profiles/rpc').iterdir():
            if not p.is_dir():continue
            env=self.env|{'STRATA_RPC_PROFILE_NAME':p.name}
            cfg=config.configuration(env)
            for role in ('worker','coordinator'):
                profile=config.profile_for(cfg,role)
                self.assertIn('192.0.2.20:51056',profile['args'])
                self.assertEqual(profile['exe'],cfg['hosts'][role]['env']['RPC_ENGINE'])

    def test_systemd_quote_spaces_dollars_and_specifiers(self):
        self.assertEqual(config.unit_value('/a b/$HOME/%u',argv=True),'"/a b/$$HOME/%%u"')
        self.assertEqual(config.unit_value('ROOT=/a b/$HOME/%u'),'"ROOT=/a b/$HOME/%%u"')
        for kind in config.SERVICES:
            unit=config.render_unit(self.cfg,kind)
            self.assertIn('WorkingDirectory=/',unit)
            self.assertNotIn('@EXEC_START@',unit)
        unit=config.render_unit(self.cfg,'worker')
        self.assertIn('/worker-profile.json',unit)
        self.assertIn('worker space',unit)

    def test_origins_are_separate_arguments(self):
        self.env['STRATA_RPC_MONITOR_ORIGINS']=json.dumps(['http://a.example:8180','http://b.example:8181'])
        cfg=config.configuration(self.env)
        unit=config.render_unit(cfg,'relay')
        command=next(line for line in unit.splitlines() if line.startswith('ExecStart='))[10:]
        args=shlex.split(command)
        self.assertEqual(args.count('--origin'),2)
        self.assertEqual(args[-4:],['--origin','http://a.example:8180','--origin','http://b.example:8181'])
        self.assertEqual(json.loads(config.monitor_js(cfg).removeprefix('export default ').strip().rstrip(';'))['url'],cfg['monitor_url'])

    def test_generated_units_pass_systemd_parser_with_spaces(self):
        import shutil
        if not shutil.which('systemd-analyze'):
            self.skipTest('systemd-analyze is unavailable')
        env=self.env|{'STRATA_RPC_WORKER_PYTHON':sys.executable,'STRATA_RPC_COORDINATOR_PYTHON':sys.executable}
        cfg=config.configuration(env)
        for host in cfg['hosts'].values():Path(host['root']).mkdir(parents=True,exist_ok=True)
        units=[]
        for kind,name in config.SERVICES.items():
            p=self.root/name;p.write_text(config.render_unit(cfg,kind));units.append(str(p))
        result=subprocess.run(['systemd-analyze','--user','verify',*units],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)

    def test_invalid_values_rejected(self):
        for key,value in [('STRATA_RPC_WORKER_BUILD_DIR','../escape'),('STRATA_RPC_WORKER_BIND','host:99999'),('STRATA_RPC_REMOTE','-oProxyCommand=bad'),('STRATA_RPC_WORKER_MODEL_ROOT','relative'),('STRATA_RPC_INTERFACE','eth0\nnext'),('STRATA_RPC_MONITOR_ORIGINS','"bad"')]:
            with self.subTest(key=key),self.assertRaises(ValueError): config.configuration(self.env|{key:value})

    def test_remote_settings_only_on_stdin(self):
        request={'action':'check','host':self.cfg['hosts']['coordinator'],'role':'coordinator','profile':{'cwd':str(self.root)}}
        with mock.patch.object(manage,'run',return_value=subprocess.CompletedProcess([],0,'{}')) as run:
            manage.remote(self.cfg,request)
        argv=run.call_args.args[0]
        self.assertNotIn(str(self.root),' '.join(argv))
        payload=json.loads(run.call_args.kwargs['input'])
        self.assertEqual(payload['request'],request)
        self.assertIn('python3 -c',argv[-1])

    def test_install_restore_preserves_profiles_and_migration_override(self):
        host=copy.deepcopy(self.cfg['hosts']['worker'])
        host['unit_dir']=str(self.root/'units')
        host['env']['STATE_DIR']=str(self.root/'state')
        directory=Path(host['unit_dir']);directory.mkdir()
        state=Path(host['env']['STATE_DIR']);state.mkdir()
        name=config.SERVICES['worker']
        (directory/name).write_text('original unit')
        (state/'worker-profile.json').write_text('original profile')
        drop=directory/(name+'.d')/'85-portable-profile.conf';drop.parent.mkdir();drop.write_text('original override')
        common={'role':'worker','host':host}
        snap=manage.host_action(common|{'action':'snapshot','services':[name]})
        with mock.patch.object(manage,'run'):
            manage.host_action(common|{'action':'install','profile':{'args':[]},'units':{name:'new unit'}})
            self.assertFalse(drop.exists())
            manage.host_action(common|{'action':'restore','snapshot':snap})
        self.assertEqual((directory/name).read_text(),'original unit')
        self.assertEqual((state/'worker-profile.json').read_text(),'original profile')
        self.assertEqual(drop.read_text(),'original override')

    def test_activation_failure_restores_both_hosts_before_restart(self):
        calls=[]
        self.cfg['hosts']['worker']['env']['STATE_DIR']=str(self.root/'state')
        def call(cfg,role,action,**kwargs):
            calls.append((role,action,kwargs))
            if action=='snapshot': return {'units':{},'current':None,'profile':None,'override':None}
            if action=='worker-ready': raise RuntimeError('simulated startup failure')
            return {}
        profiles,units=manage.rendered(self.cfg)
        with mock.patch.object(manage,'call',side_effect=call),self.assertRaisesRegex(RuntimeError,'simulated'):
            manage.activate(self.cfg,profiles,units)
        restored=[r for r,a,k in calls if a=='restore']
        self.assertEqual(restored,['coordinator','worker'])
        self.assertEqual(calls[-1][0:2],('coordinator','control'))
        self.assertEqual(calls[-1][2]['verb'],'start')

    def test_readiness_rejects_an_unexpected_model_or_context(self):
        import io
        value={'loaded':True,'model':'expected','max_context':262144}
        with mock.patch.object(manage.urllib.request,'urlopen',return_value=io.BytesIO(json.dumps(value).encode())):
            self.assertFalse(manage.healthy('http://localhost:8180',expected_model='different'))
        with mock.patch.object(manage.urllib.request,'urlopen',return_value=io.BytesIO(json.dumps(value).encode())):
            self.assertFalse(manage.healthy('http://localhost:8180',expected_context=65536))

    def test_prepare_never_stops_or_starts_services(self):
        import io
        calls=[]
        def call(cfg,role,action,**kwargs):calls.append((role,action));return {}
        process=mock.Mock(stdout=io.BytesIO(b'archive'))
        process.wait.return_value=0
        self.cfg['hosts']['coordinator']['vision_source']='/srv/reader'
        self.cfg['hosts']['worker']['root']=str(config.ROOT)
        with mock.patch.object(sys,'argv',['stack_manage','prepare']),mock.patch.object(manage,'configuration',return_value=self.cfg),mock.patch.object(manage.subprocess,'check_output',side_effect=['','a'*40]),mock.patch.object(manage.subprocess,'Popen',return_value=process),mock.patch.object(manage,'run'),mock.patch.object(manage,'checks'),mock.patch.object(manage,'call',side_effect=call):
            manage.main()
        self.assertEqual(calls,[('worker','build'),('coordinator','build')])

    def test_asset_check_fails_without_writing(self):
        profile={'exe':str(self.root/'absent'),'cwd':str(self.root),'args':[]}
        with self.assertRaisesRegex(ValueError,'does not exist'):config.validate_profile(profile)
        self.assertFalse((self.root/'absent').exists())

    def test_bootstrap_executes_json_payload_without_shell_expansion(self):
        request={'action':'worker-ready','host':self.cfg['hosts']['worker'],'role':'worker','port':51056}
        def local(argv,**kwargs):
            command=shlex.split(argv[-1])
            self.assertEqual(command[:2],['python3','-c'])
            return subprocess.run(command,input=kwargs['input'],text=True,capture_output=True,check=True)
        with mock.patch.object(manage,'run',side_effect=local):
            result=manage.remote(self.cfg,request)
        self.assertIn('ready',result)


if __name__=='__main__': unittest.main()
