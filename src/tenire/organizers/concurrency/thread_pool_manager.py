"""
Thread pool management module.

This module provides the ThreadPoolManager class for managing thread pools
and executing CPU-bound operations.
"""

# Standard library imports
import asyncio
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, TypeVar

# Local imports
from tenire.utils.logger import get_logger

# Configure logging
logger = get_logger(__name__)

# Type variables for generics
T = TypeVar('T')

# Lazy import of event loop manager to avoid circular imports
def get_event_loop_manager():
    from tenire.core.event_loop import event_loop_manager
    return event_loop_manager

class AbstractThreadPoolManager(ABC):
    """Abstract base class for thread pool management."""
    
    @abstractmethod
    def initialize(self) -> None:
        """Initialize the thread pool."""
        pass
        
    @abstractmethod
    def configure(self, max_workers: int) -> None:
        """Configure the thread pool."""
        pass
        
    @abstractmethod
    def shutdown(self, wait: bool = True) -> None:
        """Shutdown the thread pool."""
        pass
        
    @abstractmethod
    async def execute(self, func: Callable[..., T], *args, **kwargs) -> T:
        """Execute a function in the thread pool."""
        pass

class ThreadPoolManager(AbstractThreadPoolManager):
    """
    Manages thread pools for CPU-bound operations.
    
    This class follows the Single Responsibility Principle by focusing solely
    on thread pool management and execution.
    """
    
    def __init__(self, max_workers: Optional[int] = None):
        """Initialize the thread pool manager."""
        self._max_workers = max_workers or 4  # Default to 4 workers if not specified
        self._pool: Optional[ThreadPoolExecutor] = None
        self._is_initialized = False
        self._initialization_lock = asyncio.Lock()  # Add lock for thread-safe initialization
        logger.debug(f"Initialized ThreadPoolManager with max_workers={self._max_workers}")

    def initialize(self) -> None:
        """Initialize the thread pool if not already initialized."""
        if self._is_initialized:
            return
            
        try:
            logger.info("Initializing thread pool")
            if self._pool is not None:
                # Ensure clean shutdown of any existing pool
                self.shutdown(wait=True)
                
            self._pool = ThreadPoolExecutor(max_workers=self._max_workers)
            self._is_initialized = True
            logger.info(f"Thread pool initialized with {self._max_workers} workers")
        except Exception as e:
            logger.error(f"Failed to initialize thread pool: {str(e)}")
            self._is_initialized = False
            self._pool = None
            raise
            
    async def ensure_initialized(self) -> None:
        """Ensure thread pool is initialized in a thread-safe manner."""
        if self._is_initialized:
            return
            
        try:
            async with self._initialization_lock:
                if not self._is_initialized:  # Double-check under lock
                    self.initialize()
        except Exception as e:
            logger.error(f"Error ensuring thread pool initialization: {str(e)}")
            raise

    def configure(self, max_workers: int) -> None:
        """Configure the thread pool with new settings."""
        if max_workers <= 0:
            raise ValueError("max_workers must be greater than 0")
            
        if self._is_initialized:
            # If already initialized, shutdown and recreate
            self.shutdown()
        self._max_workers = max_workers
        self.initialize()

    def shutdown(self, wait: bool = True) -> None:
        """Shutdown the thread pool."""
        if self._pool:
            try:
                logger.info(f"Shutting down thread pool (wait={wait})")
                self._pool.shutdown(wait=wait)
            except Exception as e:
                logger.error(f"Error during thread pool shutdown: {str(e)}")
            finally:
                self._is_initialized = False
                self._pool = None

    async def execute(self, func: Callable[..., T], *args, **kwargs) -> T:
        """
        Execute a function in the thread pool.
        
        Args:
            func: The function to execute
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function
            
        Returns:
            The result of the function execution
            
        Raises:
            RuntimeError: If thread pool initialization fails
        """
        await self.ensure_initialized()
        
        if not self._pool:
            raise RuntimeError("Thread pool not properly initialized")
        
        logger.debug(f"Executing function {func.__name__} in thread pool")
        
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        try:
            return await loop.run_in_executor(self._pool, func, *args, **kwargs)
        except Exception as e:
            logger.error(f"Error executing {func.__name__} in thread pool: {str(e)}")
            raise
