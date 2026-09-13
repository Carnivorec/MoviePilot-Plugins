"""MoviePilot 通过挂到 venv site-packages 自动加载（入口会清掉 PYTHONPATH）。

只把 *.115.com 等域名的 urllib3_future 请求改走中转盒，不设全局 HTTPS_PROXY。
开关文件：/config/p115-middlebox/enabled  内容为 1 启用 / 0 关闭。
"""

from __future__ import annotations

import contextvars
import os
import ssl
import sys
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse

_FLAG = Path("/config/p115-middlebox/enabled")
_CA = Path("/config/p115-middlebox/ca.pem")
_PROXY = os.environ.get("P115_MIDDLEBOX_PROXY") or os.environ.get(
    "P115_PROXY_URL", "http://172.20.0.1:17891"
)
_SUFFIXES = (
    "115.com",
    "115.com.cn",
    "115cdn.com",
    "115cdn.net",
    "115img.com",
    "anxia.com",
)
_IN_PROXY = contextvars.ContextVar("p115_middlebox_in_proxy", default=False)
_SSL_PATCHED = False
_PROXY_PATCHED = False
_PROXY_PATCHING = False
_HTTPX_PATCHED = False
_HTTPX_PATCHING = False


def _enabled() -> bool:
    try:
        return _FLAG.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        return False


def _is_115(host: str | None) -> bool:
    if not host:
        return False
    h = host.lower().rstrip(".")
    for suffix in _SUFFIXES:
        if h == suffix or h.endswith("." + suffix):
            return True
    return False


def _host_of(url: object) -> str:
    try:
        return (urlparse(str(url)).hostname or "").lower()
    except Exception:
        return ""


def _load_ca(ctx: ssl.SSLContext) -> None:
    try:
        ctx.load_verify_locations(str(_CA))
    except Exception:
        pass


def _install_ssl_ca() -> None:
    """补 load_default_certs：urllib3_future 创建 SSLContext 后走这条，且是 C 方法可补。

    不要补 SSLContext.__init__，CPython 里再包一层会变成 object.__init__。
    """
    global _SSL_PATCHED
    if _SSL_PATCHED or not _CA.exists():
        return
    orig_load_default = ssl.SSLContext.load_default_certs
    orig_cdc = ssl.create_default_context

    def load_default_certs(self, purpose=ssl.Purpose.SERVER_AUTH):
        orig_load_default(self, purpose)
        _load_ca(self)

    def cdc(*args, **kwargs):
        ctx = orig_cdc(*args, **kwargs)
        _load_ca(ctx)
        return ctx

    ssl.SSLContext.load_default_certs = load_default_certs  # type: ignore[method-assign]
    ssl.create_default_context = cdc  # type: ignore[assignment]
    _SSL_PATCHED = True

    for modname in (
        "urllib3_future.util.ssl_",
        "urllib3_future.util._async.ssl_",
    ):
        try:
            mod = __import__(modname, fromlist=["create_urllib3_context"])
        except Exception:
            continue
        orig = getattr(mod, "create_urllib3_context", None)
        if orig is None or getattr(orig, "_p115_ca", False):
            continue

        def wrapped(*args, _orig=orig, **kwargs):
            ctx = _orig(*args, **kwargs)
            _load_ca(ctx)
            return ctx

        wrapped._p115_ca = True  # type: ignore[attr-defined]
        mod.create_urllib3_context = wrapped


def _install_proxy() -> None:
    """用 ContextVar 防重入。AsyncProxyManager.urlopen 会 super() 回到 PoolManager.urlopen。"""
    global _PROXY_PATCHED, _PROXY_PATCHING
    if _PROXY_PATCHED or _PROXY_PATCHING:
        return
    _PROXY_PATCHING = True
    try:
        try:
            from urllib3_future import AsyncPoolManager, AsyncProxyManager, PoolManager, ProxyManager
        except Exception:
            return

        _sync_proxy = None
        _async_proxy = None

        def sync_proxy():
            nonlocal _sync_proxy
            if _sync_proxy is None:
                _sync_proxy = ProxyManager(_PROXY)
            return _sync_proxy

        def async_proxy():
            nonlocal _async_proxy
            if _async_proxy is None:
                _async_proxy = AsyncProxyManager(_PROXY)
            return _async_proxy

        orig_sync = PoolManager.urlopen.__func__ if hasattr(PoolManager.urlopen, "__func__") else PoolManager.urlopen
        orig_async = (
            AsyncPoolManager.urlopen.__func__
            if hasattr(AsyncPoolManager.urlopen, "__func__")
            else AsyncPoolManager.urlopen
        )

        def sync_urlopen(self, method, url, *args, **kwargs):
            if _IN_PROXY.get() or not _enabled() or not _is_115(_host_of(url)):
                return orig_sync(self, method, url, *args, **kwargs)
            tok = _IN_PROXY.set(True)
            try:
                return orig_sync(sync_proxy(), method, url, *args, **kwargs)
            finally:
                _IN_PROXY.reset(tok)

        async def async_urlopen(self, method, url, *args, **kwargs):
            if _IN_PROXY.get() or not _enabled() or not _is_115(_host_of(url)):
                return await orig_async(self, method, url, *args, **kwargs)
            tok = _IN_PROXY.set(True)
            try:
                return await orig_async(async_proxy(), method, url, *args, **kwargs)
            finally:
                _IN_PROXY.reset(tok)

        PoolManager.urlopen = sync_urlopen  # type: ignore[method-assign]
        AsyncPoolManager.urlopen = async_urlopen  # type: ignore[method-assign]
        _PROXY_PATCHED = True
        _install_ssl_ca()
    finally:
        _PROXY_PATCHING = False


def _install_httpx() -> None:
    """302 cookie 模式用 httpx.AsyncClient 打 http://proapi.115.com，不走 urllib3。"""
    global _HTTPX_PATCHED, _HTTPX_PATCHING
    if _HTTPX_PATCHED or _HTTPX_PATCHING:
        return
    _HTTPX_PATCHING = True
    try:
        try:
            import httpx
        except Exception:
            return

        if not hasattr(httpx, "Client") or not hasattr(httpx, "AsyncClient"):
            return
        if getattr(httpx.Client.send, "_p115_middlebox", False) and getattr(
            httpx.AsyncClient.send, "_p115_middlebox", False
        ):
            _HTTPX_PATCHED = True
            return
        proxy_sync = None
        proxy_async = None
        proxy_lock = Lock()

        def get_sync():
            nonlocal proxy_sync
            if proxy_sync is None:
                with proxy_lock:
                    if proxy_sync is None:
                        proxy_sync = httpx.Client(proxy=_PROXY, follow_redirects=True, timeout=30.0)
            return proxy_sync

        def get_async():
            nonlocal proxy_async
            if proxy_async is None:
                with proxy_lock:
                    if proxy_async is None:
                        proxy_async = httpx.AsyncClient(proxy=_PROXY, follow_redirects=True, timeout=30.0)
            return proxy_async

        orig_send = httpx.Client.send
        orig_asend = httpx.AsyncClient.send

        def _rewrite_https(request):
            if request.url.scheme == "http":
                return httpx.Request(
                    request.method,
                    str(request.url.copy_with(scheme="https")),
                    headers=request.headers,
                    content=request.content,
                    extensions=request.extensions,
                )
            return request

        def send(self, request, *args, **kwargs):
            if _IN_PROXY.get() or not _enabled() or not _is_115(request.url.host) or self is proxy_sync:
                return orig_send(self, request, *args, **kwargs)
            token = _IN_PROXY.set(True)
            try:
                return orig_send(get_sync(), _rewrite_https(request), *args, **kwargs)
            finally:
                _IN_PROXY.reset(token)

        async def asend(self, request, *args, **kwargs):
            if _IN_PROXY.get() or not _enabled() or not _is_115(request.url.host) or self is proxy_async:
                return await orig_asend(self, request, *args, **kwargs)
            token = _IN_PROXY.set(True)
            try:
                return await orig_asend(get_async(), _rewrite_https(request), *args, **kwargs)
            finally:
                _IN_PROXY.reset(token)

        send._p115_middlebox = True
        asend._p115_middlebox = True
        httpx.Client.send = send  # type: ignore[method-assign]
        httpx.AsyncClient.send = asend  # type: ignore[method-assign]
        _HTTPX_PATCHED = True
    finally:
        _HTTPX_PATCHING = False


def _install_import_hook() -> None:
    import builtins

    orig_import = builtins.__import__

    def hooked(name, globals=None, locals=None, fromlist=(), level=0):
        module = orig_import(name, globals, locals, fromlist, level)
        if name == "urllib3_future" or name.startswith("urllib3_future"):
            _install_ssl_ca()
            if not any("pip" in str(a) for a in sys.argv[:2]):
                _install_proxy()
        if name == "httpx" or name.startswith("httpx."):
            if not any("pip" in str(a) for a in sys.argv[:2]):
                _install_httpx()
        return module

    builtins.__import__ = hooked  # type: ignore[assignment]
    if not any("pip" in str(a) for a in sys.argv[:2]):
        _install_proxy()
        _install_httpx()


try:
    _install_ssl_ca()
    _install_import_hook()
except Exception:
    pass
