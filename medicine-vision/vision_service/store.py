"""SQLite integration database, one transaction per state change."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time

TERMINAL = {'succeeded', 'failed', 'timed_out'}
WAITING = {'awaiting_question', 'awaiting_frame'}


class Problem(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class Store:
    def __init__(self, path, max_pending=32):
        self.path, self.max_pending = Path(path), max_pending

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA busy_timeout=5000')
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            if write:
                db.commit()
        except Exception:
            if write:
                db.rollback()
            raise
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(Path(__file__).with_name('schema.sql').read_text(encoding='utf-8'))

    @staticmethod
    def event(db, task, kind, **details):
        db.execute('INSERT INTO vision_events(task_id,event,detail_json,created_at) VALUES(?,?,?,?)',
                   (task, kind, json.dumps(details, ensure_ascii=False), time.time()))

    @staticmethod
    def require(db, task):
        row = db.execute('SELECT * FROM vision_tasks WHERE task_id=?', (task,)).fetchone()
        if row is None:
            raise Problem('task_not_found', '任务不存在，请先创建任务。', 404)
        return dict(row)

    def create(self, task, question, expected):
        with self.connect(True) as db:
            existing = db.execute('SELECT * FROM vision_tasks WHERE task_id=?', (task,)).fetchone()
            if existing:
                if existing['question'] == question and existing['expected_frames'] == expected:
                    return False
                raise Problem('task_conflict', '该任务编号已用于其他请求。')
            count = db.execute("SELECT count(*) FROM vision_tasks WHERE status NOT IN ('succeeded','failed','timed_out')").fetchone()[0]
            if count >= self.max_pending:
                raise Problem('queue_full', '服务待处理任务已满，请稍后重试。', 429)
            now = time.time()
            status = 'awaiting_frame' if question else 'awaiting_question'
            db.execute('INSERT INTO vision_tasks(task_id,question,expected_frames,status,created_at,updated_at) VALUES(?,?,?,?,?,?)',
                       (task, question, expected, status, now, now))
            self.event(db, task, 'created', status=status)
            return True

    @staticmethod
    def ready(db, task):
        row = Store.require(db, task)
        count = db.execute('SELECT count(*) FROM vision_frames WHERE task_id=?', (task,)).fetchone()[0]
        status = ('queued' if row['question'] and count == row['expected_frames'] else
                  'awaiting_frame' if row['question'] else 'awaiting_question')
        db.execute('UPDATE vision_tasks SET status=?, updated_at=? WHERE task_id=?', (status, time.time(), task))
        return status

    def question(self, task, question):
        with self.connect(True) as db:
            row = self.require(db, task)
            if row['question'] == question:
                return
            if row['status'] not in WAITING:
                raise Problem('task_sealed', '任务已开始处理，修改问题请创建新任务。')
            db.execute('UPDATE vision_tasks SET question=? WHERE task_id=?', (question, task))
            self.event(db, task, 'question_set', status=self.ready(db, task))

    def frame(self, task, frame, digest, path, quality):
        with self.connect(True) as db:
            row = self.require(db, task)
            same = db.execute('SELECT sha256 FROM vision_frames WHERE task_id=? AND frame_id=?', (task,frame)).fetchone()
            if same:
                if same['sha256'] == digest:
                    self.event(db, task, 'duplicate_upload', frame_id=frame)
                    return False
                raise Problem('frame_conflict', '同一 frame_id 已关联不同图片。')
            duplicate = db.execute('SELECT frame_id FROM vision_frames WHERE task_id=? AND sha256=?', (task,digest)).fetchone()
            if duplicate:
                raise Problem('duplicate_content', '图片已通过另一个 frame_id 上传。')
            if row['status'] not in WAITING:
                raise Problem('task_sealed', '任务已开始处理，不能追加图片。')
            count = db.execute('SELECT count(*) FROM vision_frames WHERE task_id=?', (task,)).fetchone()[0]
            if count >= row['expected_frames']:
                raise Problem('too_many_frames', '图片数超过任务约定。')
            db.execute('INSERT INTO vision_frames VALUES(?,?,?,?,?,?)',
                       (task, frame, digest, str(path), json.dumps(quality), time.time()))
            status = self.ready(db, task)
            self.event(db, task, 'frame_received', frame_id=frame, sha256=digest, status=status)
            if quality.get('rejected'):
                db.execute("UPDATE vision_tasks SET status='failed',error_code='image_quality',message=?,updated_at=? WHERE task_id=?",
                           ('图片严重模糊、曝光异常或缺少细节，请重新拍摄。',time.time(),task))
                self.event(db, task, 'failed', code='image_quality')
            return True

    def get(self, task, internal=False):
        with self.connect() as db:
            row = self.require(db, task)
            frames = [dict(x) for x in db.execute('SELECT * FROM vision_frames WHERE task_id=? ORDER BY created_at,frame_id', (task,))]
            for frame in frames:
                frame['quality'] = json.loads(frame.pop('quality_json'))
                if not internal:
                    frame.pop('path')
            result = db.execute('SELECT result_json FROM vision_results WHERE task_id=?', (task,)).fetchone()
            row.update(frames=frames, result=json.loads(result[0]) if result else None)
            return row

    def claim(self):
        with self.connect(True) as db:
            row = db.execute("SELECT task_id FROM vision_tasks WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            task = row[0]
            now = time.time()
            db.execute("UPDATE vision_tasks SET status='running', started_at=?,updated_at=? WHERE task_id=?", (now, now, task))
            self.event(db, task, 'inference_started')
            return task

    def finish(self, task, result):
        with self.connect(True) as db:
            row = self.require(db, task)
            if row['status'] != 'running':
                return False
            now = time.time()
            db.execute('INSERT INTO vision_results VALUES(?,?,?)', (task, json.dumps(result,ensure_ascii=False), now))
            db.execute("UPDATE vision_tasks SET status='succeeded',updated_at=?,error_code=NULL,message='' WHERE task_id=?", (now,task))
            self.event(db, task, 'succeeded')
            return True

    def fail(self, task, code, message, status='failed',allowed=None):
        with self.connect(True) as db:
            row = self.require(db, task)
            if row['status'] in TERMINAL:
                return
            if allowed is not None and row['status'] not in allowed:
                return
            db.execute('UPDATE vision_tasks SET status=?,error_code=?,message=?,updated_at=? WHERE task_id=?',
                       (status,code,message,time.time(),task))
            self.event(db, task, status, code=code)

    def recover(self):
        with self.connect(True) as db:
            tasks = [row[0] for row in db.execute("SELECT task_id FROM vision_tasks WHERE status='running'")]
            for task in tasks:
                db.execute("UPDATE vision_tasks SET status='queued',started_at=NULL,updated_at=? WHERE task_id=?", (time.time(),task))
                self.event(db, task, 'recovered_after_restart')
        return len(tasks)

    def expire(self, waiting_timeout):
        with self.connect(True) as db:
            tasks = [row[0] for row in db.execute("SELECT task_id FROM vision_tasks WHERE status IN ('awaiting_question','awaiting_frame','queued') AND created_at < ?", (time.time()-waiting_timeout,))]
            for task in tasks:
                db.execute("UPDATE vision_tasks SET status='timed_out',error_code='waiting_timeout',message=?,updated_at=? WHERE task_id=?",
                           ('等待图片、问题或排队超时，请重新创建任务。',time.time(),task))
                self.event(db, task, 'timed_out', code='waiting_timeout')

    def ping(self):
        with self.connect() as db:
            return db.execute('SELECT count(*) FROM vision_tasks').fetchone()[0]
