"""Protocol tests with an explicit OCR double; these do NOT run PaddleOCR."""
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import deploy_vision
from deployment import create_app, frontend_origins
from cleanup_frames import cleanup
from verify_bundle import verify
from vision_service.answers import make_result
from vision_service.config import Settings
from vision_service.store import Store

TOKEN = 'test-only-ocr-credential'
AUTH = {'Authorization': 'Bearer ' + TOKEN}
TASKS = '/api/v1/vision/tasks'
SAMPLES = json.loads((ROOT / 'examples/samples.json').read_text(encoding='utf-8'))


class OCRDouble:
    """Known transcripts used ONLY in tests; production never imports this class."""
    state = 'stopped'
    starts = 0
    completed = 0
    load_info = {}

    def open(self, timeout, stop):
        self.starts += 1
        self.state = 'ready'
        return True

    def run(self, job, timeout, stop):
        pages = []
        for frame in job['frames']:
            sample = next(row for row in SAMPLES if row['sha256'] == frame['sha256'])
            lines = [dict(text=text, line_index=i, source_image=frame['path'],
                          bbox=[[0, i*50], [900, i*50], [900, i*50+30], [0, i*50+30]],
                          ocr_confidence=0.95)
                     for i, text in enumerate(sample['ground_truth'].splitlines())]
            pages.append(dict(source_image=frame['path'], lines=lines))
        result = make_result(pages, job['frames'], job['question'])
        result['runtime'] = {'engine': 'test-double-not-PaddleOCR'}
        self.completed += 1
        return dict(kind='result', task_id=job['task_id'], result=result)

    def close(self):
        self.state = 'stopped'


def wait_for(client, path='/ready', terminal=False):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(path, headers=AUTH)
        value = response.json()
        if (value.get('status') in {'succeeded', 'failed', 'timed_out'} if terminal else value.get('ready')):
            return value
        time.sleep(.02)
    raise AssertionError(value)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.delenv('VISION_CORS_ALLOW_ORIGINS', raising=False)
    app = create_app(Settings(data_dir=tmp_path, api_token=TOKEN), backend=OCRDouble())
    with TestClient(app) as client:
        wait_for(client)
        yield client


def new_task(client, task, question='生产日期是什么？', expected_frames=1):
    return client.post(TASKS, json=dict(task_id=task, question=question,
                                      expected_frames=expected_frames), headers=AUTH)


def upload(client, task, data, frame='f0', **headers):
    return client.put(f'{TASKS}/{task}/frames/{frame}', content=data,
                      headers={**AUTH, 'Content-Type': 'image/jpeg', **headers})


def test_original_files_and_models_unchanged():
    assert verify()['verified_files'] == 47


@pytest.mark.parametrize('token', ['', 'has space', 'bad\nvalue', '中文'])
def test_deployment_requires_safe_token(tmp_path, token):
    with pytest.raises(ValueError, match='VISION_API_TOKEN'):
        create_app(Settings(data_dir=tmp_path, api_token=token), backend=OCRDouble())


@pytest.mark.parametrize('name', ['inference_timeout', 'startup_timeout', 'waiting_timeout'])
@pytest.mark.parametrize('value', [0, -1, float('inf'), float('nan')])
def test_invalid_timeouts(tmp_path, name, value):
    with pytest.raises(ValueError, match=name):
        create_app(Settings(data_dir=tmp_path, api_token=TOKEN, **{name: value}), backend=OCRDouble())


@pytest.mark.parametrize('origin', ['*', 'null', 'https://*.example.com',
                                  'https://example.com/', 'https://example.com/page',
                                  'https://user:pass@example.com', 'https://example.com:99999',
                                  'https://example.com?q=1', 'file:///tmp/index.html'])
def test_reject_unsafe_origins(origin):
    with pytest.raises(ValueError):
        frontend_origins(origin)


def test_cors_preflight_auth_and_patch(tmp_path, monkeypatch):
    monkeypatch.setenv('VISION_CORS_ALLOW_ORIGINS', 'https://frontend.example.com')
    app = create_app(Settings(data_dir=tmp_path, api_token=TOKEN), backend=OCRDouble())
    with TestClient(app) as client:
        for method in ['POST', 'PUT', 'PATCH', 'GET']:
            result = client.options(TASKS, headers={
                'Origin': 'https://frontend.example.com',
                'Access-Control-Request-Method': method,
                'Access-Control-Request-Headers': 'authorization,content-type,x-task-id,x-frame-id',
            })
            assert result.status_code == 200
            assert result.headers['access-control-allow-origin'] == 'https://frontend.example.com'
        blocked = client.options(TASKS, headers={
            'Origin': 'https://other.example.com', 'Access-Control-Request-Method': 'PUT'})
        assert blocked.status_code == 400
        assert 'access-control-allow-origin' not in blocked.headers
        unauthorized = client.post(TASKS, json={}, headers={'Origin': 'https://frontend.example.com'})
        assert unauthorized.status_code == 401
        assert unauthorized.headers['access-control-allow-origin'] == 'https://frontend.example.com'


def test_health_and_readiness_not_interchangeable(tmp_path):
    backend = OCRDouble()
    app = create_app(Settings(data_dir=tmp_path, api_token=TOKEN), backend=backend)
    runtime = app.state.runtime
    runtime.store.initialize()
    runtime.database_ready = True
    backend.state = 'loading'
    client = TestClient(app)
    assert client.get('/health').status_code == 200
    assert client.get('/health').json()['ready'] is False
    assert client.get('/ready').status_code == 503
    backend.state = 'error'
    assert client.get('/health').status_code == 503
    assert client.get('/ready').status_code == 503


def test_swagger_security_and_no_token_leak(api):
    schema = api.get('/openapi.json').json()
    for path, operations in schema['paths'].items():
        for method, operation in operations.items():
            if path.startswith(TASKS):
                assert operation['security'] == [{'VisionBearer': []}]
            elif path in {'/health', '/ready'}:
                assert not operation.get('security')
    assert schema['components']['securitySchemes']['VisionBearer']['scheme'] == 'bearer'
    assert TOKEN not in json.dumps(schema)
    assert TOKEN not in api.get('/health').text


@pytest.mark.parametrize('method,path,body', [
    ('POST', TASKS, {}), ('GET', TASKS + '/missing', None),
    ('PATCH', TASKS + '/missing', {'question': '读文字'}),
    ('PUT', TASKS + '/missing/frames/f0', None),
])
def test_auth_on_all_task_routes(api, method, path, body):
    for headers in ({}, {'Authorization': 'Bearer wrong'}):
        response = api.request(method, path, json=body, headers=headers)
        assert response.status_code == 401
        assert response.json()['error']['code'] == 'unauthorized'


def test_two_images_task_hash_evidence_and_idempotency(api):
    seen = []
    images = [(ROOT / 'examples' / row['file']).read_bytes() for row in SAMPLES]
    for i, (sample, data) in enumerate(zip(SAMPLES, images)):
        task = f'v_sample_{i}'
        path = f'{TASKS}/{task}'
        assert new_task(api, task).status_code == 201
        assert new_task(api, task).status_code == 200
        assert new_task(api, task, question='有效期是什么？').status_code == 409
        assert upload(api, task, data).status_code == 202
        duplicate = upload(api, task, data)
        assert duplicate.status_code == 200 and duplicate.json()['duplicate_upload']
        assert upload(api, task, images[1-i]).status_code == 409
        value = wait_for(api, path, terminal=True)
        assert value['status'] == 'succeeded'
        assert value['task_id'] == task and value['question'] == '生产日期是什么？'
        result = value['result']
        assert result['runtime']['engine'] == 'test-double-not-PaddleOCR'
        assert value['answer'] == result['answer']
        assert value['evidence_text'] == result['evidence_text']
        assert result['answer_type'] == 'field_candidate' and result['review_required']
        assert [x['value'] for x in result['fields']['生产日期']] == sample['expected_fields']['生产日期']
        for frame in [value['frames'][0], result['frames'][0]]:
            assert frame['frame_id'] == 'f0'
            assert frame['sha256'] == hashlib.sha256(data).hexdigest() == sample['sha256']
            assert 'path' not in frame
        for proof in result['fields']['生产日期'][0]['evidence']:
            assert proof['frame_id'] == 'f0' and proof['text']
            assert proof['bbox'] and proof['ocr_confidence'] == 0.95
            assert 'source_image' not in proof
        seen.append(value['answer'])
    assert seen[0] != seen[1]


def test_question_after_frame_and_sealed_task(api):
    task = 'v_late_question'
    data = (ROOT / 'examples/date_mfg_exp.jpg').read_bytes()
    assert new_task(api, task, question=None).json()['status'] == 'awaiting_question'
    assert upload(api, task, data).json()['status'] == 'awaiting_question'
    path = f'{TASKS}/{task}'
    assert api.patch(path, json={'question': '生产日期是什么？'}, headers=AUTH).status_code == 200
    assert wait_for(api, path, terminal=True)['status'] == 'succeeded'
    assert api.patch(path, json={'question': '生产日期是什么？'}, headers=AUTH).status_code == 200
    assert api.patch(path, json={'question': '有效期是什么？'}, headers=AUTH).status_code == 409


def test_payload_validation_limits_and_missing_task(api):
    assert api.get(TASKS + '/missing', headers=AUTH).status_code == 404
    for body in [dict(task_id='../bad'), dict(expected_frames=True), dict(expected_frames=5),
                 dict(question='x'*1001), dict(extra_field=1)]:
        response = api.post(TASKS, json=body, headers=AUTH)
        assert response.status_code == 422
        assert response.json()['error']['code'] in {'invalid_request', 'invalid_id'}
    too_large = api.post(TASKS, content=b' ' * 16385, headers=AUTH)
    assert too_large.status_code == 413
    assert new_task(api, 'v_limits').status_code == 201
    assert upload(api, 'v_limits', b'x', **{'Content-Type': 'multipart/form-data'}).status_code == 415
    assert upload(api, 'v_limits', b'x', **{'X-Task-Id': 'other'}).status_code == 409
    assert upload(api, 'v_limits', b'x' * (8*1024*1024+1)).status_code == 413
    invalid = upload(api, 'v_limits', b'not a JPEG')
    assert invalid.status_code == 422
    assert api.get(TASKS + '/v_limits', headers=AUTH).json()['status'] == 'failed'


def test_accepted_upload_can_be_failed_quality(api):
    stream = BytesIO()
    Image.new('RGB', (120, 120), 'white').save(stream, format='JPEG')
    assert new_task(api, 'v_uniform').status_code == 201
    result = upload(api, 'v_uniform', stream.getvalue())
    assert result.status_code == 202
    assert result.json()['status'] == 'failed'
    assert result.json()['error_code'] == 'image_quality'


@pytest.mark.parametrize('question,answer_type', [
    ('请读一下文字', 'ocr_text'), ('这是什么物体？', 'unsupported'), ('这是什么药？', 'not_found'),
])
def test_succeeded_is_not_always_a_field_candidate(api, question, answer_type):
    new_task(api, 'v_answer_type', question=question)
    upload(api, 'v_answer_type', (ROOT / 'examples/date_mfg_exp.jpg').read_bytes())
    value = wait_for(api, TASKS + '/v_answer_type', terminal=True)
    assert value['status'] == 'succeeded'
    assert value['result']['answer_type'] == answer_type


def test_multiframe_duplicate_and_conflicting_candidates(api):
    new_task(api, 'v_multiple', expected_frames=2)
    first, second = [(ROOT / 'examples' / sample['file']).read_bytes() for sample in SAMPLES]
    assert upload(api, 'v_multiple', first).json()['status'] == 'awaiting_frame'
    duplicate = upload(api, 'v_multiple', first, frame='f1')
    assert duplicate.status_code == 409
    assert duplicate.json()['error']['code'] == 'duplicate_content'
    assert upload(api, 'v_multiple', second, frame='f1').status_code == 202
    value = wait_for(api, TASKS + '/v_multiple', terminal=True)
    assert value['result']['answer_type'] == 'conflicting_candidates'
    assert len(value['result']['fields']['生产日期']) == 2


def test_unavailable_model_is_not_reported_as_success(tmp_path, monkeypatch):
    class Unavailable(OCRDouble):
        def open(self, timeout, stop):
            self.state = 'error'
            return False

    monkeypatch.delenv('VISION_CORS_ALLOW_ORIGINS', raising=False)
    app = create_app(Settings(data_dir=tmp_path, api_token=TOKEN), backend=Unavailable())
    with TestClient(app) as client:
        assert client.get('/ready').status_code == 503
        new_task(client, 'v_unavailable')
        upload(client, 'v_unavailable', (ROOT / 'examples/date_mfg_exp.jpg').read_bytes())
        value = wait_for(client, TASKS + '/v_unavailable', terminal=True)
        assert value['status'] == 'failed'
        assert value['error_code'] == 'model_unavailable'
        assert value['result'] is None


def test_inference_timeout_is_terminal(tmp_path, monkeypatch):
    class Timeout(OCRDouble):
        def run(self, job, timeout, stop):
            return {'kind': 'timeout'}

    monkeypatch.delenv('VISION_CORS_ALLOW_ORIGINS', raising=False)
    app = create_app(Settings(data_dir=tmp_path, api_token=TOKEN), backend=Timeout())
    with TestClient(app) as client:
        wait_for(client)
        new_task(client, 'v_timeout')
        upload(client, 'v_timeout', (ROOT / 'examples/date_mfg_exp.jpg').read_bytes())
        value = wait_for(client, TASKS + '/v_timeout', terminal=True)
        assert value['status'] == 'timed_out'
        assert value['error_code'] == 'inference_timeout'
        assert value['result'] is None


def test_recovery_and_waiting_timeout(tmp_path):
    store = Store(tmp_path / 'vision_test.sqlite3')
    store.initialize()
    store.create('v_resume', '读文字', 1)
    data = ROOT / 'examples/date_mfg_exp.jpg'
    store.frame('v_resume', 'f0', 'digest', data, {})
    assert store.claim() == 'v_resume'
    assert store.recover() == 1
    assert store.get('v_resume')['status'] == 'queued'
    store.expire(-1)  # Deliberately force expiry at store level, not deployment settings.
    assert store.get('v_resume')['status'] == 'timed_out'


def test_queries_survive_app_restart(tmp_path, monkeypatch):
    monkeypatch.delenv('VISION_CORS_ALLOW_ORIGINS', raising=False)
    settings = Settings(data_dir=tmp_path, api_token=TOKEN)
    for i in range(2):
        app = create_app(settings, backend=OCRDouble())
        with TestClient(app) as client:
            wait_for(client)
            if i == 0:
                new_task(client, 'v_persisted')
                upload(client, 'v_persisted', (ROOT / 'examples/date_mfg_exp.jpg').read_bytes())
                first = wait_for(client, TASKS + '/v_persisted', terminal=True)
            else:
                second = client.get(TASKS + '/v_persisted', headers=AUTH).json()
                assert second == first


@pytest.mark.parametrize('configured,expected', [(None, 8001), ('10000', 10000)])
def test_deployment_startup_port(monkeypatch, configured, expected):
    if configured is None:
        monkeypatch.delenv('PORT', raising=False)
    else:
        monkeypatch.setenv('PORT', configured)
    run = Mock()
    monkeypatch.setattr(deploy_vision.uvicorn, 'run', run)
    deploy_vision.main([])
    run.assert_called_once_with('deployment:create_app', factory=True, host='0.0.0.0',
                                port=expected, workers=1, limit_concurrency=64,
                                timeout_keep_alive=5, access_log=False)


def test_cleanup_preserves_pending_and_evidence(tmp_path):
    store = Store(tmp_path / 'vision_test.sqlite3')
    store.initialize()
    frames = tmp_path / 'frames'
    frames.mkdir()
    paths = []
    for i, status in enumerate(['succeeded', 'failed', 'timed_out', 'running', 'queued', 'awaiting_frame']):
        task = f'v_cleanup_{i}'
        store.create(task, '读文字', 1)
        path = frames / (f'{i:032x}.jpg')
        path.write_bytes(b'test-only-jpeg-placeholder')
        store.frame(task, 'f0', str(i), path, {})
        with store.connect(True) as db:
            db.execute('UPDATE vision_tasks SET status=?, updated_at=0 WHERE task_id=?', (status, task))
        paths.append(path)
    before = store.get('v_cleanup_0')
    assert cleanup(tmp_path, now=100000)['eligible'] == 3
    assert all(path.is_file() for path in paths)
    assert cleanup(tmp_path, apply=True, now=100000)['deleted'] == 3
    assert not any(path.exists() for path in paths[:3])
    assert all(path.is_file() for path in paths[3:])
    assert store.get('v_cleanup_0') == before


def test_cleanup_does_not_delete_outside_frames(tmp_path):
    store = Store(tmp_path / 'vision_test.sqlite3')
    store.initialize()
    store.create('v_external', '读文字', 1)
    protected = tmp_path / ('f'*32 + '.jpg')
    protected.write_bytes(b'do-not-delete')
    store.frame('v_external', 'f0', 'digest', protected, {})
    store.fail('v_external', 'test_failure', 'test')
    with store.connect(True) as db:
        db.execute('UPDATE vision_tasks SET updated_at=0')
    report = cleanup(tmp_path, apply=True, now=100000)
    assert report['skipped'] == 1 and report['deleted'] == 0
    assert protected.is_file()
