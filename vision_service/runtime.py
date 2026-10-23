"""One persistent OCR process. Hard timeouts terminate hung native inference."""
from __future__ import annotations

import logging
import multiprocessing as mp
from pathlib import Path
import sqlite3
import threading
import time

from .answers import make_result
from .store import Store

LOG = logging.getLogger('vision_service')


def model_worker(connection):
    try:
        import os
        import psutil
        from medicine_ocr import create_engine, recognize
        from ocr_layout import normalize_layout
        from PIL import Image, ImageOps
        import cv2
        import numpy as np
        begin = time.perf_counter()
        engine = create_engine()
        process = psutil.Process()
        connection.send(dict(kind='ready',pid=os.getpid(),load_seconds=time.perf_counter()-begin,
                             rss_bytes=process.memory_info().rss))
    except Exception:
        LOG.exception('OCR initialization failed')
        connection.send(dict(kind='load_error'))
        connection.close()
        return
    try:
        while True:
            job = connection.recv()
            if job is None:
                break
            start = time.perf_counter()
            try:
                pages = []
                for frame in job['frames']:
                    path = Path(frame['path'])
                    with Image.open(path) as image:
                        pixels = cv2.cvtColor(np.asarray(ImageOps.exif_transpose(image).convert('RGB')), cv2.COLOR_RGB2BGR)
                    lines = recognize(engine,pixels,path)
                    pages.append(dict(source_image=str(path),raw_lines=lines,lines=normalize_layout(lines)))
                result = make_result(pages,job['frames'],job['question'])
                result['runtime'] = dict(inference_seconds=time.perf_counter()-start,rss_bytes=process.memory_info().rss,
                                         engine='PaddleOCR',device='cpu',worker_pid=os.getpid(),
                                         coordinate_space='exif_upright_pixels')
                connection.send(dict(kind='result',task_id=job['task_id'],result=result))
            except Exception:
                LOG.exception('OCR inference failed')
                connection.send(dict(kind='inference_error',task_id=job['task_id']))
    except (EOFError,BrokenPipeError,OSError):
        pass
    finally:
        connection.close()


class ProcessBackend:
    def __init__(self):
        self.process = None
        self.connection = None
        self.starts = 0
        self.completed = 0
        self.state = 'stopped'
        self.load_info = {}

    def open(self, timeout, stop):
        self.state = 'loading'
        context = mp.get_context('spawn')
        parent, child = context.Pipe()
        self.connection = parent
        self.process = context.Process(target=model_worker,args=(child,),daemon=True)
        self.process.start()
        child.close()
        self.starts += 1
        end = time.monotonic()+timeout
        while not stop.is_set() and time.monotonic() < end:
            if parent.poll(.1):
                try:
                    message = parent.recv()
                except (EOFError,OSError):
                    break
                if message['kind'] == 'ready':
                    self.load_info = message
                    self.state = 'ready'
                    return True
                break
            if not self.process.is_alive():
                break
        self.close()
        self.state = 'error'
        return False

    def run(self, job, timeout, stop):
        try:
            self.connection.send(job)
            end = time.monotonic()+timeout
            while time.monotonic()<end and not stop.is_set():
                if self.connection.poll(.1):
                    message = self.connection.recv()
                    if message.get('task_id') != job['task_id']:
                        return dict(kind='inference_error')
                    if message['kind']=='result':
                        self.completed += 1
                    return message
                if not self.process.is_alive():
                    self.state = 'error'
                    return dict(kind='inference_error')
            return dict(kind='stopped' if stop.is_set() else 'timeout')
        except (OSError,EOFError,BrokenPipeError):
            self.state = 'error'
            return dict(kind='inference_error')

    def close(self):
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=5)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=5)
            self.process.close()
            self.process = None
        if self.connection is not None:
            self.connection.close()
            self.connection = None
        self.state = 'stopped'


class VisionRuntime:
    def __init__(self, settings, backend=None):
        self.settings = settings
        self.store = Store(settings.database,settings.max_pending_tasks)
        self.backend = backend if backend is not None else ProcessBackend()
        self.stop_event = threading.Event()
        self.thread = None
        self.database_ready = False

    def start(self):
        try:
            self.store.initialize()
            self.store.recover()
            self.database_ready = True
        except (sqlite3.Error,OSError):
            LOG.exception('Cannot initialize integration database')
            return
        self.thread = threading.Thread(target=self.loop,name='vision-dispatcher',daemon=True)
        self.thread.start()

    def loop(self):
        try:
            self.backend.open(self.settings.startup_timeout,self.stop_event)
            while not self.stop_event.is_set():
                try:
                    self.store.expire(self.settings.waiting_timeout)
                    task = self.store.claim()
                    if task is None:
                        self.stop_event.wait(.15)
                        continue
                    if self.backend.state != 'ready':
                        self.store.fail(task,'model_unavailable','OCR 模型不可用，请检查服务端运行日志。')
                        continue
                    record = self.store.get(task,internal=True)
                    reply = self.backend.run(dict(task_id=task,question=record['question'],frames=record['frames']),
                                             self.settings.inference_timeout,self.stop_event)
                    kind = reply['kind']
                    if kind == 'result':
                        self.store.finish(task,reply['result'])
                    elif kind == 'stopped':
                        break  # Keep running status; recover transactionally on next startup.
                    elif kind == 'timeout':
                        self.store.fail(task,'inference_timeout','识别超时，请减少图片数量或重新拍照。','timed_out')
                        self.backend.close()
                        if not self.stop_event.is_set():
                            self.backend.open(self.settings.startup_timeout,self.stop_event)
                    else:
                        self.store.fail(task,'inference_failed','图片识别失败，请重新拍照或查看服务端日志。')
                except (sqlite3.Error,OSError):
                    LOG.exception('Integration database I/O failed')
                    self.database_ready = False
                    # Do not hide a possibly uncommitted result or rerun it indefinitely.
                    break
        finally:
            self.backend.close()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=15)

    def health(self):
        try:
            self.store.ping()
            database_ok = self.database_ready
        except (sqlite3.Error,OSError):
            database_ok = False
        ready = database_ok and self.backend.state == 'ready'
        return dict(status='ok' if ready else 'loading' if database_ok and self.backend.state=='loading' else 'degraded',
                    ready=ready,database='ok' if database_ok else 'unavailable',
                    model=dict(state=self.backend.state,initializations=self.backend.starts,
                               completed_tasks=self.backend.completed,**self.backend.load_info),
                    capabilities=dict(ocr=True,explicit_field_extraction=True,object_recognition=False,generative_vqa=False),
                    database_purpose='local integration/testing SQLite, not the production team database')
