"""
P115 请求超时参数测试。

覆盖两类容易漏掉的入口：
1. 统一 timeout helper 生成的 httpcore extensions.timeout。
2. 生成 iOS UA 时发生在 configer.get_ios_ua_app() 之前的 app_version_list2 请求。
"""

from types import ModuleType, SimpleNamespace
import importlib
from functools import wraps
from pathlib import Path
import sys
from unittest import TestCase


class TestP115TimeoutUtils(TestCase):
    def test_build_p115_request_kwargs_contains_httpcore_timeout(self):
        from utils.p115_timeout import build_p115_request_kwargs

        kwargs = build_p115_request_kwargs(connect=1, read=2, write=3, pool=4)

        self.assertEqual(kwargs["timeout"], 2.0)
        self.assertEqual(
            kwargs["extensions"]["timeout"],
            {"connect": 1.0, "read": 2.0, "write": 3.0, "pool": 4.0},
        )

    def test_apply_p115_request_timeout_preserves_existing_extensions(self):
        from utils.p115_timeout import apply_p115_request_timeout

        kwargs = {"headers": {"x-test": "1"}, "extensions": {"trace": "keep"}}
        result = apply_p115_request_timeout(kwargs, timeout=9)

        self.assertIs(result, kwargs)
        self.assertEqual(result["headers"], {"x-test": "1"})
        self.assertEqual(result["timeout"], 9.0)
        self.assertEqual(result["extensions"]["trace"], "keep")
        self.assertEqual(
            result["extensions"]["timeout"],
            {"connect": 9.0, "read": 9.0, "write": 9.0, "pool": 9.0},
        )


class TestP115ClientTimeoutWrapper(TestCase):
    def setUp(self):
        self._saved_modules = {
            name: sys.modules.get(name)
            for name in ["p115client", "app", "app.log", "core.p115_client"]
        }

        fake_logger = SimpleNamespace(debug=lambda *args, **kwargs: None, info=lambda *args, **kwargs: None)
        fake_p115client = ModuleType("p115client")
        fake_p115client.P115Client = type("P115Client", (), {})
        fake_app = ModuleType("app")
        fake_app_log = ModuleType("app.log")
        fake_app_log.logger = fake_logger

        sys.modules["p115client"] = fake_p115client
        sys.modules["app"] = fake_app
        sys.modules["app.log"] = fake_app_log
        sys.modules.pop("core.p115_client", None)

    def tearDown(self):
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_wrapper_injects_default_and_slow_timeout(self):
        client_mod = importlib.import_module("core.p115_client")

        class FakeClient:
            def __init__(self):
                self.calls = []

            def fs_files(self, **kwargs):
                self.calls.append(("fs_files", kwargs))
                return kwargs

            def download_url(self, **kwargs):
                self.calls.append(("download_url", kwargs))
                return kwargs

        wrapped = client_mod.create_client_with_timeout(
            FakeClient(),
            default_timeout={"connect": 1, "read": 2, "write": 3, "pool": 4},
            slow_timeout={"connect": 10, "read": 20, "write": 30, "pool": 40},
        )

        default_result = wrapped.fs_files()
        slow_result = wrapped.download_url()

        self.assertEqual(
            default_result["extensions"]["timeout"],
            {"connect": 1, "read": 2, "write": 3, "pool": 4},
        )
        self.assertEqual(default_result["timeout"], 2.0)
        self.assertEqual(
            slow_result["extensions"]["timeout"],
            {"connect": 10, "read": 20, "write": 30, "pool": 40},
        )
        self.assertEqual(slow_result["timeout"], 20.0)

    def test_wrapper_preserves_explicit_timeout(self):
        client_mod = importlib.import_module("core.p115_client")

        class FakeClient:
            def fs_files(self, **kwargs):
                return kwargs

        wrapped = client_mod.create_client_with_timeout(
            FakeClient(),
            default_timeout={"connect": 1, "read": 2, "write": 3, "pool": 4},
        )
        result = wrapped.fs_files(
            extensions={"trace": "keep", "timeout": {"connect": 9}}
        )

        self.assertEqual(result["extensions"]["trace"], "keep")
        self.assertEqual(result["extensions"]["timeout"], {"connect": 9})
        self.assertEqual(result["timeout"], 9.0)

    def test_wrapper_propagates_timeout_exception_after_injecting_timeout(self):
        client_mod = importlib.import_module("core.p115_client")

        class FakeClient:
            def __init__(self):
                self.last_kwargs = None

            def fs_files(self, **kwargs):
                self.last_kwargs = kwargs
                raise TimeoutError("network timeout")

        wrapped = client_mod.create_client_with_timeout(
            FakeClient(),
            default_timeout={"connect": 1, "read": 2, "write": 3, "pool": 4},
        )

        with self.assertRaises(TimeoutError):
            wrapped.fs_files()

        self.assertEqual(
            wrapped.last_kwargs["extensions"]["timeout"],
            {"connect": 1, "read": 2, "write": 3, "pool": 4},
        )
        self.assertEqual(wrapped.last_kwargs["timeout"], 2.0)


class TestP115StaticTimeoutCoverage(TestCase):
    def test_qrcode_static_requests_use_short_timeout(self):
        api_path = Path(__file__).resolve().parents[1] / "api.py"
        source = api_path.read_text(encoding="utf-8")

        self.assertIn("build_p115_request_kwargs(timeout=10)", source)
        self.assertIn("P115Client.login_qrcode_token(**request_kwargs)", source)
        self.assertIn('qrcode_content = str(resp_info.get("qrcode") or "")', source)
        self.assertIn("img = qr_make(qrcode_content)", source)
        self.assertIn(
            "P115Client.login_qrcode_scan_status(payload, **request_kwargs)",
            source,
        )
        self.assertIn("uid, app=final_client_type, **request_kwargs", source)

    def test_get_pid_by_path_preserves_explicit_request_timeout(self):
        p115_path = Path(__file__).resolve().parents[1] / "core" / "p115.py"
        source = p115_path.read_text(encoding="utf-8")

        self.assertIn("request_timeout:", source)
        self.assertIn("apply_p115_request_timeout(kwargs, timeout=request_timeout)", source)


class TestAppVerAndDownloadAppPatchers(TestCase):
    def setUp(self):
        self._saved_modules = {
            name: sys.modules.get(name)
            for name in [
                "app",
                "app.log",
                "p115client",
                "p115client.client",
                "fake_p115_plugin",
                "fake_p115_plugin.patch",
                "fake_p115_plugin.utils",
                "fake_p115_plugin.utils.user_agent",
                "fake_p115_plugin.patch.app_ver",
                "fake_p115_plugin.patch.download_app",
            ]
        }

        self.warning_messages = []
        fake_logger = SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda message, *args, **kwargs: self.warning_messages.append(message),
        )

        class FakeP115Client:
            def download_folders_app(self, payload, app="web", **kwargs):
                return {"method": "folders", "payload": payload, "app": app, "kwargs": kwargs}

            def download_files_app(self, payload, app="web", **kwargs):
                return {"method": "files", "payload": payload, "app": app, "kwargs": kwargs}

        fake_p115client = ModuleType("p115client")
        fake_p115client.P115Client = FakeP115Client

        fake_p115client_client = ModuleType("p115client.client")
        fake_p115client_client.P115Client = FakeP115Client
        fake_p115client_client._app_version = "36.2.28"

        fake_app = ModuleType("app")
        fake_app_log = ModuleType("app.log")
        fake_app_log.logger = fake_logger

        fake_pkg = ModuleType("fake_p115_plugin")
        fake_pkg.__path__ = []
        fake_patch_pkg = ModuleType("fake_p115_plugin.patch")
        fake_patch_pkg.__path__ = []
        fake_utils_pkg = ModuleType("fake_p115_plugin.utils")
        fake_utils_pkg.__path__ = []
        fake_user_agent = ModuleType("fake_p115_plugin.utils.user_agent")
        fake_user_agent.UserAgentUtils = SimpleNamespace(
            get_real_app_ver=lambda: "35.9.0"
        )

        sys.modules.update(
            {
                "app": fake_app,
                "app.log": fake_app_log,
                "p115client": fake_p115client,
                "p115client.client": fake_p115client_client,
                "fake_p115_plugin": fake_pkg,
                "fake_p115_plugin.patch": fake_patch_pkg,
                "fake_p115_plugin.utils": fake_utils_pkg,
                "fake_p115_plugin.utils.user_agent": fake_user_agent,
            }
        )

    def tearDown(self):
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _load_patch_module(self, filename, module_name):
        module_path = Path(__file__).resolve().parents[1] / "patch" / filename
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    def _load_app_ver_module(self):
        return self._load_patch_module(
            "app_ver.py",
            "fake_p115_plugin.patch.app_ver",
        )

    def _load_download_app_module(self):
        return self._load_patch_module(
            "download_app.py",
            "fake_p115_plugin.patch.download_app",
        )

    def test_app_ver_patcher_replaces_and_restores_module_app_version(self):
        module = self._load_app_ver_module()
        client_mod = sys.modules["p115client.client"]

        module.AppVerPatcher.enable()

        self.assertTrue(module.AppVerPatcher._active)
        self.assertEqual(client_mod._app_version, "35.9.0")
        self.assertNotEqual(client_mod._app_version, "99.99.99.99")

        module.AppVerPatcher.disable()

        self.assertFalse(module.AppVerPatcher._active)
        self.assertEqual(client_mod._app_version, "36.2.28")

    def test_app_ver_patcher_skips_missing_module_app_version(self):
        module = self._load_app_ver_module()
        client_mod = sys.modules["p115client.client"]
        delattr(client_mod, "_app_version")

        module.AppVerPatcher.enable()

        self.assertFalse(module.AppVerPatcher._active)
        self.assertTrue(any("_app_version" in msg for msg in self.warning_messages))

    def test_download_app_patcher_forces_chrome_and_preserves_kwargs(self):
        module = self._load_download_app_module()
        client_cls = sys.modules["p115client.client"].P115Client
        client = client_cls()
        timeout_extensions = {"timeout": {"connect": 1, "read": 2}}

        module.DownloadAppPatcher.enable()

        folder_result = client.download_folders_app(
            {"pickcode": "folder"},
            "android",
            extensions=timeout_extensions,
            timeout=2,
            trace_id="keep-folder",
        )
        file_result = client.download_files_app(
            {"pickcode": "file"},
            app="windows",
            extensions=timeout_extensions,
            timeout=2,
            trace_id="keep-file",
        )

        self.assertEqual(folder_result["app"], "chrome")
        self.assertEqual(file_result["app"], "chrome")
        self.assertIs(folder_result["kwargs"]["extensions"], timeout_extensions)
        self.assertIs(file_result["kwargs"]["extensions"], timeout_extensions)
        self.assertEqual(folder_result["kwargs"]["timeout"], 2)
        self.assertEqual(file_result["kwargs"]["trace_id"], "keep-file")

        module.DownloadAppPatcher.disable()

    def test_download_app_patcher_disable_restores_original_methods(self):
        module = self._load_download_app_module()
        client_cls = sys.modules["p115client.client"].P115Client
        original = client_cls.download_files_app

        module.DownloadAppPatcher.enable()
        self.assertIsNot(client_cls.download_files_app, original)

        module.DownloadAppPatcher.disable()
        self.assertIs(client_cls.download_files_app, original)

    def test_download_app_patcher_skips_missing_methods_without_import_failure(self):
        module = self._load_download_app_module()

        class IncompleteClient:
            def download_files_app(self, payload, app="web", **kwargs):
                return {}

        sys.modules["p115client.client"].P115Client = IncompleteClient

        module.DownloadAppPatcher.enable()

        self.assertFalse(module.DownloadAppPatcher._active)
        self.assertTrue(any("download_folders_app" in msg for msg in self.warning_messages))


class TestP115DiskTimeoutWrapper(TestCase):
    def test_build_timeout_config_supports_p115disk_defaults(self):
        saved_modules = {
            name: sys.modules.get(name)
            for name in ["p115client", "app", "app.log", "p115disk_timeout_client"]
        }
        try:
            fake_logger = SimpleNamespace(debug=lambda *args, **kwargs: None, info=lambda *args, **kwargs: None)
            fake_p115client = ModuleType("p115client")
            fake_p115client.P115Client = type("P115Client", (), {})
            fake_app = ModuleType("app")
            fake_app_log = ModuleType("app.log")
            fake_app_log.logger = fake_logger
            sys.modules["p115client"] = fake_p115client
            sys.modules["app"] = fake_app
            sys.modules["app.log"] = fake_app_log

            module_path = (
                Path(__file__).resolve().parents[2]
                / "p115disk"
                / "p115_client.py"
            )
            spec = importlib.util.spec_from_file_location(
                "p115disk_timeout_client", module_path
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules["p115disk_timeout_client"] = module
            spec.loader.exec_module(module)

            self.assertEqual(
                module.build_timeout_config(True, connect=1, pool=2, read=3, write=4),
                {"connect": 1, "pool": 2, "read": 3, "write": 4},
            )
            self.assertIsNone(module.build_timeout_config(False))

            class FakeClient:
                def download_url(self, **kwargs):
                    return kwargs

            wrapped = module.create_client_with_timeout(
                FakeClient(),
                default_timeout={"connect": 1, "pool": 2, "read": 3, "write": 4},
            )
            result = wrapped.download_url()
            self.assertEqual(
                result["extensions"]["timeout"],
                {"connect": 1, "pool": 2, "read": 3, "write": 4},
            )
            self.assertEqual(result["timeout"], 3.0)
        finally:
            for name, module in saved_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


class TestUserAgentTimeout(TestCase):
    def setUp(self):
        self.calls = []

        class FakeP115Client:
            @staticmethod
            def app_version_list2(**kwargs):
                self.calls.append(kwargs)
                return {
                    "state": True,
                    "data": {
                        "iOS-iPhone": {"version_code": "37.0.8"},
                        "115wangpan_iOS": {"version_code": "36.2.21"},
                    },
                }

        def fake_check_response(resp):
            return resp

        fake_p115client = ModuleType("p115client")
        fake_p115client.P115Client = FakeP115Client
        fake_p115client.check_response = fake_check_response

        fake_cache = ModuleType("app.core.cache")
        fake_cache.cached = lambda *args, **kwargs: (lambda func: func)

        fake_logger = SimpleNamespace(info=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None)
        fake_app = ModuleType("app")
        fake_app_log = ModuleType("app.log")
        fake_app_log.logger = fake_logger
        fake_core = ModuleType("app.core")

        self._saved_modules = {
            name: sys.modules.get(name)
            for name in ["p115client", "app", "app.log", "app.core", "app.core.cache", "utils.user_agent"]
        }
        sys.modules["p115client"] = fake_p115client
        sys.modules["app"] = fake_app
        sys.modules["app.log"] = fake_app_log
        sys.modules["app.core"] = fake_core
        sys.modules["app.core.cache"] = fake_cache
        sys.modules.pop("utils.user_agent", None)

    def tearDown(self):
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def test_get_real_app_ver_passes_timeout_to_app_version_request(self):
        user_agent = importlib.import_module("utils.user_agent")

        version = user_agent.UserAgentUtils.get_real_app_ver()

        self.assertEqual(version, "37.0.8")
        self.assertEqual(len(self.calls), 1)
        kwargs = self.calls[0]
        self.assertEqual(kwargs["timeout"], 10.0)
        self.assertEqual(
            kwargs["extensions"]["timeout"],
            {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0},
        )

    def test_generate_u115_ios_passes_timeout_to_app_version_request(self):
        user_agent = importlib.import_module("utils.user_agent")

        ua = user_agent.UserAgentUtils.generate_u115_ios()

        self.assertIn("iPhone", ua)
        self.assertEqual(len(self.calls), 1)
        kwargs = self.calls[0]
        self.assertEqual(kwargs["timeout"], 10.0)
        self.assertEqual(
            kwargs["extensions"]["timeout"],
            {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0},
        )
