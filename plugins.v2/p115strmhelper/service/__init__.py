from logging import ERROR
from time import time
from threading import Lock, Thread, Event as ThreadEvent
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from aligo.core import set_config_folder
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from p115client import P115Client
from pytz import timezone
from watchfiles import watch, Change

from ..core.aliyunpan import BAligo
from ..core.config import configer
from ..core.i18n import i18n
from ..core.message import post_message
from ..core.p115 import get_pid_by_path
from ..helper.clean import Cleaner
from ..helper.life import MonitorLife
from ..helper.mediainfo_download import MediaInfoDownloader
from ..helper.monitor.directory_upload_queue import (
    DirectoryUploadTask,
    directory_upload_queue,
)
from ..helper.offline import OfflineDownloadHelper
from ..helper.r302 import Redirect
from ..helper.share import ShareTransferHelper
from ..helper.strm import (
    FullSyncStrmHelper,
    IncrementSyncStrmHelper,
    ShareInteractiveGenStrmQueue,
    ShareStrmHelper,
)
from ..helper.strm.share import share_strm_cleaner
from ..helper.transfer import TransferTaskManager, TransferHandler
from ..helper.webdav import WebdavCore
from ..helper.mediaserver import emby_mediainfo_queue
from ..helper.mediasyncdel.webhook_queue import sync_del_webhook_queue
from ..patch import TransferChainPatcher
from ..schemas.monitor import ObserverInfo
from ..service.fuse import FuseManager
from ..service.life import monitor_life_thread_worker
from ..service.hdhive_checkin.scheduler import hdhive_checkin_scheduler_tick
from ..utils.sentry import sentry_manager
from ..utils.sync_lock import (
    STRM_SYNC_TASK_FULL,
    STRM_SYNC_TASK_INCREMENT,
    StrmSyncRunGuard,
)

from app.log import logger
from app.core.config import settings
from app.schemas import NotificationType
from app.scheduler import Scheduler


@sentry_manager.capture_all_class_exceptions
class ServiceHelper:
    """
    服务项
    """

    FULL_SYNC_PRIORITY_LEAD_SECONDS = 30

    def __init__(self):
        self.client = None
        self.mediainfodownloader: Optional[MediaInfoDownloader] = None
        self.monitorlife: Optional[MonitorLife] = None
        self.aligo: Optional[BAligo] = None

        self.sharetransferhelper: Optional[ShareTransferHelper] = None

        self.monitor_stop_event: Optional[ThreadEvent] = None
        self.monitor_life_thread: Optional[Thread] = None
        self.monitor_life_lock = Lock()
        self.monitor_life_fail_time: Optional[float] = None

        self.offlinehelper: Optional[OfflineDownloadHelper] = None

        self.redirect: Optional[Redirect] = None

        self.scheduler: Optional[BackgroundScheduler] = None

        self.service_observer: List[ObserverInfo] = []

        self.fuse_manager: Optional[FuseManager] = None

        self.transfer_task_manager: Optional[TransferTaskManager] = None
        self.transfer_handler: Optional[TransferHandler] = None

        self.webdav_core: Optional[WebdavCore] = None

        self.share_interactive_gen_strm_queue = ShareInteractiveGenStrmQueue()
        self.strm_sync_guard = StrmSyncRunGuard()

    @staticmethod
    def _full_sync_config_available() -> bool:
        """
        检查全量同步核心配置是否可用
        """
        return bool(
            configer.get_config("full_sync_strm_paths")
            and configer.get_config("moviepilot_address")
            and configer.get_config("user_download_mediaext")
        )

    @classmethod
    def _timed_full_sync_config_available(cls) -> bool:
        """
        检查定期全量同步配置是否可用
        """
        return bool(
            configer.get_config("timing_full_sync_strm")
            and configer.get_config("cron_full_sync_strm")
            and cls._full_sync_config_available()
        )

    @classmethod
    def _is_full_sync_priority_window(cls, now: Optional[datetime] = None) -> bool:
        """
        判断当前是否处于全量优先窗口
        """
        if not cls._timed_full_sync_config_available():
            return False

        try:
            sync_timezone = timezone(settings.TZ)
            current_time = now or datetime.now(tz=sync_timezone)
            if current_time.tzinfo is None:
                current_time = sync_timezone.localize(current_time)
            minute_start = current_time.replace(second=0, microsecond=0)
            minute_end = minute_start + timedelta(minutes=1)
            trigger = CronTrigger.from_crontab(
                configer.get_config("cron_full_sync_strm"),
                timezone=sync_timezone,
            )
            current_minute_fire_time = trigger.get_next_fire_time(
                None, minute_start - timedelta(seconds=1)
            )
            if (
                current_minute_fire_time
                and minute_start <= current_minute_fire_time < minute_end
            ):
                return True

            upcoming_fire_time = trigger.get_next_fire_time(None, current_time)
            return bool(
                upcoming_fire_time
                and current_time < upcoming_fire_time
                and upcoming_fire_time
                <= current_time + timedelta(seconds=cls.FULL_SYNC_PRIORITY_LEAD_SECONDS)
            )
        except Exception as e:
            logger.warning(f"【STRM同步互斥】判断全量优先窗口失败: {e}")
            return False

    def _should_skip_increment_for_full_priority(self) -> bool:
        """
        判断增量任务是否应该为全量优先策略让出执行机会
        """
        if self.strm_sync_guard.pending_full_sync:
            if not self._full_sync_config_available():
                self.strm_sync_guard.clear_full_sync_pending()
                logger.warning("【STRM同步互斥】待执行全量配置已失效，清除待执行标记")
                return False
            logger.warning(
                "【STRM同步互斥】增量STRM生成触发时存在待执行全量，跳过本次增量"
            )
            return True

        if self._is_full_sync_priority_window():
            logger.warning(
                "【STRM同步互斥】增量STRM生成触发时处于全量优先窗口，跳过本次增量"
            )
            return True

        return False

    def _enter_strm_sync_task(self, task_name: str, task_kind: str) -> bool:
        """
        尝试进入 STRM 同步任务
        """
        if self.strm_sync_guard.acquire(task_name, task_kind=task_kind):
            if task_kind == STRM_SYNC_TASK_FULL:
                self.strm_sync_guard.clear_full_sync_pending()
            logger.info(f"【STRM同步互斥】{task_name} 已取得同步执行权")
            return True

        running_task = self.strm_sync_guard.current_task or "其它STRM同步任务"
        running_task_kind = self.strm_sync_guard.current_task_kind
        if (
            task_kind == STRM_SYNC_TASK_FULL
            and running_task_kind == STRM_SYNC_TASK_INCREMENT
        ):
            first_pending = self.strm_sync_guard.mark_full_sync_pending()
            if first_pending:
                logger.warning(
                    f"【STRM同步互斥】{task_name} 触发时 {running_task} 正在运行，"
                    "已登记待执行全量"
                )
            else:
                logger.warning(
                    f"【STRM同步互斥】{task_name} 触发时 {running_task} 正在运行，"
                    "待执行全量已存在，本次合并"
                )
            return False

        logger.warning(
            f"【STRM同步互斥】{task_name} 触发时 {running_task} 正在运行，跳过本次执行"
        )
        return False

    def _start_pending_full_sync_if_needed(self) -> None:
        """
        如存在待执行全量请求则启动一次补跑线程
        """
        if not self.strm_sync_guard.pending_full_sync:
            return
        if not self._full_sync_config_available():
            self.strm_sync_guard.clear_full_sync_pending()
            logger.warning("【STRM同步互斥】待执行全量补跑前配置已失效，清除待执行标记")
            return
        if not self.strm_sync_guard.reserve_full_sync_runner():
            return

        pending_thread = Thread(
            target=self._run_pending_full_sync,
            name="P115StrmHelper-PendingFullSync",
            daemon=True,
        )
        pending_thread.start()
        logger.info("【STRM同步互斥】待执行全量补跑线程已启动")

    def _run_pending_full_sync(self) -> None:
        """
        执行待补跑全量同步
        """
        logger.info("【STRM同步互斥】待执行全量补跑开始")
        try:
            if not self._full_sync_config_available():
                self.strm_sync_guard.clear_full_sync_pending()
                logger.warning("【STRM同步互斥】待执行全量补跑时配置已失效，清除待执行标记")
                return
            self.full_sync_strm_files(_from_pending=True)
        finally:
            self.strm_sync_guard.finish_full_sync_runner()
            logger.info("【STRM同步互斥】待执行全量补跑线程已结束")

    def _leave_strm_sync_task(self, task_name: str) -> None:
        """
        退出 STRM 同步任务
        """
        released_task_kind = self.strm_sync_guard.release()
        logger.info(f"【STRM同步互斥】{task_name} 已释放同步执行权")
        if released_task_kind == STRM_SYNC_TASK_INCREMENT:
            self._start_pending_full_sync_if_needed()

    def _create_mediainfo_downloader_for_task(
        self, task_name: str
    ) -> MediaInfoDownloader:
        """
        为当前任务创建独立媒体信息下载器
        """
        logger.info(f"【媒体信息文件下载】为 {task_name} 创建独立下载器")
        return MediaInfoDownloader(cookie=configer.get_config("cookies"))

    @staticmethod
    def _close_mediainfo_downloader_for_task(
        downloader: Optional[MediaInfoDownloader], task_name: str
    ) -> None:
        """
        释放当前任务的媒体信息下载器
        """
        if not downloader:
            return
        downloader.close()
        logger.info(f"【媒体信息文件下载】{task_name} 独立下载器已释放")

    def init_service(self):
        """
        初始化服务
        """
        try:
            # 115 网盘客户端初始化
            self.client = P115Client(configer.cookies)

            # 阿里云盘登入
            aligo_config = configer.get_config("PLUGIN_ALIGO_PATH")
            if configer.get_config("aliyundrive_token"):
                set_config_folder(aligo_config)
                if Path(aligo_config / "aligo.json").exists():
                    logger.debug("Config login aliyunpan")
                    self.aligo = BAligo(level=ERROR, re_login=False)
                else:
                    logger.debug("Refresh token login aliyunpan")
                    self.aligo = BAligo(
                        refresh_token=configer.get_config("aliyundrive_token"),
                        level=ERROR,
                        re_login=False,
                    )
                # 默认操作资源盘
                v2_user = self.aligo.v2_user_get()
                logger.debug(f"AliyunPan user info: {v2_user}")
                resource_drive_id = v2_user.resource_drive_id
                self.aligo.default_drive_id = resource_drive_id
            elif (
                not configer.get_config("aliyundrive_token")
                and not Path(aligo_config / "aligo.json").exists()
            ):
                logger.debug("Login out aliyunpan")
                self.aligo = None

            # 媒体信息下载工具初始化
            self.mediainfodownloader = MediaInfoDownloader(
                cookie=configer.get_config("cookies")
            )
            self.share_interactive_gen_strm_queue.bind_mediainfodownloader(
                self.mediainfodownloader
            )
            self.share_interactive_gen_strm_queue.bind_mediainfo_downloader_factory(
                self._create_mediainfo_downloader_for_task
            )

            # 生活事件监控初始化
            self.monitorlife = MonitorLife(
                client=self.client,
                mediainfodownloader=self.mediainfodownloader,
                stop_event=None,
            )

            # 分享转存初始化
            self.sharetransferhelper = ShareTransferHelper(self.client, self.aligo)

            # 离线下载初始化
            self.offlinehelper = OfflineDownloadHelper(
                client=self.client, monitorlife=self.monitorlife
            )

            # 多端播放初始化
            pid = None
            if configer.get_config("same_playback"):
                pid = get_pid_by_path(self.client, "/多端播放", True, False, False)

            # 302跳转初始化
            self.redirect = Redirect(client=self.client, pid=pid)

            # FUSE 初始化
            self.fuse_manager = FuseManager(client=self.client)
            if configer.fuse_enabled and configer.fuse_mountpoint:
                self.fuse_manager._start_fuse_internal()

            # 初始化整理任务管理器和 TransferChain 补丁
            self._init_transfer_enhancement()

            # 初始化 Webdav 服务
            self.webdav_core = WebdavCore(client=self.client)

            # 启动 Emby 媒体信息提取全局队列 worker
            emby_mediainfo_queue.start()

            return True
        except Exception as e:
            logger.error(f"服务项初始化失败: {e}")
            return False

    def _init_transfer_enhancement(self):
        """
        初始化或更新接管网盘整理功能
        """
        try:
            TransferChainPatcher.disable()
        except Exception:
            pass

        if self.transfer_task_manager:
            try:
                self.transfer_task_manager.shutdown()
            except Exception:
                pass
            self.transfer_task_manager = None
        self.transfer_handler = None

        if configer.pan_transfer_takeover:
            if configer.storage_module != "115网盘Plus":
                logger.warn(
                    "【整理接管】接管网盘整理功能需要存储模块为 '115网盘Plus'，当前存储模块为 "
                    f"'{configer.storage_module}'，接管功能已禁用"
                )
            else:
                try:
                    self.transfer_handler = TransferHandler(
                        client=self.client,
                        storage_name="115网盘Plus",
                    )
                    self.transfer_task_manager = TransferTaskManager(
                        batch_delay=10.0,
                        batch_max_size=500,
                        batch_callback=self.transfer_handler.process_batch,
                    )
                    TransferChainPatcher.enable(
                        task_manager=self.transfer_task_manager,
                        handler=self.transfer_handler,
                        storage_module="115网盘Plus",
                    )
                    logger.info("【整理接管】已启用")
                except Exception as e:
                    logger.error(f"【整理接管】初始化失败: {e}", exc_info=True)
                    self.transfer_task_manager = None
                    self.transfer_handler = None

    def check_monitor_life_guard(self):
        """
        检查并守护生活事件监控线程
        """
        should_run = (
            configer.monitor_life_enabled
            and configer.monitor_life_paths
            and configer.monitor_life_event_modes
        ) or (configer.pan_transfer_enabled and configer.pan_transfer_paths)

        with self.monitor_life_lock:
            if should_run:
                is_alive = (
                    self.monitor_life_thread and self.monitor_life_thread.is_alive()
                )

                if is_alive:
                    if self.monitor_life_fail_time is not None:
                        logger.debug("【监控生活事件】线程运行正常，清除失败时间记录")
                        self.monitor_life_fail_time = None
                else:
                    current_time = time()
                    if self.monitor_life_fail_time is None:
                        self.monitor_life_fail_time = current_time
                        logger.debug(
                            "【监控生活事件】检测到线程已停止，开始记录失败时间"
                        )
                    else:
                        fail_duration = current_time - self.monitor_life_fail_time
                        fail_duration_minutes = int(fail_duration / 60)
                        fail_duration_seconds = int(fail_duration % 60)
                        logger.debug(
                            f"【监控生活事件】线程已停止，持续失败时间: {fail_duration_minutes}分{fail_duration_seconds}秒"
                        )

                        if fail_duration >= 300:
                            logger.warning(
                                "【监控生活事件】连续5分钟检测到线程已停止，正在重新启动..."
                            )
                            if configer.notify:
                                post_message(
                                    mtype=NotificationType.Plugin,
                                    title=i18n.translate(
                                        "monitor_life_auto_restart_title"
                                    ),
                                    text=f"\n{i18n.translate('monitor_life_auto_restart_text')}\n",
                                )
                            self._start_monitor_life_internal()
                            self.monitor_life_fail_time = None
            else:
                if self.monitor_life_thread and self.monitor_life_thread.is_alive():
                    logger.info("【监控生活事件】配置已关闭，守护线程正在停止线程")
                    self._stop_monitor_life_internal()
                self.monitor_life_fail_time = None

    def start_monitor_life(self):
        """
        启动生活事件监控
        """
        with self.monitor_life_lock:
            self._start_monitor_life_internal(register_guard_service=False)

    def _stop_monitor_life_internal(self):
        """
        停止生活事件监控线程
        """
        if self.monitor_life_thread and self.monitor_life_thread.is_alive():
            logger.info("【监控生活事件】停止生活事件监控线程")
            if self.monitor_stop_event:
                self.monitor_stop_event.set()

            self.monitor_life_thread.join(timeout=25)
            if self.monitor_life_thread.is_alive():
                logger.warning("【监控生活事件】线程未在预期时间内结束")
            else:
                logger.info("【监控生活事件】线程已正常退出")

            self.monitor_life_thread = None
            if self.monitor_stop_event:
                self.monitor_stop_event = None

    def _start_monitor_life_internal(self, register_guard_service: bool = True):
        """
        启动生活事件监控线程

        :param register_guard_service: 是否在启动后调用 _update_monitor_life_guard_service
            初始化时（start_monitor_life）设为 False，让 get_service() 统一注册
            运行时恢复时（check_monitor_life_guard）保留 True，确保守护服务存在
        """
        if (
            configer.get_config("monitor_life_enabled")
            and configer.get_config("monitor_life_paths")
            and configer.get_config("monitor_life_event_modes")
        ) or (
            configer.get_config("pan_transfer_enabled")
            and configer.get_config("pan_transfer_paths")
        ):
            if self.monitor_life_thread and self.monitor_life_thread.is_alive():
                logger.info("【监控生活事件】检测到已有线程在运行，停止旧线程中...")
                self._stop_monitor_life_internal()

            if self.monitor_life_thread and self.monitor_life_thread.is_alive():
                logger.debug("【监控生活事件】线程仍在运行，跳过启动")
                return

            self.monitor_stop_event = ThreadEvent()

            if not self.monitorlife:
                logger.error("【监控生活事件】monitorlife 未初始化，无法启动监控线程")
                return

            self.monitor_life_thread = Thread(
                target=monitor_life_thread_worker,
                args=(
                    self.monitorlife,
                    self.monitor_stop_event,
                ),
                name="P115StrmHelper-MonitorLife",
                daemon=False,
            )
            self.monitor_life_thread.start()
            logger.info("【监控生活事件】生活事件监控线程已启动")
            self.monitor_life_fail_time = None

            if register_guard_service:
                try:
                    self._update_monitor_life_guard_service()
                except Exception as e:
                    logger.debug(f"【监控生活事件】重新注册守护服务失败: {e}")
        else:
            self._stop_monitor_life_internal()

    def _update_monitor_life_guard_service(self):
        """
        只重新注册115生活事件线程守护服务
        """
        pid = "P115StrmHelper"
        service_id = "P115StrmHelper_monitor_life_guard"
        job_id = f"{pid}_{service_id}"

        should_register = (
            configer.monitor_life_enabled
            and configer.monitor_life_paths
            and configer.monitor_life_event_modes
        ) or (configer.pan_transfer_enabled and configer.pan_transfer_paths)

        if not should_register:
            logger.debug("【监控生活事件】守护服务未启用，跳过注册")
            return

        guard_service = {
            "id": service_id,
            "name": "115生活事件线程守护",
            "trigger": CronTrigger.from_crontab("* * * * *"),
            "func": self.check_monitor_life_guard,
            "kwargs": {},
        }

        scheduler = Scheduler()
        scheduler.remove_plugin_job(pid, job_id)

        with scheduler._lock:
            try:
                sid = f"{pid}_{service_id}"
                scheduler._jobs[job_id] = {
                    "func": guard_service["func"],
                    "name": guard_service["name"],
                    "pid": pid,
                    "provider_name": "115网盘STRM助手",
                    "kwargs": guard_service.get("func_kwargs") or {},
                    "running": False,
                }
                scheduler._scheduler.add_job(
                    scheduler.start,
                    guard_service["trigger"],
                    id=sid,
                    name=guard_service["name"],
                    **(guard_service.get("kwargs") or {}),
                    kwargs={"job_id": job_id},
                    replace_existing=True,
                )
                logger.debug("【监控生活事件】已重新注册115生活事件线程守护服务")
            except Exception as e:
                logger.error(f"【监控生活事件】注册守护服务失败: {str(e)}")

    def full_sync_strm_files(self, _from_pending: bool = False):
        """
        全量同步
        """
        task_name = "全量STRM生成"
        if (
            not configer.get_config("full_sync_strm_paths")
            or not configer.get_config("moviepilot_address")
            or not configer.get_config("user_download_mediaext")
        ):
            if _from_pending:
                self.strm_sync_guard.clear_full_sync_pending()
                logger.warning("【STRM同步互斥】待执行全量配置已失效，跳过补跑")
            return

        if not self._enter_strm_sync_task(task_name, STRM_SYNC_TASK_FULL):
            return

        mediainfo_downloader: Optional[MediaInfoDownloader] = None
        try:
            mediainfo_downloader = self._create_mediainfo_downloader_for_task(
                task_name
            )
            strm_helper = FullSyncStrmHelper(
                client=self.client,
                mediainfodownloader=mediainfo_downloader,
            )
            strm_helper.strm_exec_history_kind = "full"
            strm_helper.generate_strm_files(
                full_sync_strm_paths=configer.get_config("full_sync_strm_paths"),
            )
            (
                strm_count,
                mediainfo_count,
                strm_fail_count,
                mediainfo_fail_count,
                remove_unless_strm_count,
                strm_cleanup_deferred_count,
            ) = strm_helper.get_generate_total()
            if configer.get_config("notify"):
                text = f"""
📄 生成STRM文件 {strm_count} 个
⬇️ 下载媒体文件 {mediainfo_count} 个
❌ 生成STRM失败 {strm_fail_count} 个
🚫 下载媒体失败 {mediainfo_fail_count} 个
"""
                if remove_unless_strm_count != 0:
                    text += f"🗑️ 清理无效STRM文件 {remove_unless_strm_count} 个"
                if strm_cleanup_deferred_count != 0:
                    text += f"\n⏳ 待二次确认清理无效 STRM {strm_cleanup_deferred_count} 个"
                post_message(
                    mtype=NotificationType.Plugin,
                    title=i18n.translate("full_sync_done_title"),
                    text=text,
                )
        finally:
            self._close_mediainfo_downloader_for_task(
                mediainfo_downloader, task_name
            )
            self._leave_strm_sync_task(task_name)

    def start_full_sync(self):
        """
        启动全量同步
        """
        self.scheduler = BackgroundScheduler(timezone=settings.TZ)
        self.scheduler.add_job(
            func=self.full_sync_strm_files,
            trigger="date",
            run_date=datetime.now(tz=timezone(settings.TZ)) + timedelta(seconds=3),
            name="115网盘助手全量生成STRM",
        )
        if self.scheduler.get_jobs():
            self.scheduler.print_jobs()
            self.scheduler.start()

    def full_sync_database(self):
        """
        全量同步数据库
        """
        if (
            not configer.get_config("full_sync_strm_paths")
            or not configer.get_config("moviepilot_address")
            or not configer.get_config("user_download_mediaext")
        ):
            return

        strm_helper = FullSyncStrmHelper(
            client=self.client,
            mediainfodownloader=self.mediainfodownloader,
        )
        strm_helper.generate_database(
            full_sync_strm_paths=configer.get_config("full_sync_strm_paths"),
        )

    def start_full_sync_db(self):
        """
        启动全量同步数据库
        """
        self.scheduler = BackgroundScheduler(timezone=settings.TZ)
        self.scheduler.add_job(
            func=self.full_sync_database,
            trigger="date",
            run_date=datetime.now(tz=timezone(settings.TZ)) + timedelta(seconds=3),
            name="115网盘助手全量同步数据库",
        )
        if self.scheduler.get_jobs():
            self.scheduler.print_jobs()
            self.scheduler.start()

    def share_strm_cleanup_run(self):
        """
        定时任务：分享 STRM 失效清理扫描
        """
        try:
            share_strm_cleaner.run_full_cleanup()
        except Exception as e:
            logger.error(f"【分享STRM清理】定时任务失败: {e}", exc_info=True)

    def share_strm_files(self):
        """
        分享生成STRM
        """
        if not configer.share_strm_config or not configer.moviepilot_address:
            return

        task_name = "分享STRM生成"
        mediainfo_downloader: Optional[MediaInfoDownloader] = None
        try:
            mediainfo_downloader = self._create_mediainfo_downloader_for_task(
                task_name
            )
            strm_helper = ShareStrmHelper(mediainfodownloader=mediainfo_downloader)
            strm_helper.strm_exec_history_kind = "share"
            strm_helper.generate_strm_files()
            strm_count, mediainfo_count, strm_fail_count, mediainfo_fail_count = (
                strm_helper.get_generate_total()
            )
            if configer.get_config("notify"):
                post_message(
                    mtype=NotificationType.Plugin,
                    title=i18n.translate("share_sync_done_title"),
                    text=f"\n📄 生成STRM文件 {strm_count} 个\n"
                    + f"⬇️ 下载媒体文件 {mediainfo_count} 个\n"
                    + f"❌ 生成STRM失败 {strm_fail_count} 个\n"
                    + f"🚫 下载媒体失败 {mediainfo_fail_count} 个",
                )
        except Exception as e:
            logger.error(f"【分享STRM生成】运行失败: {e}")
            return
        finally:
            self._close_mediainfo_downloader_for_task(
                mediainfo_downloader, task_name
            )

    def start_share_sync(self):
        """
        启动分享同步
        """
        self.scheduler = BackgroundScheduler(timezone=settings.TZ)
        self.scheduler.add_job(
            func=self.share_strm_files,
            trigger="date",
            run_date=datetime.now(tz=timezone(settings.TZ)) + timedelta(seconds=3),
            name="115网盘助手分享生成STRM",
        )
        if self.scheduler.get_jobs():
            self.scheduler.print_jobs()
            self.scheduler.start()

    def increment_sync_strm_files(self, send_msg: bool = False):
        """
        增量同步
        """
        task_name = "增量STRM生成"
        if (
            not configer.get_config("increment_sync_strm_paths")
            or not configer.get_config("moviepilot_address")
            or not configer.get_config("user_download_mediaext")
        ):
            return

        if self._should_skip_increment_for_full_priority():
            return

        if not self._enter_strm_sync_task(task_name, STRM_SYNC_TASK_INCREMENT):
            return

        mediainfo_downloader: Optional[MediaInfoDownloader] = None
        try:
            mediainfo_downloader = self._create_mediainfo_downloader_for_task(
                task_name
            )
            strm_helper = IncrementSyncStrmHelper(
                client=self.client, mediainfodownloader=mediainfo_downloader
            )
            strm_helper.strm_exec_history_kind = "increment"
            strm_helper.generate_strm_files(
                sync_strm_paths=configer.get_config("increment_sync_strm_paths"),
            )
            (
                strm_count,
                mediainfo_count,
                strm_fail_count,
                mediainfo_fail_count,
                remove_unless_strm_count,
            ) = strm_helper.get_generate_total()
            if configer.get_config("notify") and (
                send_msg
                or (
                    strm_count != 0
                    or mediainfo_count != 0
                    or strm_fail_count != 0
                    or mediainfo_fail_count != 0
                    or remove_unless_strm_count != 0
                )
            ):
                text = f"""
📄 生成STRM文件 {strm_count} 个
⬇️ 下载媒体文件 {mediainfo_count} 个
❌ 生成STRM失败 {strm_fail_count} 个
🚫 下载媒体失败 {mediainfo_fail_count} 个
"""
                if remove_unless_strm_count != 0:
                    text += f"🗑️ 清理无效STRM文件 {remove_unless_strm_count} 个"
                post_message(
                    mtype=NotificationType.Plugin,
                    title=i18n.translate("inc_sync_done_title"),
                    text=text,
                )
        finally:
            self._close_mediainfo_downloader_for_task(
                mediainfo_downloader, task_name
            )
            self._leave_strm_sync_task(task_name)

    def hdhive_checkin_scheduler_tick(self) -> None:
        """
        HDHive 签到调度
        """
        hdhive_checkin_scheduler_tick()

    def start_directory_upload(self):
        """
        启动目录上传监控
        """
        if configer.directory_upload_enabled:
            directory_upload_queue.start()
            for item in configer.directory_upload_path:
                if not item:
                    continue
                mon_path = item.get("src", "")
                if not mon_path:
                    continue
                try:
                    stop_event = ThreadEvent()
                    force_polling = configer.directory_upload_mode == "compatibility"

                    def watch_worker(path: str, stop_evt: ThreadEvent, polling: bool):
                        try:
                            for changes in watch(
                                path,
                                recursive=True,
                                force_polling=polling,
                                stop_event=stop_evt,
                                debounce=1600,
                                step=50,
                            ):
                                for change in changes:
                                    change_type, path_str = change
                                    if change_type == Change.added:
                                        directory_upload_queue.enqueue(
                                            DirectoryUploadTask(
                                                servicer.client,
                                                path_str,
                                                path,
                                            )
                                        )
                        except Exception as e:
                            logger.error(
                                f"【目录上传】{path} 监控线程异常: {e}",
                                exc_info=True,
                            )

                    watch_thread = Thread(
                        target=watch_worker,
                        args=(mon_path, stop_event, force_polling),
                        name=f"P115StrmHelper-DirectoryUpload-{mon_path}",
                        daemon=True,
                    )
                    watch_thread.start()

                    self.service_observer.append(
                        ObserverInfo(
                            thread=watch_thread,
                            stop_event=stop_event,
                            mon_path=mon_path,
                        )
                    )
                    logger.info(f"【目录上传】{mon_path} 实时监控服务启动")
                except Exception as e:
                    logger.error(f"【目录上传】{mon_path} 启动实时监控失败：{e}")

    def main_cleaner(self):
        """
        主清理模块
        """
        client = Cleaner(client=self.client)

        if configer.get_config("clear_receive_path_enabled"):
            client.clear_receive_path()

        if configer.get_config("clear_recyclebin_enabled"):
            client.clear_recyclebin()

    def offline_status(self):
        """
        监控 115 网盘离线下载进度
        """
        if self.offlinehelper:
            self.offlinehelper.pull_status_to_task()

    def start_fuse(self, mountpoint: Optional[str] = None, readdir_ttl: float = 60):
        """
        启动 FUSE 文件系统

        :param mountpoint: 挂载点路径，如果为 None 则使用配置中的路径
        :param readdir_ttl: 目录读取缓存 TTL（秒）
        :return: 是否启动成功
        """
        if not self.fuse_manager:
            logger.error("【FUSE】FuseManager 未初始化")
            return False
        return self.fuse_manager.start_fuse(mountpoint, readdir_ttl)

    def run_backup_task(self, task_name: str):
        """
        执行备份任务

        :param task_name: 备份任务名称
        """
        if not configer.strm_backup_enabled:
            return

        backup_items = configer.strm_backup_items
        task = None
        for item in backup_items:
            if item.name == task_name:
                task = item
                break

        if not task:
            logger.error(f"【STRM备份】备份任务不存在: {task_name}")
            return

        from ..helper.backup import backup_helper

        logger.info(f"【STRM备份】开始执行备份任务: {task_name}")
        history = backup_helper.execute_backup(task, client=self.client)

        if history.status == "success":
            logger.info(
                f"【STRM备份】备份成功: {task_name}, "
                f"文件: {history.filename}, 大小: {history.file_size} 字节"
            )
        elif history.status == "skipped":
            logger.info(
                f"【STRM备份】备份任务已跳过: {task_name}, 原因: {history.error_msg}"
            )
        else:
            logger.error(
                f"【STRM备份】备份失败: {task_name}, 错误: {history.error_msg}"
            )

    def start_backup_task(self, task):
        """
        启动备份任务

        :param task: StrmBackupItem 备份任务配置
        """
        scheduler = BackgroundScheduler(timezone=settings.TZ)
        scheduler.add_job(
            func=self.run_backup_task,
            args=[task.name],
            trigger="date",
            run_date=datetime.now(tz=timezone(settings.TZ)) + timedelta(seconds=3),
            name=f"STRM备份-{task.name}",
        )
        if scheduler.get_jobs():
            scheduler.print_jobs()
            scheduler.start()

    def run_restore_task(self, task_name: str, backup_path: str):
        """
        执行恢复任务

        :param task_name: 备份任务名称
        :param backup_path: 备份文件路径
        """
        if not configer.strm_backup_enabled:
            return

        backup_items = configer.strm_backup_items
        task = None
        for item in backup_items:
            if item.name == task_name:
                task = item
                break

        if not task:
            logger.error(f"【STRM恢复】备份任务不存在: {task_name}")
            return

        from ..helper.backup import backup_helper

        logger.info(f"【STRM恢复】开始执行恢复任务: {task_name}, 路径: {backup_path}")

        if task.target_type.value == "local":
            success, error_msg = backup_helper.restore_from_local(
                backup_path=backup_path,
                source_paths=task.source_paths,
            )
        elif task.target_type.value == "cloud":
            success, error_msg = backup_helper.restore_from_cloud(
                cloud_path=backup_path,
                source_paths=task.source_paths,
                client=self.client,
            )
        else:
            success, error_msg = False, f"不支持的备份目标类型: {task.target_type}"

        if success:
            logger.info(f"【STRM恢复】恢复成功: {task_name}")
        else:
            logger.error(f"【STRM恢复】恢复失败: {task_name}, 错误: {error_msg}")

    def start_restore_task(self, task_name: str, backup_path: str):
        """
        启动恢复任务

        :param task_name: 备份任务名称
        :param backup_path: 备份文件路径
        """
        scheduler = BackgroundScheduler(timezone=settings.TZ)
        scheduler.add_job(
            func=self.run_restore_task,
            args=[task_name, backup_path],
            trigger="date",
            run_date=datetime.now(tz=timezone(settings.TZ)) + timedelta(seconds=3),
            name=f"STRM恢复-{task_name}",
        )
        if scheduler.get_jobs():
            scheduler.print_jobs()
            scheduler.start()

    def stop_fuse(self):
        """
        停止 FUSE 文件系统
        """
        if self.fuse_manager:
            self.fuse_manager.stop_fuse()

    def stop(self):
        """
        停止所有服务
        """
        try:
            if self.service_observer:
                for ob in self.service_observer:
                    try:
                        ob.stop_event.set()
                        if ob.thread.is_alive():
                            ob.thread.join(timeout=5)
                            logger.debug(f"【目录上传】{ob.mon_path} 监控线程已关闭")
                    except Exception as e:
                        logger.error(f"【目录上传】关闭失败: {e}")
                logger.info("【目录上传】目录监控已关闭")
            self.service_observer = []
            try:
                directory_upload_queue.stop()
            except Exception as e:
                logger.debug(f"【目录上传】停止 worker 异常: {e}")
            if self.scheduler:
                self.scheduler.remove_all_jobs()
                if self.scheduler.running:
                    self.scheduler.shutdown()
                self.scheduler = None
            with self.monitor_life_lock:
                if self.monitor_life_thread:
                    self._stop_monitor_life_internal()
                elif self.monitor_stop_event:
                    self.monitor_stop_event.set()
                    self.monitor_stop_event = None
            if self.fuse_manager:
                self.fuse_manager.stop_fuse()
            if self.redirect:
                self.redirect.close_http_client_sync()
            try:
                emby_mediainfo_queue.stop()
            except Exception as e:
                logger.debug(f"【Emby 媒体信息队列】停止 worker 异常: {e}")
            try:
                sync_del_webhook_queue.stop()
            except Exception as e:
                logger.debug(f"【同步删除 Webhook 队列】停止 worker 异常: {e}")
            try:
                TransferChainPatcher.disable()
            except Exception as e:
                logger.error(f"【整理接管】禁用补丁失败: {e}")
            if self.transfer_task_manager:
                try:
                    self.transfer_task_manager.shutdown()
                except Exception as e:
                    logger.error(f"【整理接管】关闭任务管理器失败: {e}")
                self.transfer_task_manager = None
            self.transfer_handler = None
        except Exception as e:
            logger.error(f"发生错误: {e}")


servicer = ServiceHelper()
