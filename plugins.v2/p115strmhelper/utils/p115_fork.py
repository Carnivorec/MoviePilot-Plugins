def reset_p115client_cache_locks() -> int:
    """
    在新建子进程中重建 p115client 全局 JSON 缓存的文件锁

    客户端实例不拥有这些模块级缓存；仅重建客户端不会解除继承锁的进程绑定
    本方法必须在子进程开始使用 p115client 之前调用，不能用于正在工作的父进程

    :return int: 已重建的缓存锁数量
    """
    from filelock import FileLock
    from p115client import util

    seen = set()
    for cache in vars(util).values():
        if isinstance(cache, util.LockedJsonKV) and id(cache) not in seen:
            seen.add(id(cache))
            # 保留缓存对象，兼容其他模块已经导入的引用；不释放父进程拥有的旧锁
            cache._lock = FileLock(cache._lock.lock_file)
    return len(seen)
