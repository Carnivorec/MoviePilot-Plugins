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


class TestAppVerPatcherCompatibility(TestCase):
    def setUp(self):
        self._saved_modules = {
            name: sys.modules.get(name)
            for name in [
                "app",
                "app.log",
                "p115client",
                "p115client.client",
                "p115client.util",
                "p115cipher",
                "fake_p115_plugin",
                "fake_p115_plugin.patch",
                "fake_p115_plugin.utils",
                "fake_p115_plugin.utils.user_agent",
                "fake_p115_plugin.patch.app_ver",
            ]
        }

        self.warning_messages = []
        fake_logger = SimpleNamespace(
            info=lambda *args, **kwargs: None,
            warning=lambda message, *args, **kwargs: self.warning_messages.append(message),
        )

        class FakeP115Client:
            user_id = "1"
            user_key = "key"

            def request(self, **kwargs):
                return kwargs

            def upload_init(self, payload, **kwargs):
                return payload, kwargs

        def fake_get_request(*args, **kwargs):
            return (lambda **request_kwargs: {"ok": True}), {
                "params": {"app_ver": "99.99.99.99"}
            }

        fake_p115client = ModuleType("p115client")
        fake_p115client.P115Client = FakeP115Client

        fake_p115client_client = ModuleType("p115client.client")
        fake_p115client_client.get_request = fake_get_request

        fake_p115client_util = ModuleType("p115client.util")
        fake_p115client_util.complete_url = lambda path, base_url=None: f"{base_url}{path}"

        fake_p115cipher = ModuleType("p115cipher")
        fake_p115cipher.rsa_encrypt = lambda value: value
        fake_p115cipher.rsa_decrypt = lambda value: value
        fake_p115cipher.ecdh_aes_encrypt = lambda value: value
        fake_p115cipher.ecdh_aes_decrypt = lambda value: value
        fake_p115cipher.make_upload_payload = lambda payload: {"data": payload}

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
                "p115client.util": fake_p115client_util,
                "p115cipher": fake_p115cipher,
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

    def _load_app_ver_module(self):
        module_path = Path(__file__).resolve().parents[1] / "patch" / "app_ver.py"
        spec = importlib.util.spec_from_file_location(
            "fake_p115_plugin.patch.app_ver", module_path
        )
        module = importlib.util.module_from_spec(spec)
        sys.modules["fake_p115_plugin.patch.app_ver"] = module
        spec.loader.exec_module(module)
        return module

    def test_enable_skips_missing_lixianssp_method_without_failing_reload(self):
        module = self._load_app_ver_module()

        module.AppVerPatcher.enable()

        self.assertTrue(module.AppVerPatcher._active)
        self.assertTrue(
            any("_clouddownload_lixianssp_request" in msg for msg in self.warning_messages)
        )
        module.AppVerPatcher.disable()

    def test_disable_restores_stale_get_request_when_previous_enable_failed(self):
        module = self._load_app_ver_module()
        client_mod = sys.modules["p115client.client"]
        original = client_mod.get_request

        @wraps(original)
        def stale_patched(*args, **kwargs):
            return lambda **request_kwargs: {"broken": True}

        setattr(stale_patched, module._MARKER, True)
        client_mod.get_request = stale_patched

        module.AppVerPatcher.disable()

        self.assertIs(client_mod.get_request, original)

    def test_enable_rewraps_stale_get_request_from_previous_module(self):
        module = self._load_app_ver_module()
        client_mod = sys.modules["p115client.client"]
        original = client_mod.get_request

        @wraps(original)
        def stale_patched(*args, **kwargs):
            return lambda **request_kwargs: {"broken": True}

        setattr(stale_patched, module._MARKER, True)
        client_mod.get_request = stale_patched

        module.AppVerPatcher.enable()
        request, request_kwargs = client_mod.get_request(
            "https://example.test", params={}
        )

        self.assertIs(request, object().__class__ if False else request)
        self.assertIs(client_mod.get_request.__wrapped__, original)
        self.assertEqual(request_kwargs["params"]["app_ver"], "35.9.0")
        module.AppVerPatcher.disable()

    def test_enable_recursively_unwraps_nested_stale_get_request_wrappers(self):
        module = self._load_app_ver_module()
        client_mod = sys.modules["p115client.client"]
        original = client_mod.get_request

        @wraps(original)
        def stale_inner(*args, **kwargs):
            return lambda **request_kwargs: {"broken": "inner"}

        setattr(stale_inner, module._MARKER, True)

        @wraps(stale_inner)
        def stale_outer(*args, **kwargs):
            return stale_inner(*args, **kwargs)

        setattr(stale_outer, module._MARKER, True)
        client_mod.get_request = stale_outer

        module.AppVerPatcher.enable()
        request, request_kwargs = client_mod.get_request(
            "https://example.test", params={}
        )

        self.assertIs(client_mod.get_request.__wrapped__, original)
        self.assertIsNotNone(request)
        self.assertEqual(request_kwargs["params"]["app_ver"], "35.9.0")
        module.AppVerPatcher.disable()

    def test_enable_normalizes_callable_url_before_calling_original_get_request(self):
        module = self._load_app_ver_module()
        client_mod = sys.modules["p115client.client"]

        def strict_get_request(url, **kwargs):
            if not isinstance(url, str):
                raise TypeError("Constructor parameter should be str")
            return (lambda **request_kwargs: {"ok": True}), {
                "url": url,
                "params": {"app_ver": "99.99.99.99"},
            }

        client_mod.get_request = strict_get_request

        module.AppVerPatcher.enable()
        _, request_kwargs = client_mod.get_request(lambda: "https://example.test")

        self.assertEqual(request_kwargs["url"], "https://example.test")
        self.assertEqual(request_kwargs["params"]["app_ver"], "35.9.0")
        module.AppVerPatcher.disable()

    def test_enable_unwraps_unmarked_broken_get_request_wrapper_by_shape(self):
        module = self._load_app_ver_module()
        client_mod = sys.modules["p115client.client"]
        original = client_mod.get_request

        @wraps(original)
        def unmarked_broken_wrapper(*args, **kwargs):
            return lambda **request_kwargs: {"broken": True}

        client_mod.get_request = unmarked_broken_wrapper

        module.AppVerPatcher.enable()
        request, request_kwargs = client_mod.get_request(
            "https://example.test", params={}
        )

        self.assertIs(client_mod.get_request.__wrapped__, original)
        self.assertIsNotNone(request)
        self.assertEqual(request_kwargs["params"]["app_ver"], "35.9.0")
        module.AppVerPatcher.disable()

    def test_enable_reloads_get_request_when_stale_wrapper_has_no_original(self):
        module = self._load_app_ver_module()
        client_mod = sys.modules["p115client.client"]
        original = client_mod.get_request

        def stale_patched(*args, **kwargs):
            return lambda **request_kwargs: {"broken": True}

        setattr(stale_patched, module._MARKER, True)
        client_mod.get_request = stale_patched

        module.AppVerPatcher.enable()
        request, request_kwargs = client_mod.get_request(
            "https://example.test", params={}
        )

        self.assertIsNot(client_mod.get_request.__wrapped__, original)
        self.assertIn(
            "compatible_get_request", client_mod.get_request.__wrapped__.__qualname__
        )
        self.assertIsNotNone(request)
        self.assertEqual(request_kwargs["params"]["app_ver"], "35.9.0")
        module.AppVerPatcher.disable()


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

        fake_app = ModuleType("app")
        fake_core = ModuleType("app.core")

        self._saved_modules = {
            name: sys.modules.get(name)
            for name in ["p115client", "app", "app.core", "app.core.cache", "utils.user_agent"]
        }
        sys.modules["p115client"] = fake_p115client
        sys.modules["app"] = fake_app
        sys.modules["app.core"] = fake_core
        sys.modules["app.core.cache"] = fake_cache
        sys.modules.pop("utils.user_agent", None)

    def tearDown(self):
        for name, module in self._saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

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
