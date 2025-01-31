"""
Data management module for concurrent operations.

This module provides the DataManager class for handling concurrent data operations,
including document processing, batch operations, and caching.
"""

# Standard library imports
import asyncio
import time
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

# Third-party imports
import numpy as np
import faiss

# Local imports
from tenire.utils.logger import get_logger
from tenire.utils.optimizer import optimizer
from tenire.core.event_loop import event_loop_manager

# Configure logging
logger = get_logger(__name__)

class AbstractDataManager(ABC):
    """Abstract base class for data management."""
    
    @abstractmethod
    async def process_documents(self, documents: List[Dict], embedder: Any, index: faiss.Index) -> List[np.ndarray]:
        """Process documents concurrently with batching and caching."""
        pass
        
    @abstractmethod
    async def update_index(self, index: faiss.Index, embeddings: np.ndarray, priority: bool = False) -> None:
        """Update FAISS index with new embeddings."""
        pass
        
    @abstractmethod
    async def search_index(self, index: faiss.Index, query_vector: np.ndarray, k: int, use_cache: bool = True) -> Tuple[np.ndarray, np.ndarray]:
        """Search FAISS index concurrently with caching."""
        pass
        
    @abstractmethod
    async def shutdown(self) -> None:
        """Shutdown the data manager."""
        pass

class DataManager(AbstractDataManager):
    """
    Manages concurrent data operations for RAG system.
    
    This class handles:
    - Concurrent document processing
    - Batch operations for embeddings
    - Memory-efficient data loading
    - Thread-safe index updates
    - Caching and prefetching
    """
    
    def __init__(
        self,
        max_workers: Optional[int] = None,
        batch_size: Optional[int] = None,
        memory_config: Optional[Dict[str, Any]] = None,
        cache_size: int = 1000,  # Number of documents to cache
        prefetch_size: int = 5,  # Number of batches to prefetch
        use_tfidf: bool = True,
        embedding_dim: int = 384
    ):
        """
        Initialize the data manager.
        
        Args:
            max_workers: Maximum number of worker threads
            batch_size: Size of batches for processing
            memory_config: Memory configuration settings
            cache_size: Number of documents to keep in cache
            prefetch_size: Number of batches to prefetch
            use_tfidf: Whether to use TF-IDF for embedding
            embedding_dim: Dimension of the embedding
        """
        self.max_workers = max_workers or optimizer.num_workers
        self.batch_size = batch_size or optimizer.batch_size
        self.memory_config = memory_config or optimizer.get_memory_config()
        self.cache_size = cache_size
        self.prefetch_size = prefetch_size
        self.use_tfidf = use_tfidf
        self.embedding_dim = embedding_dim
        
        # Thread pool for CPU-bound operations
        self._pool = ThreadPoolExecutor(max_workers=self.max_workers)
        
        # Locks for thread safety
        self._index_lock = asyncio.Lock()
        self._embedding_lock = asyncio.Lock()
        self._tfidf_lock = asyncio.Lock()
        self._cache_lock = asyncio.Lock()
        
        # Queues for batch processing
        self._document_queue = asyncio.Queue()
        self._embedding_queue = asyncio.Queue()
        self._prefetch_queue = asyncio.Queue(maxsize=prefetch_size)
        
        # Cache for frequently accessed documents
        self._document_cache: Dict[str, Dict] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        
        # Metrics and monitoring
        self._metrics = {
            'processed_documents': 0,
            'processed_batches': 0,
            'total_processing_time': 0.0,
            'cache_hits': 0,
            'cache_misses': 0,
            'failed_batches': 0
        }
        
        # Start background tasks
        self._prefetch_task = None
        self._cleanup_task = None
        self._start_background_tasks()
        
        logger.info(f"Initialized DataManager with {self.max_workers} workers")
        
    def _start_background_tasks(self):
        """Start background tasks for prefetching and cleanup."""
        loop = asyncio.get_event_loop()
        self._prefetch_task = loop.create_task(self._prefetch_loop())
        self._cleanup_task = loop.create_task(self._cleanup_loop())
        
        # Track tasks
        async def track_tasks():
            await event_loop_manager.track_task(self._prefetch_task)
            await event_loop_manager.track_task(self._cleanup_task)
        
        # Create tracking task
        loop.create_task(track_tasks())
        
    async def _prefetch_loop(self):
        """Background task for prefetching batches."""
        try:
            while True:
                if self._prefetch_queue.qsize() < self.prefetch_size:
                    try:
                        batch = await self._get_next_batch()
                        if batch:
                            await self._prefetch_queue.put(batch)
                    except asyncio.QueueEmpty:
                        pass
                await asyncio.sleep(0.1)  # Prevent busy waiting
        except asyncio.CancelledError:
            logger.debug("Prefetch loop cancelled")
            
    async def _cleanup_loop(self):
        """Background task for cache cleanup."""
        try:
            while True:
                await self._cleanup_cache()
                await asyncio.sleep(60)  # Run cleanup every minute
        except asyncio.CancelledError:
            logger.debug("Cleanup loop cancelled")
            
    async def _cleanup_cache(self):
        """Clean up the document cache when it exceeds the size limit."""
        async with self._cache_lock:
            if len(self._document_cache) > self.cache_size:
                # Remove least recently used documents
                sorted_cache = sorted(
                    self._document_cache.items(),
                    key=lambda x: x[1].get('last_access', 0)
                )
                to_remove = len(self._document_cache) - self.cache_size
                for doc_id, _ in sorted_cache[:to_remove]:
                    del self._document_cache[doc_id]
                logger.debug(f"Cleaned up {to_remove} documents from cache")
                
    async def process_documents(
        self,
        documents: List[Dict],
        embedder: Any,
        index: faiss.Index
    ) -> List[np.ndarray]:
        """
        Process documents concurrently with batching and caching.
        
        Args:
            documents: List of documents to process
            embedder: Embedding model to use
            index: FAISS index to update
            
        Returns:
            List of document embeddings
        """
        start_time = time.time()
        logger.info(f"Processing {len(documents)} documents")
        
        # Add documents to queue
        for doc in documents:
            await self._document_queue.put(doc)
            
        # Split documents into batches
        batches = [
            documents[i:i + self.batch_size]
            for i in range(0, len(documents), self.batch_size)
        ]
        
        # Process batches concurrently
        async def process_batch(batch: List[Dict]) -> np.ndarray:
            try:
                # Check cache first
                cached_vectors = []
                uncached_docs = []
                
                async with self._cache_lock:
                    for doc in batch:
                        doc_id = doc.get('id')
                        if doc_id and doc_id in self._document_cache:
                            cached_vectors.append(self._document_cache[doc_id]['vector'])
                            self._cache_hits += 1
                        else:
                            uncached_docs.append(doc)
                            self._cache_misses += 1
                            
                # Generate embeddings for uncached documents
                if uncached_docs:
                    async with self._embedding_lock:
                        embeddings_dict = await asyncio.get_event_loop().run_in_executor(
                            self._pool,
                            embedder.embed_documents,
                            uncached_docs
                        )
                        
                        # Update cache with new embeddings
                        async with self._cache_lock:
                            for doc, vector in zip(uncached_docs, embeddings_dict['vector']):
                                doc_id = doc.get('id')
                                if doc_id:
                                    self._document_cache[doc_id] = {
                                        'vector': vector,
                                        'last_access': time.time()
                                    }
                                    
                        cached_vectors.extend(embeddings_dict['vector'])
                        
                # Combine cached and new vectors
                batch_vectors = np.vstack(cached_vectors) if cached_vectors else None
                
                # Update index if we have vectors
                if batch_vectors is not None:
                    async with self._index_lock:
                        index.add(batch_vectors)
                        
                return batch_vectors
                
            except Exception as e:
                logger.error(f"Error processing batch: {str(e)}")
                self._metrics['failed_batches'] += 1
                raise
                
        # Process all batches
        tasks = [process_batch(batch) for batch in batches]
        results = await asyncio.gather(*tasks)
        
        # Update metrics
        processing_time = time.time() - start_time
        self._metrics.update({
            'processed_documents': self._metrics['processed_documents'] + len(documents),
            'processed_batches': self._metrics['processed_batches'] + len(batches),
            'total_processing_time': self._metrics['total_processing_time'] + processing_time
        })
        
        return np.vstack([r for r in results if r is not None])
        
    async def update_index(
        self,
        index: faiss.Index,
        embeddings: np.ndarray,
        priority: bool = False
    ) -> None:
        """
        Update FAISS index with new embeddings.
        
        Args:
            index: FAISS index to update
            embeddings: New embeddings to add
            priority: Whether this is a priority update
        """
        async with self._index_lock:
            if priority:
                # Priority updates are processed immediately
                await asyncio.get_event_loop().run_in_executor(
                    self._pool,
                    index.add,
                    embeddings
                )
            else:
                # Non-priority updates go through the queue
                await self._embedding_queue.put((index, embeddings))
                
    async def search_index(
        self,
        index: faiss.Index,
        query_vector: np.ndarray,
        k: int,
        use_cache: bool = True
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Search FAISS index concurrently with caching.
        
        Args:
            index: FAISS index to search
            query_vector: Query vector
            k: Number of results to return
            use_cache: Whether to use the cache
            
        Returns:
            Tuple of (distances, indices)
        """
        # Check cache if enabled
        cache_key = None
        if use_cache:
            cache_key = hash(query_vector.tobytes())
            async with self._cache_lock:
                cached_result = self._document_cache.get(cache_key)
                if cached_result:
                    self._cache_hits += 1
                    return cached_result['distances'], cached_result['indices']
                self._cache_misses += 1
                
        # Perform search
        async with self._index_lock:
            result = await asyncio.get_event_loop().run_in_executor(
                self._pool,
                lambda: index.search(query_vector, k)
            )
            
        # Cache result if enabled
        if use_cache and cache_key:
            async with self._cache_lock:
                self._document_cache[cache_key] = {
                    'distances': result[0],
                    'indices': result[1],
                    'last_access': time.time()
                }
                
        return result
        
    async def _get_next_batch(self) -> Optional[List[Dict]]:
        """Get the next batch of documents from the queue."""
        if self._document_queue.empty():
            return None
            
        batch = []
        try:
            while len(batch) < self.batch_size:
                batch.append(await asyncio.wait_for(
                    self._document_queue.get(),
                    timeout=0.1
                ))
        except (asyncio.TimeoutError, asyncio.QueueEmpty):
            pass
            
        return batch if batch else None
        
    async def _process_document_queue(self):
        """Process documents in the queue."""
        batch = await self._get_next_batch()
        if batch:
            logger.debug(f"Processing batch of {len(batch)} documents")
            await self._process_batch(batch)
            
    async def _process_embedding_queue(self):
        """Process embeddings in the queue."""
        if self._embedding_queue.empty():
            return
            
        try:
            index, embeddings = await self._embedding_queue.get_nowait()
            await self.update_index(index, embeddings, priority=True)
        except asyncio.QueueEmpty:
            pass
            
    async def _process_batch(self, batch: List[Dict]):
        """Process a batch of documents."""
        try:
            # Implement specific batch processing logic here
            # This will be called by the document queue processor
            pass
        except Exception as e:
            logger.error(f"Error processing batch: {str(e)}")
            self._metrics['failed_batches'] += 1
            raise
            
    def get_metrics(self) -> Dict[str, Any]:
        """Get current metrics."""
        metrics = self._metrics.copy()
        metrics.update({
            'cache_size': len(self._document_cache),
            'cache_hits': self._cache_hits,
            'cache_misses': self._cache_misses,
            'queue_sizes': {
                'document': self._document_queue.qsize(),
                'embedding': self._embedding_queue.qsize(),
                'prefetch': self._prefetch_queue.qsize()
            }
        })
        return metrics
        
    async def shutdown(self):
        """Shutdown the data manager."""
        logger.info("Shutting down DataManager")
        
        # Cancel background tasks
        if self._prefetch_task:
            self._prefetch_task.cancel()
            try:
                await self._prefetch_task
            except asyncio.CancelledError:
                pass
                
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
                
        # Process remaining items
        try:
            while not self._document_queue.empty():
                await self._process_document_queue()
                
            while not self._embedding_queue.empty():
                await self._process_embedding_queue()
        except Exception as e:
            logger.error(f"Error during final batch processing: {str(e)}")
            
        # Shutdown thread pool
        self._pool.shutdown(wait=True)
        
        # Clear caches
        self._document_cache.clear()
        
        # Log final metrics
        logger.info(f"Final metrics: {self.get_metrics()}")
        logger.info("DataManager shutdown complete")
