"""多源目录备份格式、原子写入和受目标目录约束的恢复"""

import json
import os
import posixpath
import re
import tarfile
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path, PurePosixPath
from shutil import copyfileobj
from tempfile import NamedTemporaryFile
from typing import Dict, Iterator, List, Optional


MANIFEST_NAME = ".p115strmhelper-backup.json"


def backup_sources(source_paths: List[str], output_path: Optional[Path] = None) -> List[Path]:
    """
    校验备份源及输出位置，防止缺失源伪装成成功或把备份包递归打进自身

    :param source_paths (List): 要备份的目录
    :param output_path (Path): 可选备份文件位置

    :return List: 规范化后的源目录
    """
    roots = _target_roots(source_paths)
    for root in roots:
        if not root.is_dir():
            raise FileNotFoundError(f"备份源目录不存在或不是目录: {root}")
        if output_path is not None and Path(output_path).resolve().is_relative_to(root):
            raise ValueError("备份输出必须位于源目录之外，避免递归打包备份文件")
    return roots


def _target_roots(paths: List[str]) -> List[Path]:
    if not paths or any(not str(path).strip() for path in paths):
        raise ValueError("必须指定非空的目录列表")
    roots = [Path(path).expanduser().resolve() for path in paths]
    for index, root in enumerate(roots):
        if root.exists() and not root.is_dir():
            raise ValueError(f"配置的目录实际为文件: {root}")
        if any(root.is_relative_to(other) or other.is_relative_to(root) for other in roots[:index]):
            raise ValueError("目录不能重复或互相包含，请只配置必要的父目录")
    return roots


@contextmanager
def backup_tar_writer(output_path: Path, source_paths: List[str]) -> Iterator[tarfile.TarFile]:
    """
    创建带源目录清单的备份包，完整写入后才替换目标文件

    :param output_path (Path): 最终备份包路径
    :param source_paths (List): 与 source-N 归档根一一对应的源目录

    :yields TarFile: 可写入文件的 tar 对象
    """
    roots = backup_sources(source_paths, output_path)
    manifest = json.dumps({"version": 1, "sources": [
        {"prefix": f"source-{index}", "path": str(root)} for index, root in enumerate(roots)
    ]}, ensure_ascii=False).encode("utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=output_path.parent, prefix=".p115-backup-", suffix=".part", delete=False) as file:
        temporary = Path(file.name)
    try:
        with tarfile.open(temporary, "w:gz", dereference=True) as archive:
            info = tarfile.TarInfo(MANIFEST_NAME)
            info.size = len(manifest)
            info.mode = 0o600
            archive.addfile(info, BytesIO(manifest))
            yield archive
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_roots(archive: tarfile.TarFile, paths: List[str]) -> Dict[str, Path]:
    targets = _target_roots(paths)
    try:
        member = archive.getmember(MANIFEST_NAME)
    except KeyError:
        mapping = {Path(path).name: root for path, root in zip(paths, targets)}
        if len(mapping) != len(targets) or "" in mapping:
            raise ValueError("旧格式备份无法区分同名源目录，请使用带目录清单的新备份")
        return mapping
    if not member.isfile() or member.size > 1024 * 1024:
        raise ValueError("备份目录清单无效或过大")
    with archive.extractfile(member) as file:
        manifest = json.load(file)
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        raise ValueError("不支持的备份格式版本")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or len(sources) != len(targets):
        raise ValueError("恢复目标数量与备份源目录数量不一致")
    mapping = {}
    existing_targets = {str(target): target for target in targets}
    same_paths = all(isinstance(source, dict) and isinstance(source.get("path"), str)
                     and source["path"] in existing_targets for source in sources)
    for source, target in zip(sources, targets):
        prefix = source.get("prefix") if isinstance(source, dict) else None
        if (not isinstance(prefix, str) or not re.fullmatch(r"source-\d+", prefix) or prefix in mapping
                or not isinstance(source.get("path"), str)):
            raise ValueError("备份目录清单包含无效或重复的根目录")
        mapping[prefix] = existing_targets[source["path"]] if same_paths else target
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("备份目录清单包含重复的源目录")
    return mapping


def _member_parts(name: str):
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"备份包含越界路径: {name}")
    return path.parts


def _destination(root: Path, relative_parts) -> Path:
    destination = root.joinpath(*relative_parts)
    if not destination.resolve().is_relative_to(root):
        raise ValueError(f"恢复路径通过符号链接越界: {destination}")
    return destination


def _regular_member(archive, member, roots):
    seen = set()
    while member.issym() or member.islnk():
        if member.name in seen or len(seen) >= 100:
            raise ValueError("备份包含循环链接")
        seen.add(member.name)
        if PurePosixPath(member.linkname).is_absolute():
            raise ValueError("备份包含绝对链接")
        name = member.linkname
        if member.issym():
            name = posixpath.join(posixpath.dirname(member.name), name)
        name = posixpath.normpath(name)
        if _member_parts(name)[0] not in roots:
            raise ValueError("备份链接指向未知源目录")
        member = archive.getmember(name)
    if not member.isfile():
        raise ValueError(f"不支持的备份文件类型: {member.name}")
    return member


def restore_backup_archive(archive_path: Path, source_paths: List[str]) -> None:
    """
    按显式配置的多根目录恢复，写入前验证全部路径、链接和目标边界

    原位置恢复按路径匹配，目录重定位时按配置顺序匹配新格式的源目录
    旧格式按唯一的目录名匹配；链接按包内目标内容恢复为普通文件

    :param archive_path (Path): 备份包
    :param source_paths (List): 恢复目标目录列表
    """
    with tarfile.open(archive_path, "r:gz") as archive:
        roots = _restore_roots(archive, source_paths)
        plan = []
        destinations = set()
        for member in archive.getmembers():
            if member.name == MANIFEST_NAME:
                continue
            parts = _member_parts(member.name)
            if parts[0] not in roots:
                raise ValueError(f"备份条目不属于配置的源目录: {member.name}")
            root = roots[parts[0]]
            if len(parts) == 1 and not member.isdir():
                raise ValueError("备份根条目必须是目录")
            destination = _destination(root, parts[1:])
            if destination in destinations:
                raise ValueError(f"备份包含重复目标: {destination}")
            destinations.add(destination)
            regular = None if member.isdir() else _regular_member(archive, member, roots)
            if destination.exists() and destination.is_dir() != member.isdir():
                raise ValueError(f"恢复目标文件与目录类型冲突: {destination}")
            plan.append((root, parts[1:], member, regular))
        file_destinations = {root.joinpath(*parts) for root, parts, _, regular in plan if regular is not None}
        for root, parts, _, _ in plan:
            for parent in root.joinpath(*parts).parents:
                if parent == root or not parent.is_relative_to(root):
                    break
                if parent in file_destinations or (parent.exists() and not parent.is_dir()):
                    raise ValueError(f"恢复目标的父路径不是目录: {parent}")
        for root in roots.values():
            root.mkdir(parents=True, exist_ok=True)
        for root, parts, member, regular in plan:
            destination = _destination(root, parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=destination.parent, prefix=".p115-restore-", suffix=".part", delete=False) as output:
                temporary = Path(output.name)
                try:
                    with archive.extractfile(regular) as source:
                        copyfileobj(source, output, length=1024 * 1024)
                except BaseException:
                    output.close()
                    temporary.unlink(missing_ok=True)
                    raise
            try:
                temporary.chmod((member.mode & 0o755) | 0o600)
                os.utime(temporary, (member.mtime, member.mtime))
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
