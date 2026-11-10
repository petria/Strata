#!/usr/bin/env python3
"""Exercise an already running stage endpoint; never starts/stops inference.

Run AFTER replay benchmarking, since these requests deliberately warm/evict caches.
Requires stdlib only. API keys are read from an environment variable, never logged.
"""
import argparse
import base64
import datetime
import http.client
import json
import os
from pathlib import Path
import socket
import shlex
import struct
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib


def png64():
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    # A deterministic red square: real 64x64 RGB PNG, no image library required.
    raw = (b'\x00' + bytes((240, 16, 16)) * 64) * 64
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 64, 64, 8, 2, 0, 0, 0)) +
            chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


def text_output(response):
    return ''.join(part.get('text', '') for item in response.get('output', [])
                   if item.get('type') == 'message' for part in item.get('content', [])
                   if part.get('type') == 'output_text')


def worker(metrics):
    return (((metrics.get('engine') or {}).get('rpc_stage') or {}).get('worker') or {}).get('hardware') or {}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--base-url', required=True, help='Server root, e.g. http://127.0.0.1:8080 (or /v1)')
    ap.add_argument('--output', required=True, type=Path)
    ap.add_argument('--model', help='Defaults to first /v1/models entry')
    ap.add_argument('--api-key-env', default='STRATA_API_KEY')
    ap.add_argument('--timeout', type=float, default=180, help='HTTP socket timeout seconds')
    ap.add_argument('--cancel-wait', type=float, default=45, help='Maximum time to observe partial prefill')
    ap.add_argument('--cancel-words', type=int, default=4096)
    ap.add_argument('--cache-words', type=int, default=1024)
    ap.add_argument('--max-output-tokens', type=int, default=64)
    ap.add_argument('--cases', default='text,image,cancel,cache')
    ap.add_argument('--expect-transport', choices=('rdma', 'tcp'), default='rdma')
    ap.add_argument('--cpu-worker-unit', default='strata-unsloth-q4-v023-rdma-worker.service')
    ap.add_argument('--cpu-coordinator-unit', default='strata-unsloth-q4-v023-rdma-coordinator.service')
    ap.add_argument('--cpu-coordinator-ssh', default=os.environ.get('STRATA_RPC_REMOTE'))
    args = ap.parse_args()
    if min(args.timeout, args.cancel_wait, args.cancel_words, args.cache_words, args.max_output_tokens) <= 0:
        ap.error('timeouts, word counts and output token limit must be positive')
    base = args.base_url.rstrip('/')
    if base.endswith('/v1'):
        base = base[:-3]
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.query or parsed.fragment or parsed.username:
        ap.error('base-url must be an http(s) server URL without credentials, query or fragment')
    cases = args.cases.split(',')
    if any(case not in ('text', 'image', 'cancel', 'cache') for case in cases):
        ap.error('cases must be comma-separated text,image,cancel,cache')
    headers = {'Content-Type': 'application/json'}
    key = os.environ.get(args.api_key_env)
    if key:
        headers['Authorization'] = 'Bearer ' + key
    report = {'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'base_url': base,
              'configuration': vars(args) | {'output': str(args.output)}, 'tests': [],
              'limits': ['HTTP image success does not independently prove GPU image row placement.',
                         'Worker parked counts bound retained handles; per-handle release identity is not exposed by HTTP.']}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        tmp = args.output.with_suffix('.tmp')
        tmp.write_text(json.dumps(report, indent=2) + '\n')
        tmp.replace(args.output)

    def fetch(path, body=None, timeout=None):
        request = urllib.request.Request(base + path, headers=headers,
                                         data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(request, timeout=timeout or args.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as e:
            raise RuntimeError(f'HTTP {e.code} {path}: ' + e.read(8192).decode(errors='replace')) from e

    def metrics():
        return fetch('/metrics', timeout=min(10, args.timeout))

    def cpu_snapshot():
        """Read cumulative systemd cgroup CPU counters; never changes units."""
        result = {}
        for role, unit in [('worker', args.cpu_worker_unit), ('coordinator', args.cpu_coordinator_unit)]:
            if not unit or (role == 'coordinator' and not args.cpu_coordinator_ssh):
                continue
            cmd = ['systemctl', '--user', 'show', unit, '--property=CPUUsageNSec', '--property=ControlGroup']
            env = dict(os.environ)
            env.setdefault('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid()))
            env.setdefault('DBUS_SESSION_BUS_ADDRESS', 'unix:path=' + env['XDG_RUNTIME_DIR'] + '/bus')
            if role == 'coordinator':
                cmd = ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', args.cpu_coordinator_ssh,
                       'export XDG_RUNTIME_DIR=/run/user/$(id -u); '
                       'export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus; ' + shlex.join(cmd)]
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=env, check=True)
                values = dict(line.split('=', 1) for line in p.stdout.splitlines() if '=' in line)
                result[role] = {'unit': unit, 'control_group': values.get('ControlGroup'),
                                'cpu_usage_nsec': int(values['CPUUsageNSec'])}
            except Exception as e:
                result[role] = {'unit': unit, 'error': str(e)}
        return result

    def request(items, **overrides):
        body = {'model': args.model, 'input': items, 'store': False, 'stream': False,
                'temperature': 0, 'reasoning': {'effort': 'none'}, 'max_output_tokens': args.max_output_tokens}
        body.update(overrides)
        answer = fetch('/v1/responses', body)
        if answer.get('error') or answer.get('status') not in ('completed', 'incomplete') or not text_output(answer).strip():
            raise RuntimeError('Responses failed or returned no output text: ' + json.dumps(answer)[:2000])
        return answer

    def record_case(name, action):
        start = time.monotonic()
        row = {'name': name, 'passed': False}
        row['cpu_before'] = cpu_snapshot()
        report['tests'].append(row)
        save()
        try:
            action(row)
            row['passed'] = True
        except Exception as e:
            row['error'] = str(e)
        row['cpu_after'] = cpu_snapshot()
        row['cpu_seconds'] = {role: (end['cpu_usage_nsec'] - row['cpu_before'][role]['cpu_usage_nsec']) / 1e9
                              for role, end in row['cpu_after'].items()
                              if 'cpu_usage_nsec' in end and 'cpu_usage_nsec' in row['cpu_before'].get(role, {})}
        row['seconds'] = round(time.monotonic() - start, 3)
        save()
        print(name + ': ' + ('PASS' if row['passed'] else 'FAIL'), flush=True)

    def text_case(row):
        row['response'] = request('Reply with the word READY.')
        row['metrics'] = metrics()
        backend = worker(row['metrics']).get('transport', {}).get('backend')
        row['observed_worker_backend'] = backend
        if backend != args.expect_transport:
            raise RuntimeError(f'Worker backend {backend!r}; expected {args.expect_transport!r}. Backend proof required.')

    def image_case(row):
        before = metrics()
        if not before.get('engine', {}).get('images'):
            raise RuntimeError('Endpoint reports vision disabled; image path not exercised')
        image = png64()
        row.update(width=64, height=64, png_bytes=len(image))
        row['response'] = request([{'role': 'user', 'content': [
            {'type': 'input_text', 'text': 'Name the main color of this square in one word.'},
            {'type': 'input_image', 'image_url': 'data:image/png;base64,' + base64.b64encode(image).decode()}]}])
        row['metrics_after'] = metrics()
        row['image_rows_placement_verified'] = False

    def cancel_case(row):
        before = fetch('/v1/telemetry', timeout=10)
        if before.get('work', {}).get('requests'):
            raise RuntimeError('Endpoint has another active request; run verifier exclusively after replay')
        baseline = before.get('work', {}).get('client_disconnect_cancels', 0)
        # Unique prefix prevents a previous run making this a cache-hit decode.
        context = report['initial_metrics'].get('engine', {}).get('max_context')
        count = args.cancel_words
        if isinstance(context, int):
            count = min(count, max(64, (context - 512) // 4))
        row['prompt_words'] = count
        prompt = ('Cancellation probe ' + uuid.uuid4().hex + '.\n' +
                  ' '.join('word' + str(i) for i in range(count)) + '\nReply READY.')
        body = json.dumps({'model': args.model, 'input': prompt, 'store': False, 'stream': True,
                           'reasoning': {'effort': 'none'}, 'max_output_tokens': 16}).encode()
        cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
        conn = cls(parsed.hostname, parsed.port, timeout=args.timeout)
        observed = False
        try:
            conn.request('POST', parsed.path.rstrip('/') + '/v1/responses', body=body, headers=headers)
            deadline = time.monotonic() + args.cancel_wait
            while time.monotonic() < deadline:
                snapshot = metrics()
                live = snapshot.get('live', {})
                read, total = live.get('prompt_read'), live.get('prompt_total')
                if live.get('state') == 'reading' and isinstance(read, (int, float)) and isinstance(total, (int, float)) and 0 < read < total:
                    row['partial_prefill'] = live
                    observed = True
                    break
                if live.get('state') == 'generating':
                    row['last_live'] = live
                    break
                time.sleep(0.1)
            # Close without waiting for SSE headers or a first output token.
            if conn.sock:
                conn.sock.shutdown(socket.SHUT_RDWR)
        finally:
            conn.close()
        row['partial_prefill_observed'] = observed
        row['recovery_response'] = request('Reply with the word RECOVERED.')
        after = fetch('/v1/telemetry', timeout=10)
        row['telemetry_after'] = after
        row['disconnect_cancel_delta'] = after.get('work', {}).get('client_disconnect_cancels', 0) - baseline
        if not observed:
            raise RuntimeError('No partial prefill observed; cancellation timing unverified (adjust --cancel-words)')
        if row['disconnect_cancel_delta'] < 1:
            raise RuntimeError('Recovery succeeded but no server disconnect cancellation was recorded')

    def cache_case(row):
        before = metrics()
        row['before'] = before
        cache = before.get('conversation_cache', {})
        slots = cache.get('slots')
        if not cache.get('enabled') or not isinstance(slots, int) or not 1 <= slots <= 4:
            raise RuntimeError('Five-session eviction needs an enabled 1..4-slot conversation cache; no settings changed')
        histories = [[] for _ in range(5)]
        markers = ['SESSION_' + uuid.uuid4().hex[:10] + '_' + str(i) for i in range(5)]
        row['turns'] = []
        row['model_quality_failures'] = []
        # First fill five distinct conversations, then interleave resident and
        # evicted ones while replaying the complete Responses output items.
        for turn, session in enumerate([0, 1, 2, 3, 4, 4, 1, 3, 2, 0]):
            marker = markers[session]
            if not histories[session]:
                # Keep each prefix distinct without making the hexadecimal
                # marker repeat into many thousands of tokenizer fragments.
                words = ('alpha', 'beta', 'gamma', 'delta')
                prefix = ' '.join(words[(i + session) % len(words)] for i in range(args.cache_words))
                user = 'Remember the session marker ' + marker + '.\n' + prefix + '\nReply with only that marker.'
            else:
                user = 'Reply with this conversation\'s session marker only.'
            histories[session].append({'role': 'user', 'content': user})
            answer = request(histories[session])
            text = text_output(answer)
            row['turns'].append({'turn': turn, 'session': session, 'response': answer})
            save()
            if marker not in text or any(other in text for other in markers if other != marker):
                row['model_quality_failures'].append({'turn': turn, 'session': session,
                    'expected_marker': marker, 'actual_text': text[:1000]})
            histories[session].extend(answer['output'])
        after = metrics()
        row['after'] = after
        ac = after.get('conversation_cache', {})
        row['cache_delta'] = {name: ac.get(name, 0) - cache.get(name, 0) for name in ('evictions', 'parks', 'restores')}
        bw, aw = worker(before), worker(after)
        row['worker_delta'] = {name: aw.get(name, 0) - bw.get(name, 0) for name in ('parks', 'activations', 'park_misses')}
        row['worker_parked_conversations'] = aw.get('parked_conversations')
        row['worker_handles_bounded'] = isinstance(aw.get('parked_conversations'), int) and 0 <= aw['parked_conversations'] <= slots
        if any(row['cache_delta'][name] <= 0 for name in ('evictions', 'parks', 'restores')):
            raise RuntimeError('Conversation requests succeeded but cache park/restore/eviction not all observed')
        if args.expect_transport == 'rdma' and (not row['worker_handles_bounded'] or row['worker_delta']['parks'] <= 0 or row['worker_delta']['activations'] <= 0):
            raise RuntimeError('Worker park/activate counters or bounded retained handles not proven')
        row['cache_engine_checks_passed'] = True
        row['model_quality_passed'] = not row['model_quality_failures']
        if not row['model_quality_passed']:
            print('cache: model marker quality FAILED; cache engine checks passed (see report)', flush=True)

    save()
    try:
        if not args.model:
            models = fetch('/v1/models').get('data', [])
            if not models:
                raise RuntimeError('No model returned by /v1/models')
            args.model = models[0]['id']
        report['model'] = args.model
        report['initial_metrics'] = metrics()
        for name in cases:
            record_case(name, {'text': text_case, 'image': image_case, 'cancel': cancel_case, 'cache': cache_case}[name])
    except Exception as e:
        report['error'] = str(e)
    finally:
        report['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        report['passed'] = bool(report['tests']) and all(row['passed'] for row in report['tests']) and not report.get('error')
        report['model_quality_passed'] = all(row.get('model_quality_passed', True) for row in report['tests'])
        save()
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
