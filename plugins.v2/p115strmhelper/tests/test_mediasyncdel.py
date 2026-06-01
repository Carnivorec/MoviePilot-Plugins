"""
MediaSyncDelHelper 测试模块

包含同步删除相关方法的单元测试
"""

import importlib
import sys
from pathlib import Path
from types import ModuleType
from unittest import TestCase
from unittest.mock import Mock, patch


def _fake_logger():
    return Mock(
        debug=Mock(),
        info=Mock(),
        warning=Mock(),
        warn=Mock(),
        error=Mock(),
    )


def _install_app_stubs(plugin_root: Path):
    app = sys.modules.setdefault("app", ModuleType("app"))
    app.__path__ = []

    app_plugins = sys.modules.setdefault("app.plugins", ModuleType("app.plugins"))
    app_plugins.__path__ = []

    app_core = sys.modules.setdefault("app.core", ModuleType("app.core"))
    app_core.__path__ = []

    app_core_event = ModuleType("app.core.event")
    app_core_event.Event = type("Event", (), {})
    sys.modules["app.core.event"] = app_core_event

    app_log = ModuleType("app.log")
    app_log.logger = _fake_logger()
    sys.modules["app.log"] = app_log

    app_utils = sys.modules.setdefault("app.utils", ModuleType("app.utils"))
    app_utils.__path__ = []
    app_utils_system = ModuleType("app.utils.system")
    app_utils_system.SystemUtils = type(
        "SystemUtils",
        (),
        {"exits_files": staticmethod(lambda *args, **kwargs: False)},
    )
    sys.modules["app.utils.system"] = app_utils_system

    app_core_config = ModuleType("app.core.config")
    app_core_config.settings = Mock(RMT_MEDIAEXT=[".iso", ".mkv"])
    sys.modules["app.core.config"] = app_core_config

    transferhistory_model = ModuleType("app.db.models.transferhistory")
    transferhistory_model.TransferHistory = type("TransferHistory", (), {})
    sys.modules.setdefault("app.db", ModuleType("app.db")).__path__ = []
    sys.modules.setdefault("app.db.models", ModuleType("app.db.models")).__path__ = []
    sys.modules["app.db.models.transferhistory"] = transferhistory_model

    for module_name, class_name in [
        ("app.db.transferhistory_oper", "TransferHistoryOper"),
        ("app.db.downloadhistory_oper", "DownloadHistoryOper"),
        ("app.db.plugindata_oper", "PluginDataOper"),
        ("app.helper.downloader", "DownloaderHelper"),
        ("app.chain.storage", "StorageChain"),
    ]:
        parent_name = module_name.rsplit(".", 1)[0]
        sys.modules.setdefault(parent_name, ModuleType(parent_name)).__path__ = []
        module = ModuleType(module_name)
        module.__dict__[class_name] = type(class_name, (), {})
        sys.modules[module_name] = module

    schemas_types = ModuleType("app.schemas.types")
    schemas_types.MediaType = type("MediaType", (), {"MOVIE": "MOV", "TV": "TV"})
    schemas_types.MediaImageType = type("MediaImageType", (), {})
    schemas_types.NotificationType = type("NotificationType", (), {})
    sys.modules.setdefault("app.schemas", ModuleType("app.schemas")).__path__ = []
    sys.modules["app.schemas.types"] = schemas_types

    schemas_mediaserver = ModuleType("app.schemas.mediaserver")
    schemas_mediaserver.WebhookEventInfo = type("WebhookEventInfo", (), {})
    sys.modules["app.schemas.mediaserver"] = schemas_mediaserver

    package_prefix = "app.plugins.p115strmhelper"
    stubs = {
        f"{package_prefix}.core.config": {"configer": Mock(storage_module="115网盘Plus")},
        f"{package_prefix}.core.i18n": {"i18n": Mock(translate=lambda key: key)},
        f"{package_prefix}.core.message": {"post_message": Mock()},
        f"{package_prefix}.core.plunins": {"PluginChian": type("PluginChian", (), {})},
        f"{package_prefix}.db_manager.oper": {"TransferHBOper": type("TransferHBOper", (), {})},
        f"{package_prefix}.helper.mediaserver": {"EmbyOperate": type("EmbyOperate", (), {})},
        f"{package_prefix}.utils.sentry": {
            "sentry_manager": Mock(capture_all_class_exceptions=lambda cls: cls)
        },
        f"{package_prefix}.utils.webhook": {"WebhookUtils": type("WebhookUtils", (), {})},
    }
    for module_name, attrs in stubs.items():
        parent_name = module_name.rsplit(".", 1)[0]
        parent = sys.modules.setdefault(parent_name, ModuleType(parent_name))
        parent.__path__ = [str(plugin_root / "/".join(parent_name.split(".")[3:]))]
        module = ModuleType(module_name)
        module.__dict__.update(attrs)
        sys.modules[module_name] = module


def _load_mediasyncdel_module():
    """
    按 MoviePilot 插件包路径加载同步删除模块

    :return: 已加载模块
    """
    plugin_root = Path(__file__).resolve().parent.parent
    moviepilot_path = next(
        (
            path
            for path in sys.path
            if path and Path(path).name == "MoviePilot" and Path(path).exists()
        ),
        None,
    )
    if moviepilot_path:
        sys.path.remove(moviepilot_path)
        sys.path.insert(0, moviepilot_path)

    _install_app_stubs(plugin_root)

    package_name = "app.plugins.p115strmhelper"
    package = ModuleType(package_name)
    package.__path__ = [str(plugin_root)]
    sys.modules[package_name] = package
    module_name = "app.plugins.p115strmhelper.helper.mediasyncdel"
    sys.modules.pop(module_name, None)
    return importlib.import_module(module_name)


class TestMediaSyncDelHelper(TestCase):
    """
    测试 MediaSyncDelHelper
    """

    def test_get_p115_media_suffix_uses_embedded_iso_suffix(self):
        """
        ISO STRM 文件名已带真实后缀时直接返回，不访问网盘目录
        """
        module = _load_mediasyncdel_module()
        helper = object.__new__(module.MediaSyncDelHelper)
        helper.storagechain = Mock()

        with patch.object(module.settings, "RMT_MEDIAEXT", [".iso", ".mkv"]):
            result = helper._MediaSyncDelHelper__get_p115_media_suffix(
                "/媒体库/ISO/极限审判 (2026)/极限审判 (2026).iso.strm",
                "/媒体库#/mp#/115",
            )

        self.assertEqual(result, "iso")
        helper.storagechain.get_file_item.assert_not_called()
