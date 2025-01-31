"""
Compressor module for efficient bet data compression.

This module provides flexible compression strategies and efficient batch processing
for bet data compression and decompression.
"""

# Standard library imports
import json
import os
import asyncio
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Tuple, Protocol
from dataclasses import dataclass
from abc import ABC, abstractmethod
from pathlib import Path

# Third-party imports
import lz4.frame
import zstandard as zstd

# Local imports
from tenire.utils.logger import get_logger
from tenire.core.codex import Signal, SignalType
from tenire.servicers import SignalManager
from tenire.organizers.compactor import compactor
from tenire.data.validation.schemas import CleanedBetRecord, CompressedBetRecord
from .ende import BetDataCodec

logger = get_logger(__name__)

class CompressionError(Exception):
    """Base exception for compression errors."""
    pass

class CompressionStrategyError(CompressionError):
    """Raised when compression strategy fails."""
    pass

@dataclass
class CompressionStats:
    """Statistics for compression operations."""
    original_size: int = 0
    compressed_size: int = 0
    compression_ratio: float = 0.0
    compression_time: float = 0.0
    num_records: int = 0
    error_count: int = 0
    last_error: Optional[str] = None

class CompressionStrategy(Protocol):
    """Protocol for compression strategies."""
    
    @abstractmethod
    async def compress(self, data: bytes) -> bytes:
        """Compress data."""
        pass
        
    @abstractmethod
    async def decompress(self, data: bytes) -> bytes:
        """Decompress data."""
        pass
        
    @abstractmethod
    async def cleanup(self) -> None:
        """Cleanup strategy resources."""
        pass

class ZstdStrategy:
    """Zstandard compression strategy."""
    
    def __init__(self, level: int = 3):
        self.compressor = zstd.ZstdCompressor(level=level)
        self.decompressor = zstd.ZstdDecompressor()
        
    async def compress(self, data: bytes) -> bytes:
        """Compress data using zstd."""
        try:
            return self.compressor.compress(data)
        except Exception as e:
            raise CompressionStrategyError(f"Zstd compression failed: {str(e)}")
            
    async def decompress(self, data: bytes) -> bytes:
        """Decompress data using zstd."""
        try:
            return self.decompressor.decompress(data)
        except Exception as e:
            raise CompressionStrategyError(f"Zstd decompression failed: {str(e)}")
            
    async def cleanup(self) -> None:
        """Cleanup zstd resources."""
        self.compressor = None
        self.decompressor = None

class Lz4Strategy:
    """LZ4 compression strategy."""
    
    def __init__(self, level: int = 3):
        self.compression_level = level
        
    async def compress(self, data: bytes) -> bytes:
        """Compress data using lz4."""
        try:
            return lz4.frame.compress(data, compression_level=self.compression_level)
        except Exception as e:
            raise CompressionStrategyError(f"LZ4 compression failed: {str(e)}")
            
    async def decompress(self, data: bytes) -> bytes:
        """Decompress data using lz4."""
        try:
            return lz4.frame.decompress(data)
        except Exception as e:
            raise CompressionStrategyError(f"LZ4 decompression failed: {str(e)}")
            
    async def cleanup(self) -> None:
        """Cleanup lz4 resources."""
        pass

class BetDataCompressor:
    """
    Handles compression of bet data using various algorithms.
    
    This class provides:
    1. Multiple compression strategies (zstd, lz4)
    2. Batch processing with progress tracking
    3. Compression statistics and monitoring
    4. Error handling and recovery
    """
    
    def __init__(
        self,
        compression_level: int = 3,
        compression_method: str = 'zstd',
        chunk_size: int = 1000
    ):
        """Initialize the compressor."""
        self.compression_level = compression_level
        self.compression_method = compression_method
        self.chunk_size = chunk_size
        
        # Initialize compression strategy
        self.strategy = self._create_strategy(compression_method, compression_level)
        
        # Statistics
        self.stats = CompressionStats()
        
        # Locks
        self._compression_lock = asyncio.Lock()
        self._stats_lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()
        self._is_cleaned_up = False
        self._is_registered = False
        
    def _create_strategy(
        self,
        method: str,
        level: int
    ) -> CompressionStrategy:
        """Create compression strategy based on method."""
        if method == 'zstd':
            return ZstdStrategy(level=level)
        elif method == 'lz4':
            return Lz4Strategy(level=level)
        else:
            raise ValueError(f"Unsupported compression method: {method}")
            
    async def _update_stats(self, **kwargs):
        """Update compression statistics."""
        async with self._stats_lock:
            for key, value in kwargs.items():
                if hasattr(self.stats, key):
                    setattr(self.stats, key, value)
                    
    def _register_cleanup(self):
        """Register cleanup tasks with compactor only when needed."""
        if not self._is_registered:
            from tenire.organizers.compactor import compactor
            compactor.register_cleanup_task(
                name=f"compressor_cleanup_{id(self)}",
                cleanup_func=self.cleanup,
                priority=80,
                is_async=True,
                metadata={"tags": ["data", "compression"]}
            )
            self._is_registered = True

    async def compress_records(
        self,
        records: List[CleanedBetRecord],
        output_dir: str
    ) -> List[str]:
        """
        Compress a list of bet records into chunks.
        
        Args:
            records: List of cleaned bet records to compress
            output_dir: Directory to save compressed chunks
            
        Returns:
            List of paths to compressed chunk files
        """
        # Register cleanup when first used
        self._register_cleanup()
        
        try:
            # Create output directory
            output_path = Path(output_dir)
            output_path.mkdir(parents=True, exist_ok=True)
            
            # Split records into chunks
            chunks = [
                records[i:i + self.chunk_size]
                for i in range(0, len(records), self.chunk_size)
            ]
            
            # Compress chunks in parallel
            compressed_paths = []
            for i, chunk in enumerate(chunks):
                chunk_path = output_path / f"chunk_{i}_{datetime.now(timezone.utc):%Y%m%d_%H%M%S}.{self.compression_method}"
                
                # Compress chunk
                await self._compress_chunk(chunk, chunk_path)
                compressed_paths.append(str(chunk_path))
                
                # Emit compression progress signal
                await SignalManager().emit(Signal(
                    type=SignalType.RAG_COMPONENT_INITIALIZED,
                    data={
                        'operation': 'compress',
                        'chunk': i + 1,
                        'total_chunks': len(chunks),
                        'records_processed': (i + 1) * self.chunk_size,
                        'compression_stats': self.stats.__dict__
                    },
                    source='compressor'
                ))
                
            return compressed_paths
            
        except Exception as e:
            logger.error(f"Error compressing records: {str(e)}")
            await SignalManager().emit(Signal(
                type=SignalType.RAG_COMPONENT_ERROR,
                data={'error': str(e)},
                source='compressor'
            ))
            raise CompressionError(f"Compression failed: {str(e)}")
            
    async def _compress_chunk(
        self,
        chunk: List[CleanedBetRecord],
        output_path: Path
    ) -> None:
        """Compress a single chunk of records."""
        try:
            # Convert records to dict for encoding
            records_dict = [record.model_dump() for record in chunk]
            
            # First encode records using BetDataCodec (without compression)
            encoded_records = [
                BetDataCodec.encode_bet_data(record, compress=False)
                for record in records_dict
            ]
            
            # Join encoded records with newlines
            data = '\n'.join(encoded_records).encode('utf-8')
            
            async with self._compression_lock:
                # Compress using selected strategy
                compressed = await self.strategy.compress(data)
                
                # Create compressed record
                compressed_record = CompressedBetRecord(
                    id=chunk[0].id,  # Use first record's ID as chunk ID
                    compressed_data=compressed.hex(),
                    compression_ratio=len(compressed) / len(data),
                    original_size=len(data),
                    compressed_size=len(compressed),
                    created_at=datetime.now(timezone.utc),
                    compression_method=self.compression_method
                )
                
                # Update statistics
                await self._update_stats(
                    original_size=self.stats.original_size + len(data),
                    compressed_size=self.stats.compressed_size + len(compressed),
                    compression_ratio=compressed_record.compression_ratio,
                    num_records=self.stats.num_records + len(chunk)
                )
                
                # Write compressed data
                output_path.write_bytes(compressed)
                
            logger.info(
                f"Compressed chunk of {len(chunk)} records. "
                f"Compression ratio: {compressed_record.compression_ratio:.2f}"
            )
            
        except Exception as e:
            await self._update_stats(
                error_count=self.stats.error_count + 1,
                last_error=str(e)
            )
            raise CompressionStrategyError(f"Chunk compression failed: {str(e)}")
            
    async def decompress_chunk(self, chunk_path: str) -> List[CleanedBetRecord]:
        """
        Decompress a chunk file back into records.
        
        Args:
            chunk_path: Path to compressed chunk file
            
        Returns:
            List of decompressed records
        """
        try:
            # Read compressed data
            chunk_path = Path(chunk_path)
            compressed_data = chunk_path.read_bytes()
            
            async with self._compression_lock:
                # Decompress using selected strategy
                decompressed = await self.strategy.decompress(compressed_data)
                
            # Split into records and decode
            encoded_records = decompressed.decode('utf-8').split('\n')
            records_dict = [
                BetDataCodec.decode_bet_data(encoded, decompress=False)
                for encoded in encoded_records
            ]
            
            # Convert back to CleanedBetRecord objects
            return [CleanedBetRecord(**record) for record in records_dict]
            
        except Exception as e:
            logger.error(f"Error decompressing chunk {chunk_path}: {str(e)}")
            raise CompressionError(f"Decompression failed: {str(e)}")
            
    async def cleanup(self) -> None:
        """Cleanup compressor resources with proper error handling."""
        async with self._cleanup_lock:
            if self._is_cleaned_up:
                return
                
            try:
                logger.info("Cleaning up compressor resources")
                
                # Cleanup compression strategy
                if self.strategy:
                    await self.strategy.cleanup()
                    self.strategy = None
                
                # Clear statistics
                self.stats = CompressionStats()
                
                # Clear locks
                self._compression_lock = None
                self._stats_lock = None
                
                # Mark as cleaned up
                self._is_cleaned_up = True
                
                logger.info("Compressor cleanup completed successfully")
                
            except Exception as e:
                logger.error(f"Error cleaning up compressor: {str(e)}")
                raise CompressionError(f"Cleanup failed: {str(e)}")
            
    def get_compression_stats(self) -> Dict[str, Any]:
        """Get current compression statistics."""
        return self.stats.__dict__ 