"""Environment-only configuration and rendering for the optional two-host RPC stack.

Nothing in this module reads .env files or executes configuration as shell code.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import string
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
SERVICES = {'worker': 'strata-swift-iq3s-rpc-worker.service',
            'coordinator': 'strata-swift-iq3s-rpc-coordinator.service',
            'proxy': 'codex-llama-proxy.service',
            'worker-monitor': 'strata-live-monitor-worker.service',
            'relay': 'strata-live-monitor-relay.service'}


def text(value):
    value = str(value)
    if not value or any(c in value for c in '\n\r\x00'):
        raise ValueError('configuration values must be nonempty, single-line strings')
    return value


def absolute(value):
    value = text(value)
    if value != value.strip():
        raise ValueError('paths must not start or end with whitespace')
    if not Path(value).is_absolute():
        raise ValueError('expected an absolute path: ' + value)
    return value


def endpoint(value):
    value = text(value)
    parsed = urlsplit('//' + value)
    if not parsed.hostname or not parsed.port or parsed.path or parsed.username or parsed.password:
        raise ValueError('expected HOST:PORT: ' + value)
    if not 1 <= parsed.port <= 65535:
        raise ValueError('port outside 1..65535')
    return value


def url(value):
    value = text(value).rstrip('/')
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('expected an HTTP(S) URL without credentials/query/fragment')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('URL port outside 1..65535')
    return value


def unit_value(value, argv=False):
    """Quote a systemd string; prevent specifier and ExecStart dollar expansion."""
    value = text(value).replace('%', '%%')
    if argv:
        value = value.replace('$', '$$')
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def configuration(env=None, root=ROOT):
    env = dict(os.environ if env is None else env)
    def get(key, default=None):
        value = env.get('STRATA_RPC_' + key, default)
        if value is None or value == '':
            raise ValueError('Set STRATA_RPC_' + key)
        return text(value)
    profile = get('PROFILE_NAME', 'swift-iq3s-v0212')
    if not re.fullmatch(r'[A-Za-z0-9._-]+', profile):
        raise ValueError('invalid profile name')
    remote = get('REMOTE')
    if remote.startswith('-') or any(c.isspace() for c in remote):
        raise ValueError('REMOTE must be an SSH alias or user@host')
    worker_bind = endpoint(get('WORKER_BIND'))
    peer = get('WORKER_PEER')
    if any(c.isspace() for c in peer) or '/' in peer:
        raise ValueError('WORKER_PEER must be a hostname or IP address')
    home = absolute(get('COORDINATOR_HOME'))
    base = absolute(get('REMOTE_BASE', home + '/.local/share/strata-rpc'))
    api = url(get('COORDINATOR_URL'))
    proxy_url = url(get('PROXY_URL'))
    api_bind = endpoint(get('COORDINATOR_BIND', '127.0.0.1:8080'))
    proxy_bind = endpoint(get('PROXY_BIND', '127.0.0.1:8081'))
    worker_monitor = endpoint(get('WORKER_MONITOR_BIND'))
    relay_bind = endpoint(get('MONITOR_BIND'))
    origins = json.loads(get('MONITOR_ORIGINS', json.dumps([api, proxy_url])))
    if not isinstance(origins, list) or not origins:
        raise ValueError('MONITOR_ORIGINS must be a nonempty JSON array')
    origins = [url(v) for v in origins]
    for origin in origins:
        if urlsplit(origin).path:
            raise ValueError('monitor origins must not contain a URL path')
    result = {'remote': remote, 'base': base, 'profile': profile, 'api_url': api,
              'proxy_url': proxy_url, 'proxy_stats': absolute(get('PROXY_STATS', home+'/.local/state/codex-llama-proxy/stats.jsonl')), 'worker_bind': worker_bind,
              'worker_monitor': worker_monitor, 'relay_bind': relay_bind,
              'monitor_url': url(get('MONITOR_URL')),
              'interface': get('INTERFACE'), 'origins': origins, 'hosts': {}}
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', result['interface']):
        raise ValueError('invalid network interface name')
    for role, default_root, role_home in [('worker', str(root), str(Path.home())),
                                          ('coordinator', base + '/current', home)]:
        prefix = role.upper() + '_'
        host_root = absolute(get(prefix + 'ROOT', default_root))
        model = absolute(get(prefix + 'MODEL_ROOT'))
        build = get(prefix + 'BUILD_DIR', 'build-rpc-' + role)
        if Path(build).is_absolute() or '..' in Path(build).parts:
            raise ValueError(prefix + 'BUILD_DIR must stay inside the runtime root')
        cuda = absolute(get(prefix + 'CUDA_ROOT', '/usr/local/cuda'))
        state = absolute(get(prefix + 'STATE_ROOT', role_home + '/.local/state/strata-stage-rpc')) + '/' + profile
        values = {'STRATA_ROOT': host_root, 'MODEL_DATA_ROOT': model,
                  'NATIVE_DATA_ROOT': absolute(get(prefix + 'NATIVE_ROOT', model)),
                  'TLS_DIR': absolute(get(prefix + 'TLS_DIR', role_home + '/.local/share/strata-stage-rpc/tls')),
                  'STATE_DIR': state, 'RPC_ENGINE': host_root + '/' + build + '/strata',
                  'RPC_STAGE_ADDRESS': worker_bind, 'RPC_ALLOWED_PEER': peer,
                  'RPC_CUDA_BIN': cuda + '/bin', 'RPC_CUDA_LIB': cuda + '/lib64'}
        result['hosts'][role] = {'root': host_root, 'home': role_home, 'env': values,
            'python': absolute(get(prefix + 'PYTHON', sys.executable if role == 'worker' else base + '/python-env/bin/python')),
            'build_dir': build, 'cuda_compiler': absolute(get(prefix + 'CUDA_COMPILER', cuda + '/bin/nvcc')),
            'arch': get(prefix + 'CUDA_ARCH', '86' if role == 'worker' else '120'),
            'unit_dir': absolute(get(prefix + 'UNIT_DIR', role_home + '/.config/systemd/user'))}
    result['hosts']['coordinator']['vision_source'] = env.get('STRATA_RPC_VISION_EXE', '')
    result['hosts']['coordinator']['vision_llama_dir'] = env.get('STRATA_RPC_VISION_LLAMA_DIR', '')
    result['api_bind'] = api_bind
    result['proxy_bind'] = proxy_bind
    result['jobs'] = int(get('BUILD_JOBS', '6'))
    if result['jobs'] < 1:
        raise ValueError('BUILD_JOBS must be positive')
    return result


def expand(value, env):
    if isinstance(value, str):
        try:
            return string.Template(value).substitute(env)
        except KeyError as e:
            raise ValueError('Missing profile variable: ' + str(e.args[0])) from e
    if isinstance(value, list):
        return [expand(v, env) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, env) for k, v in value.items()}
    return value


def profile_for(config, role, source=ROOT):
    data = json.loads((Path(source)/'profiles/rpc'/config['profile']/(role+'.json')).read_text())
    data = expand(data, config['hosts'][role]['env'])
    if role == 'coordinator':
        addr = urlsplit('//' + config['api_bind'])
        data['host'], data['port'] = addr.hostname, addr.port
    return data


def validate_profile(profile, manifest=None):
    paths = [profile['exe'], profile['cwd']]
    args = profile['args']
    for i, value in enumerate(args[:-1]):
        if value in ('--pack', '--native', '--ple-gguf', '--expert-profile', '--rpc-ca', '--rpc-cert',
                     '--rpc-key', '--mtp', '--native-dense-gguf'):
            paths.append(args[i+1])
    if 'tokenizer' in profile:
        paths.append(profile['tokenizer'])
    if profile.get('vision'):
        paths.extend(profile['vision'][k] for k in ('exe', 'mmproj', 'model'))
    for value in paths:
        if not Path(value).exists():
            raise ValueError('Profile path does not exist: ' + value)
    if not os.access(profile['exe'], os.X_OK):
        raise ValueError('Engine is not executable')
    if manifest:
        native = Path(args[args.index('--native')+1]).parent
        for item in manifest['files']:
            path = native/item['name']
            stat = path.stat()
            verified = json.loads(path.with_suffix('.verified.json').read_text())
            if stat.st_size != item['bytes'] or verified['sha256'] != item['sha256'] or verified['mtime_ns'] != stat.st_mtime_ns:
                raise ValueError('Model shard verification does not match: ' + item['name'])


def render_unit(config, kind, source=ROOT):
    role = 'worker' if kind in ('worker', 'worker-monitor') else 'coordinator'
    host = config['hosts'][role]
    env = dict(host['env'])
    state, root = env['STATE_DIR'], host['root']
    script = root + '/tools/rpc/'
    python = host['python']
    if kind == 'worker':
        args = [python, script+'strata_stage_rpc.py', 'worker', '--config', state+'/worker-profile.json']
    elif kind == 'coordinator':
        addr = urlsplit('//'+config['api_bind'])
        args = [python, script+'strata_stage_rpc.py', 'coordinator', '--config', state+'/coordinator-profile.json', '--state-dir', state, '--host', addr.hostname, '--port', str(addr.port)]
        env['STRATA_API_KEY'] = 'none'
    elif kind == 'proxy':
        args = [python, script+'codex_llama_proxy.py', '--listen', config['proxy_bind'], '--upstream', config['api_url'], '--stats-file', config['proxy_stats'], '--debug']
    elif kind == 'worker-monitor':
        args = [python, script+'live_monitor.py', 'worker', '--listen', config['worker_monitor']]
    else:
        args = [python, script+'live_monitor.py', 'relay', '--listen', config['relay_bind'], '--interface', config['interface'], '--upstream', 'http://'+config['worker_monitor']+'/metrics']
        for origin in config['origins']:
            args.extend(['--origin', origin])
    values = {'WORKING_DIRECTORY': text(root).replace('%', '%%'),
              'ENVIRONMENT': '\n'.join('Environment='+unit_value(k+'='+v) for k,v in env.items()),
              'EXEC_START': ' '.join(unit_value(a, argv=True) for a in args),
              'STDOUT': 'append:'+text(state).replace('%', '%%')+'/'+kind+'.log' if kind in ('worker','coordinator') else 'journal',
              'STDERR': 'append:'+text(state).replace('%', '%%')+'/'+kind+'.log' if kind in ('worker','coordinator') else 'journal'}
    template = (Path(source)/'packaging/rpc/systemd'/SERVICES[kind]).read_text()
    for key, value in values.items():
        template = template.replace('@'+key+'@', value)
    if re.search(r'@[A-Z_]+@', template):
        raise ValueError('unrendered systemd template')
    return template


def monitor_js(config):
    return 'export default '+json.dumps({'url': config['monitor_url']})+';\n'
