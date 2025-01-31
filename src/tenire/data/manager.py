"""
High-level data management module.

This module provides centralized data management operations and coordinates between
different components like document store, embeddings, and retrieval systems.
"""

# Standard library imports
import asyncio
from typing import Dict, List, Optional, Any, Tuple
import time
from uuid import UUID
from concurrent.futures import ThreadPoolExecutor

# Third-party imports
import numpy as np
from pydantic import ValidationError
import faiss

# Local imports
from tenire.utils.logger import get_logger
from tenire.utils.optimizer import optimizer
from tenire.rag.docustore import DocumentStore
from tenire.rag.embeddings import LocalEmbeddings
from tenire.organizers.compactor import compactor
from tenire.servicers import SignalManager
from tenire.core.codex import Signal, SignalType
from tenire.organizers.concurrency import concurrency_manager
from tenire.data.validation.schemas import (
    BetRecord,
    CleanedBetRecord,
    CompressedBetRecord,
    BetAnalysis,
    GameType
)

logger = get_logger(__name__)

class DataManager:
    """
    Manages high-level data operations and coordinates between components.
    
    This class is responsible for:
    1. Data validation and processing
    2. Document storage and retrieval
    3. Batch operations and caching
    4. Thread-safe access to shared resources
    """
    
    def __init__(
        self,
        max_workers: Optional[int] = None,
        batch_size: Optional[int] = None,
        memory_config: Optional[Dict[str, Any]] = None,
        cache_size: int = 1000,
        prefetch_size: int = 5,
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
            use_tfidf: Whether to use TF-IDF for text search
            embedding_dim: Dimension of embeddings
        """
        self.max_workers = max_workers or optimizer.num_workers
        self.batch_size = batch_size or optimizer.batch_size
        self.memory_config = memory_config or optimizer.get_memory_config()
        self.cache_size = cache_size
        self.prefetch_size = prefetch_size
        self.use_tfidf = use_tfidf
        self.embedding_dim = embedding_dim
        
        # Register cleanup tasks with compactor
        compactor.register_cleanup_task(
            name=f"data_manager_cleanup_{id(self)}",
            cleanup_func=self.cleanup,
            priority=90,  # High priority to ensure data is saved
            is_async=True,
            metadata={"tags": ["data", "core"]}
        )
        
        # Register thread pool with compactor
        self._pool = ThreadPoolExecutor(max_workers=self.max_workers)
        compactor.register_thread_pool(self._pool)
        
        # Locks for thread safety
        self._index_lock = asyncio.Lock()
        self._embedding_lock = asyncio.Lock()
        self._tfidf_lock = asyncio.Lock()
        self._cache_lock = asyncio.Lock()
        self._component_lock = asyncio.Lock()
        
        # Document cache
        self._document_cache: Dict[UUID, CleanedBetRecord] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        
        # Queues for batch processing
        self._document_queue: asyncio.Queue[BetRecord] = asyncio.Queue()
        self._embedding_queue: asyncio.Queue[CleanedBetRecord] = asyncio.Queue()
        self._prefetch_queue: asyncio.Queue[List[CleanedBetRecord]] = asyncio.Queue(maxsize=prefetch_size)
        
        # Component references
        self._document_store: Optional[DocumentStore] = None
        self._embeddings: Optional[LocalEmbeddings] = None
        
        # Background tasks
        self._processing_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        
        # Metrics
        self._metrics = {
            'processed_documents': 0,
            'processed_batches': 0,
            'total_processing_time': 0.0,
            'cache_hits': 0,
            'cache_misses': 0,
            'failed_batches': 0
        }
        
        # Register signal handlers
        SignalManager().register_handler(
            SignalType.RAG_DOCUMENT_ADDED,
            self._handle_document_added,
            priority=90
        )
        SignalManager().register_handler(
            SignalType.RAG_CLEANUP_STARTED,
            self._handle_cleanup_started,
            priority=100
        )
        
        logger.info(f"Initialized DataManager with {self.max_workers} workers")
        
    async def process_documents(
        self,
        documents: List[Dict[str, Any]],
        validate: bool = True
    ) -> Tuple[List[CleanedBetRecord], np.ndarray]:
        """
        Process documents with validation and batching.
        
        Args:
            documents: List of documents to process
            validate: Whether to validate documents
            
        Returns:
            Tuple of (processed records, embeddings)
        """
        start_time = time.time()
        logger.info(f"Processing {len(documents)} documents")
        
        try:
            processed_records = []
            embeddings_list = []
            
            # Process in batches
            batches = [
                documents[i:i + self.batch_size]
                for i in range(0, len(documents), self.batch_size)
            ]
            
            for batch in batches:
                try:
                    # Validate and convert to BetRecords
                    if validate:
                        batch_records = [
                            BetRecord(**doc) for doc in batch
                        ]
                    else:
                        batch_records = [
                            BetRecord.model_construct(**doc) for doc in batch
                        ]
                        
                    # Process batch
                    batch_embeddings = await self._process_batch(batch_records)
                    if batch_embeddings is not None:
                        processed_records.extend(batch_records)
                        embeddings_list.append(batch_embeddings)
                        
                        await SignalManager().emit(Signal(
                            type=SignalType.RAG_BATCH_PROCESSED,
                            data={
                                'batch_size': len(batch),
                                'embeddings_shape': batch_embeddings.shape
                            },
                            source='data_manager'
                        ))
                        
                except ValidationError as e:
                    logger.warning(f"Validation error in batch: {str(e)}")
                    self._metrics['failed_batches'] += 1
                except Exception as e:
                    logger.error(f"Error processing batch: {str(e)}")
                    self._metrics['failed_batches'] += 1
                    
            # Update metrics
            processing_time = time.time() - start_time
            self._metrics.update({
                'processed_documents': self._metrics['processed_documents'] + len(processed_records),
                'processed_batches': self._metrics['processed_batches'] + len(batches),
                'total_processing_time': self._metrics['total_processing_time'] + processing_time
            })
            
            # Combine embeddings
            embeddings = np.vstack(embeddings_list) if embeddings_list else np.array([])
            
            await SignalManager().emit(Signal(
                type=SignalType.RAG_EMBEDDING_COMPLETED,
                data={
                    'num_documents': len(processed_records),
                    'processing_time': processing_time,
                    'metrics': self._metrics
                },
                source='data_manager'
            ))
            
            return processed_records, embeddings
            
        except Exception as e:
            logger.error(f"Error processing documents: {str(e)}")
            await SignalManager().emit(Signal(
                type=SignalType.RAG_COMPONENT_ERROR,
                data={'error': str(e)},
                source='data_manager'
            ))
            raise
            
    async def _process_batch(
        self,
        batch: List[BetRecord]
    ) -> Optional[np.ndarray]:
        """Process a batch of validated records."""
        try:
            # Check cache first
            cached_vectors = []
            uncached_records = []
            
            async with self._cache_lock:
                for record in batch:
                    if record.id in self._document_cache:
                        cached_vectors.append(self._document_cache[record.id])
                        self._cache_hits += 1
                    else:
                        uncached_records.append(record)
                        self._cache_misses += 1
                        
            # Generate embeddings for uncached records
            if uncached_records and self._embeddings:
                async with self._embedding_lock:
                    embeddings = await self._embeddings.embed_documents(uncached_records)
                    
                    # Update cache
                    async with self._cache_lock:
                        for record, vector in zip(uncached_records, embeddings):
                            self._document_cache[record.id] = vector
                            
                    cached_vectors.extend(embeddings)
                    
            # Combine vectors
            batch_vectors = np.vstack(cached_vectors) if cached_vectors else None
            
            return batch_vectors
            
        except Exception as e:
            logger.error(f"Error processing batch: {str(e)}")
            return None
            
    async def get_document(self, doc_id: UUID) -> Optional[CleanedBetRecord]:
        """Get a document by ID from cache or storage."""
        async with self._cache_lock:
            if doc_id in self._document_cache:
                self._cache_hits += 1
                return self._document_cache[doc_id]
                
        self._cache_misses += 1
        return None
        
    async def save_document(self, document: CleanedBetRecord) -> None:
        """Save a document to cache and storage."""
        async with self._cache_lock:
            self._document_cache[document.id] = document
            
    async def cleanup_cache(self) -> None:
        """Clean up the document cache."""
        async with self._cache_lock:
            if len(self._document_cache) > self.cache_size:
                # Remove least recently used documents
                to_remove = len(self._document_cache) - self.cache_size
                for _ in range(to_remove):
                    self._document_cache.popitem(last=False)
                    
    def get_metrics(self) -> Dict[str, Any]:
        """Get current metrics."""
        return {
            **self._metrics,
            'cache_size': len(self._document_cache),
            'cache_hits': self._cache_hits,
            'cache_misses': self._cache_misses
        }
        
    async def cleanup(self) -> None:
        """Cleanup all resources."""
        logger.info("Shutting down DataManager")
        
        # Cancel background tasks
        if self._processing_task:
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass
                
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
                
        # Process remaining items in queue
        while not self._document_queue.empty():
            try:
                record = self._document_queue.get_nowait()
                await self._process_batch([record])
            except asyncio.QueueEmpty:
                break
                
        # Cleanup components
        if self._document_store:
            await self._document_store.cleanup()
            
        if self._embeddings:
            await self._embeddings.cleanup()
            
        # Shutdown thread pool
        self._pool.shutdown(wait=True)
        
        # Clear caches
        self._document_cache.clear()
        
        # Force garbage collection
        compactor.force_garbage_collection()
        
        # Log final metrics
        logger.info(f"Final metrics: {self.get_metrics()}")
        logger.info("DataManager shutdown complete") 