#!/usr/bin/env python3
"""Bounded no-GPU, two-host Connection/Client integration tests.

Uses fresh temporary binaries and certificates; never restarts a service.
"""
import argparse
import datetime
import getpass
import hashlib
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import ssl
import struct
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = Path('/home/operator/AI-tools/bench/strata-rdma-20261026/transport-tests.json')


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--remote', default='operator@192.0.2.10')
    ap.add_argument('--host-key-alias', default='192.0.2.10', help='Existing known_hosts identity for coordinator.example')
    ap.add_argument('--local-ip', default='192.0.2.20')
    ap.add_argument('--peer-ip', default='192.0.2.10')
    ap.add_argument('--port', type=int, default=50166)
    ap.add_argument('--rdma-port', type=int, default=50168)
    ap.add_argument('--interface', default='roce0')
    ap.add_argument('--rdma-depth', type=int, choices=(2, 4, 8, 16), default=2)
    ap.add_argument('--credit-batch', type=int, default=1)
    ap.add_argument('--poll-mode', choices=('event', 'adaptive', 'spin'), default='adaptive')
    ap.add_argument('--spin-us', type=int, default=50)
    ap.add_argument('--binary', type=Path, help='reuse an already compiled transport fixture binary')
    ap.add_argument('--tls-dir', default='/home/operator/.local/share/strata-stage-rpc/tls')
    ap.add_argument('--timeout', type=int, default=5)
    ap.add_argument('--case-timeout', type=int, default=35)
    ap.add_argument('--large-payloads', action='store_true', help='Also allocate ~512 MiB payloads (several GiB peak per host)')
    ap.add_argument('--large-payload-timeout', type=int, default=30,
                    help='Operation timeout for optional ~512 MiB correctness cases; normal cases retain --timeout')
    ap.add_argument('--only-case', action='append', default=[], help='Run only these exact case names (repeatable)')
    ap.add_argument('--close-repetitions', type=int, default=20,
                    help='Repeat immediate server exit after final reply (alternating short/32 MiB payloads)')
    ap.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = ap.parse_args()
    if not 1 <= args.timeout <= 3600 or args.case_timeout < args.timeout + 5 or args.close_repetitions < 0:
        ap.error('timeout must be 1..3600 and case-timeout at least timeout+5')
    if not 1 <= args.credit_batch <= args.rdma_depth or not 0 <= args.spin_us <= 1000:
        ap.error('credit-batch must be 1..rdma-depth and spin-us 0..1000')
    if not 1 <= args.large_payload_timeout <= 3600:
        ap.error('large-payload-timeout must be 1..3600')
    ssh_options = ['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '-o', 'StrictHostKeyChecking=yes']
    if args.host_key_alias:
        ssh_options += ['-o', 'HostKeyAlias=' + args.host_key_alias]
    ssh = ['ssh', *ssh_options, args.remote]
    remote_user = args.remote.rsplit('@',1)[0] if '@' in args.remote else getpass.getuser()
    scp = ['scp', *ssh_options]
    report = {'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'configuration': vars(args) | {'output': str(args.output),
                  'binary': str(args.binary) if args.binary else None}, 'tests': [], 'commands': []}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        tmp = args.output.with_suffix('.tmp')
        tmp.write_text(json.dumps(report, indent=2) + '\n')
        tmp.replace(args.output)

    def command(argv, timeout=30):
        start = time.monotonic()
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
            result = {'argv': argv, 'returncode': p.returncode, 'stdout': p.stdout, 'stderr': p.stderr,
                      'seconds': round(time.monotonic() - start, 3)}
        except subprocess.TimeoutExpired as e:
            result = {'argv': argv, 'returncode': None, 'timed_out': True,
                      'stdout': (e.stdout or b'').decode(), 'stderr': (e.stderr or b'').decode()}
        report['commands'].append(result)
        save()
        if result['returncode'] != 0:
            raise RuntimeError('command failed: ' + shlex.join(argv) + '\n' + result['stderr'])
        return result['stdout'].strip()

    remote_dir = None
    save()
    try:
        with tempfile.TemporaryDirectory(prefix='strata-connection-rdma-') as tmp:
            tmp = Path(tmp)
            exe = args.binary.resolve() if args.binary else tmp / 'transport-fixture'
            sources = ['tests/rpc/transport_fixture.cpp', 'src/rpc/transport.cpp',
                       'src/rpc/client.cpp', 'src/rpc/rdma_transport.cpp']
            report['source_sha256'] = {s: hashlib.sha256((ROOT / s).read_bytes()).hexdigest() for s in sources}
            if args.binary:
                if not exe.is_file() or not os.access(exe,os.X_OK):
                    raise RuntimeError(f'fixture binary is not executable: {exe}')
                report['reused_binary']={'path':str(exe),'sha256':hashlib.sha256(exe.read_bytes()).hexdigest()}
            else:
                command(['g++', '-std=c++20', '-O2', '-pthread', '-Wall', '-Wextra', '-Werror',
                         '-DSTRATA_HAS_RPC_RDMA=1', '-I' + str(ROOT / 'include'),
                         *[str(ROOT / s) for s in sources], '-lssl', '-lcrypto', '-libverbs', '-lrdmacm',
                         '-o', str(exe)], timeout=120)
            command([*ssh, 'hostname; ip -4 addr show ' + shlex.quote(args.interface)])
            remote_dir = command([*ssh, 'mktemp -d /tmp/strata-connection-rdma-XXXXXXXX'])
            if not remote_dir.startswith('/tmp/strata-connection-rdma-') or '\n' in remote_dir:
                raise RuntimeError('unexpected remote temporary directory')
            remote_exe = remote_dir + '/transport-fixture'
            command([*scp, str(exe), args.remote + ':' + remote_exe])
            local_prefix=[]; remote_prefix=[]
            if args.rdma_depth > 2:
                limit='--memlock=67108864:67108864'
                local_prefix=['sudo','-n','--preserve-env=RPC_TEST_ALLOW_PEER,RPC_TEST_INTERFACE,RPC_TEST_RDMA_PORT,RPC_TEST_TIMEOUT,RPC_TEST_TRANSPORT,RPC_TEST_PAYLOAD_BYTES,RPC_TEST_RDMA_DEPTH,RPC_TEST_RDMA_CREDIT_BATCH,RPC_TEST_RDMA_POLL_MODE,RPC_TEST_RDMA_SPIN_US',
                    'prlimit',limit,'--','runuser','--preserve-environment','-u',getpass.getuser(),'--']
                remote_prefix=['sudo','-n','prlimit',limit,'--','runuser','--preserve-environment','-u',remote_user,'--']
                report['memlock_test_limit_bytes']=67108864
            # A fresh CA-signed wrong-role certificate exercises identity checks,
            # without requiring the remote worker private key or changing TLS files.
            command(['openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes',
                     '-keyout', str(tmp / 'wrong-role.key'), '-out', str(tmp / 'wrong-role.csr'),
                     '-subj', '/CN=wrong-role', '-addext', 'subjectAltName=DNS:wrong-role'])
            command(['openssl', 'x509', '-req', '-in', str(tmp / 'wrong-role.csr'),
                     '-CA', args.tls_dir + '/ca.crt', '-CAkey', args.tls_dir + '/ca.key',
                     '-set_serial', str(int.from_bytes(os.urandom(16), 'big')), '-days', '1',
                     '-copy_extensions', 'copy', '-out', str(tmp / 'wrong-role.crt')])
            remote_tls = remote_dir + '/tls'
            setup = 'mkdir ' + shlex.quote(remote_tls)
            for name in ('ca.crt', 'strata-stage-coordinator.crt', 'strata-stage-coordinator.key'):
                setup += '; ln -s ' + shlex.quote(args.tls_dir + '/' + name) + ' ' + shlex.quote(remote_tls + '/' + name)
            command([*ssh, setup])
            command([*scp, str(tmp / 'wrong-role.crt'), str(tmp / 'wrong-role.key'), args.remote + ':' + remote_tls + '/'])
            endpoint = f'{args.local_ip}:{args.port}'
            base_env = {k: v for k, v in os.environ.items() if not k.startswith('RPC_TEST_')}
            settings = {'RPC_TEST_ALLOW_PEER': args.peer_ip, 'RPC_TEST_INTERFACE': args.interface,
                        'RPC_TEST_RDMA_PORT': str(args.rdma_port), 'RPC_TEST_TIMEOUT': str(args.timeout),
                        'RPC_TEST_RDMA_DEPTH': str(args.rdma_depth),
                        'RPC_TEST_RDMA_CREDIT_BATCH': str(args.credit_batch),
                        'RPC_TEST_RDMA_POLL_MODE': args.poll_mode,
                        'RPC_TEST_RDMA_SPIN_US': str(args.spin_us)}

            def pair(name, server_transport='rdma', client_transport='rdma', payload=32 * 1024**2 + 31,
                     idle=False, server_role='strata-stage-worker', client_role='strata-stage-coordinator',
                     interface=None, expected_error=None, client_rdma_port=None, final_reply=False,
                     operation_timeout=None):
                if args.only_case and name not in args.only_case:
                    return
                start = time.monotonic()
                env = settings | {'RPC_TEST_TRANSPORT': server_transport, 'RPC_TEST_PAYLOAD_BYTES': str(payload)}
                if operation_timeout is not None:
                    env['RPC_TEST_TIMEOUT'] = str(operation_timeout)
                outer_timeout = max(args.case_timeout, int(env['RPC_TEST_TIMEOUT']) + 5)
                if interface:
                    env['RPC_TEST_INTERFACE'] = interface
                # Check availability without connecting to the single-accept fixture.
                with socket.socket() as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind((args.local_ip, args.port))
                server = subprocess.Popen([*local_prefix,str(exe), 'server', endpoint, args.tls_dir, server_role],
                                          env=base_env | env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                          text=True, start_new_session=True)
                record = {'name': name, 'server_transport': server_transport, 'client_transport': client_transport,
                          'payload_bytes': payload, 'idle_seconds': args.timeout + 2 if idle else 0,
                          'expected_error': expected_error, 'full_sized_final_reply': final_reply,
                          'operation_timeout_seconds': int(env['RPC_TEST_TIMEOUT']),
                          'outer_timeout_seconds': outer_timeout}
                try:
                    time.sleep(0.3)
                    client_env = env | {'RPC_TEST_TRANSPORT': client_transport}
                    if client_rdma_port:
                        client_env['RPC_TEST_RDMA_PORT'] = str(client_rdma_port)
                    # GNU timeout runs on the remote host too, so an SSH disconnect
                    # cannot leave our client alive indefinitely.
                    remote_cmd = [*remote_prefix,'timeout', '-k', '2s', str(outer_timeout - 3) + 's', 'env',
                                  *[k + '=' + v for k, v in client_env.items()], remote_exe,
                                  'idle-client' if idle else ('final-client' if final_reply else 'client'),
                                  endpoint, remote_tls, client_role]
                    client = subprocess.run([*ssh, shlex.join(remote_cmd)], capture_output=True,
                                            text=True, timeout=outer_timeout)
                    record['client'] = {'returncode': client.returncode, 'stdout': client.stdout, 'stderr': client.stderr}
                    # A rejected oversized send leaves the server waiting for the
                    # next request until client disconnect; bounded wait covers it.
                    out, err = server.communicate(timeout=8)
                    record['server'] = {'returncode': server.returncode, 'stdout': out, 'stderr': err}
                    if expected_error:
                        errors = client.stderr + '\n' + err
                        record['passed'] = (client.returncode not in (0, 124, 137, 255) and server.returncode != 0
                                            and any(s in errors for s in expected_error))
                    else:
                        record['passed'] = client.returncode == 0 and server.returncode == 0
                        if record['passed']:
                            sm = json.loads(out.strip().splitlines()[-1])
                            cm = json.loads(client.stdout.strip().splitlines()[-1])
                            record['server_metrics'], record['client_metrics'] = sm, cm
                            record['passed'] = (sm.get('backend') == server_transport and
                                                cm.get('transport', {}).get('backend') == client_transport)
                            if name == 'tcp-baseline':
                                record['passed'] = (record['passed'] and sm.get('backend') == 'tcp' and
                                                    cm.get('transport', {}).get('backend') == 'tcp' and
                                                    sm.get('payload_encrypted') is True and
                                                    cm.get('transport', {}).get('payload_encrypted') is True)
                except (subprocess.TimeoutExpired, ValueError) as e:
                    record.update(passed=False, error=str(e))
                finally:
                    if server.poll() is None:
                        os.killpg(server.pid, signal.SIGKILL)
                        out, err = server.communicate()
                        record['server_cleanup'] = {'stdout': out, 'stderr': err, 'killed_owned_pid': server.pid}
                    record['seconds'] = round(time.monotonic() - start, 3)
                    report['tests'].append(record)
                    save()
                    print(name + ': ' + ('PASS' if record['passed'] else 'FAIL'), flush=True)

            def slow_bootstrap(kind):
                """Drip valid proposal/cookie bytes; progress must not renew deadline."""
                if args.only_case and 'rdma-slow-tls-' + kind not in args.only_case:
                    return
                record = {'name': 'rdma-slow-tls-' + kind, 'configured_timeout': args.timeout}
                proc = None
                start = time.monotonic()
                stream = None
                listener = None
                try:
                    env = settings | {'RPC_TEST_TRANSPORT': 'rdma'}
                    if kind == 'proposal':
                        env['RPC_TEST_ALLOW_PEER'] = args.local_ip
                        proc = subprocess.Popen([*local_prefix,str(exe), 'server', endpoint, args.tls_dir, 'strata-stage-worker'],
                                                env=base_env | env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                text=True, start_new_session=True)
                        time.sleep(0.3)
                        ctx = ssl.create_default_context(cafile=args.tls_dir + '/ca.crt')
                        ctx.load_cert_chain(args.tls_dir + '/strata-stage-coordinator.crt',
                                            args.tls_dir + '/strata-stage-coordinator.key')
                        ctx.set_alpn_protocols(['strata-rdma-v1'])
                        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
                        stream = ctx.wrap_socket(socket.create_connection((args.local_ip, args.port), timeout=3),
                                                 server_hostname='strata-stage-worker')
                        data = struct.pack('<QQ', 0x31414d4452415453, args.rdma_port)
                    else:
                        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                        ctx.minimum_version = ssl.TLSVersion.TLSv1_3
                        ctx.load_cert_chain(args.tls_dir + '/strata-stage-worker.crt',
                                            args.tls_dir + '/strata-stage-worker.key')
                        ctx.load_verify_locations(args.tls_dir + '/ca.crt')
                        ctx.verify_mode = ssl.CERT_REQUIRED
                        ctx.set_alpn_protocols(['strata-rdma-v1'])
                        listener = socket.socket()
                        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        listener.bind((args.local_ip, args.port))
                        listener.listen(1)
                        listener.settimeout(8)
                        remote_cmd = [*remote_prefix,'timeout', '-k', '2s', str(args.case_timeout - 3) + 's', 'env',
                                      *[k + '=' + v for k, v in env.items()], remote_exe,
                                      'client', endpoint, remote_tls, 'strata-stage-coordinator']
                        proc = subprocess.Popen([*ssh, shlex.join(remote_cmd)], stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, text=True, start_new_session=True)
                        peer, address = listener.accept()
                        peer.settimeout(3)
                        if address[0] != args.peer_ip:
                            peer.close()
                            raise RuntimeError('unexpected slow-cookie client IP')
                        stream = ctx.wrap_socket(peer, server_side=True)
                        proposal = b''
                        while len(proposal) < 16:
                            part = stream.recv(16 - len(proposal))
                            if not part:
                                raise RuntimeError('client closed before proposal')
                            proposal += part
                        if proposal != struct.pack('<QQ', 0x31414d4452415453, args.rdma_port):
                            raise RuntimeError('unexpected RDMA proposal')
                        data = os.urandom(32)
                    if stream.selected_alpn_protocol() != 'strata-rdma-v1':
                        raise RuntimeError('RDMA ALPN not negotiated')
                    established = time.monotonic()
                    sent = 0
                    # Every individual write arrives within the configured read
                    # timeout; completing the whole message would exceed it.
                    interval = (args.timeout + 2) / len(data)
                    for byte in data:
                        if proc.poll() is not None:
                            break
                        try:
                            stream.sendall(bytes([byte]))
                            sent += 1
                        except OSError:
                            break
                        time.sleep(interval)
                    # Keep TLS open while collecting the fixture result: our
                    # close must not be the event causing the expected rejection.
                    out, err = proc.communicate(timeout=args.timeout + 2)
                    elapsed = time.monotonic() - established
                    record.update(returncode=proc.returncode, stdout=out, stderr=err,
                                  bootstrap_seconds=round(elapsed, 3), bytes_dripped=sent)
                    record['passed'] = (proc.returncode not in (0, 124, 137, 255) and
                                        'RDMA bootstrap deadline exceeded' in err and
                                        args.timeout - 0.5 <= elapsed <= args.timeout + 1.5 and sent < len(data))
                except Exception as e:
                    record.update(passed=False, error=str(e))
                finally:
                    if stream:
                        stream.close()
                    if listener:
                        listener.close()
                    if proc and proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                        out, err = proc.communicate()
                        record['cleanup'] = {'stdout': out, 'stderr': err, 'killed_owned_pid': proc.pid}
                    record['seconds'] = round(time.monotonic() - start, 3)
                    report['tests'].append(record)
                    save()
                    print(record['name'] + ': ' + ('PASS' if record['passed'] else 'FAIL'), flush=True)

            pair('rdma-32m-plus-31')
            pair('rdma-short', payload=31)
            pair('rdma-idle-beyond-timeout', payload=31, idle=True)
            pair('tcp-baseline', server_transport='tcp', client_transport='tcp')
            pair('rdma-wrong-client-role', client_role='wrong-role', expected_error=['certificate identity'])
            pair('rdma-wrong-server-role', server_role='strata-stage-coordinator',
                 expected_error=['certificate identity', 'certificate verify failed'])
            mismatch = ['application protocol', 'transport negotiation mismatch', 'no application protocol']
            pair('tcp-server-rdma-client', server_transport='tcp', expected_error=mismatch)
            pair('rdma-server-tcp-client', client_transport='tcp', expected_error=mismatch)
            pair('rdma-invalid-interface', interface='strata-no-such', expected_error=['interface does not exist'])
            pair('rdma-port-mismatch', client_rdma_port=args.rdma_port + 1, expected_error=['protocol/port mismatch'])
            slow_bootstrap('proposal')
            slow_bootstrap('cookie')
            # The fixture server returns immediately after sending its final
            # reply. No sleep/ack hides final SEND vs peer-close ordering.
            for repetition in range(args.close_repetitions):
                pair('rdma-final-reply-immediate-close-' + str(repetition + 1),
                     payload=31 if repetition % 2 == 0 else 32 * 1024**2 + 31, final_reply=True)
            if args.large_payloads:
                # RPC status u64 + empty error string length consume 16 reply bytes.
                pair('rdma-max-reply', payload=(512 << 20) - 16, operation_timeout=args.large_payload_timeout)
                pair('rdma-oversized-send', payload=(512 << 20) + 1, expected_error=['payload exceeds bound'],
                     operation_timeout=args.large_payload_timeout)
            else:
                report['not_run'] = ['rdma-max-reply', 'rdma-oversized-send (enable --large-payloads)']
    except Exception as e:
        report['error'] = str(e)
        print(str(e), flush=True)
    finally:
        if remote_dir and remote_dir.startswith('/tmp/strata-connection-rdma-') and '\n' not in remote_dir:
            try:
                command([*ssh, 'rm -rf -- ' + shlex.quote(remote_dir)])
            except Exception as e:
                report['cleanup_error'] = str(e)
        report['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        report['passed'] = bool(report['tests']) and all(t['passed'] for t in report['tests']) and not report.get('error') and not report.get('cleanup_error')
        save()
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
