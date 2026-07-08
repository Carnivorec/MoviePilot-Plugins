from typing import Optional

import p115client.client as _p115_client_mod

from app.log import logger

from ..utils.user_agent import UserAgentUtils

_APP_VERSION_ATTR = "_app_version"


class AppVerPatcher:
    """
    app_ver 补丁。
    """

    _original_app_version: Optional[str] = None
    _patched_app_version: Optional[str] = None
    _active: bool = False

    @classmethod
    def enable(cls) -> None:
        """
        /* 步骤1：校验补丁目标
        ========
        目标：
        1) 避免重复启用补丁。
        2) 确认 p115client 暴露统一的 _app_version 入口。
        数据源：
        1) cls._active。
        2) p115client.client 模块属性。
        操作要点：
        1) 已启用时直接返回。
        2) 依赖不兼容时记录 warning，不影响插件导入。
        */
        """
        logger.info("【app_ver】补丁启用步骤1开始")
        # // 1.1 已启用时避免重复覆盖原始版本号
        if cls._active:
            logger.info("【app_ver】补丁启用步骤1结束：已启用")
            return
        # // 1.2 新版 p115client 必须通过 _app_version 统一控制 app_ver/appversion/UA
        if not hasattr(_p115_client_mod, _APP_VERSION_ATTR):
            logger.warning(
                "【app_ver】未找到 p115client.client._app_version，跳过补丁"
                "（p115client 版本可能不兼容）"
            )
            logger.info("【app_ver】补丁启用步骤1结束：目标缺失")
            return
        logger.info("【app_ver】补丁启用步骤1结束")

        """
        /* 步骤2：写入真实版本号
        ========
        目标：
        1) 把新版 p115client 的统一版本入口替换为真实 App 版本。
        2) 让 GET、离线、上传初始化等请求共享同一个版本来源。
        数据源：
        1) p115client.client._app_version 原始值。
        2) UserAgentUtils.get_real_app_ver() 探测结果。
        操作要点：
        1) 保存原始值，便于 disable 恢复。
        2) 只改版本号，不修改请求 kwargs，避免覆盖 timeout。
        */
        """
        logger.info("【app_ver】补丁启用步骤2开始")
        # // 2.1 保存原始版本，支持插件 reload 时恢复
        cls._original_app_version = getattr(_p115_client_mod, _APP_VERSION_ATTR)
        # // 2.2 用带 timeout 的版本探测结果替换统一 app_version
        real = UserAgentUtils.get_real_app_ver()
        setattr(_p115_client_mod, _APP_VERSION_ATTR, real)
        cls._patched_app_version = real
        cls._active = True
        logger.info("【app_ver】补丁启用步骤2结束")
        logger.info(f"【app_ver】app_ver 补丁应用成功，app_ver={real}")

    @classmethod
    def disable(cls) -> None:
        """
        /* 步骤1：恢复原始版本号
        ========
        目标：
        1) 在插件停用或 reload 时恢复 p115client 原始状态。
        2) 避免覆盖其它模块后续主动写入的版本号。
        数据源：
        1) cls._active。
        2) cls._original_app_version 和 cls._patched_app_version。
        操作要点：
        1) 只有当前值仍是本补丁写入值时才恢复。
        2) 清理类状态，保证下一次 enable 可重新应用。
        */
        """
        logger.info("【app_ver】补丁恢复步骤1开始")
        # // 1.1 未启用时无需恢复
        if not cls._active:
            logger.info("【app_ver】补丁恢复步骤1结束：未启用")
            return
        # // 1.2 只恢复本补丁写入的版本值，避免误覆盖外部更新
        if cls._original_app_version is not None and (
            getattr(_p115_client_mod, _APP_VERSION_ATTR, None)
            == cls._patched_app_version
        ):
            setattr(_p115_client_mod, _APP_VERSION_ATTR, cls._original_app_version)
        # // 1.3 清理补丁状态
        cls._original_app_version = None
        cls._patched_app_version = None
        cls._active = False
        logger.info("【app_ver】补丁恢复步骤1结束")
        logger.info("【app_ver】app_ver 补丁恢复原始状态成功")
