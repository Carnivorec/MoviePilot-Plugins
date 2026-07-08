from random import randint, choice

from p115client import P115Client, check_response

from app.core.cache import cached
from app.log import logger
from .p115_timeout import build_p115_request_kwargs


class UserAgentUtils:
    """
    User-Agent 生成工具
    """

    @staticmethod
    @cached(region="p115strmhelper_util_real_app_ver", ttl=60 * 60, skip_none=True)
    def get_real_app_ver() -> str:
        """
        /* 步骤1：获取真实 app_ver
        ========
        目标：
        1) 从 115 appversion 接口获取真实 iOS 端版本号。
        2) 为 AppVerPatcher 提供新版 p115client._app_version 值。
        数据源：
        1) P115Client.app_version_list2()。
        2) build_p115_request_kwargs(timeout=10)。
        操作要点：
        1) 版本探测请求必须携带短 timeout，避免插件初始化被长期阻塞。
        2) 请求失败时使用本地固定版本兜住初始化流程。
        */
        """
        logger.info("【User-Agent】真实 app_ver 获取步骤1开始")
        # // 1.1 先使用短 timeout 探测真实 iOS App 版本
        try:
            resp = P115Client.app_version_list2(
                **build_p115_request_kwargs(timeout=10)
            )
            check_response(resp)
            version = resp["data"]["iOS-iPhone"]["version_code"]
        except Exception as e:
            # // 1.2 版本探测失败时使用固定版本，避免插件初始化失败
            logger.warning(f"【User-Agent】真实 app_ver 获取失败，使用默认版本: {e}")
            version = "38.0.2"
        logger.info("【User-Agent】真实 app_ver 获取步骤1结束")
        return version

    @staticmethod
    @cached(
        region="p115strmhelper_util_user_agent_u115_ios", ttl=60 * 60, skip_none=True
    )
    def generate_u115_ios() -> str:
        """
        生成 115 iOS User-Agent 字符串

        :return str: 完整的 User-Agent 字符串
        """
        try:
            resp = P115Client.app_version_list2(
                **build_p115_request_kwargs(timeout=10)
            )
            check_response(resp)
            udown_version = resp["data"]["iOS-iPhone"]["version_code"]
            wangpan_version = resp["data"]["115wangpan_iOS"]["version_code"]
        except Exception:
            udown_version = "38.0.2"
            wangpan_version = "36.2.20"
        ios_versions = [
            "15_0",
            "15_1",
            "15_2",
            "15_3",
            "15_4",
            "15_5",
            "15_6",
            "15_7",
            "15_8",
            "16_0",
            "16_1",
            "16_2",
            "16_3",
            "16_4",
            "16_5",
            "16_6",
            "16_7",
            "17_0",
            "17_1",
            "17_2",
            "17_3",
            "17_4",
            "17_5",
            "18_0",
            "18_1",
        ]
        build_num = randint(15, 21)
        build_letter = choice("ABCDE")
        build_tail = randint(100, 999)
        build = f"{build_num}{build_letter}{build_tail}"
        webkit = "605.1.15"
        os_ver = choice(ios_versions)
        client = choice(
            [
                f"115wangpan_ios/{wangpan_version}",
                f"UDown/{udown_version}",
            ]
        )
        return (
            f"Mozilla/5.0 (iPhone; CPU iPhone OS {os_ver} like Mac OS X) "
            f"AppleWebKit/{webkit} (KHTML, like Gecko) Mobile/{build} {client}"
        )
