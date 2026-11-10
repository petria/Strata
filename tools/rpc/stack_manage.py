#!/usr/bin/env python3
"""Manage the optional RPC stack using explicit environment configuration."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import urllib.request

from stack_config import ROOT, SERVICES, configuration, monitor_js, profile_for, render_unit, validate_profile


def run(argv, **kwargs):
    return subprocess.run(argv, check=True, **kwargs)


def host_action(request):
    """Small host operations. Requests travel as JSON on stdin, never shell assignments."""
    action, host = request['action'], request['host']
    env = host['env']
    if action == 'check':
        validate_profile(request['profile'], request.get('manifest'))
        if not Path(host['python']).is_file():
            raise ValueError('Python interpreter does not exist: '+host['python'])
        return {'ready': True}
    if action == 'control':
        run(['systemctl', '--user', request['verb'], *request['services']])
        return {}
    if action == 'worker-ready':
        p = subprocess.run(['ss', '-H', '-ltn', 'sport = :'+str(request['port'])], capture_output=True, text=True, check=True)
        return {'ready': bool(p.stdout.strip())}
    if action == 'snapshot':
        directory = Path(host['unit_dir'])
        if request.get('current') and Path(request['current']).exists() and not Path(request['current']).is_symlink():
            raise ValueError('The managed coordinator root must be a symlink before release activation')
        return {'units': {name: (directory/name).read_text() if (directory/name).exists() else None for name in request['services']},
                'current': os.readlink(request['current']) if request.get('current') and Path(request['current']).is_symlink() else None,
                'profile': (Path(env['STATE_DIR'])/(request['role']+'-profile.json')).read_text() if (Path(env['STATE_DIR'])/(request['role']+'-profile.json')).exists() else None,
                'override': (directory/(SERVICES[request['role']]+'.d')/'85-portable-profile.conf').read_text() if (directory/(SERVICES[request['role']]+'.d')/'85-portable-profile.conf').exists() else None}
    if action == 'restore':
        for name, value in request['snapshot']['units'].items():
            p = Path(host['unit_dir'])/name
            if value is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(value)
        for path, value in [(Path(env['STATE_DIR'])/(request['role']+'-profile.json'), request['snapshot'].get('profile')),
                            (Path(host['unit_dir'])/(SERVICES[request['role']]+'.d')/'85-portable-profile.conf', request['snapshot'].get('override'))]:
            if value is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(value)
                path.chmod(0o600)
        if request.get('current') and request['snapshot'].get('current'):
            switch_link(request['current'], request['snapshot']['current'])
        run(['systemctl', '--user', 'daemon-reload'])
        return {}
    if action == 'install':
        state = Path(env['STATE_DIR'])
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory = Path(host['unit_dir']); directory.mkdir(parents=True, exist_ok=True)
        if 'profile' in request:
            target = state/(request['role']+'-profile.json')
            target.write_text(json.dumps(request['profile'], indent=2)+'\n')
            target.chmod(0o600)
        for name, value in request['units'].items():
            (directory/name).write_text(value)
        if 'profile' in request:
            (directory/(SERVICES[request['role']]+'.d')/'85-portable-profile.conf').unlink(missing_ok=True)
        if request.get('release'):
            switch_link(request['current'], request['release'])
        run(['systemctl', '--user', 'daemon-reload'])
        return {}
    if action == 'monitor-files':
        for relative, data in request['files'].items():
            target = Path(host['root'])/relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(data)
        return {}
    if action == 'build':
        release = Path(request['release'])
        vision = host.get('vision_source')
        llama = host.get('vision_llama_dir')
        if request['role'] == 'coordinator':
            if vision:
                if not Path(vision).is_file() or not os.access(vision, os.X_OK):
                    raise ValueError('STRATA_RPC_VISION_EXE is not executable')
            elif not llama or not (Path(llama)/'CMakeLists.txt').is_file():
                raise ValueError('Set STRATA_RPC_VISION_EXE or STRATA_RPC_VISION_LLAMA_DIR before building')
        build = release/host['build_dir']
        args = ['cmake', '-S', str(release), '-B', str(build), '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release',
                '-DCMAKE_CUDA_COMPILER='+host['cuda_compiler'], '-DCMAKE_CUDA_ARCHITECTURES='+host['arch'],
                '-DSTRATA_ENABLE_CUDA=ON', '-DSTRATA_ENABLE_STAGE_RPC=ON', '-DSTRATA_ENABLE_RPC_RDMA=ON',
                '-DSTRATA_MMQ_KQUANTS=ON', '-DSTRATA_MMQ_TQ_OVERLAY=ON', '-DSTRATA_NATIVE_EXPERTS=ON',
                '-DSTRATA_Q6K_EXPERTS=ON', '-DSTRATA_PORTABLE=OFF', '-DSTRATA_BUILD_TESTS=OFF',
                '-DSTRATA_PLE_FIXTURE_DIR='+str(release/'bench/micro')]
        run(args, stdout=sys.stderr)
        run(['cmake', '--build', str(build), '--target', 'strata', '--parallel', str(request['jobs'])], stdout=sys.stderr)
        if request['role'] == 'coordinator':
            import shutil
            target = release/'engine/strata-vision'; target.parent.mkdir(parents=True, exist_ok=True)
            if not vision:
                vb = release/'build-vision'
                run(['cmake', '-S', str(release/'tools/vision'), '-B', str(vb), '-G', 'Ninja', '-DCMAKE_BUILD_TYPE=Release', '-DLLAMA_DIR='+llama, '-DSTRATA_VISION_CUDA=OFF', '-DSTRATA_PORTABLE=OFF'], stdout=sys.stderr)
                run(['cmake', '--build', str(vb), '--target', 'strata-vision', '--parallel', str(request['jobs'])], stdout=sys.stderr)
                vision = str(vb/'bin/strata-vision')
            shutil.copy2(vision, target)
            python = Path(host['python'])
            if not python.exists():
                run(['python3', '-m', 'venv', str(python.parent.parent)], stdout=sys.stderr)
            run([str(python), '-m', 'pip', 'install', '-r', str(release/'requirements.txt')], stdout=sys.stderr)
        return {}
    raise ValueError('unknown host operation: '+action)


def switch_link(current, target):
    path = Path(current)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.next')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(target)
    temporary.replace(path)


def remote(config, request):
    # Only program source is executed. Configuration itself is parsed as JSON.
    payload = {'request': request, 'config_source': (ROOT/'tools/rpc/stack_config.py').read_text(),
               'manage_source': (ROOT/'tools/rpc/stack_manage.py').read_text()}
    bootstrap = """import json,sys,types
p=json.load(sys.stdin)
c=types.ModuleType('stack_config');c.__file__='/rpc/bootstrap/tools/rpc/stack_config.py'
sys.modules['stack_config']=c
exec(compile(p['config_source'],c.__file__,'exec'),c.__dict__)
n={'__name__':'rpc_host','__file__':'/rpc/bootstrap/tools/rpc/stack_manage.py'}
exec(compile(p['manage_source'],n['__file__'],'exec'),n)
print(json.dumps(n['host_action'](p['request'])))
"""
    result = run(['ssh', '-o', 'BatchMode=yes', '--', config['remote'], shlex.join(['python3', '-c', bootstrap])],
                 input=json.dumps(payload), text=True, stdout=subprocess.PIPE)
    return json.loads(result.stdout)


def call(config, role, action, **kwargs):
    request = {'action': action, 'host': config['hosts'][role], 'role': role, **kwargs}
    return host_action(request) if role == 'worker' else remote(config, request)


def rendered(config):
    # Complete local rendering before any remote writes or service operations.
    profiles = {role: profile_for(config, role) for role in ('worker', 'coordinator')}
    units = {kind: render_unit(config, kind) for kind in SERVICES}
    return profiles, units


def checks(config, profiles, staged=None):
    manifest_path = ROOT/'profiles/rpc'/config['profile']/'model-manifest.json'
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else None
    call(config, 'worker', 'check', profile=profiles['worker'], manifest=manifest)
    co = copy.deepcopy(profiles['coordinator'])
    if staged:
        current = config['hosts']['coordinator']['root']
        def substitute(v):
            if isinstance(v, str):
                return staged+v[len(current):] if v == current or v.startswith(current+'/') else v
            if isinstance(v, list): return [substitute(x) for x in v]
            if isinstance(v, dict): return {k: substitute(x) for k,x in v.items()}
            return v
        co = substitute(co)
    call(config, 'coordinator', 'check', profile=co, manifest=manifest)


def wait_for(predicate, timeout, description):
    end = time.monotonic()+timeout
    while time.monotonic() < end:
        if predicate(): return
        time.sleep(2)
    raise RuntimeError('Timed out waiting for '+description)


def healthy(base, models=False, expected_model=None, expected_context=None):
    try:
        with urllib.request.urlopen(base+('/v1/models' if models else '/health'), timeout=5) as response:
            value = json.load(response)
        if models:
            return bool(value.get('data')) and (expected_model is None or value['data'][0].get('id') == expected_model)
        return bool(value.get('loaded')) and (expected_model is None or value.get('model') == expected_model) and (expected_context is None or value.get('max_context') == expected_context)
    except (OSError, ValueError):
        return False


def activate(config, profiles, units, release=None):
    worker_services = [SERVICES['worker']]
    co_services = [SERVICES['coordinator'], SERVICES['proxy']]
    current = config['hosts']['coordinator']['root'] if release else None
    snapshots = {r: call(config, r, 'snapshot', services=s, current=current if r == 'coordinator' else None)
                 for r,s in [('worker', worker_services), ('coordinator', co_services)]}
    backup = Path(config['hosts']['worker']['env']['STATE_DIR'])/'unit-backups'/time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    (backup/'units.json').write_text(json.dumps(snapshots, indent=2)); (backup/'units.json').chmod(0o600)
    try:
        call(config, 'coordinator', 'control', verb='stop', services=list(reversed(co_services)))
        call(config, 'worker', 'control', verb='stop', services=worker_services)
        call(config, 'worker', 'install', profile=profiles['worker'], units={SERVICES['worker']: units['worker']})
        call(config, 'coordinator', 'install', profile=profiles['coordinator'], units={SERVICES[k]: units[k] for k in ('coordinator','proxy')}, release=release, current=current)
        call(config, 'worker', 'control', verb='start', services=worker_services)
        port = int(config['worker_bind'].rsplit(':',1)[1])
        wait_for(lambda: call(config, 'worker', 'worker-ready', port=port)['ready'], 900, 'worker listener')
        call(config, 'coordinator', 'control', verb='start', services=[SERVICES['coordinator']])
        wait_for(lambda: healthy(config['api_url'], expected_model=profiles['coordinator']['model_name'], expected_context=int(profiles['coordinator']['args'][profiles['coordinator']['args'].index('--max-context')+1])), 600, 'coordinator model')
        call(config, 'coordinator', 'control', verb='start', services=[SERVICES['proxy']])
        wait_for(lambda: healthy(config['proxy_url'], True, expected_model=profiles['coordinator']['model_name']), 90, 'proxy')
    except BaseException:
        call(config, 'coordinator', 'control', verb='stop', services=list(reversed(co_services)))
        call(config, 'worker', 'control', verb='stop', services=worker_services)
        for role in ('coordinator','worker'):
            call(config, role, 'restore', snapshot=snapshots[role], current=current if role == 'coordinator' else None)
        call(config, 'worker', 'control', verb='start', services=worker_services)
        call(config, 'coordinator', 'control', verb='start', services=co_services)
        raise
    print('RPC stack ready; previous units saved privately at', backup)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('render','check','activate','prepare','deploy','deploy-activate','monitor'))
    parser.add_argument('--profile')
    parser.add_argument('--remote')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    env = dict(os.environ)
    if args.profile: env['STRATA_RPC_PROFILE_NAME'] = args.profile
    if args.remote: env['STRATA_RPC_REMOTE'] = args.remote
    try:
        cfg = configuration(env)
        profiles, units = rendered(cfg)
        if args.command == 'render':
            if args.output is None: parser.error('render requires --output')
            args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
            for kind, value in units.items(): (args.output/SERVICES[kind]).write_text(value)
            for role, data in profiles.items():
                p=args.output/(role+'-profile.json');p.write_text(json.dumps(data,indent=2)+'\n');p.chmod(0o600)
            (args.output/'rpc-live-config.js').write_text(monitor_js(cfg))
            print('Rendered RPC configuration at', args.output)
            return
        if args.command == 'monitor':
            # Settings for both nodes have already been validated; stage files before restarting sidecars.
            files = {p: (ROOT/p).read_text() for p in ('tools/rpc/live_monitor.py','serve/web/rpc-live-monitor.js','serve/web/app.js')}
            files['serve/web/rpc-live-config.js'] = monitor_js(cfg)
            call(cfg,'coordinator','monitor-files',files=files)
            for role, kinds in [('worker',['worker-monitor']),('coordinator',['relay'])]:
                call(cfg,role,'install',units={SERVICES[k]:units[k] for k in kinds})
                call(cfg,role,'control',verb='enable',services=[SERVICES[k] for k in kinds])
                call(cfg,role,'control',verb='restart',services=[SERVICES[k] for k in kinds])
            return
        release = None
        if args.command in ('prepare','deploy','deploy-activate'):
            if subprocess.check_output(['git','-C',str(ROOT),'status','--porcelain'],text=True).strip():
                raise ValueError('Commit source changes before deployment')
            revision = subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()
            release = cfg['base']+'/releases/'+revision
            co = cfg['hosts']['coordinator']
            if not co['vision_source'] and not co['vision_llama_dir']:
                raise ValueError('Set STRATA_RPC_VISION_EXE or STRATA_RPC_VISION_LLAMA_DIR')
            if args.command != 'deploy-activate':
                if Path(cfg['hosts']['worker']['root']).resolve() != ROOT.resolve():
                    raise ValueError('WORKER_ROOT must name this source checkout when building a matched pair')
                call(cfg,'worker','build',release=cfg['hosts']['worker']['root'],jobs=cfg['jobs'])
                # Shell tokens are individually quoted, including the remote release path.
                cmd='mkdir -p '+shlex.quote(release)+' && tar -xf - -C '+shlex.quote(release)
                archive = subprocess.Popen(['git','-C',str(ROOT),'archive','--format=tar',revision],stdout=subprocess.PIPE)
                try:
                    run(['ssh','-o','BatchMode=yes','--',cfg['remote'],cmd],stdin=archive.stdout)
                finally:
                    archive.stdout.close()
                if archive.wait(): raise RuntimeError('git archive failed')
                call(cfg,'coordinator','build',release=release,jobs=cfg['jobs'])
        checks(cfg, profiles, release)
        if args.command in ('activate','deploy','deploy-activate'):
            activate(cfg, profiles, units, release)
        else:
            print('RPC preflight passed; inference services were not restarted')
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as e:
        parser.exit(2, 'RPC configuration error: '+str(e)+'\n')


if __name__ == '__main__':
    main()
