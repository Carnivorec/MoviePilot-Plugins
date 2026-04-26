"""
P115 请求超时参数测试。

覆盖两类容易漏掉的入口：
1. 统一 timeout helper 生成的 httpcore extensions.timeout。
2. 生成 iOS UA 时发生在 configer.get_ios_ua_app() 之前的 app_version_list2 请求。
"""

from types import ModuleType, SimpleNamespace
import importlib
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
