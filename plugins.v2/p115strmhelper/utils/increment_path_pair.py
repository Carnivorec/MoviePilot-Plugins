from dataclasses import dataclass
from os.path import splitext
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class PathPairDecision:
    local_path: str
    pan_path: Optional[str]
    should_process: bool
    reason: str
    duplicate_count: int = 0


def normalize_extensions(extensions: Iterable[str]) -> set[str]:
    """
    步骤1：规范化扩展名
    ====================
    目标：把配置中的扩展名转换为统一可比较格式。
    数据源：调用方传入的 extensions。
    操作要点：
    1) 去掉首尾空白。
    2) 转为小写并补齐前导点。
    3) 跳过空值。
    """
    normalized: set[str] = set()
    for extension in extensions:
        # 1.1 [清理单个扩展名文本]
        clean_extension = extension.strip().lower()
        if not clean_extension:
            continue

        # 1.2 [补齐扩展名前导点]
        if not clean_extension.startswith("."):
            clean_extension = f".{clean_extension}"
        normalized.add(clean_extension)

    return normalized


def build_path_pair_index(path_pairs: Iterable[Tuple[str, str]]) -> Dict[str, List[str]]:
    """
    步骤2：构建本地路径到网盘路径索引
    ================================
    目标：用明确路径键替代树行号配对。
    数据源：调用方传入的 local_path 与 pan_path 二元组。
    操作要点：
    1) 按传入顺序保存所有候选源路径。
    2) 重复本地目标不覆盖旧值。
    """
    path_pair_index: Dict[str, List[str]] = {}
    for local_path, pan_path in path_pairs:
        # 2.1 [按本地路径追加网盘候选路径]
        path_pair_index.setdefault(local_path, []).append(pan_path)

    return path_pair_index


def validate_path_pair(
    local_path: str,
    pan_path: str,
    media_extensions: Iterable[str],
    download_extensions: Iterable[str],
    auto_download_mediainfo: bool,
) -> str:
    """
    步骤3：校验本地目标和网盘源路径类型
    ==================================
    目标：阻止 STRM 与媒体信息文件内容互写。
    数据源：本地目标路径、网盘源路径和扩展名配置。
    操作要点：
    1) STRM 目标只接受媒体源文件。
    2) 开启媒体信息下载时，同后缀媒体信息目标只接受同后缀源文件。
    3) 其它本地目标类型直接拒绝。
    """
    media_extension_set = normalize_extensions(media_extensions)
    download_extension_set = normalize_extensions(download_extensions)

    # 3.1 [提取本地目标和网盘源路径后缀]
    local_extension = splitext(local_path)[1].lower()
    pan_extension = splitext(pan_path)[1].lower()

    # 3.2 [校验 STRM 目标必须来自媒体源]
    if local_extension == ".strm":
        if pan_extension in media_extension_set:
            return ""
        return "本地 STRM 目标必须对应媒体源文件"

    # 3.3 [校验媒体信息目标必须来自同后缀源]
    if auto_download_mediainfo and local_extension in download_extension_set:
        if pan_extension == local_extension:
            return ""
        return "媒体信息目标必须对应相同后缀源文件"

    return "不支持的本地目标文件类型"


def select_pan_path_for_local_path(
    local_path: str,
    pan_paths: Sequence[str],
    media_extensions: Iterable[str],
    download_extensions: Iterable[str],
    auto_download_mediainfo: bool,
) -> PathPairDecision:
    """
    步骤4：按本地路径选择网盘源路径
    ==============================
    目标：在树行号错位时仍能选到正确网盘源路径。
    数据源：本地目标路径和候选网盘源路径列表。
    操作要点：
    1) 使用 pan_paths 接收候选列表。
    2) 重复映射按传入顺序选择第一个。
    3) 返回带原因的处理决策。
    """
    duplicate_count = len(pan_paths)

    # 4.1 [处理找不到映射的本地目标]
    if not pan_paths:
        return PathPairDecision(
            local_path=local_path,
            pan_path=None,
            should_process=False,
            reason="无法根据本地目标路径找到网盘源路径",
            duplicate_count=0,
        )

    # 4.2 [按传入顺序选择第一个网盘源路径]
    pan_path = pan_paths[0]
    reason = validate_path_pair(
        local_path=local_path,
        pan_path=pan_path,
        media_extensions=media_extensions,
        download_extensions=download_extensions,
        auto_download_mediainfo=auto_download_mediainfo,
    )

    # 4.3 [返回校验后的选择结果]
    if reason:
        return PathPairDecision(
            local_path=local_path,
            pan_path=pan_path,
            should_process=False,
            reason=reason,
            duplicate_count=duplicate_count,
        )

    return PathPairDecision(
        local_path=local_path,
        pan_path=pan_path,
        should_process=True,
        reason="OK",
        duplicate_count=duplicate_count,
    )
