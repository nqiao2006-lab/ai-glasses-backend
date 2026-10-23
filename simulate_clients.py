"""Android task/question + ESP32 raw JPEG upload + Android polling over real HTTP."""
import argparse
import json
import os
from pathlib import Path
import time
import uuid
import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://127.0.0.1:8000')
    parser.add_argument('--image', type=Path, required=True)
    parser.add_argument('--question', default='帮我读一下这上面的文字')
    parser.add_argument('--image-first', action='store_true')
    parser.add_argument('--timeout', type=float, default=240)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    token = os.environ.get('VISION_API_TOKEN', '')
    headers = {'Authorization': 'Bearer '+token} if token else {}
    task = 'sim_'+uuid.uuid4().hex
    with httpx.Client(base_url=args.url.rstrip('/'), headers=headers, timeout=30, trust_env=False) as client:
        created = client.post('/api/v1/vision/tasks', json=dict(task_id=task,
                              question=None if args.image_first else args.question))
        created.raise_for_status()
        uploaded = client.put(f'/api/v1/vision/tasks/{task}/frames/f0', content=args.image.read_bytes(),
                              headers={'Content-Type': 'image/jpeg', 'X-Task-Id': task, 'X-Frame-Id': 'f0'})
        uploaded.raise_for_status()
        if args.image_first:
            response = client.patch('/api/v1/vision/tasks/'+task, json={'question': args.question})
            response.raise_for_status()
        deadline = time.monotonic()+args.timeout
        while time.monotonic()<deadline:
            response = client.get('/api/v1/vision/tasks/'+task)
            response.raise_for_status()
            result = response.json()
            if result['status'] in {'succeeded', 'failed', 'timed_out'}:
                text = json.dumps(result, ensure_ascii=False, indent=2)
                print(text)
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    args.output.write_text(text, encoding='utf-8')
                return int(result['status'] != 'succeeded')
            time.sleep(.5)
    raise TimeoutError('Polling timed out; query the same task_id before retrying: '+task)


if __name__ == '__main__':
    raise SystemExit(main())
