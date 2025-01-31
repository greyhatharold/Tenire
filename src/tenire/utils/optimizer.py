"""
Performance optimizer for Apple Silicon M4 Max.

This module configures system settings and ML frameworks for optimal performance
on Apple Silicon, particularly leveraging the M4 Max's Neural Engine and Metal
Performance Shaders (MPS).
"""

import os
import platform
import faiss
import torch
import numpy as np
from typing import Dict, Any
from tenire.utils.logger import get_logger

logger = get_logger(__name__)

class M4Optimizer:
    """Optimizes system and ML framework settings for M4 Max performance."""
    
    def __init__(
        self,
        use_mps: bool = True,
        batch_size: int = 32,
        num_workers: int = 12,  # Matches performance cores
        memory_fraction: float = 0.7,  # Fraction of system memory to use
        enable_metal_optimizations: bool = True
    ):
        """
        Initialize the optimizer.
        
        Args:
            use_mps: Whether to use Metal Performance Shaders
            batch_size: Batch size for ML operations
            num_workers: Number of worker processes for data loading
            memory_fraction: Fraction of system memory to use
            enable_metal_optimizations: Whether to enable Metal-specific optimizations
        """
        self.system_info = self._get_system_info()
        self.use_mps = use_mps and self.system_info['has_mps']
        self.batch_size = batch_size
        self.num_workers = num_workers
        self.memory_fraction = memory_fraction
        self.enable_metal_optimizations = enable_metal_optimizations
        
        logger.info(f"Initializing M4 Optimizer with MPS={self.use_mps}")
        
    def _get_system_info(self) -> Dict[str, Any]:
        """Get system information and capabilities."""
        info = {
            'platform': platform.platform(),
            'processor': platform.processor(),
            'memory_gb': os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES') / (1024.**3),
            'has_mps': torch.backends.mps.is_available(),
            'is_apple_silicon': platform.processor() == 'arm',
            'num_cores': os.cpu_count()
        }
        
        logger.debug(f"System info: {info}")
        return info
        
    def optimize_torch(self) -> None:
        """Configure PyTorch for optimal performance."""
        if self.use_mps and torch.backends.mps.is_available() and torch.backends.mps.is_built():
            # Enable MPS device
            torch.set_default_device('mps')
            
            # Enable fallback for operations not supported by MPS
            torch.backends.mps.enable_fallback_to_cpu = True
            
            # Configure memory allocation
            memory_bytes = int(self.system_info['memory_gb'] * 1024 * 1024 * 1024 * self.memory_fraction)
            
            # Enable Metal optimizations if requested
            if self.enable_metal_optimizations:
                os.environ['PYTORCH_ENABLE_MPS_FALLBACK'] = '1'
                os.environ['MPS_USE_GUARD_MODE'] = '1'
            
            logger.info("Configured PyTorch with MPS device and optimizations")
        else:
            torch.set_default_device('cpu')
            logger.info("Configured PyTorch with CPU device")
            
    def optimize_numpy(self) -> None:
        """Configure NumPy for optimal performance."""
        # Enable Metal backend for numpy operations
        if self.use_mps:
            os.environ['NUMPY_METAL_ENABLED'] = '1'
            
        # Set number of threads for parallel operations
        np.set_num_threads(self.num_workers)
        
        logger.info(f"Configured NumPy with {self.num_workers} threads")
        
    def get_dataloader_config(self) -> Dict[str, Any]:
        """
        Get optimized configuration for data loaders.
        
        Returns:
            Dictionary of optimized data loader settings
        """
        return {
            'batch_size': self.batch_size,
            'num_workers': self.num_workers,
            'pin_memory': True,
            'prefetch_factor': 2,
            'persistent_workers': True
        }
        
    def optimize_sentence_transformers(self, model) -> None:
        """
        Optimize a sentence-transformers model for M4 Max.
        
        Args:
            model: SentenceTransformer model to optimize
        """
        if self.use_mps and torch.backends.mps.is_available() and torch.backends.mps.is_built():
            try:
                # Move model to MPS device
                model.to('mps')
                
                # Enable model compilation with fallback
                if self.enable_metal_optimizations:
                    try:
                        model.encode = torch.compile(
                            model.encode,
                            backend="aot_eager",  # Use AOT compilation instead of MPS backend
                            mode="reduce-overhead"
                        )
                    except Exception as e:
                        logger.warning(f"Model compilation failed, falling back to uncompiled: {str(e)}")
                
                logger.info("Using MPS for SentenceTransformer with optimizations")
            except Exception as e:
                logger.warning(f"Failed to move model to MPS, falling back to CPU: {str(e)}")
                model.to('cpu')
        else:
            model.to('cpu')
            logger.info("Using CPU for SentenceTransformer")
        
    def optimize_faiss(self, index) -> None:
        """
        Optimize a FAISS index for M4 Max.
        
        Args:
            index: FAISS index to optimize
        """
        if self.use_mps:
            # Configure FAISS to use Metal
            os.environ['FAISS_USE_METAL'] = '1'
            
            # Set number of threads for CPU operations
            faiss.omp_set_num_threads(self.num_workers)
            
        logger.info("Optimized FAISS index")
        
    def get_cross_encoder_config(self) -> Dict[str, Any]:
        """
        Get optimized configuration for cross-encoder.
        
        Returns:
            Dictionary of optimized cross-encoder settings
        """
        return {
            'device': 'mps' if self.use_mps else 'cpu',
            'batch_size': self.batch_size,
            'max_length': 512,  # Optimal for M4 Max memory architecture
            'use_amp': True  # Use automatic mixed precision
        }
        
    def optimize_all(self) -> None:
        """Apply all optimizations."""
        logger.info("Applying all optimizations")
        
        # Set environment variables
        os.environ['PYTORCH_ENABLE_MPS_FALLBACK'] = '1'
        os.environ['MPS_USE_GUARD_MODE'] = '1'  # Enhanced error checking
        
        # Apply framework-specific optimizations
        self.optimize_torch()
        self.optimize_numpy()
        
        logger.info("All optimizations applied")
        
    @property
    def device(self) -> str:
        """Get the optimal device for tensor operations."""
        return 'mps' if self.use_mps else 'cpu'
        
    def get_memory_config(self) -> Dict[str, Any]:
        """
        Get memory-related configuration.
        
        Returns:
            Dictionary of memory settings
        """
        total_memory = self.system_info['memory_gb']
        allocated_memory = total_memory * self.memory_fraction
        
        return {
            'total_memory_gb': total_memory,
            'allocated_memory_gb': allocated_memory,
            'per_process_limit_gb': allocated_memory / 2,  # Split between processes
            'cache_size_gb': allocated_memory * 0.2  # 20% for cache
        }


# Create global optimizer instance
optimizer = M4Optimizer(
    use_mps=True,
    batch_size=32,
    num_workers=12,  # Using performance cores
    memory_fraction=0.7,
    enable_metal_optimizations=True
) 