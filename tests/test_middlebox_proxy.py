"""中转导入钩子的重入与代理行为测试，不连接网络"""

import ast
import asyncio
import builtins
from contextvars import ContextVar
from pathlib import Path
from threading import Lock
from types import ModuleType
from unittest import TestCase

import httpx


SOURCE = Path(__file__).resolve().parents[1] / "local_tools/p115_middlebox_sitecustomize.py"


def load_hook(source=SOURCE):
    clients = []

    class Client:
        def __init__(self, **kwargs):
            self.options = kwargs
            clients.append(self)

        def send(self, request, *args, **kwargs):
            if request.headers.get("x-test-error"):
                raise ValueError("transport failed")
            return request, kwargs

    class AsyncClient(Client):
        async def send(self, request, *args, **kwargs):
            return request, kwargs

    fake = ModuleType("httpx")
    fake.Client, fake.AsyncClient, fake.Request = Client, AsyncClient, httpx.Request
    ns = {
        "_HTTPX_PATCHED": False, "_HTTPX_PATCHING": False,
        "_IN_PROXY": ContextVar("test_in_proxy", default=False),
        "_PROXY": "http://127.0.0.1:1", "_enabled": lambda: True,
        "_SUFFIXES": ("115.com", "115cdn.net"), "Lock": Lock,
    }
    imports = []

    def reentrant_import(name, *args, **kwargs):
        if name == "httpx":
            imports.append(name)
            if len(imports) > 8:
                raise ImportError("stop old recursive import in test")
            # 模拟现有 hooked(__import__) 在 import httpx 后再次调用安装函数
            ns["_install_httpx"]()
            return fake
        return builtins.__import__(name, *args, **kwargs)

    ns["__builtins__"] = dict(vars(builtins), __import__=reentrant_import)
    tree = ast.parse(source.read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in ("_install_httpx", "_is_115")]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), "exec"), ns)
    return ns, fake, clients, imports


class TestMiddleboxProxy(TestCase):
    def test_import_reentry_installs_one_wrapper_and_one_proxy(self):
        ns, fake, clients, imports = load_hook()
        ns["_install_httpx"]()
        outer = fake.Client()
        outer.send(httpx.Request("GET", "https://proapi.115.com/test"))
        outer.send(httpx.Request("GET", "https://proapi.115.com/test"))
        self.assertEqual(len([client for client in clients if client.options.get("proxy")]), 1)
        self.assertEqual(len(imports), 1)

    def test_reinstallation_does_not_wrap_a_tagged_send_again(self):
        ns, fake, _, _ = load_hook()
        ns["_install_httpx"]()
        original = fake.Client.send
        ns["_HTTPX_PATCHED"] = False
        ns["_install_httpx"]()
        self.assertIs(fake.Client.send, original)

    def test_http_upgrade_preserves_body_headers_and_timeout(self):
        ns, fake, _, _ = load_hook()
        ns["_install_httpx"]()
        request = httpx.Request("POST", "http://proapi.115.com/test", headers={"x-test": "value"},
                                content=b"payload", extensions={"timeout": {"read": 10}})
        result, kwargs = fake.Client().send(request, stream=True)
        self.assertEqual(result.url.scheme, "https")
        self.assertEqual(result.content, b"payload")
        self.assertEqual(result.headers["x-test"], "value")
        self.assertEqual(result.extensions, request.extensions)
        self.assertTrue(kwargs["stream"])

    def test_async_proxy_is_reused_and_foreign_domains_are_untouched(self):
        ns, fake, clients, _ = load_hook()
        ns["_install_httpx"]()

        async def run():
            client = fake.AsyncClient()
            await client.send(httpx.Request("GET", "http://proapi.115.com/test"))
            await client.send(httpx.Request("GET", "http://proapi.115.com/test"))
            foreign = httpx.Request("GET", "http://proapi.115.com.example.invalid/test")
            result, _ = await client.send(foreign)
            self.assertIs(result, foreign)

        asyncio.run(run())
        self.assertEqual(len([client for client in clients if client.options.get("proxy")]), 1)

    def test_transport_error_does_not_leave_reentry_flag_set(self):
        ns, fake, _, _ = load_hook()
        ns["_install_httpx"]()
        with self.assertRaises(ValueError):
            fake.Client().send(httpx.Request("GET", "http://proapi.115.com/test", headers={"x-test-error": "yes"}))
        self.assertFalse(ns["_IN_PROXY"].get())

    def test_layered_wrappers_do_not_create_recursive_proxy_clients(self):
        ns, fake, clients, _ = load_hook()
        ns["_install_httpx"]()
        fake.Client.send._p115_middlebox = False
        ns["_HTTPX_PATCHED"] = False
        ns["_install_httpx"]()
        fake.Client().send(httpx.Request("GET", "http://proapi.115.com/test"))
        self.assertEqual(len([client for client in clients if client.options.get("proxy")]), 1)
