"""Exercise the extracted package with native OCR, real HTTP and an isolated DB."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import uuid

import httpx
from verify_bundle import ROOT, verify


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    begin = time.monotonic()
    verification = verify()
    output = ROOT / 'smoke-results' / (time.strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8])
    output.mkdir(parents=True, exist_ok=False)
    stop_file = output / 'stop'
    token = secrets.token_urlsafe(32)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = dict(os.environ, PYTHONUTF8='1', VISION_API_TOKEN=token,
               VISION_INFERENCE_TIMEOUT='120', VISION_STARTUP_TIMEOUT='180',
               VISION_WAITING_TIMEOUT='600')
    # The child imports only delivered sources; no developer PYTHONPATH is inherited.
    env.pop('PYTHONPATH', None)
    report = dict(bundle_verification=verification, real_http=True, real_ocr=True,
                  sample_kind='synthetic', cases=[], passed=False)
    process = None
    try:
        with (output / 'server.log').open('w', encoding='utf-8') as log:
            process = subprocess.Popen(
                [sys.executable, '-u', str(ROOT / 'serve_vision.py'), '--port', str(port),
                 '--data-dir', str(output / 'data'), '--stop-file', str(stop_file)],
                cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{port}',
                                  timeout=30, trust_env=False) as client:
                    deadline = time.monotonic() + 200
                    while time.monotonic() < deadline:
                        require(process.poll() is None, 'Service exited; see server.log')
                        try:
                            response = client.get('/health')
                            health = response.json()
                            if health.get('ready'):
                                break
                            require(health.get('status') != 'degraded', 'Model/database unavailable; see server.log')
                        except httpx.HTTPError:
                            pass
                        time.sleep(.25)
                    else:
                        raise TimeoutError('Model startup timed out; see server.log')
                    report['initial_health'] = health
                    bad_auth = client.post('/api/v1/vision/tasks', json={'question': '读文字'})
                    require(bad_auth.status_code == 401, 'Unauthenticated request was not rejected')
                    client.headers['Authorization'] = 'Bearer ' + token
                    pid = health['model']['pid']
                    samples = json.loads((ROOT / 'examples' / 'samples.json').read_text(encoding='utf-8'))
                    for index, sample in enumerate(samples):
                        task = f'apk_sample_{index}'
                        payload = dict(task_id=task, question='生产日期是什么？', expected_frames=1)
                        response = client.post('/api/v1/vision/tasks', json=payload)
                        require(response.status_code == 201, response.text)
                        data = (ROOT / 'examples' / sample['file']).read_bytes()
                        path = f'/api/v1/vision/tasks/{task}/frames/f0'
                        response = client.put(path, content=data, headers={'Content-Type': 'image/jpeg'})
                        require(response.status_code == 202, response.text)
                        deadline = time.monotonic() + 180
                        while time.monotonic() < deadline:
                            response = client.get('/api/v1/vision/tasks/' + task)
                            response.raise_for_status()
                            result = response.json()
                            if result['status'] in {'succeeded', 'failed', 'timed_out'}:
                                break
                            time.sleep(.5)
                        else:
                            raise TimeoutError('Task did not finish: ' + task)
                        require(result['status'] == 'succeeded', json.dumps(result, ensure_ascii=False))
                        recognized = result['result']
                        require(recognized['answer_type'] == 'field_candidate', 'Expected a date candidate')
                        for field, value in sample['expected_fields'].items():
                            require([item['value'] for item in recognized['fields'][field]] == value,
                                    'Incorrect ' + field + ' in ' + sample['file'])
                        require(recognized['runtime']['worker_pid'] == pid, 'Unexpected OCR worker reload')
                        require(recognized['frames'][0]['sha256'] == hashlib.sha256(data).hexdigest(),
                                'Task/frame association mismatch')
                        response = client.put(path, content=data, headers={'Content-Type': 'image/jpeg'})
                        require(response.status_code == 200 and response.json()['duplicate_upload'],
                                'Exact upload retry was not idempotent')
                        (output / (task + '.json')).write_text(
                            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                        report['cases'].append(dict(sample=sample['file'], passed=True,
                                                    answer=result['answer'],
                                                    inference_seconds=recognized['runtime']['inference_seconds']))
                        print(sample['file'], result['answer'], flush=True)
                    report['final_health'] = client.get('/health').json()
                    require(report['final_health']['model']['initializations'] == 1,
                            'Model was initialized more than once')
                    report['passed'] = True
            finally:
                stop_file.touch()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    import psutil
                    for child in psutil.Process(process.pid).children(recursive=True):
                        child.terminate()
                    process.terminate()
                    process.wait(timeout=10)
                    raise RuntimeError('Service did not stop gracefully')
                require(process.returncode == 0, 'Service exited with code ' + str(process.returncode))
    except Exception as exc:
        report.update(passed=False, error=str(exc), error_type=type(exc).__name__)
        raise
    finally:
        report['seconds'] = time.monotonic() - begin
        (output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Report:', output / 'summary.json', flush=True)
    print('PASS: extracted package, two native OCR tasks, auth, idempotency and one model load.')


if __name__ == '__main__':
    main()
