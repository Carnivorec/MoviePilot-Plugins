from pathlib import PurePosixPath
from typing import Dict, Iterable, Set


class PathPairIndex:
    """保存单次目录导出的明确路径关联，拒绝歧义和不兼容的文件类型"""

    def __init__(self) -> None:
        self._sources: Dict[str, Set[str]] = {}

    def clear(self) -> None:
        """丢弃上一条同步路径或失败尝试的关联"""
        self._sources.clear()

    def add(self, local_path: str, pan_path: str) -> None:
        """保存同一次路径转换产生的目标和源，相同条目重复导出时去重"""
        self._sources.setdefault(local_path, set()).add(pan_path)

    def resolve(
        self,
        local_path: str,
        media_extensions: Iterable[str],
        download_extensions: Iterable[str],
        auto_download_mediainfo: bool,
    ) -> str:
        """
        返回唯一且类型兼容的源路径

        :raises ValueError: 关联缺失、冲突或源与目标类型不兼容
        """
        sources = self._sources.get(local_path, set())
        if not sources:
            raise ValueError("找不到本地目标对应的网盘源路径")
        if len(sources) != 1:
            raise ValueError(f"多个网盘源映射到同一本地目标: {sorted(sources)}")
        pan_path = next(iter(sources))
        local_ext = PurePosixPath(local_path).suffix.lower()
        pan_ext = PurePosixPath(pan_path).suffix.lower()
        media = {f".{ext.strip().lower().lstrip('.')}" for ext in media_extensions}
        downloads = {f".{ext.strip().lower().lstrip('.')}" for ext in download_extensions}
        if local_ext == ".strm" and pan_ext in media:
            return pan_path
        if auto_download_mediainfo and local_ext in downloads and pan_ext == local_ext:
            return pan_path
        raise ValueError(f"本地目标与网盘源文件类型不兼容: {pan_path}")
