"""
Advanced caching system for the Tenire framework.

This module provides a flexible and efficient caching system with:
1. Multi-level caching (memory, disk, distributed)
2. Configurable eviction policies
3. Specialized cache implementations for different data types
4. Thread-safe operations
5. Monitoring and statistics
"""

# Standard library imports
import asyncio
import functools
import sys
import json
import pickle
import time
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Generic, List, Optional, Set, Tuple, TypeVar, Union
import weakref

# Third-party imports
import aiofiles
import diskcache
import msgpack
import xxhash
import numpy as np
from cachetools import TTLCache, LRUCache, LFUCache

# Local imports
from tenire.utils.logger import get_logger
from tenire.core.codex import Signal, SignalType
from tenire.servicers import SignalManager
from tenire.organizers.compactor import compactor
from tenire.organizers.concurrency import ConcurrencyManager

logger = get_logger(__name__)

T = TypeVar('T')  # Generic type for cache values
K = TypeVar('K')  # Generic type for cache keys

class CachePolicy(Enum):
    """Available cache eviction policies."""
    LRU = "least_recently_used"
    LFU = "least_frequently_used"
    TTL = "time_to_live"
    FIFO = "first_in_first_out"
    RANDOM = "random"

class CacheSerializer:
    """Handles serialization/deserialization of cache values."""
    
    @staticmethod
    async def serialize(value: Any) -> bytes:
        """Serialize value to bytes."""
        if isinstance(value, (dict, list)):
            return msgpack.packb(value, use_bin_type=True)
        elif isinstance(value, np.ndarray):
            return msgpack.packb({
                'type': 'ndarray',
                'data': value.tobytes(),
                'shape': value.shape,
                'dtype': str(value.dtype)
            }, use_bin_type=True)
        else:
            return pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
            
    @staticmethod
    async def deserialize(data: bytes) -> Any:
        """Deserialize bytes to value."""
        try:
            # Try msgpack first
            value = msgpack.unpackb(data, raw=False)
            if isinstance(value, dict) and value.get('type') == 'ndarray':
                return np.frombuffer(value['data'], dtype=value['dtype']).reshape(value['shape'])
            return value
        except:
            # Fall back to pickle
            return pickle.loads(data)

class CacheKey:
    """Efficient cache key generation and handling."""
    
    @staticmethod
    def generate(key: Any, prefix: str = "") -> str:
        """Generate cache key with optional prefix."""
        if isinstance(key, str):
            base_key = key
        elif isinstance(key, (int, float, bool)):
            base_key = str(key)
        elif isinstance(key, (list, tuple, dict)):
            base_key = xxhash.xxh64(json.dumps(key, sort_keys=True)).hexdigest()
        else:
            base_key = xxhash.xxh64(str(key)).hexdigest()
            
        return f"{prefix}:{base_key}" if prefix else base_key
        
    @staticmethod
    def parse(key: str) -> Tuple[str, str]:
        """Parse key into prefix and base key."""
        parts = key.split(":", 1)
        return parts if len(parts) == 2 else ("", key)

@dataclass
class CacheStats:
    """Statistics for cache monitoring."""
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    size: int = 0
    last_cleanup: datetime = field(default_factory=datetime.now)
    total_get_time: float = 0.0
    total_set_time: float = 0.0
    memory_usage: int = 0
    disk_usage: int = 0
    cache_keys: Set[str] = field(default_factory=set)
    operation_counts: Dict[str, int] = field(default_factory=lambda: {
        'get': 0, 'set': 0, 'delete': 0, 'clear': 0
    })
    
    @property
    def hit_ratio(self) -> float:
        """Calculate cache hit ratio."""
        total = self.hits + self.misses
        return self.hits / total if total > 0 else 0.0
    
    @property
    def avg_get_time(self) -> float:
        """Calculate average get operation time."""
        return self.total_get_time / (self.hits + self.misses) if (self.hits + self.misses) > 0 else 0.0
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert stats to dictionary."""
        return {
            'hits': self.hits,
            'misses': self.misses,
            'evictions': self.evictions,
            'size': self.size,
            'hit_ratio': self.hit_ratio,
            'avg_get_time': self.avg_get_time,
            'last_cleanup': self.last_cleanup.isoformat(),
            'memory_usage': self.memory_usage,
            'disk_usage': self.disk_usage,
            'num_keys': len(self.cache_keys),
            'operations': self.operation_counts
        }
        
    async def save_to_file(self, path: Union[str, Path]) -> None:
        """Save stats to file."""
        async with aiofiles.open(path, 'w') as f:
            await f.write(json.dumps(self.to_dict(), indent=2))

class CacheBackend(ABC, Generic[K, T]):
    """Abstract base class for cache backends."""
    
    def __init__(self):
        self.stats = CacheStats()
        self.serializer = CacheSerializer()
        self._weak_refs = weakref.WeakSet()
        self._lock = asyncio.Lock()
        self._semaphore = ConcurrencyManager.semaphores.get_semaphore(
            f"cache_{id(self)}",
            max_concurrent=10
        )
        
    async def start(self):
        """Start the cache backend."""
        # Method kept for backward compatibility and future extensions
        pass
        
    @asynccontextmanager
    async def _acquire_lock(self):
        """Context manager for acquiring cache lock with timeout."""
        try:
            async with self._semaphore:
                async with self._lock:
                    yield
        except asyncio.TimeoutError:
            logger.warning("Timeout acquiring cache lock")
            raise
            
    @abstractmethod
    async def get(self, key: K) -> Optional[T]:
        """Get value from cache."""
        pass
    
    @abstractmethod
    async def set(self, key: K, value: T, ttl: Optional[int] = None) -> None:
        """Set value in cache."""
        pass
    
    @abstractmethod
    async def delete(self, key: K) -> None:
        """Delete value from cache."""
        pass
    
    @abstractmethod
    async def clear(self) -> None:
        """Clear all values from cache."""
        pass
    
    @abstractmethod
    async def contains(self, key: K) -> bool:
        """Check if key exists in cache."""
        pass

    @abstractmethod
    async def clear_expired(self) -> None:
        """Clear expired entries from cache."""
        pass
        
    async def get_many(self, keys: List[K]) -> Dict[K, Optional[T]]:
        """Get multiple values from cache."""
        async with self._acquire_lock():
            results = {}
            for key in keys:
                results[key] = await self.get(key)
            return results
        
    async def set_many(
        self,
        items: Dict[K, T],
        ttl: Optional[int] = None
    ) -> None:
        """Set multiple values in cache."""
        async with self._acquire_lock():
            for key, value in items.items():
                await self.set(key, value, ttl)
            
    async def delete_many(self, keys: List[K]) -> None:
        """Delete multiple values from cache."""
        async with self._acquire_lock():
            for key in keys:
                await self.delete(key)
            
    def track_object(self, obj: Any) -> None:
        """Track object with weak reference."""
        self._weak_refs.add(obj)

class MemoryCache(CacheBackend[K, T]):
    """In-memory cache implementation."""
    
    def __init__(
        self,
        max_size: int = 1000,
        policy: CachePolicy = CachePolicy.LRU,
        ttl: Optional[int] = None
    ):
        super().__init__()
        self._lock = asyncio.Lock()
        
        if policy == CachePolicy.LRU:
            self._cache = LRUCache(maxsize=max_size)
        elif policy == CachePolicy.LFU:
            self._cache = LFUCache(maxsize=max_size)
        elif policy == CachePolicy.TTL:
            self._cache = TTLCache(maxsize=max_size, ttl=ttl or 3600)
        else:
            self._cache = OrderedDict()
            
        # Register with compactor
        compactor.register_cleanup_task(
            name=f"memory_cache_{id(self)}",
            cleanup_func=self.cleanup,
            priority=80,
            is_async=True,
            metadata={'type': 'cache', 'backend': 'memory'}
        )
        
        self._cleanup_task = None
        self._should_stop = False
    
    async def start(self) -> None:
        """Start the cache background tasks."""
        if self._cleanup_task is None:
            self._should_stop = False
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            
    async def stop(self) -> None:
        """Stop the cache background tasks."""
        if self._cleanup_task is not None:
            self._should_stop = True
            try:
                await asyncio.wait_for(self._cleanup_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._cleanup_task.cancel()
            self._cleanup_task = None
    
    async def get(self, key: K) -> Optional[T]:
        start_time = time.perf_counter()
        async with self._acquire_lock():
            try:
                value = self._cache[key]
                self.stats.hits += 1
                self.stats.operation_counts['get'] += 1
                return value
            except KeyError:
                self.stats.misses += 1
                self.stats.operation_counts['get'] += 1
                return None
            finally:
                self.stats.total_get_time += time.perf_counter() - start_time
    
    async def set(self, key: K, value: T, ttl: Optional[int] = None) -> None:
        start_time = time.perf_counter()
        async with self._acquire_lock():
            if len(self._cache) >= self._cache.maxsize:
                self.stats.evictions += 1
            self._cache[key] = value
            self.stats.size = len(self._cache)
            self.stats.cache_keys.add(key)
            self.stats.operation_counts['set'] += 1
            self.stats.total_set_time += time.perf_counter() - start_time
            
            # Track memory usage
            self.stats.memory_usage = sum(
                sys.getsizeof(k) + sys.getsizeof(v)
                for k, v in self._cache.items()
            )
    
    async def delete(self, key: K) -> None:
        async with self._acquire_lock():
            try:
                del self._cache[key]
                self.stats.size = len(self._cache)
                self.stats.cache_keys.remove(key)
                self.stats.operation_counts['delete'] += 1
            except KeyError:
                pass
    
    async def clear(self) -> None:
        async with self._acquire_lock():
            self._cache.clear()
            self.stats.size = 0
            self.stats.cache_keys.clear()
            self.stats.operation_counts['clear'] += 1
            self.stats.memory_usage = 0
    
    async def contains(self, key: K) -> bool:
        async with self._acquire_lock():
            return key in self._cache
            
    async def cleanup(self) -> None:
        """Cleanup expired items."""
        async with self._acquire_lock():
            if isinstance(self._cache, TTLCache):
                self._cache.expire()
            self.stats.last_cleanup = datetime.now()
            
    async def _cleanup_loop(self):
        """Background task for periodic cleanup."""
        try:
            while not self._should_stop:
                await asyncio.sleep(60)  # Run every minute
                await self.cleanup()
        except asyncio.CancelledError:
            logger.debug("Cleanup loop cancelled")
        finally:
            await self.cleanup()

    async def clear_expired(self) -> None:
        """Clear expired entries from cache."""
        async with self._acquire_lock():
            if isinstance(self._cache, TTLCache):
                self._cache.expire()
                self.stats.size = len(self._cache)
                self.stats.cache_keys = set(self._cache.keys())

class DiskCache(CacheBackend[K, T]):
    """Disk-based cache implementation using diskcache."""
    
    def __init__(
        self,
        cache_dir: Union[str, Path],
        max_size: int = 1_000_000,  # 1GB default
        policy: CachePolicy = CachePolicy.LRU,
        ttl: Optional[int] = None,
        compression: bool = True
    ):
        super().__init__()
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._compression = compression
        self._compressor = None  # Will be initialized lazily when needed
        
        self._cache = diskcache.Cache(
            directory=str(self.cache_dir),
            size_limit=max_size,
            eviction_policy='least-recently-used' if policy == CachePolicy.LRU else 'least-frequently-used',
            disk_min_file_size=1024,  # 1KB minimum file size
            disk_pickle_protocol=pickle.HIGHEST_PROTOCOL
        )
        
        # Register with compactor
        compactor.register_cleanup_task(
            name=f"disk_cache_{id(self)}",
            cleanup_func=self.cleanup,
            priority=70,
            is_async=True,
            metadata={'type': 'cache', 'backend': 'disk'}
        )
        
        self._cleanup_task = None
        self._should_stop = False
    
    async def _get_compressor(self):
        """Lazy initialize compressor when needed."""
        if self._compression and self._compressor is None:
            from tenire.data.cleanup.compressor import BetDataCompressor
            self._compressor = BetDataCompressor(
                compression_level=3,
                compression_method='zstd',
                chunk_size=1  # We compress individual items
            )
        return self._compressor

    async def _compress_value(self, value: bytes) -> bytes:
        """Compress value if compression is enabled."""
        compressor = await self._get_compressor()
        if not self._compression or not compressor:
            return value
        try:
            return await compressor.strategy.compress(value)
        except Exception as e:
            logger.warning(f"Compression failed, storing uncompressed: {str(e)}")
            return value
            
    async def _decompress_value(self, value: bytes) -> bytes:
        """Decompress value if compression is enabled."""
        compressor = await self._get_compressor()
        if not self._compression or not compressor:
            return value
        try:
            return await compressor.strategy.decompress(value)
        except Exception as e:
            logger.warning(f"Decompression failed: {str(e)}")
            return value
    
    async def get(self, key: K) -> Optional[T]:
        start_time = time.perf_counter()
        try:
            async with self._acquire_lock():
                value = await ConcurrencyManager.thread_pool.execute(
                    self._cache.get, key
                )
                if value is not None:
                    self.stats.hits += 1
                    self.stats.operation_counts['get'] += 1
                    decompressed = await self._decompress_value(value)
                    return await self.serializer.deserialize(decompressed)
                self.stats.misses += 1
                self.stats.operation_counts['get'] += 1
                return None
        finally:
            self.stats.total_get_time += time.perf_counter() - start_time
    
    async def set(self, key: K, value: T, ttl: Optional[int] = None) -> None:
        start_time = time.perf_counter()
        async with self._acquire_lock():
            serialized = await self.serializer.serialize(value)
            compressed = await self._compress_value(serialized)
            await ConcurrencyManager.thread_pool.execute(
                self._cache.set,
                key,
                compressed,
                expire=ttl
            )
            self.stats.size = self._cache.volume()
            self.stats.cache_keys.add(key)
            self.stats.operation_counts['set'] += 1
            self.stats.total_set_time += time.perf_counter() - start_time
            
            # Update disk usage
            self.stats.disk_usage = sum(
                f.stat().st_size for f in self.cache_dir.glob('**/*')
                if f.is_file()
            )
    
    async def delete(self, key: K) -> None:
        await ConcurrencyManager.thread_pool.execute(self._cache.delete, key)
        self.stats.size = self._cache.volume()
        self.stats.cache_keys.remove(key)
        self.stats.operation_counts['delete'] += 1
    
    async def clear(self) -> None:
        await ConcurrencyManager.thread_pool.execute(self._cache.clear)
        self.stats.size = 0
        self.stats.cache_keys.clear()
        self.stats.operation_counts['clear'] += 1
        self.stats.disk_usage = 0
    
    async def contains(self, key: K) -> bool:
        return await ConcurrencyManager.thread_pool.execute(self._cache.__contains__, key)
            
    async def cleanup(self) -> None:
        """Cleanup expired items and optimize storage."""
        # Expire old items
        await ConcurrencyManager.thread_pool.execute(self._cache.expire)
        
        # Perform cache maintenance
        try:
            # Use diskcache's internal maintenance methods
            await ConcurrencyManager.thread_pool.execute(self._cache.expire)
            await ConcurrencyManager.thread_pool.execute(self._cache.cull)
        except Exception as e:
            logger.warning(f"Cache maintenance error: {str(e)}")
            
        if self._compressor:
            await self._compressor.cleanup()
            
        self.stats.last_cleanup = datetime.now()

    async def clear_expired(self) -> None:
        """Clear expired entries from cache."""
        async with self._acquire_lock():
            # Use diskcache's expire method to remove expired entries
            await ConcurrencyManager.thread_pool.execute(self._cache.expire)
            # Update cache stats
            self.stats.size = self._cache.volume()
            # Update cache keys - we need to do this in a thread since iterkeys() might be slow
            self.stats.cache_keys = set(
                await ConcurrencyManager.thread_pool.execute(
                    lambda: list(self._cache.iterkeys())
                )
            )

class TieredCache(CacheBackend[K, T]):
    """Multi-level cache implementation."""
    
    def __init__(
        self,
        memory_size: int = 1000,
        disk_size: int = 1_000_000,
        disk_path: Optional[Union[str, Path]] = None,
        policy: CachePolicy = CachePolicy.LRU,
        ttl: Optional[int] = None,
        compression: bool = True
    ):
        super().__init__()
        self.memory_cache = MemoryCache(
            max_size=memory_size,
            policy=policy,
            ttl=ttl
        )
        
        if disk_path:
            self.disk_cache = DiskCache(
                cache_dir=disk_path,
                max_size=disk_size,
                policy=policy,
                ttl=ttl,
                compression=compression
            )
        else:
            self.disk_cache = None
            
        # Register with compactor
        compactor.register_cleanup_task(
            name=f"tiered_cache_{id(self)}",
            cleanup_func=self.cleanup,
            priority=90,
            is_async=True,
            metadata={'type': 'cache', 'backend': 'tiered'}
        )
        
        self._monitor_task = None
        self._should_stop = False
    
    async def start(self) -> None:
        """Start all cache components."""
        await self.memory_cache.start()
        if self.disk_cache:
            await self.disk_cache.start()
        if self._monitor_task is None:
            self._should_stop = False
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            
    async def stop(self) -> None:
        """Stop all cache components."""
        if self._monitor_task is not None:
            self._should_stop = True
            try:
                await asyncio.wait_for(self._monitor_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._monitor_task.cancel()
            self._monitor_task = None
            
        await self.memory_cache.stop()
        if self.disk_cache:
            await self.disk_cache.stop()
    
    async def get(self, key: K) -> Optional[T]:
        # Try memory cache first
        value = await self.memory_cache.get(key)
        if value is not None:
            self.stats.hits += 1
            self.stats.operation_counts['get'] += 1
            return value
            
        # Try disk cache if available
        if self.disk_cache:
            value = await self.disk_cache.get(key)
            if value is not None:
                # Promote to memory cache
                await self.memory_cache.set(key, value)
                self.stats.hits += 1
                self.stats.operation_counts['get'] += 1
                return value
                
        self.stats.misses += 1
        self.stats.operation_counts['get'] += 1
        return None
    
    async def set(self, key: K, value: T, ttl: Optional[int] = None) -> None:
        # Set in memory cache
        await self.memory_cache.set(key, value, ttl)
        
        # Set in disk cache if available
        if self.disk_cache:
            await self.disk_cache.set(key, value, ttl)
            
        self.stats.size = (
            self.memory_cache.stats.size +
            (self.disk_cache.stats.size if self.disk_cache else 0)
        )
        self.stats.operation_counts['set'] += 1
        
        # Update usage stats
        self.stats.memory_usage = self.memory_cache.stats.memory_usage
        if self.disk_cache:
            self.stats.disk_usage = self.disk_cache.stats.disk_usage
    
    async def delete(self, key: K) -> None:
        await self.memory_cache.delete(key)
        if self.disk_cache:
            await self.disk_cache.delete(key)
        self.stats.operation_counts['delete'] += 1
        self.stats.size = (
            self.memory_cache.stats.size +
            (self.disk_cache.stats.size if self.disk_cache else 0)
        )
    
    async def clear(self) -> None:
        await self.memory_cache.clear()
        if self.disk_cache:
            await self.disk_cache.clear()
        self.stats.size = 0
        self.stats.operation_counts['clear'] += 1
    
    async def contains(self, key: K) -> bool:
        return (
            await self.memory_cache.contains(key) or
            (self.disk_cache and await self.disk_cache.contains(key))
        )
    
    async def cleanup(self) -> None:
        """Cleanup all cache levels."""
        await self.memory_cache.cleanup()
        if self.disk_cache:
            await self.disk_cache.cleanup()
        self.stats.last_cleanup = datetime.now()
        
    async def _monitor_loop(self):
        """Background task for monitoring cache performance."""
        try:
            while not self._should_stop:
                await asyncio.sleep(60)  # Monitor every minute
                
                # Emit monitoring signal
                await SignalManager().emit(Signal(
                    type=SignalType.RAG_COMPONENT_INITIALIZED,
                    data={
                        'component': 'tiered_cache',
                        'stats': self.stats.to_dict()
                    },
                    source='cache_monitor'
                ))
                
                # Save stats to file
                stats_path = Path('data/cache/stats')
                stats_path.mkdir(parents=True, exist_ok=True)
                await self.stats.save_to_file(
                    stats_path / f"cache_stats_{datetime.now():%Y%m%d_%H%M%S}.json"
                )
        except asyncio.CancelledError:
            logger.debug("Monitor loop cancelled")
        finally:
            await self.cleanup()

    async def clear_expired(self) -> None:
        """Clear expired entries from all cache levels."""
        await self.memory_cache.clear_expired()
        if self.disk_cache:
            await self.disk_cache.clear_expired()
        self.stats.size = (
            self.memory_cache.stats.size +
            (self.disk_cache.stats.size if self.disk_cache else 0)
        )

class BetCache(TieredCache[str, Dict[str, Any]]):
    """Specialized cache for bet data."""
    
    def __init__(
        self,
        memory_size: int = 10000,
        disk_size: int = 10_000_000,
        disk_path: Optional[Union[str, Path]] = None
    ):
        super().__init__(
            memory_size=memory_size,
            disk_size=disk_size,
            disk_path=disk_path,
            policy=CachePolicy.LRU,
            ttl=3600  # 1 hour TTL for bet data
        )
        
        self._bet_patterns: Dict[str, List[Dict[str, Any]]] = {}
        self._user_stats: Dict[str, Dict[str, Any]] = {}
        
    async def add_bet(self, bet_data: Dict[str, Any]) -> None:
        """Add a bet to the cache with pattern analysis."""
        bet_id = bet_data.get('id')
        if not bet_id:
            return
            
        # Generate cache key
        key = f"bet:{bet_id}"
        
        # Update pattern analysis
        pattern_key = f"{bet_data.get('game_type')}:{bet_data.get('bet_type')}"
        if pattern_key not in self._bet_patterns:
            self._bet_patterns[pattern_key] = []
        self._bet_patterns[pattern_key].append(bet_data)
        
        # Update user stats
        user_id = bet_data.get('user_id')
        if user_id:
            if user_id not in self._user_stats:
                self._user_stats[user_id] = {'total_bets': 0, 'wins': 0, 'losses': 0}
            self._user_stats[user_id]['total_bets'] += 1
            if bet_data.get('won'):
                self._user_stats[user_id]['wins'] += 1
            else:
                self._user_stats[user_id]['losses'] += 1
                
        # Cache the bet data
        await self.set(key, bet_data)
        
    async def get_user_stats(self, user_id: str) -> Dict[str, Any]:
        """Get betting statistics for a user."""
        return self._user_stats.get(user_id, {'total_bets': 0, 'wins': 0, 'losses': 0})
        
    async def get_pattern_analysis(self, game_type: str, bet_type: str) -> List[Dict[str, Any]]:
        """Get pattern analysis for a specific game and bet type."""
        pattern_key = f"{game_type}:{bet_type}"
        return self._bet_patterns.get(pattern_key, [])

class EmbeddingCache(TieredCache[str, np.ndarray]):
    """Specialized cache for document embeddings."""
    
    def __init__(
        self,
        memory_size: int = 5000,
        disk_size: int = 5_000_000,
        disk_path: Optional[Union[str, Path]] = None,
        embedding_dim: int = 384
    ):
        super().__init__(
            memory_size=memory_size,
            disk_size=disk_size,
            disk_path=disk_path,
            policy=CachePolicy.LFU  # Use LFU for embeddings
        )
        self.embedding_dim = embedding_dim
        
    async def add_embedding(
        self,
        doc_id: str,
        embedding: np.ndarray,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Add a document embedding to the cache."""
        if embedding.shape != (self.embedding_dim,):
            raise ValueError(f"Embedding dimension mismatch: expected {self.embedding_dim}, got {embedding.shape[0]}")
            
        key = f"emb:{doc_id}"
        value = {
            'embedding': embedding,
            'metadata': metadata or {},
            'created_at': datetime.now().isoformat()
        }
        await self.set(key, value)
        
    async def get_embedding(
        self,
        doc_id: str
    ) -> Tuple[Optional[np.ndarray], Optional[Dict[str, Any]]]:
        """Get a document embedding and its metadata."""
        key = f"emb:{doc_id}"
        value = await self.get(key)
        if value:
            return value['embedding'], value['metadata']
        return None, None
        
    async def get_similar_embeddings(
        self,
        query_embedding: np.ndarray,
        k: int = 5,
        threshold: float = 0.7
    ) -> List[Tuple[str, float, Dict[str, Any]]]:
        """Find similar embeddings using cosine similarity."""
        results = []
        async with self._lock:
            for key, value in self._cache.items():
                if not key.startswith('emb:'):
                    continue
                doc_embedding = value['embedding']
                similarity = np.dot(query_embedding, doc_embedding) / (
                    np.linalg.norm(query_embedding) * np.linalg.norm(doc_embedding)
                )
                if similarity >= threshold:
                    doc_id = key[4:]  # Remove 'emb:' prefix
                    results.append((doc_id, similarity, value['metadata']))
                    
        return sorted(results, key=lambda x: x[1], reverse=True)[:k]

class ModelCache(TieredCache[str, Any]):
    """Specialized cache for ML models and their artifacts."""
    
    def __init__(
        self,
        memory_size: int = 5,  # Small number since models are large
        disk_size: int = 10_000_000_000,  # 10GB default
        disk_path: Optional[Union[str, Path]] = None,
        model_configs: Optional[Dict[str, Dict[str, Any]]] = None
    ):
        super().__init__(
            memory_size=memory_size,
            disk_size=disk_size,
            disk_path=disk_path,
            policy=CachePolicy.LRU  # LRU is best for model caching
        )
        
        # Default configurations for different model types
        self.model_configs = {
            'sentence_transformer': {
                'max_size': 2_000_000_000,  # 2GB
                'ttl': None,  # No expiration
                'compression': True
            },
            'random_forest': {
                'max_size': 500_000_000,  # 500MB
                'ttl': None,
                'compression': True
            },
            'tfidf': {
                'max_size': 100_000_000,  # 100MB
                'ttl': None,
                'compression': True
            },
            'svd': {
                'max_size': 50_000_000,  # 50MB
                'ttl': None,
                'compression': True
            },
            'cross_encoder': {
                'max_size': 1_000_000_000,  # 1GB
                'ttl': None,
                'compression': True
            },
            'ollama': {
                'max_size': 5_000_000_000,  # 5GB
                'ttl': None,
                'compression': False  # Ollama handles its own caching
            },
            'claude': {
                'max_size': 1_000_000,  # 1MB (just for config)
                'ttl': 3600,  # 1 hour
                'compression': False
            },
            'openai': {
                'max_size': 1_000_000,  # 1MB (just for config)
                'ttl': 3600,  # 1 hour
                'compression': False
            }
        }
        
        # Update with custom configs if provided
        if model_configs:
            self.model_configs.update(model_configs)
            
        # Register cleanup task
        compactor.register_cleanup_task(
            name=f"model_cache_{id(self)}",
            cleanup_func=self.cleanup,
            priority=100,  # High priority for model cleanup
            is_async=True,
            metadata={'type': 'cache', 'backend': 'model'}
        )
        
    async def add_model(
        self,
        model_type: str,
        model_id: str,
        model: Any,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Add a model to the cache.
        
        Args:
            model_type: Type of model (e.g., 'sentence_transformer', 'random_forest')
            model_id: Unique identifier for the model
            model: The model object to cache
            metadata: Optional metadata about the model
        """
        if model_type not in self.model_configs:
            raise ValueError(f"Unknown model type: {model_type}")
            
        config = self.model_configs[model_type]
        key = f"{model_type}:{model_id}"
        
        value = {
            'model': model,
            'metadata': metadata or {},
            'created_at': datetime.now().isoformat(),
            'config': config
        }
        
        await self.set(
            key,
            value,
            ttl=config['ttl']
        )
        
    async def get_model(
        self,
        model_type: str,
        model_id: str
    ) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
        """
        Get a model from the cache.
        
        Args:
            model_type: Type of model
            model_id: Unique identifier for the model
            
        Returns:
            Tuple of (model, metadata) if found, else (None, None)
        """
        key = f"{model_type}:{model_id}"
        value = await self.get(key)
        
        if value:
            return value['model'], value['metadata']
        return None, None
        
    async def update_model_metadata(
        self,
        model_type: str,
        model_id: str,
        metadata: Dict[str, Any]
    ) -> None:
        """Update metadata for a cached model."""
        key = f"{model_type}:{model_id}"
        value = await self.get(key)
        
        if value:
            value['metadata'].update(metadata)
            await self.set(
                key,
                value,
                ttl=self.model_configs[model_type]['ttl']
            )
            
    async def remove_model(
        self,
        model_type: str,
        model_id: str
    ) -> None:
        """Remove a model from the cache."""
        key = f"{model_type}:{model_id}"
        await self.delete(key)
        
    async def get_model_info(
        self,
        model_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get information about cached models.
        
        Args:
            model_type: Optional filter for specific model type
            
        Returns:
            Dictionary with information about cached models
        """
        info = {
            'total_models': 0,
            'models': {}
        }
        
        async with self._lock:
            for key in self.stats.cache_keys:
                if ':' not in key:
                    continue
                    
                cached_type, model_id = key.split(':', 1)
                if model_type and cached_type != model_type:
                    continue
                    
                value = await self.get(key)
                if value:
                    if cached_type not in info['models']:
                        info['models'][cached_type] = []
                        
                    info['models'][cached_type].append({
                        'id': model_id,
                        'created_at': value['created_at'],
                        'metadata': value['metadata']
                    })
                    info['total_models'] += 1
                    
        return info
        
    async def cleanup(self) -> None:
        """Cleanup expired models and optimize storage."""
        await super().cleanup()
        
        # Additional model-specific cleanup
        async with self._lock:
            for key in list(self.stats.cache_keys):
                if ':' not in key:
                    continue
                    
                model_type, _ = key.split(':', 1)
                if model_type in self.model_configs:
                    value = await self.get(key)
                    if value:
                        config = self.model_configs[model_type]
                        if sys.getsizeof(value) > config['max_size']:
                            await self.delete(key)
                            logger.warning(f"Removed model {key} due to size limit")
                            
        self.stats.last_cleanup = datetime.now()

    async def clear_expired(self) -> None:
        """Clear expired entries from cache."""
        async with self._lock:
            for key in list(self.stats.cache_keys):
                if ':' not in key:
                    continue
                    
                model_type, _ = key.split(':', 1)
                if model_type in self.model_configs:
                    value = await self.get(key)
                    if value:
                        config = self.model_configs[model_type]
                        if sys.getsizeof(value) > config['max_size']:
                            await self.delete(key)
                            logger.warning(f"Removed model {key} due to size limit")
                            
        self.stats.last_cleanup = datetime.now()

# Create global instance but don't start background tasks yet
model_cache = ModelCache(
    disk_path=Path("data/cache/models")
)

# Initialize function for framework startup
async def initialize_caches():
    """Initialize all global cache instances."""
    try:
        # Ensure we have a valid event loop
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        # Start model cache
        await model_cache.start()
        
        # Register with container after initialization
        from tenire.core.container import container
        container.register_sync('model_cache', model_cache)
        
        logger.info("Cache initialization completed")
    except Exception as e:
        logger.error(f"Error initializing caches: {str(e)}")
        # Schedule retry
        loop = asyncio.get_event_loop()
        if not loop.is_closed():
            loop.call_later(1.0, lambda: asyncio.create_task(initialize_caches()))

# Register initialization with container
from tenire.core.container import container
container.register_lazy_sync(
    'cache_init',
    lambda: asyncio.create_task(initialize_caches()),
    dependencies={'compactor'}  # Only depend on compactor, not signal_manager
)

def cache_decorator(
    cache: CacheBackend,
    key_prefix: str = "",
    ttl: Optional[int] = None
):
    """
    Decorator for caching function results.
    
    Example:
        ```python
        cache = MemoryCache[str, Dict]()
        
        @cache_decorator(cache, key_prefix="user", ttl=3600)
        async def get_user_data(user_id: str) -> Dict:
            # Expensive operation
            return {"id": user_id, "data": "..."}
        ```
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Generate cache key
            key_parts = [key_prefix, func.__name__]
            key_parts.extend(str(arg) for arg in args)
            key_parts.extend(f"{k}:{v}" for k, v in sorted(kwargs.items()))
            cache_key = ":".join(key_parts)
            
            async with cache._acquire_lock():
                # Try to get from cache
                cached_value = await cache.get(cache_key)
                if cached_value is not None:
                    return cached_value
                
                # Call function and cache result
                result = await func(*args, **kwargs)
                await cache.set(cache_key, result, ttl=ttl)
                return result
        return wrapper
    return decorator
