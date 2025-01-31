"""
Data cleaner module for parsing and cleaning bet data.

This module provides components for validating, cleaning, and transforming bet data
through a pipeline of specialized processors.
"""

# Standard library imports
import asyncio
import json
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple, Protocol
from dataclasses import dataclass
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor

# Local imports
from tenire.organizers.concurrency import ConcurrencyManager
from tenire.core.codex import SignalType, Signal
from tenire.servicers import get_signal_manager
from tenire.organizers.compactor import compactor
from tenire.utils.logger import get_logger
from tenire.data.validation.schemas import BetRecord, CleanedBetRecord, SchemaValidator

logger = get_logger(__name__)

class CleanerError(Exception):
    """Base exception for cleaner errors."""
    pass

class ValidationError(CleanerError):
    """Raised when data validation fails."""
    pass

class ProcessingError(CleanerError):
    """Raised when data processing fails."""
    pass

@dataclass
class ProcessingMetrics:
    """Metrics for data processing."""
    total_records: int = 0
    valid_records: int = 0
    invalid_records: int = 0
    processing_time: float = 0.0
    error_count: int = 0
    last_error: Optional[str] = None
    last_processed: Optional[datetime] = None

class DataProcessor(Protocol):
    """Protocol for data processors in the pipeline."""
    
    @abstractmethod
    async def process(self, data: Any) -> Any:
        """Process a single data item."""
        pass
        
    @abstractmethod
    async def cleanup(self) -> None:
        """Cleanup processor resources."""
        pass

class BaseProcessor(ABC):
    """Base class for data processors."""
    
    def __init__(self):
        self.metrics = ProcessingMetrics()
        self._lock = asyncio.Lock()
        
    async def _update_metrics(self, **kwargs):
        """Update processor metrics."""
        async with self._lock:
            for key, value in kwargs.items():
                if hasattr(self.metrics, key):
                    setattr(self.metrics, key, value)
            self.metrics.last_processed = datetime.now(timezone.utc)

class ValidationProcessor(BaseProcessor):
    """Validates bet data against schemas."""
    
    def __init__(self, validator: Optional[SchemaValidator] = None):
        super().__init__()
        self.validator = validator or SchemaValidator()
        
    async def process(self, data: Dict[str, Any]) -> Optional[BetRecord]:
        """Validate a single bet record."""
        try:
            result = await self.validator.validate(data, 'bet_record')
            if result:
                await self._update_metrics(valid_records=self.metrics.valid_records + 1)
                return result
            await self._update_metrics(invalid_records=self.metrics.invalid_records + 1)
            return None
        except Exception as e:
            await self._update_metrics(
                error_count=self.metrics.error_count + 1,
                last_error=str(e)
            )
            raise ValidationError(f"Validation failed: {str(e)}")
            
    async def cleanup(self) -> None:
        """Cleanup validation resources."""
        if hasattr(self.validator, 'cleanup'):
            await self.validator.cleanup()

class TransformationProcessor(BaseProcessor):
    """Transforms validated records into cleaned format."""
    
    async def process(self, record: BetRecord) -> Optional[CleanedBetRecord]:
        """Transform a validated record."""
        try:
            # Create cleaned record with metadata
            cleaned_record = CleanedBetRecord(
                **record.model_dump(),
                metadata={
                    'cleaned_at': datetime.now(timezone.utc).isoformat(),
                    'original_size': len(json.dumps(record.model_dump())),
                    'cleaning_version': '1.0'
                }
            )
            await self._update_metrics(total_records=self.metrics.total_records + 1)
            return cleaned_record
        except Exception as e:
            await self._update_metrics(
                error_count=self.metrics.error_count + 1,
                last_error=str(e)
            )
            raise ProcessingError(f"Transformation failed: {str(e)}")
            
    async def cleanup(self) -> None:
        """Cleanup transformation resources."""
        pass

class BetDataCleaner:
    """
    Coordinates data cleaning pipeline.
    
    This class manages the sequence of processors that validate,
    clean, and transform bet data.
    """
    
    def __init__(
        self,
        max_workers: Optional[int] = None,
        validator: Optional[SchemaValidator] = None,
        batch_size: Optional[int] = None
    ):
        """Initialize the data cleaner."""
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.batch_size = batch_size or ConcurrencyManager.data_manager.batch_size
        
        # Initialize processors
        self.validation_processor = ValidationProcessor(validator)
        self.transformation_processor = TransformationProcessor()
        
        # Locks for thread safety
        self._cleaning_lock = asyncio.Lock()
        self._batch_lock = asyncio.Lock()
        
        # Queue for batch processing
        self._record_queue = asyncio.Queue()
        
        # Register cleanup tasks with compactor
        compactor.register_cleanup_task(
            name=f"cleaner_cleanup_{id(self)}",
            cleanup_func=self.cleanup,
            priority=85,
            is_async=True,
            metadata={"tags": ["data", "cleaning"]}
        )
        
        # Register with concurrency manager's data manager
        ConcurrencyManager.data_manager._document_cache['cleaner'] = self
        
        # Start background task for batch processing
        self._processing_task = asyncio.create_task(self._process_queue_loop())
        
    async def cleanup(self) -> None:
        """Cleanup cleaner resources."""
        try:
            logger.info("Cleaning up cleaner resources")
            
            # Cancel background task
            if self._processing_task:
                self._processing_task.cancel()
                try:
                    await self._processing_task
                except asyncio.CancelledError:
                    pass
            
            # Process remaining items in queue
            while not self._record_queue.empty():
                batch = await self._get_next_batch()
                if batch:
                    await self._process_batch(batch)
            
            # Cleanup processors
            await self.validation_processor.cleanup()
            await self.transformation_processor.cleanup()
            
            # Shutdown thread pool
            self.executor.shutdown(wait=True)
            
            # Force garbage collection
            compactor.force_garbage_collection()
            
        except Exception as e:
            logger.error(f"Error cleaning up cleaner: {str(e)}")
            raise CleanerError(f"Cleanup failed: {str(e)}")
            
    async def _process_queue_loop(self):
        """Background task for processing record queue."""
        try:
            while True:
                if not self._record_queue.empty():
                    batch = await self._get_next_batch()
                    if batch:
                        await self._process_batch(batch)
                await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            logger.debug("Processing loop cancelled")
            
    async def _get_next_batch(self) -> Optional[List[Dict[str, Any]]]:
        """Get next batch of records from queue."""
        async with self._batch_lock:
            if self._record_queue.empty():
                return None
                
            batch = []
            try:
                while len(batch) < self.batch_size:
                    batch.append(await asyncio.wait_for(
                        self._record_queue.get(),
                        timeout=0.1
                    ))
            except (asyncio.TimeoutError, asyncio.QueueEmpty):
                pass
                
            return batch if batch else None
            
    async def _process_batch(self, batch: List[Dict[str, Any]]) -> None:
        """Process a batch of records through the pipeline."""
        try:
            # First validate records
            validated_records = []
            for record in batch:
                try:
                    validated = await self.validation_processor.process(record)
                    if validated:
                        validated_records.append(validated)
                except ValidationError as e:
                    logger.warning(f"Validation error: {str(e)}")
                    continue
                    
            # Then transform valid records
            cleaned_records = []
            for record in validated_records:
                try:
                    cleaned = await self.transformation_processor.process(record)
                    if cleaned:
                        cleaned_records.append(cleaned)
                except ProcessingError as e:
                    logger.warning(f"Processing error: {str(e)}")
                    continue
                    
            # Update concurrency manager's cache
            async with ConcurrencyManager.data_manager._cache_lock:
                for record in cleaned_records:
                    ConcurrencyManager.data_manager._document_cache[str(record.id)] = {
                        'record': record,
                        'last_access': datetime.now(timezone.utc).timestamp()
                    }
                    
            # Emit batch processed signal
            await get_signal_manager().emit(Signal(
                type=SignalType.RAG_BATCH_PROCESSED,
                data={
                    'valid_records': len(cleaned_records),
                    'invalid_records': len(batch) - len(cleaned_records),
                    'batch_size': len(batch)
                },
                source='cleaner'
            ))
            
        except Exception as e:
            logger.error(f"Error processing batch: {str(e)}")
            
    async def clean_bet_records(
        self,
        records: List[Dict[str, Any]]
    ) -> Tuple[List[CleanedBetRecord], List[Dict[str, Any]]]:
        """
        Clean and validate a list of bet records in parallel.
        
        Args:
            records: List of raw bet records
            
        Returns:
            Tuple of (cleaned_records, invalid_records)
        """
        async with self._cleaning_lock:
            try:
                # Process records through pipeline
                valid_records = []
                invalid_records = []
                
                for record in records:
                    try:
                        # Validate
                        validated = await self.validation_processor.process(record)
                        if not validated:
                            invalid_records.append(record)
                            continue
                            
                        # Transform
                        cleaned = await self.transformation_processor.process(validated)
                        if cleaned:
                            valid_records.append(cleaned)
                        else:
                            invalid_records.append(record)
                            
                    except (ValidationError, ProcessingError) as e:
                        logger.warning(f"Processing error for record: {str(e)}")
                        invalid_records.append(record)
                        
                # Emit cleaning completed signal
                await get_signal_manager().emit(Signal(
                    type=SignalType.RAG_DOCUMENT_ADDED,
                    data={
                        'num_records': len(valid_records),
                        'num_invalid': len(invalid_records)
                    },
                    source='cleaner'
                ))
                
                return valid_records, invalid_records
                
            except Exception as e:
                logger.error(f"Error cleaning bet records: {str(e)}")
                await get_signal_manager().emit(Signal(
                    type=SignalType.RAG_COMPONENT_ERROR,
                    data={'error': str(e)},
                    source='cleaner'
                ))
                raise CleanerError(f"Cleaning failed: {str(e)}")
                
    async def add_records(self, records: List[Dict[str, Any]]) -> None:
        """Add records to processing queue."""
        for record in records:
            await self._record_queue.put(record)
            
    def __enter__(self):
        """Context manager entry."""
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.executor.shutdown(wait=True) 