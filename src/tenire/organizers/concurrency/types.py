"""
Shared type definitions for the concurrency package.

This module contains type definitions that are used across multiple modules
in the concurrency package, helping to avoid circular imports.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set, Callable

class AbstractTaskResult(ABC):
    """Abstract base class for task execution results."""
    @abstractmethod
    def get_result(self) -> Any:
        """Get the task result."""
        pass

    @abstractmethod
    def get_error(self) -> Optional[str]:
        """Get the error message if any."""
        pass

    @abstractmethod
    def is_successful(self) -> bool:
        """Check if the task was successful."""
        pass

@dataclass
class TaskResult(AbstractTaskResult):
    """Data structure for task execution results."""
    success: bool
    result: Optional[Any] = None
    error: Optional[str] = None
    task_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

    def get_result(self) -> Any:
        """Get the task result."""
        return self.result

    def get_error(self) -> Optional[str]:
        """Get the error message if any."""
        return self.error

    def is_successful(self) -> bool:
        """Check if the task was successful."""
        return self.success

@dataclass
class InitializationTask:
    """Task for component initialization."""
    component: str
    dependencies: Set[str]
    priority: int
    init_fn: Callable
    timeout: float = 30.0  # Default 30 second timeout
    retry_count: int = 3
    retry_delay: float = 1.0
    health_check: Optional[Callable] = None
    metadata: Optional[Dict[str, Any]] = None

@dataclass
class TimingConfig:
    """Configuration for component timing."""
    min_interval: float  # Minimum interval between operations
    max_interval: float  # Maximum interval for operations
    burst_limit: int    # Maximum operations in burst
    cooldown: float     # Cooldown period after burst
    priority: int       # Priority level (higher = more important)
    enabled: bool = True  # Whether this timing configuration is enabled 