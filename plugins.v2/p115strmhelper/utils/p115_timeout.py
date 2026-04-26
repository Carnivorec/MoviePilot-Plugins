from copy import deepcopy
from typing import Any, Dict, Optional, Union

DEFAULT_CONNECT_TIMEOUT = 10.0
DEFAULT_READ_TIMEOUT = 60.0
DEFAULT_WRITE_TIMEOUT = 60.0
DEFAULT_POOL_TIMEOUT = 10.0


def build_p115_timeout_extensions(
    *,
    connect: Union[int, float] = DEFAULT_CONNECT_TIMEOUT,
    read: Union[int, float] = DEFAULT_READ_TIMEOUT,
    write: Union[int, float] = DEFAULT_WRITE_TIMEOUT,
    pool: Union[int, float] = DEFAULT_POOL_TIMEOUT,
) -> Dict[str, Dict[str, float]]:
    """
    生成 httpcore_request 可识别的 timeout 扩展参数。
    """
    return {
        "timeout": {
            "connect": float(connect),
            "read": float(read),
            "write": float(write),
            "pool": float(pool),
        }
    }


def apply_p115_request_timeout(
    kwargs: Optional[Dict[str, Any]] = None,
    *,
    connect: Union[int, float] = DEFAULT_CONNECT_TIMEOUT,
    read: Union[int, float] = DEFAULT_READ_TIMEOUT,
    write: Union[int, float] = DEFAULT_WRITE_TIMEOUT,
    pool: Union[int, float] = DEFAULT_POOL_TIMEOUT,
    timeout: Optional[Union[int, float]] = None,
) -> Dict[str, Any]:
    """
    给 p115client/httpcore_request 请求参数补齐超时。

    当前 p115client 的底层同步请求链路会读取 httpcore 的
    extensions.timeout；同时保留普通 timeout 字段，兼容其它 request
    实现或后续版本。
    """
    if kwargs is None:
        kwargs = {}

    if timeout is not None:
        connect = read = write = pool = float(timeout)

    extensions = deepcopy(kwargs.get("extensions") or {})
    extensions.update(
        build_p115_timeout_extensions(
            connect=connect,
            read=read,
            write=write,
            pool=pool,
        )
    )
    kwargs["extensions"] = extensions
    kwargs["timeout"] = float(timeout if timeout is not None else read)
    return kwargs


def build_p115_request_kwargs(
    *,
    connect: Union[int, float] = DEFAULT_CONNECT_TIMEOUT,
    read: Union[int, float] = DEFAULT_READ_TIMEOUT,
    write: Union[int, float] = DEFAULT_WRITE_TIMEOUT,
    pool: Union[int, float] = DEFAULT_POOL_TIMEOUT,
    timeout: Optional[Union[int, float]] = None,
) -> Dict[str, Any]:
    """
    构造只包含超时控制的 p115client 请求参数。
    """
    return apply_p115_request_timeout(
        {},
        connect=connect,
        read=read,
        write=write,
        pool=pool,
        timeout=timeout,
    )
