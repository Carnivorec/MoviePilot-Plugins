from functools import wraps
from collections import UserString
from collections.abc import Buffer
from traceback import format_exc
from typing import Any, Callable, Coroutine, Literal, Optional, Union
from urllib.parse import urlencode

from orjson import dumps, loads as json_loads
from dicttools import dict_update, dict_key_to_lower_merge
from ensure import ensure_bytes
from http_request import complete_url as make_url
import p115client.client as _p115_client_mod
from p115client import P115Client
from p115client.util import complete_url
from p115cipher import (
    rsa_encrypt,
    rsa_decrypt,
    ecdh_aes_encrypt,
    ecdh_aes_decrypt,
    make_upload_payload,
)
from yarl import URL

from app.log import logger

from ..utils.user_agent import UserAgentUtils


PLACEHOLDER_APP_VER = "99.99.99.99"
_DEFAULT_K_EC = "HQMOgKF43O7OzaN33hKNAHMAAAAAAAAAjtndz1WuYe1G6hIaHPyBAAEAAAAMfvVI"

_MARKER = "__p115strmhelper_app_ver_patched__"


def _json_loads(content: Buffer, /):
    """
    解析 115 JSON 响应
    """
    return json_loads(memoryview(content).cast("B"))


def _json_parse(_, content: Buffer, /):
    """
    解析普通 JSON 响应
    """
    return _json_loads(content)


def _json_decrypt_parse(_, content: Buffer, /):
    """
    解析 ecdh 加密 JSON 响应
    """
    return _json_loads(ecdh_aes_decrypt(content))


def _real_ua(real: str) -> str:
    """
    生成使用真实版本号的 115disk User-Agent
    """
    return f"Mozilla/5.0 115disk/{real} 115Browser/{real} 115wangpan_android/{real}"


class AppVerPatcher:
    """
    app_ver 补丁

    1. ``p115client.client.get_request``：所有 GET 请求的 ``params["app_ver"]``
       由该函数用 setdefault 塞入占位值，这里在其返回后统一替换。覆盖
       behavior/detail、life_show、iter_life_list 等全部 GET 接口。
    2. ``P115Client._clouddownload_lixianssp_request``：离线接口在方法体内无条件
       写死 ``app_ver`` / UA 并立即 RSA 加密，无法事后修改，故复制方法体、替换占位值。
    3. ``P115Client.upload_init``：上传初始化在方法体内无条件写死 ``appversion`` / UA
       并立即 ``make_upload_payload`` 加密，同样复制方法体、替换占位值。间接经由
       ``upload_file`` / ``upload_file_init`` / ``tool.upload`` 触达。
    """

    _original_get_request: Optional[Callable[..., Any]] = None
    _original_lixianssp: Optional[Callable[..., Any]] = None
    _original_upload_init: Optional[Callable[..., Any]] = None
    _active: bool = False

    @staticmethod
    def _is_valid_get_request_result(result: Any) -> bool:
        """
        判断 get_request 的返回值是否符合 p115client.request 的解包约定
        """
        return (
            isinstance(result, tuple)
            and len(result) == 2
            and callable(result[0])
            and isinstance(result[1], dict)
        )

    @classmethod
    def _get_request_shape_is_valid(cls, func: Callable[..., Any]) -> bool:
        """
        用无网络参数检查 get_request 返回形态
        """
        try:
            result = func(
                "https://example.test",
                params={},
                request=lambda **request_kwargs: {"ok": True},
            )
        except Exception:
            return False
        return cls._is_valid_get_request_result(result)

    @classmethod
    def _build_compatible_get_request(cls) -> Callable[..., tuple[Callable, dict]]:
        """
        构建与 p115client 0.0.9.3.6 等价的 get_request

        只复制 GET 请求参数整理逻辑，不重新加载整个 ``p115client.client`` 模块，
        避免当前容器内 ``p115oss`` 依赖链在 reload 时触发额外导入错误。
        """

        def compatible_get_request(
            url: str,
            method: str = "GET",
            payload: Any = None,
            headers: Any = None,
            ecdh_encrypt: bool = False,
            request: Optional[Callable] = None,
            **request_kwargs,
        ) -> tuple[Callable, dict]:
            """
            /* 步骤1：恢复请求函数
            ========
            目标：
            1) 保持 p115client 原始默认请求库选择。
            2) 不访问网络，只准备 request 与 request_kwargs。
            数据源：
            1) 调用参数 request。
            操作要点：
            1) request 缺失时延迟导入 urllib3_future_request.request。
            */
            """
            logger.info("【app_ver】get_request 兼容恢复步骤1开始")
            # // 1.1 缺省请求函数时使用 p115client 原默认请求函数
            if request is None:
                try:
                    from urllib3_future_request import request as default_request
                except Exception as e:
                    logger.warning(
                        f"【app_ver】urllib3_future_request 不可用，切换到 httpcore_request: {e}"
                    )
                    from httpcore_request import request as default_request

                request = default_request
            logger.info("【app_ver】get_request 兼容恢复步骤1结束")

            """
            /* 步骤2：整理请求载荷
            ========
            目标：
            1) 复刻 p115client.client.get_request 的参数落位。
            2) 为 GET payload 注入 params，为 POST/PUT payload 注入 data。
            数据源：
            1) url、method、payload、request_kwargs。
            操作要点：
            1) open API 不启用 ecdh 加密。
            2) params 中缺省 app_ver 时仍先写占位值，外层补丁再替换真实版本。
            */
            """
            logger.info("【app_ver】get_request 兼容恢复步骤2开始")
            # // 2.1 兼容 p115client 允许 url/base_url 为可调用对象的路径
            if callable(url):
                url = url()
            if not isinstance(url, str):
                logger.warning(
                    f"【app_ver】get_request URL 非字符串，已转为字符串: type={type(url)!r}, value={url!r}"
                )
                url = str(url)
            # // 2.2 设置基础请求参数
            request_kwargs.update(url=url, method=method)
            # // 2.3 open API 保持 p115client 原始语义，不使用 ecdh 加密
            try:
                is_open_api = URL(url).path.startswith("/open/")
            except Exception as e:
                logger.error(
                    f"【app_ver】get_request URL 解析失败: type={type(url)!r}, value={url!r}, error={e}, traceback={format_exc()}"
                )
                raise
            if is_open_api:
                ecdh_encrypt = False
            # // 2.4 根据请求方法放置 payload
            if payload is not None:
                request_kwargs.setdefault(
                    "data" if method.upper() in ("POST", "PUT") else "params",
                    payload,
                )
            # // 2.4 先写入占位 app_ver，后续 patched_get_request 会替换为真实版本
            params = request_kwargs.get("params")
            if isinstance(params, dict):
                params.setdefault("app_ver", PLACEHOLDER_APP_VER)
            logger.info("【app_ver】get_request 兼容恢复步骤2结束")

            """
            /* 步骤3：整理请求头与解析器
            ========
            目标：
            1) 复刻 referer、headers 小写合并和响应 parse 默认值。
            2) 保留 ecdh 加密请求的 payload 与 parse 处理。
            数据源：
            1) headers、request_kwargs、ecdh_encrypt。
            操作要点：
            1) 所需函数来自已加载的 p115client.client 模块 globals。
            */
            """
            logger.info("【app_ver】get_request 兼容恢复步骤3开始")
            # // 3.1 合并并规范化请求头
            header_dict = request_kwargs["headers"] = dict_key_to_lower_merge(
                headers or ()
            )
            # // 3.2 补齐 referer
            header_dict["referer"] = header_dict.get("referer") or str(
                URL(url).origin()
            )
            # // 3.3 处理 ecdh 加密请求
            if ecdh_encrypt:
                url_with_ec = make_url(url, params={"k_ec": _DEFAULT_K_EC})
                request_kwargs["url"] = url_with_ec
                if data := request_kwargs.get("data"):
                    if not isinstance(
                        data,
                        (
                            Buffer,
                            str,
                            UserString,
                        ),
                    ):
                        data = urlencode(data)
                    request_kwargs["data"] = ecdh_aes_encrypt(ensure_bytes(data) + b"&")
                    header_dict["content-type"] = "application/x-www-form-urlencoded"
                request_kwargs.setdefault("parse", _json_decrypt_parse)
            else:
                request_kwargs.setdefault("parse", _json_parse)
            logger.info("【app_ver】get_request 兼容恢复步骤3结束")
            return request, request_kwargs

        return compatible_get_request

    @classmethod
    def _restore_get_request_with_compatible_impl(cls) -> Callable[..., Any]:
        """
        用本地兼容实现恢复 get_request
        """
        restored = cls._build_compatible_get_request()
        cls._sync_get_request_globals(restored)
        logger.warning("【app_ver】已使用兼容实现恢复 p115client.client.get_request")
        return restored


    @classmethod
    def _sync_get_request_globals(cls, func: Callable[..., Any]) -> None:
        """
        同步 p115client.client 模块属性和所有已知 P115Client.request globals
        """
        _p115_client_mod.get_request = func
        candidates = {P115Client}
        try:
            candidates.add(_p115_client_mod.P115Client)
        except Exception:
            pass
        for client_class in candidates:
            request_func = getattr(client_class, "request", None)
            request_globals = getattr(request_func, "__globals__", None)
            if isinstance(request_globals, dict):
                request_globals["get_request"] = func

    @staticmethod
    def _normalize_get_request_url_args(
        args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> tuple[tuple[Any, ...], dict[str, Any]]:
        """
        /* 步骤1：规范化 get_request URL 参数
        ========
        目标：
        1) 兼容 p115client 0.0.9.3.6 部分调用链传入 Callable URL 的情况。
        2) 避免原始 get_request 在 yarl.URL(url) 处因非字符串 URL 抛错。
        数据源：
        1) get_request 的位置参数 args。
        2) get_request 的关键字参数 kwargs。
        操作要点：
        1) 只处理第一个 URL 参数或 url 关键字。
        2) Callable 先求值，仍非 str 时记录 warning 并转为 str。
        */
        """
        logger.info("【app_ver】get_request URL 参数规范化步骤1开始")

        # // 1.1 从位置参数或关键字参数中读取 URL
        target = None
        url = None
        if args:
            target = "args"
            url = args[0]
        elif "url" in kwargs:
            target = "kwargs"
            url = kwargs["url"]

        # // 1.2 没有 URL 参数时保持原调用参数
        if target is None:
            logger.info("【app_ver】get_request URL 参数规范化步骤1结束")
            return args, kwargs

        # // 1.3 Callable URL 先求值，兼容 p115client 完整 URL 延迟计算入口
        if callable(url):
            url = url()

        # // 1.4 非字符串 URL 转为字符串，避免 yarl.URL 构造失败
        if not isinstance(url, str):
            logger.warning(
                f"【app_ver】get_request URL 非字符串，已转为字符串: type={type(url)!r}, value={url!r}"
            )
            url = str(url)

        # // 1.5 写回规范化后的 URL
        if target == "args":
            args_list = list(args)
            args_list[0] = url
            args = tuple(args_list)
        else:
            kwargs = dict(kwargs)
            kwargs["url"] = url

        logger.info("【app_ver】get_request URL 参数规范化步骤1结束")
        return args, kwargs

    @classmethod
    def _unwrap_marked_get_request(cls) -> Optional[Callable[..., Any]]:
        """
        清理上一次 reload 中残留的 get_request 补丁

        如果上一次 enable 在包装 get_request 后、设置 _active 前失败，MoviePilot
        清理模块缓存后新 AppVerPatcher 已不知道旧 _original_get_request。此时只能从
        functools.wraps 写入的 __wrapped__ 恢复原始函数。
        """
        current = getattr(_p115_client_mod, "get_request", None)
        if not getattr(current, _MARKER, False):
            if callable(current) and not cls._get_request_shape_is_valid(current):
                original = getattr(current, "__wrapped__", None)
                unwrap_count = 0
                while callable(original):
                    unwrap_count += 1
                    if cls._get_request_shape_is_valid(original):
                        cls._sync_get_request_globals(original)
                        logger.warning(
                            f"【app_ver】发现返回形态异常的 get_request 包装，已恢复原始函数，清理层数: {unwrap_count}"
                        )
                        return original
                    original = getattr(original, "__wrapped__", None)
                return cls._restore_get_request_with_compatible_impl()
            return current

        original = current
        unwrap_count = 0
        while getattr(original, _MARKER, False):
            next_original = getattr(original, "__wrapped__", None)
            if not callable(next_original):
                return cls._restore_get_request_with_compatible_impl()
            original = next_original
            unwrap_count += 1

        if callable(original) and cls._get_request_shape_is_valid(original):
            cls._sync_get_request_globals(original)
            logger.warning(
                f"【app_ver】发现残留 get_request 补丁，已恢复原始函数，清理层数: {unwrap_count}"
            )
            return original

        return cls._restore_get_request_with_compatible_impl()


    @classmethod
    def _wrap_get_request(cls) -> None:
        original = cls._unwrap_marked_get_request()
        if getattr(original, _MARKER, False):
            original = cls._restore_get_request_with_compatible_impl()
        if not callable(original) or not cls._get_request_shape_is_valid(original):
            raise RuntimeError("p115client.client.get_request 返回形态异常")

        @wraps(original)
        def patched(*args, **kwargs):
            args, kwargs = cls._normalize_get_request_url_args(args, kwargs)
            request, request_kwargs = original(*args, **kwargs)
            params = request_kwargs.get("params")
            if (
                isinstance(params, dict)
                and params.get("app_ver") == PLACEHOLDER_APP_VER
            ):
                params["app_ver"] = UserAgentUtils.get_real_app_ver()
            return request, request_kwargs

        setattr(patched, _MARKER, True)
        cls._original_get_request = original
        cls._sync_get_request_globals(patched)

    @staticmethod
    def _patched_lixianssp_request(
        self_instance: P115Client,
        payload: dict = {},
        /,
        action: str = "",
        base_url: Union[str, Callable[[], str]] = "https://clouddownload.115.com",
        *,
        async_: Literal[False, True] = False,
        **request_kwargs,
    ) -> Union[dict, Coroutine[Any, Any, dict]]:
        """
        重实现 ``_clouddownload_lixianssp_request``，使用真实 app_ver / UA
        """
        real = UserAgentUtils.get_real_app_ver()
        api = complete_url("/lixianssp/", base_url=base_url)
        request_kwargs["method"] = "POST"
        for k, v in payload.items():
            payload[k] = str(v)
        if action:
            payload["ac"] = action
        payload["app_ver"] = real
        request_kwargs["headers"] = {
            **(request_kwargs.get("headers") or {}),
            "user-agent": _real_ua(real),
        }
        request_kwargs["ecdh_encrypt"] = False

        def parse(_, content: bytes, /) -> dict:
            json = json_loads(content)
            if data := json.get("data"):
                try:
                    json["data"] = json_loads(rsa_decrypt(data))
                except Exception:
                    pass
            return json

        request_kwargs.setdefault("parse", parse)
        return self_instance.request(
            url=api,
            data={"data": rsa_encrypt(dumps(payload)).decode("ascii")},
            async_=async_,
            **request_kwargs,
        )

    @staticmethod
    def _patched_upload_init(
        self_instance: P115Client,
        payload: dict,
        /,
        base_url: Union[str, Callable[[], str]] = "https://uplb.115.com",
        *,
        async_: Literal[False, True] = False,
        **request_kwargs,
    ) -> Union[dict, Coroutine[Any, Any, dict]]:
        """
        重实现 ``upload_init``，使用真实 appversion / UA
        """
        real = UserAgentUtils.get_real_app_ver()
        api = complete_url("/4.0/initupload.php", base_url=base_url)
        payload = {
            "appid": 0,
            "target": "U_1_0",
            "sign_key": "",
            "sign_val": "",
            "topupload": "true",
            **payload,
            "appversion": real,
        }
        if "userid" not in payload:
            payload["userid"] = self_instance.user_id
        if "userkey" not in payload:
            payload["userkey"] = self_instance.user_key
        request_kwargs["headers"] = dict_update(
            dict(request_kwargs.get("headers") or ()),
            {
                "content-type": "application/x-www-form-urlencoded",
                "user-agent": _real_ua(real),
            },
        )
        request_kwargs.update(make_upload_payload(payload))

        def parse_upload_init_response(_, content: bytes, /) -> dict:
            data = ecdh_aes_decrypt(content)
            return json_loads(data)

        request_kwargs.setdefault("parse", parse_upload_init_response)
        return self_instance.request(
            url=api, method="POST", async_=async_, **request_kwargs
        )

    @classmethod
    def _wrap_method(
        cls,
        method_name: str,
        impl: Callable,
        *,
        required: bool = True,
    ) -> Optional[Callable]:
        """
        用 ``impl`` 包装 ``P115Client.<method_name>``，返回被替换的原方法。

        若目标已被本补丁包装（带 ``_MARKER``），则跳过并返回 None。
        """
        original = getattr(P115Client, method_name, None)
        if original is None:
            if required:
                raise AttributeError(
                    f"type object 'P115Client' has no attribute '{method_name}'"
                )
            logger.warning(
                f"【app_ver】当前 P115Client 缺少 {method_name}，已跳过对应补丁"
            )
            return None
        if getattr(original, _MARKER, False):
            return None

        @wraps(original)
        def patched(self, *args, **kwargs):
            return impl(self, *args, **kwargs)

        setattr(patched, _MARKER, True)
        setattr(P115Client, method_name, patched)
        return original

    @classmethod
    def _restore_method(cls, method_name: str, original: Optional[Callable]) -> None:
        """
        仅当当前方法仍是本补丁的包装时，才还原为 ``original``
        """
        if original is None:
            return
        current = getattr(P115Client, method_name, None)
        if getattr(current, _MARKER, False):
            setattr(P115Client, method_name, original)

    @classmethod
    def enable(cls) -> None:
        """
        应用补丁
        """
        if cls._active:
            return
        cls._wrap_get_request()
        cls._original_lixianssp = cls._wrap_method(
            "_clouddownload_lixianssp_request",
            cls._patched_lixianssp_request,
            required=False,
        )
        cls._original_upload_init = cls._wrap_method(
            "upload_init",
            cls._patched_upload_init,
            required=False,
        )
        cls._active = True
        logger.info("【app_ver】app_ver 补丁应用成功")

    @classmethod
    def disable(cls) -> None:
        """
        禁用补丁
        """
        current_get_request = getattr(_p115_client_mod, "get_request", None)
        if cls._original_get_request is not None and getattr(
            current_get_request, _MARKER, False
        ):
            cls._sync_get_request_globals(cls._original_get_request)
        elif getattr(current_get_request, _MARKER, False):
            cls._unwrap_marked_get_request()
        cls._original_get_request = None

        if not cls._active:
            return

        cls._restore_method("_clouddownload_lixianssp_request", cls._original_lixianssp)
        cls._original_lixianssp = None

        cls._restore_method("upload_init", cls._original_upload_init)
        cls._original_upload_init = None

        cls._active = False
        logger.info("【app_ver】app_ver 补丁恢复原始状态成功")
