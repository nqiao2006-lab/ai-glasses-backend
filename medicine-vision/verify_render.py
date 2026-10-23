"""Run from the unpacked OCR root. Creates real test tasks; never prints the token."""
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen, build_opener, HTTPRedirectHandler
import uuid


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


BASE = os.environ.get('VISION_BASE_URL', '').rstrip('/')
TOKEN = os.environ.get('VISION_API_TOKEN', '')
assert urlparse(BASE).scheme == 'https' and urlparse(BASE).netloc, 'Set HTTPS VISION_BASE_URL'
assert TOKEN and '\n' not in TOKEN and '\r' not in TOKEN, 'Set VISION_API_TOKEN privately'
OPENER = build_opener(NoRedirect())


def call(method, path, body=None, content_type='application/json', codes=(200,), auth=True):
    headers = {'Content-Type': content_type}
    if auth:
        headers['Authorization'] = 'Bearer ' + TOKEN
    req = Request(BASE + path, data=body, method=method, headers=headers)
    try:
        response = OPENER.open(req, timeout=30)
    except HTTPError as exc:
        response = exc
    with response:
        code = response.code
        raw = response.read(4 * 1024 * 1024 + 1)
    assert len(raw) <= 4 * 1024 * 1024, 'Response too large'
    assert code in codes, f'{method} {path}: HTTP {code}'
    return json.loads(raw)


def send_json(path, value, codes):
    return call('POST', path, json.dumps(value, ensure_ascii=False).encode(), codes=codes)


def snapshot(value, task, question):
    assert value['task_id'] == task and value['question'] == question
    assert value['expected_frames'] == 1
    assert value['status'] in {'awaiting_frame', 'queued', 'running', 'succeeded', 'failed', 'timed_out'}


def verify_result(value, digest, expected):
    assert value['status'] == 'succeeded', (value.get('error_code'), value.get('message'))
    result = value['result']
    assert value['answer'] == result['answer'] and value['evidence_text'] == result['evidence_text']
    assert result['answer_type'] == 'field_candidate' and result['review_required'] is True
    assert result['intent'] == '生产日期' and result['evidence_text'].strip()
    assert len(value['frames']) == len(result['frames']) == 1
    for frame in (value['frames'][0], result['frames'][0]):
        assert frame['frame_id'] == 'f0' and frame['sha256'] == digest
    assert result['frames'][0]['ocr_text'].strip()
    found = result['fields']['生产日期']
    assert len(found) == 1
    # Separators can vary; digits and date must match the actual sample.
    assert re.sub(r'\D', '', found[0]['value']) == expected
    assert found[0]['evidence']
    for proof in found[0]['evidence']:
        assert proof['frame_id'] == 'f0' and proof['text'].strip()


def main():
    health = call('GET', '/health', auth=False)
    assert health['ready'] is True and health['capabilities']['ocr'] is True, 'Model not ready'
    print('Health:', json.dumps(health, ensure_ascii=False))
    root = Path(__file__).resolve().parent
    images = [(root / 'examples/date_mfg_exp.jpg').read_bytes(),
              (root / 'examples/date_other.jpg').read_bytes()]
    samples = [('20260812', '742d83dc6a57d3ca2d6722bc0f6d8e4b268561f311ad56f839d7634ef087c713'),
               ('20251203', '7aaf5ee437d8a4f41bf818d32ddfd32a52fde4ed02179503ab9dd22f9913adc5')]
    for index, image in enumerate(images):
        expected, digest = samples[index]
        assert hashlib.sha256(image).hexdigest() == digest, 'Original sample hash mismatch'
        task = 'render_' + uuid.uuid4().hex
        path = '/api/v1/vision/tasks/' + task
        question = '生产日期是什么？'
        call('GET', path, codes=(401,), auth=False)
        new = {'task_id': task, 'question': question, 'expected_frames': 1}
        snapshot(send_json('/api/v1/vision/tasks', new, (201,)), task, question)
        snapshot(send_json('/api/v1/vision/tasks', new, (200,)), task, question)
        send_json('/api/v1/vision/tasks', {**new, 'question': '有效期是什么？'}, (409,))
        uploaded = call('PUT', path + '/frames/f0', image, 'image/jpeg', (202,))
        snapshot(uploaded, task, question)
        assert uploaded['status'] not in {'failed', 'timed_out'}, uploaded.get('message')
        duplicate = call('PUT', path + '/frames/f0', image, 'image/jpeg', (200,))
        assert duplicate['duplicate_upload'] is True
        call('PUT', path + '/frames/f0', images[1 - index], 'image/jpeg', (409,))
        deadline = time.monotonic() + 240
        while True:
            value = call('GET', path)
            snapshot(value, task, question)
            if value['status'] in {'succeeded', 'failed', 'timed_out'}:
                break
            assert time.monotonic() < deadline, 'Client polling timeout'
            time.sleep(1)
        verify_result(value, digest, expected)
        # Evidence for the backend handoff; no auth data is present in snapshots.
        print('Result:', json.dumps(value, ensure_ascii=False))
        print(f'PASS sample {index + 1}: task={task}, sha256={digest}, date={expected}')
    print('PASS: HTTP contract and both synthetic samples. Test real packaging separately.')


if __name__ == '__main__':
    main()
