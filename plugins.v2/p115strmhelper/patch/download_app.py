from functools import wraps
from typing import Any, Callable

import p115client.client as _p115_client_mod

from app.log import logger


_TARGET_METHODS = ("download_folders_app", "download_files_app")
_MARKER = "__p115strmhelper_download_app_patched__"


class DownloadAppPatcher:
    """
    download_folders_app / download_files_app 补丁。
    """

    _originals: dict[str, Callable[..., Any]] = {}
    _active: bool = False

    @staticmethod
    def _force_chrome(original: Callable[..., Any]) -> Callable[..., Any]:
        """
        /* 步骤1：构建 chrome 下载包装器
        ========
        目标：
        1) 强制下载列表接口使用 chrome app 语义。
        2) 保留调用方传入的 timeout、extensions 和其它 kwargs。
        数据源：
        1) p115client 原始 download_folders_app/download_files_app 方法。
        操作要点：
        1) 只替换 app 参数。
        2) 不删除 args/kwargs，避免本地 timeout 包装器失效。
        */
        """
        logger.info("【download_app】包装器构建步骤1开始")

        @wraps(original)
        def wrapper(
            self_instance: Any,
            payload: Any,
            /,
            app: str = "chrome",
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            """
            /* 步骤1：执行 chrome 下载请求
            ========
            目标：
            1) 忽略外部传入的非 chrome app。
            2) 原样透传 payload、args 和 kwargs。
            数据源：
            1) 调用方传入的 payload、args、kwargs。
            操作要点：
            1) app 固定传入 chrome。
            2) kwargs 中的 extensions.timeout 不做任何修改。
            */
            """
            logger.info("【download_app】chrome 下载请求步骤1开始")
            # // 1.1 固定 app=chrome，并保留所有附加请求参数
            result = original(self_instance, payload, "chrome", *args, **kwargs)
            logger.info("【download_app】chrome 下载请求步骤1结束")
            return result

        setattr(wrapper, _MARKER, True)
        logger.info("【download_app】包装器构建步骤1结束")
        return wrapper

    @classmethod
    def enable(cls) -> None:
        """
        /* 步骤1：校验并应用补丁
        ========
        目标：
        1) 找到新版 p115client 的下载 app 方法。
        2) 用 chrome 包装器替换目标方法。
        数据源：
        1) p115client.client.P115Client。
        2) _TARGET_METHODS。
        操作要点：
        1) 缺少方法时记录 warning，不影响插件导入。
        2) 已被本补丁包装时不重复包装。
        */
        """
        logger.info("【download_app】补丁启用步骤1开始")
        # // 1.1 已启用时避免重复包装
        if cls._active:
            logger.info("【download_app】补丁启用步骤1结束：已启用")
            return

        # // 1.2 读取 p115client.P115Client 类
        client_cls = getattr(_p115_client_mod, "P115Client", None)
        if client_cls is None:
            logger.warning(
                "【download_app】未找到 p115client.client.P115Client，跳过补丁"
                "（p115client 版本可能不兼容）"
            )
            logger.info("【download_app】补丁启用步骤1结束：客户端类缺失")
            return

        # // 1.3 确认两个目标方法均存在
        missing = [m for m in _TARGET_METHODS if not hasattr(client_cls, m)]
        if missing:
            logger.warning(
                f"【download_app】未找到方法 {missing}，跳过补丁"
                "（p115client 版本可能不兼容）"
            )
            logger.info("【download_app】补丁启用步骤1结束：目标方法缺失")
            return

        # // 1.4 包装目标方法，保留原始方法用于恢复
        for name in _TARGET_METHODS:
            original = getattr(client_cls, name)
            if getattr(original, _MARKER, False):
                continue
            cls._originals[name] = original
            setattr(client_cls, name, cls._force_chrome(original))

        cls._active = True
        logger.info("【download_app】补丁启用步骤1结束")
        logger.info(
            "【download_app】download_folders_app/download_files_app 补丁应用成功，强制走 chrome"
        )

    @classmethod
    def disable(cls) -> None:
        """
        /* 步骤1：恢复下载 app 方法
        ========
        目标：
        1) 还原 p115client 原始下载 app 方法。
        2) 清理补丁状态，支持插件 reload。
        数据源：
        1) cls._originals。
        2) p115client.client.P115Client。
        操作要点：
        1) 只有客户端类存在时才恢复。
        2) 恢复后清空 originals。
        */
        """
        logger.info("【download_app】补丁恢复步骤1开始")
        # // 1.1 未启用时无需恢复
        if not cls._active:
            logger.info("【download_app】补丁恢复步骤1结束：未启用")
            return

        # // 1.2 将已包装方法还原为原始方法
        client_cls = getattr(_p115_client_mod, "P115Client", None)
        if client_cls is not None:
            for name, original in cls._originals.items():
                setattr(client_cls, name, original)

        # // 1.3 清理补丁状态
        cls._originals = {}
        cls._active = False
        logger.info("【download_app】补丁恢复步骤1结束")
        logger.info(
            "【download_app】download_folders_app/download_files_app 补丁恢复原始状态成功"
        )
