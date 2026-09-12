"""静态请求复用官方超时后端适配的回归测试"""

import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest import TestCase
from unittest.mock import Mock, patch


class TestStaticRequestTimeout(TestCase):
    """验证静态入口采用官方参数格式且确实传入请求"""

    def test_helper_delegates_to_backend_adapter(self):
        adapter = ModuleType("static_test.core.p115_client")
        adapter._detect_timeout_style = Mock(return_value="urllib3_future")
        adapter._build_request_timeout = Mock(return_value={"timeout": "adapted"})
        source = Path(__file__).resolve().parents[1] / "utils/p115_timeout.py"
        spec = importlib.util.spec_from_file_location("static_test.utils.p115_timeout", source)
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"static_test.core.p115_client": adapter}):
            spec.loader.exec_module(module)
        self.assertEqual(module.build_p115_request_kwargs(10), {"timeout": "adapted"})
        adapter._build_request_timeout.assert_called_once_with(
            {"connect": 10.0, "read": 10.0, "write": 10.0, "pool": 10.0}, "urllib3_future")

    def test_both_user_agent_probes_pass_timeout_to_client(self):
        source = Path(__file__).resolve().parents[1] / "utils/user_agent.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                      and node.name == "get_real_app_ver")
        method.decorator_list = []
        client = Mock()
        client.app_version_list2.return_value = {"data": {"iOS-iPhone": {"version_code": "38.0.2"}}}
        namespace = {"P115Client": client, "check_response": lambda value: value,
                     "build_p115_request_kwargs": lambda timeout: {"timeout": timeout}}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(source), "exec"), namespace)
        self.assertEqual(namespace[method.name](), "38.0.2")
        client.app_version_list2.assert_called_once_with(timeout=10)
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "app_version_list2"]
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(any(k.arg is None for k in call.keywords) for call in calls))
