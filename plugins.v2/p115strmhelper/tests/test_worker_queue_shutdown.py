"""队列停止、任务入队顺序与 worker 生命周期测试"""

import ast
import sys
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread
from types import ModuleType, SimpleNamespace
from typing import Optional
from unittest import TestCase
from unittest.mock import Mock, patch


def _load_queue(filename="helper/mediasyncdel/webhook_queue.py", class_name="SyncDelWebhookQueue"):
    source = Path(__file__).resolve().parents[1] / filename
    tree = ast.parse(source.read_text(encoding="utf-8"))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    namespace = {
        "__package__": "audit_queue_parent", "Optional": Optional, "Queue": Queue,
        "Thread": Thread, "Lock": Lock, "logger": Mock(), "SyncDelWebhookTask": SimpleNamespace,
        "DirectoryUploadTask": SimpleNamespace,
    }
    exec(compile(ast.Module(body=[cls], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[cls.name]()


class TestWebhookQueueShutdown(TestCase):
    def test_upload_queue_does_not_swallow_interrupted_stop(self):
        queue = _load_queue("helper/monitor/directory_upload_queue.py", "DirectoryUploadQueue")
        queue._queue = Queue()
        worker = Mock()
        worker.is_alive.return_value = True
        worker.join.side_effect = KeyboardInterrupt()
        queue._worker_thread = worker
        with self.assertRaises(KeyboardInterrupt):
            queue.stop()
        self.assertIs(queue._worker_thread, worker)

    def test_stop_timeout_keeps_worker_and_rejects_more_tasks_until_exit(self):
        queue = _load_queue()
        parent = ModuleType("audit_queue_parent")
        entered, release = Event(), Event()

        def handle(**kwargs):
            entered.set()
            release.wait(2)

        parent.MediaSyncDelHelper = Mock(return_value=SimpleNamespace(sync_del_by_webhook=handle))
        task = SimpleNamespace(event_data=None, enabled=True, notify=False, del_source=False,
                               p115_library_path="/local#/cloud", p115_force_delete_files=False)
        with patch.dict(sys.modules, {"audit_queue_parent": parent}):
            try:
                queue.enqueue(task)
                self.assertTrue(entered.wait(1))
                worker = queue._worker_thread
                with patch.object(worker, "join", return_value=None):
                    queue.stop()
                self.assertIs(queue._worker_thread, worker)
                self.assertFalse(queue.enqueue(task))
                release.set()
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertTrue(queue.enqueue(task))
                self.assertIsNot(queue._worker_thread, worker)
            finally:
                release.set()
                queue.stop()

    def test_accepted_enqueue_is_ordered_before_stop_sentinel(self):
        queue = _load_queue()
        actual_queue = Queue()
        worker = Mock()
        worker.is_alive.return_value = True
        queue._queue = actual_queue
        queue._worker_thread = worker
        queue._stopping = False
        inside_put, release_put, sentinel_written, stop_started = Event(), Event(), Event(), Event()
        original_put = actual_queue.put
        task = object()

        def put(item, *args, **kwargs):
            if item is task:
                inside_put.set()
                release_put.wait(2)
            if item is queue._SENTINEL:
                sentinel_written.set()
            return original_put(item, *args, **kwargs)

        def stop():
            stop_started.set()
            queue.stop()

        with patch.object(actual_queue, "put", side_effect=put):
            producer = Thread(target=queue.enqueue, args=(task,))
            stopper = Thread(target=stop)
            try:
                producer.start()
                self.assertTrue(inside_put.wait(1))
                stopper.start()
                self.assertTrue(stop_started.wait(1))
                sentinel_written.wait(0.05)
            finally:
                release_put.set()
                producer.join(2)
                stopper.join(2)
        self.assertFalse(producer.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertIs(actual_queue.get_nowait(), task)
        self.assertIs(actual_queue.get_nowait(), queue._SENTINEL)
        with self.assertRaises(Empty):
            actual_queue.get_nowait()
