import importlib.util
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from filelock import Timeout
from p115client import util


_spec = importlib.util.spec_from_file_location(
    "p115_fork_under_test",
    Path(__file__).resolve().parents[1] / "utils" / "p115_fork.py",
)
_helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helper)


def _use_child_caches(connection, wait_for_parent):
    try:
        caches = [util.UID_TO_STABLE_POINT, util.UID_TO_USER_KEY]
        previous_locks = [cache._lock for cache in caches]
        count = _helper.reset_p115client_cache_locks()
        rebuilt = all(cache._lock is not old for cache, old in zip(caches, previous_locks))
        if wait_for_parent:
            try:
                with caches[0]._lock.acquire(timeout=0):
                    connection.send("incorrectly_acquired")
                    return
            except Timeout:
                connection.send("blocked_by_parent")
            if not connection.poll(5) or connection.recv() != "released":
                raise TimeoutError("parent did not release the cache lock")
        for index, cache in enumerate(caches):
            with cache._lock.acquire(timeout=2):
                cache["child"] = index + 1
        connection.send({"count": count, "rebuilt": rebuilt})
    except Exception as exc:
        connection.send({"error": f"{type(exc).__name__}: {exc}"})
    finally:
        connection.close()


@unittest.skipUnless("fork" in multiprocessing.get_all_start_methods(), "需要 fork 子进程")
class P115ForkCacheTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.original = (util.UID_TO_STABLE_POINT, util.UID_TO_USER_KEY)
        util.UID_TO_STABLE_POINT = util.LockedJsonKV(Path(self.directory.name) / "pickcode.json")
        util.UID_TO_USER_KEY = util.LockedJsonKV(Path(self.directory.name) / "userkey.json")
        self.caches = (util.UID_TO_STABLE_POINT, util.UID_TO_USER_KEY)
        self.parent_locks = tuple(cache._lock for cache in self.caches)
        self.process = None
        self.connection = None

    def tearDown(self):
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(5)
            self.process.close()
        if self.connection is not None:
            self.connection.close()
        util.UID_TO_STABLE_POINT, util.UID_TO_USER_KEY = self.original
        self.directory.cleanup()

    def _start(self, wait_for_parent=False):
        context = multiprocessing.get_context("fork")
        self.connection, child_connection = context.Pipe()
        self.process = context.Process(target=_use_child_caches, args=(child_connection, wait_for_parent))
        self.process.start()
        child_connection.close()

    def _receive(self):
        self.assertTrue(self.connection.poll(5), "子进程未按时返回")
        return self.connection.recv()

    def _verify_completed(self, result):
        self.assertEqual(result, {"count": 2, "rebuilt": True})
        self.process.join(5)
        self.assertEqual(self.process.exitcode, 0)
        for index, (cache, parent_lock) in enumerate(zip(self.caches, self.parent_locks)):
            self.assertIs(cache._lock, parent_lock)
            with parent_lock.acquire(timeout=1):
                with cache.with_lock():
                    self.assertEqual(cache["child"], index + 1)

    def test_child_can_update_both_caches_without_changing_parent_locks(self):
        self._start()
        self._verify_completed(self._receive())

    def test_child_still_waits_for_parent_holding_same_cache_file(self):
        with self.parent_locks[0]:
            self._start(wait_for_parent=True)
            self.assertEqual(self._receive(), "blocked_by_parent")
            self.assertTrue(self.parent_locks[0].is_locked)
        self.connection.send("released")
        self._verify_completed(self._receive())


if __name__ == "__main__":
    unittest.main()
