"""
Semaphore management module.

This module provides the SemaphoreManager class for managing semaphores
and resource limiting.
"""

# Standard library imports
import asyncio
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Dict, AsyncGenerator

# Local imports
from tenire.utils.logger import get_logger

# Configure logging
logger = get_logger(__name__)

class AbstractSemaphoreManager(ABC):
    """Abstract base class for semaphore management."""
    
    @abstractmethod
    def get_semaphore(self, name: str, max_concurrent: int) -> asyncio.Semaphore:
        """Get or create a semaphore."""
        pass
        
    @abstractmethod
    async def acquire(self, name: str, max_concurrent: int) -> AsyncGenerator[None, None]:
        """Acquire a semaphore."""
        pass

class SemaphoreManager(AbstractSemaphoreManager):
    """
    Manages semaphores for resource limiting.
    
    This class follows the Single Responsibility Principle by focusing solely
    on semaphore management.
    """
    
    def __init__(self):
        """Initialize the semaphore manager."""
        self._semaphores: Dict[str, asyncio.Semaphore] = {}
        logger.debug("Initialized SemaphoreManager")

    def get_semaphore(self, name: str, max_concurrent: int) -> asyncio.Semaphore:
        """
        Get or create a semaphore.
        
        Args:
            name: Unique identifier for the semaphore
            max_concurrent: Maximum number of concurrent operations
            
        Returns:
            The requested semaphore
        """
        if name not in self._semaphores:
            logger.debug(f"Creating new semaphore '{name}' with max_concurrent={max_concurrent}")
            self._semaphores[name] = asyncio.Semaphore(max_concurrent)
        return self._semaphores[name]

    @asynccontextmanager
    async def acquire(self, name: str, max_concurrent: int):
        """
        Context manager for semaphore acquisition.
        
        Args:
            name: Unique identifier for the semaphore
            max_concurrent: Maximum number of concurrent operations
            
        Yields:
            None when the semaphore is acquired
        """
        semaphore = self.get_semaphore(name, max_concurrent)
        try:
            logger.debug(f"Acquiring semaphore '{name}'")
            await semaphore.acquire()
            yield
        finally:
            logger.debug(f"Releasing semaphore '{name}'")
            semaphore.release()
