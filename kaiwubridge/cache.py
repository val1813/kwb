"""性能优化模块：多级缓存 + 批量操作控制

职责：
- QueryCache: LRU查询结果缓存，避免重复SQL执行
- SchemaCache: 表结构缓存，减少SQLite元数据读取
- EmbeddingCache: embedding向量持久化到SQLite，避免重启后重算
- BatchProcessor: 批量LLM调用的并发控制与进度回调

设计决策：
- 查询缓存用OrderedDict实现LRU，比functools.lru_cache更灵活（支持TTL和手动失效）
- Schema缓存TTL较长（10分钟），因为表结构变化频率低
- Embedding持久化用SQLite而非文件，与项目元数据库保持一致
- 批量处理用asyncio.Semaphore限流，保护本地Ollama不被打爆
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Coroutine

import numpy as np

# ============ 模块级常量 ============

# 查询缓存最大条目数：200条覆盖典型用户一次会话的查询量，内存占用可控（约50MB上限）
QUERY_CACHE_MAX_SIZE = 200

# 查询缓存TTL（秒）：5分钟，平衡数据新鲜度和性能（数据库数据可能随时变化）
QUERY_CACHE_TTL = 300

# Schema缓存TTL（秒）：10分钟，表结构变化频率远低于数据本身
SCHEMA_CACHE_TTL = 600

# 批量LLM调用默认并发数：3，本地Ollama单卡通常只能并行处理2-3个请求
BATCH_CONCURRENCY_LIMIT = 3


class QueryCache:
    """LRU查询结果缓存

    缓存key由(db_id, sql_hash, row_filters_hash)三元组构成，
    确保不同数据库、不同SQL、不同行级过滤条件的结果互不干扰。

    使用OrderedDict实现LRU淘汰：每次访问将条目移到末尾，
    容量满时从头部淘汰最久未使用的条目。
    """

    def __init__(
        self,
        max_size: int = QUERY_CACHE_MAX_SIZE,
        ttl: int = QUERY_CACHE_TTL,
    ):
        self._max_size = max_size
        self._ttl = ttl
        self._cache: OrderedDict[tuple, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def _make_key(db_id: str, sql: str, row_filters: dict | None = None) -> tuple:
        """生成缓存key：对SQL和过滤条件取哈希，避免超长key"""
        sql_hash = hashlib.sha256(sql.encode("utf-8")).hexdigest()
        filters_str = str(sorted(row_filters.items())) if row_filters else ""
        filters_hash = hashlib.sha256(filters_str.encode("utf-8")).hexdigest()
        return (db_id, sql_hash, filters_hash)

    def get(self, db_id: str, sql: str, row_filters: dict | None = None) -> Any | None:
        """查询缓存，命中则返回结果，未命中或过期返回None"""
        key = self._make_key(db_id, sql, row_filters)
        with self._lock:
            if key not in self._cache:
                return None
            expire_at, value = self._cache[key]
            # 检查TTL过期
            if time.time() > expire_at:
                del self._cache[key]
                return None
            # LRU：移到末尾表示最近使用
            self._cache.move_to_end(key)
            return value

    def put(self, db_id: str, sql: str, result: Any, row_filters: dict | None = None) -> None:
        """写入缓存，超出容量时淘汰最久未使用的条目"""
        key = self._make_key(db_id, sql, row_filters)
        expire_at = time.time() + self._ttl
        with self._lock:
            # 如果key已存在，先删除再插入（确保移到末尾）
            if key in self._cache:
                del self._cache[key]
            self._cache[key] = (expire_at, result)
            # 超出容量，淘汰最旧条目
            while len(self._cache) > self._max_size:
                self._cache.popitem(last=False)

    def invalidate(self, db_id: str | None = None) -> None:
        """手动失效缓存

        Args:
            db_id: 指定数据库ID则只清除该库的缓存，None则清除全部
        """
        with self._lock:
            if db_id is None:
                self._cache.clear()
            else:
                keys_to_remove = [k for k in self._cache if k[0] == db_id]
                for key in keys_to_remove:
                    del self._cache[key]

    @property
    def size(self) -> int:
        """当前缓存条目数"""
        with self._lock:
            return len(self._cache)


class SchemaCache:
    """表结构缓存

    缓存已扫描的表结构信息，避免每次请求都读取SQLite元数据库。
    key为"db_id.table_name"格式，value为TableInfo对象。

    典型场景：用户连续提问同一数据库的不同表，
    第一次扫描后后续请求直接从内存读取。
    """

    def __init__(self, ttl: int = SCHEMA_CACHE_TTL):
        self._ttl = ttl
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _make_key(db_id: str, table_name: str | None = None) -> str:
        """生成缓存key"""
        if table_name:
            return f"{db_id}.{table_name}"
        return db_id

    def get(self, db_id: str, table_name: str | None = None) -> Any | None:
        """获取缓存的表结构，过期返回None"""
        key = self._make_key(db_id, table_name)
        with self._lock:
            if key not in self._cache:
                return None
            expire_at, value = self._cache[key]
            if time.time() > expire_at:
                del self._cache[key]
                return None
            return value

    def put(self, db_id: str, table_name: str | None, value: Any) -> None:
        """写入表结构缓存"""
        key = self._make_key(db_id, table_name)
        expire_at = time.time() + self._ttl
        with self._lock:
            self._cache[key] = (expire_at, value)

    def invalidate(self, db_id: str | None = None, table_name: str | None = None) -> None:
        """手动失效缓存

        扫描完成后调用，确保下次读取到最新结构。

        Args:
            db_id: 指定数据库ID，None则清除全部
            table_name: 指定表名，None则清除该库全部表
        """
        with self._lock:
            if db_id is None:
                self._cache.clear()
                return
            if table_name:
                # 精确失效单张表
                key = self._make_key(db_id, table_name)
                self._cache.pop(key, None)
            else:
                # 失效该数据库下所有表
                prefix = f"{db_id}."
                keys_to_remove = [
                    k for k in self._cache
                    if k == db_id or k.startswith(prefix)
                ]
                for key in keys_to_remove:
                    del self._cache[key]

    @property
    def size(self) -> int:
        """当前缓存条目数"""
        with self._lock:
            return len(self._cache)


class EmbeddingCache:
    """Embedding向量持久化缓存

    将embedding向量存储到SQLite，避免服务重启后重新计算。
    embedding计算是CPU/GPU密集操作，持久化可显著加速冷启动。

    存储格式：numpy数组通过tobytes()序列化为BLOB，
    读取时用frombuffer()还原，零拷贝高效。
    """

    def __init__(self, db_path: str | Path):
        """初始化embedding缓存

        Args:
            db_path: SQLite数据库文件路径（通常与元数据库相同）
        """
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_table()

    def _init_table(self) -> None:
        """创建缓存表（如果不存在）"""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS embedding_cache (
                        text_hash TEXT PRIMARY KEY,
                        embedding BLOB NOT NULL,
                        created_at TEXT NOT NULL DEFAULT (datetime('now'))
                    )
                """)
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _hash_text(text: str) -> str:
        """对文本取SHA256哈希作为缓存key"""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def get(self, text: str) -> np.ndarray | None:
        """查询缓存的embedding向量

        Args:
            text: 原始文本

        Returns:
            numpy数组（float32），未命中返回None
        """
        text_hash = self._hash_text(text)
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                cursor = conn.execute(
                    "SELECT embedding FROM embedding_cache WHERE text_hash = ?",
                    (text_hash,),
                )
                row = cursor.fetchone()
                if row is None:
                    return None
                # BLOB还原为numpy float32数组
                return np.frombuffer(row[0], dtype=np.float32).copy()
            finally:
                conn.close()

    def put(self, text: str, embedding: np.ndarray) -> None:
        """持久化embedding向量

        Args:
            text: 原始文本
            embedding: numpy数组（会转为float32存储）
        """
        text_hash = self._hash_text(text)
        # 统一转为float32，节省存储空间且精度足够
        blob = embedding.astype(np.float32).tobytes()
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute(
                    """INSERT OR REPLACE INTO embedding_cache (text_hash, embedding, created_at)
                       VALUES (?, ?, datetime('now'))""",
                    (text_hash, blob),
                )
                conn.commit()
            finally:
                conn.close()

    def batch_get(self, texts: list[str]) -> dict[str, np.ndarray | None]:
        """批量查询embedding缓存

        Args:
            texts: 文本列表

        Returns:
            {text: embedding}字典，未命中的值为None
        """
        results: dict[str, np.ndarray | None] = {}
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                for text in texts:
                    text_hash = self._hash_text(text)
                    cursor = conn.execute(
                        "SELECT embedding FROM embedding_cache WHERE text_hash = ?",
                        (text_hash,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        results[text] = None
                    else:
                        results[text] = np.frombuffer(row[0], dtype=np.float32).copy()
            finally:
                conn.close()
        return results

    def clear(self) -> None:
        """清空全部缓存（重新扫描时使用）"""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute("DELETE FROM embedding_cache")
                conn.commit()
            finally:
                conn.close()

    @property
    def count(self) -> int:
        """缓存中的embedding条目数"""
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                cursor = conn.execute("SELECT COUNT(*) FROM embedding_cache")
                return cursor.fetchone()[0]
            finally:
                conn.close()


class BatchProcessor:
    """批量操作并发控制器

    批量生成语义名片时，需要对LLM调用进行限流：
    - 本地Ollama单卡资源有限，并发过高会OOM或响应超时
    - 通过asyncio.Semaphore控制同时进行的LLM调用数
    - 支持进度回调，方便前端展示进度条

    使用示例：
        processor = BatchProcessor(concurrency=3)
        results = await processor.run(tasks, worker_fn, on_progress=callback)
    """

    def __init__(self, concurrency: int = BATCH_CONCURRENCY_LIMIT):
        """初始化批量处理器

        Args:
            concurrency: 最大并发数，默认3（本地Ollama安全上限）
        """
        self._concurrency = concurrency

    async def run(
        self,
        items: list[Any],
        worker: Callable[[Any], Coroutine[Any, Any, Any]],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[Any]:
        """批量执行异步任务，带并发控制和进度回调

        Args:
            items: 待处理的数据列表
            worker: 异步工作函数，接收单个item返回结果
            on_progress: 进度回调函数，参数为(已完成数, 总数)

        Returns:
            结果列表，顺序与items对应
        """
        total = len(items)
        if total == 0:
            return []

        semaphore = asyncio.Semaphore(self._concurrency)
        results: list[Any] = [None] * total
        completed = 0
        # 用锁保护completed计数器（多个协程并发更新）
        progress_lock = asyncio.Lock()

        async def _wrapped_worker(index: int, item: Any) -> None:
            nonlocal completed
            async with semaphore:
                result = await worker(item)
                results[index] = result
            # 更新进度
            async with progress_lock:
                completed += 1
                if on_progress:
                    on_progress(completed, total)

        # 创建所有任务并发执行（由semaphore控制实际并发数）
        tasks = [
            asyncio.create_task(_wrapped_worker(i, item))
            for i, item in enumerate(items)
        ]
        await asyncio.gather(*tasks, return_exceptions=False)
        return results

    async def run_safe(
        self,
        items: list[Any],
        worker: Callable[[Any], Coroutine[Any, Any, Any]],
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[tuple[bool, Any]]:
        """批量执行，捕获单个任务异常不影响整体

        与run()的区别：单个任务失败不会中断整批处理。

        Returns:
            [(success, result_or_error), ...]列表，顺序与items对应
        """
        total = len(items)
        if total == 0:
            return []

        semaphore = asyncio.Semaphore(self._concurrency)
        results: list[tuple[bool, Any]] = [(False, None)] * total
        completed = 0
        progress_lock = asyncio.Lock()

        async def _wrapped_worker(index: int, item: Any) -> None:
            nonlocal completed
            async with semaphore:
                try:
                    result = await worker(item)
                    results[index] = (True, result)
                except Exception as e:
                    results[index] = (False, e)
            async with progress_lock:
                completed += 1
                if on_progress:
                    on_progress(completed, total)

        tasks = [
            asyncio.create_task(_wrapped_worker(i, item))
            for i, item in enumerate(items)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
        return results
