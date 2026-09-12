from typing import Any, Dict

from ..core.p115_client import _build_request_timeout, _detect_timeout_style


def build_p115_request_kwargs(timeout: float = 10.0) -> Dict[str, Any]:
    """为实例包装器覆盖不到的静态请求复用官方超时后端适配"""
    values = {key: float(timeout) for key in ("connect", "read", "write", "pool")}
    return _build_request_timeout(values, _detect_timeout_style())
