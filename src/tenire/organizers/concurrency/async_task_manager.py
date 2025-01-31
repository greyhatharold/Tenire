"""
Async task management module.

This module provides the AsyncTaskManager class for managing async tasks
and their lifecycle.
"""

# Standard library imports
import asyncio
from abc import ABC, abstractmethod
from typing import Any, Coroutine, Dict, List, Optional, TypeVar

# Local imports
from tenire.utils.logger import get_logger
from tenire.core.event_loop import event_loop_manager
from .types import TaskResult

# Configure logging
logger = get_logger(__name__)

# Type variables for generics
T = TypeVar('T')

class AbstractAsyncTaskManager(ABC):
    """Abstract base class for async task management."""
    
    @abstractmethod
    async def create_task(self, coro: Coroutine[Any, Any, T], task_id: str) -> asyncio.Task[T]:
        """Create and track an async task."""
        pass
        
    @abstractmethod
    async def wait_for_tasks(self, task_ids: Optional[List[str]] = None) -> Dict[str, TaskResult]:
        """Wait for specified tasks to complete."""
        pass
        
    @abstractmethod
    def cancel_tasks(self, task_ids: Optional[List[str]] = None) -> None:
        """Cancel specified tasks."""
        pass
        
    @abstractmethod
    def trigger_shutdown(self) -> None:
        """Trigger the shutdown event."""
        pass

class AsyncTaskManager(AbstractAsyncTaskManager):
    """
    Manages async tasks and their lifecycle.
    
    This class follows the Single Responsibility Principle by focusing solely
    on async task management and execution.
    """
    
    def __init__(self):
        """Initialize the async task manager."""
        self._tasks: Dict[str, asyncio.Task] = {}
        self._results: Dict[str, TaskResult] = {}
        self._shutdown_event = asyncio.Event()
        self._task_lock = asyncio.Lock()
        self._initialized = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        logger.debug("Initialized AsyncTaskManager")

    async def initialize(self) -> bool:
        """Initialize the async task manager."""
        if self._initialized:
            return True
            
        try:
            # Get event loop from event_loop_manager
            if not event_loop_manager.is_initialized:
                logger.error("Event loop manager must be initialized first")
                return False
                
            # Get the current running loop instead of creating a new one
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                # If no loop is running, get it from event_loop_manager
                self._loop = event_loop_manager.get_event_loop()
                if not self._loop:
                    logger.error("No event loop available")
                    return False
                
            # Clear any existing tasks
            async with self._task_lock:
                for task in self._tasks.values():
                    if not task.done():
                        task.cancel()
                        try:
                            # Use shield to prevent cancellation of this cleanup
                            await asyncio.shield(task)
                        except (asyncio.CancelledError, Exception):
                            pass
                
                self._tasks.clear()
                self._results.clear()
                
            # Reset shutdown event
            self._shutdown_event.clear()
            
            self._initialized = True
            logger.debug("AsyncTaskManager initialization completed")
            return True
            
        except Exception as e:
            logger.error(f"Error initializing AsyncTaskManager: {str(e)}")
            self._initialized = False
            return False

    @property
    def is_initialized(self) -> bool:
        """Check if the async task manager is initialized."""
        return self._initialized and self._loop is not None

    async def create_task(self, coro: Coroutine[Any, Any, T], task_id: str) -> asyncio.Task[T]:
        """
        Create and track an async task.
        
        Args:
            coro: The coroutine to execute
            task_id: Unique identifier for the task
            
        Returns:
            The created task
        """
        if not self.is_initialized:
            if not await self.initialize():
                raise RuntimeError("AsyncTaskManager not initialized")
            
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = event_loop_manager.get_event_loop()
            if not self._loop:
                raise RuntimeError("No event loop available")
            
        async with self._task_lock:
            # Cancel existing task with same ID if it exists
            if task_id in self._tasks and not self._tasks[task_id].done():
                logger.info(f"Cancelling existing task with ID {task_id}")
                self._tasks[task_id].cancel()
                try:
                    await asyncio.shield(self._tasks[task_id])
                except (asyncio.CancelledError, Exception):
                    pass
            
            logger.debug(f"Creating new task with ID {task_id}")
            task = self._loop.create_task(self._wrap_coroutine(coro, task_id))
            self._tasks[task_id] = task
            return task

    async def _wrap_coroutine(self, coro: Coroutine[Any, Any, T], task_id: str) -> T:
        """Wrap a coroutine to track its result."""
        try:
            logger.debug(f"Starting execution of task {task_id}")
            result = await coro
            logger.debug(f"Task {task_id} completed successfully")
            self._results[task_id] = TaskResult(
                success=True,
                result=result,
                task_id=task_id
            )
            return result
        except asyncio.CancelledError:
            logger.warning(f"Task {task_id} was cancelled")
            self._results[task_id] = TaskResult(
                success=False,
                error="Task cancelled",
                task_id=task_id
            )
            raise
        except Exception as e:
            logger.error(f"Task {task_id} failed with error: {str(e)}")
            self._results[task_id] = TaskResult(
                success=False,
                error=str(e),
                task_id=task_id
            )
            raise
        finally:
            async with self._task_lock:
                self._tasks.pop(task_id, None)

    async def wait_for_tasks(self, task_ids: Optional[List[str]] = None) -> Dict[str, TaskResult]:
        """Wait for specified tasks to complete."""
        if not self._loop:
            self._loop = event_loop_manager.get_event_loop()
            
        async with self._task_lock:
            tasks_to_wait = []
            if task_ids:
                tasks_to_wait = [self._tasks[tid] for tid in task_ids if tid in self._tasks]
            else:
                tasks_to_wait = list(self._tasks.values())
            
            if not tasks_to_wait:
                logger.debug("No tasks to wait for")
                return {}
            
            logger.debug(f"Waiting for {len(tasks_to_wait)} tasks to complete")
            done, pending = await asyncio.wait(
                tasks_to_wait,
                return_when=asyncio.ALL_COMPLETED
            )
            
            return {
                tid: self._results[tid]
                for tid in (task_ids or self._tasks.keys())
                if tid in self._results
            }

    def cancel_tasks(self, task_ids: Optional[List[str]] = None) -> None:
        """Cancel specified tasks."""
        tasks_to_cancel = []
        if task_ids:
            tasks_to_cancel = [self._tasks[tid] for tid in task_ids if tid in self._tasks]
        else:
            tasks_to_cancel = list(self._tasks.values())
        
        logger.info(f"Cancelling {len(tasks_to_cancel)} tasks")
        for task in tasks_to_cancel:
            if not task.done():
                task.cancel()

    def trigger_shutdown(self) -> None:
        """Trigger the shutdown event."""
        logger.info("Triggering shutdown event")
        self._shutdown_event.set()

    @property
    def should_shutdown(self) -> bool:
        """Check if shutdown has been triggered."""
        return self._shutdown_event.is_set()
