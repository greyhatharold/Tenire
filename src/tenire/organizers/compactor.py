"""
Centralized garbage collection and cleanup management for the Tenire framework.

This module provides a comprehensive system for managing cleanup operations,
resource disposal, and garbage collection across the entire framework.
It ensures proper cleanup of:
1. Event loops and async resources
2. GUI components and processes
3. Thread pools and executors
4. System resources and file handles
5. Browser sessions and network connections

The Compactor follows the Singleton pattern and provides both synchronous
and asynchronous cleanup methods for different types of resources.
"""

# Standard library imports
import asyncio
import gc
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Set, Any, Callable, Union
import time

# Third-party imports
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer

# Local imports
from tenire.servicers.signal import get_signal_manager
from tenire.core.codex import Signal, SignalType
from tenire.utils.logger import get_logger
from tenire.core.container import container
from tenire.core.event_loop import event_loop_manager

logger = get_logger(__name__)

@dataclass
class CleanupTask:
    """Data structure for cleanup tasks with metadata."""
    name: str
    cleanup_func: Union[Callable[[], None], Callable[[], Any]]
    priority: int = field(default=0)  # Higher number = higher priority
    is_async: bool = field(default=False)
    metadata: Dict[str, Any] = field(default_factory=dict)
    error_count: int = field(default=0)
    last_error: Optional[Exception] = field(default=None)
    last_run: Optional[datetime] = field(default=None)
    
    def __post_init__(self):
        """Validate cleanup function."""
        if not callable(self.cleanup_func):
            raise ValueError(f"Cleanup function must be callable, got {type(self.cleanup_func)}")

class Compactor:
    """
    Centralized cleanup and garbage collection manager.
    
    This class is responsible for:
    1. Managing cleanup tasks and their execution order
    2. Handling graceful shutdown of components
    3. Monitoring resource usage and triggering cleanup when needed
    4. Providing both manual and automatic cleanup capabilities
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        with cls._lock:
            if not cls._instance:
                cls._instance = super(Compactor, cls).__new__(cls)
            return cls._instance
    
    def __init__(self):
        """Initialize the compactor if not already initialized."""
        if not hasattr(self, '_initialized'):
            self._initialized = True
            self._cleanup_tasks: Dict[str, CleanupTask] = {}
            self._resource_registry: weakref.WeakSet = weakref.WeakSet()
            self._is_shutting_down = False
            self._shutdown_event = asyncio.Event()
            self._cleanup_lock = asyncio.Lock()
            self._event_loops: Set[asyncio.AbstractEventLoop] = set()
            self._thread_pools: Set[ThreadPoolExecutor] = set()
            self._qt_timers: Set[QTimer] = set()
            self._is_cleaning = False
            self._error_handlers: List[Callable[[Exception], None]] = []
            self._signal_handlers_registered = False
            self._signal_init_retries = 0
            self._max_signal_init_retries = 5
            self._signal_init_delay = 1.0  # Initial delay in seconds
            self._signal_manager = None
            self._initialization_lock = asyncio.Lock()
            self._dependency_init_retries = 0
            self._init_task: Optional[asyncio.Task] = None
            
            logger.debug("Creating global Compactor instance")
            
            # Register with container using sync method
            container.register_sync('compactor', self)
            
            # Initialize signal handlers in a safe way
            self._schedule_signal_handler_init()

    def _schedule_signal_handler_init(self):
        """Schedule signal handler initialization with proper error handling."""
        try:
            from tenire.core.event_loop import event_loop_manager
            
            # Try to get the event loop through the manager first
            loop = event_loop_manager.get_event_loop()
            
            # If no loop exists or is not properly initialized, create one
            if loop is None or not hasattr(loop, '_running'):
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                
                # Ensure the loop is properly initialized
                if not hasattr(loop, '_running'):
                    loop._running = False
                
                # Start the loop if it's not running
                if not loop.is_running() and not loop.is_closed():
                    def run_loop():
                        asyncio.set_event_loop(loop)
                        loop.run_forever()
                    threading.Thread(target=run_loop, daemon=True).start()
                    time.sleep(0.1)  # Give loop time to start
                
                loop._running = True
                logger.debug("Created and started new event loop")
            
            # Create initialization task if not already running
            if not self._init_task or self._init_task.done():
                if loop.is_running():
                    # Create a future to track completion
                    future = loop.create_future()
                    
                    async def init_and_complete():
                        try:
                            await self._safe_initialize_signal_handlers()
                            if not future.done():
                                future.set_result(True)
                        except Exception as e:
                            if not future.done():
                                future.set_exception(e)
                                
                    self._init_task = loop.create_task(init_and_complete())
                else:
                    # If loop is not running, schedule for next iteration
                    def schedule_init():
                        self._init_task = asyncio.create_task(self._safe_initialize_signal_handlers())
                    loop.call_soon(schedule_init)
                
            # Register for lazy initialization regardless of loop state
            from tenire.core.container import container
            container.register_lazy_sync(
                'compactor_signal_init',
                lambda: self._init_task or asyncio.create_task(self._safe_initialize_signal_handlers()),
                dependencies={'signal_manager'}
            )
                
        except Exception as e:
            logger.error(f"Error scheduling signal handler initialization: {str(e)}")
            # Still register for lazy initialization as fallback
            try:
                from tenire.core.container import container
                container.register_lazy_sync(
                    'compactor_signal_init',
                    lambda: asyncio.create_task(self._safe_initialize_signal_handlers()),
                    dependencies={'signal_manager'}
                )
            except Exception as inner_e:
                logger.error(f"Fallback initialization also failed: {str(inner_e)}")

    async def _safe_initialize_signal_handlers(self) -> None:
        """Initialize signal handlers with proper error handling and task tracking."""
        try:
            from tenire.core.event_loop import event_loop_manager
            loop = event_loop_manager.get_event_loop()
            
            if not loop or loop.is_closed():
                logger.error("No valid event loop available for signal handler initialization")
                return
            
            # Create a future to track completion
            init_future = loop.create_future()
            
            async def register_and_complete():
                try:
                    await self._register_signal_handlers()
                    if not init_future.done():
                        init_future.set_result(True)
                except Exception as e:
                    if not init_future.done():
                        init_future.set_exception(e)
                    
            # Create and track the registration task
            task = loop.create_task(register_and_complete())
            await event_loop_manager.track_task(task)
            
            try:
                # Wait for initialization with timeout
                await asyncio.wait_for(init_future, timeout=10.0)
                self._signal_handlers_registered = True
                logger.debug("Signal handlers registered successfully")
            except asyncio.TimeoutError:
                logger.error("Signal handler registration timed out")
                if not task.done():
                    task.cancel()
            except Exception as e:
                logger.error(f"Error during signal handler registration: {str(e)}")
                if not task.done():
                    task.cancel()
                
        except Exception as e:
            logger.error(f"Error registering signal handlers: {str(e)}")

    async def _register_signal_handlers(self) -> None:
        """Register signal handlers with the signal manager."""
        try:
            from tenire.servicers.signal_servicer import get_signal_manager
            from tenire.core.codex import SignalType
            
            # Get signal manager instance
            signal_manager = await get_signal_manager()
            if not signal_manager:
                raise ValueError("Signal manager not available")
            
            # Register handlers
            signal_manager.register_handler(
                SignalType.CLEANUP_REQUESTED,
                self._handle_cleanup_request,
                priority=100
            )
            
            signal_manager.register_handler(
                SignalType.GUI_CLEANUP_REQUESTED,
                self.cleanup_gui,
                priority=90
            )
            
            signal_manager.register_handler(
                SignalType.BROWSER_CLEANUP_REQUESTED,
                self.cleanup_browser,
                priority=90
            )
            
            logger.debug("Registered compactor signal handlers")
            
        except Exception as e:
            logger.error(f"Error registering signal handlers: {str(e)}")
            raise

    def register_event_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Register an event loop for cleanup tracking."""
        self._event_loops.add(loop)
        logger.debug(f"Registered event loop")
        
    def register_thread_pool(self, pool: ThreadPoolExecutor) -> None:
        """Register a thread pool for cleanup tracking."""
        self._thread_pools.add(pool)
        logger.debug(f"Registered thread pool")
        
    def register_qt_timer(self, timer: QTimer) -> None:
        """Register a Qt timer for cleanup tracking."""
        self._qt_timers.add(timer)
        logger.debug(f"Registered Qt timer: {timer}")
        
    def register_cleanup_task(
        self,
        name: str,
        cleanup_func: callable,
        priority: int = 0,
        is_async: bool = False,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Register a cleanup task with the compactor."""
        task = CleanupTask(
            name=name,
            priority=priority,
            cleanup_func=cleanup_func,
            is_async=is_async,
            metadata=metadata or {}
        )
        self._cleanup_tasks[name] = task
        logger.debug(f"Registered cleanup task: {name} (priority={priority})")
        
    def register_resource(self, resource: Any) -> None:
        """Register a resource for tracking and automatic cleanup."""
        self._resource_registry.add(resource)
        logger.debug(f"Registered resource for tracking: {resource}")
        
    async def _handle_cleanup_request(self, signal: Signal) -> None:
        """Handle cleanup request signals."""
        try:
            cleanup_type = signal.data.get('cleanup_type', 'full')
            if cleanup_type == 'full':
                await self.cleanup()
            elif cleanup_type == 'gui':
                await self.cleanup_gui()
            elif cleanup_type == 'browser':
                await self.cleanup_browser()
            logger.debug(f"Handled cleanup request of type: {cleanup_type}")
        except Exception as e:
            logger.error(f"Error handling cleanup request: {str(e)}")

    async def _handle_component_cleanup(self, signal: Signal) -> None:
        """Handle component cleanup signals."""
        try:
            component = signal.data.get('component')
            if not component:
                logger.warning("Component cleanup signal missing component name")
                return
                
            # Execute component-specific cleanup tasks
            component_tasks = {
                name: task for name, task in self._cleanup_tasks.items()
                if component in task.metadata.get('components', [])
            }
            
            for task in sorted(component_tasks.values(), key=lambda x: x.priority, reverse=True):
                try:
                    if task.is_async:
                        await task.cleanup_func()
                    else:
                        task.cleanup_func()
                    task.last_run = datetime.now()
                except Exception as e:
                    logger.error(f"Error in component cleanup task {task.name}: {str(e)}")
                    task.error_count += 1
                    task.last_error = e
                    
            logger.debug(f"Completed cleanup for component: {component}")
            
        except Exception as e:
            logger.error(f"Error handling component cleanup: {str(e)}")

    async def _handle_system_shutdown(self, signal: Signal) -> None:
        """Handle system shutdown signals."""
        try:
            # Set shutdown event
            self._shutdown_event.set()
            
            # Run full cleanup
            await self.cleanup()
            
            # Emit completion signal
            try:
                signal_manager = await get_signal_manager()
                if signal_manager:
                    await signal_manager.emit(Signal(
                        type=SignalType.CLEANUP_COMPLETED,
                        data={'source': 'compactor'},
                        source='compactor'
                    ))
            except Exception as e:
                logger.error(f"Error emitting cleanup completion signal: {str(e)}")
                
            logger.info("System shutdown cleanup completed")
            
        except Exception as e:
            logger.error(f"Error handling system shutdown: {str(e)}")

    async def cleanup_gui(self, signal: Optional[Signal] = None) -> None:
        """
        Clean up GUI-specific resources.
        
        Args:
            signal: Optional signal that triggered the cleanup
        """
        async with self._cleanup_lock:
            logger.info("Starting GUI cleanup")
            
            # Stop all Qt timers
            for timer in self._qt_timers:
                try:
                    timer.stop()
                    logger.debug(f"Stopped Qt timer: {timer}")
                except Exception as e:
                    logger.error(f"Error stopping Qt timer: {str(e)}")
            
            # Quit Qt application if running
            app = QApplication.instance()
            if app:
                try:
                    app.quit()
                    logger.debug("Qt application quit successfully")
                except Exception as e:
                    logger.error(f"Error quitting Qt application: {str(e)}")
                    
    async def cleanup_browser(self, signal: Optional[Signal] = None) -> None:
        """
        Clean up browser-specific resources.
        
        Args:
            signal: Optional signal that triggered the cleanup
        """
        async with self._cleanup_lock:
            logger.info("Starting browser cleanup")
            
            # Execute browser-specific cleanup tasks
            browser_tasks = {
                name: task for name, task in self._cleanup_tasks.items()
                if 'browser' in task.metadata.get('tags', [])
            }
            
            for task in sorted(browser_tasks.values(), key=lambda x: x.priority, reverse=True):
                try:
                    if task.is_async:
                        await task.cleanup_func()
                    else:
                        task.cleanup_func()
                    task.last_run = datetime.now()
                except Exception as e:
                    logger.error(f"Error in browser cleanup task {task.name}: {str(e)}")
                    task.error_count += 1
                    
    async def cleanup(self) -> None:
        """Perform full cleanup of all resources."""
        if self._is_shutting_down:
            logger.warning("Cleanup already in progress")
            return
            
        self._is_shutting_down = True
        async with self._cleanup_lock:
            try:
                logger.info("Starting full cleanup")
                
                # Set shutdown event
                self._shutdown_event.set()
                
                # Execute all cleanup tasks in priority order
                for task in sorted(self._cleanup_tasks.values(), key=lambda x: x.priority, reverse=True):
                    try:
                        if task.is_async:
                            await task.cleanup_func()
                        else:
                            task.cleanup_func()
                        task.last_run = datetime.now()
                    except Exception as e:
                        logger.error(f"Error in cleanup task {task.name}: {str(e)}")
                        task.error_count += 1
                
                # Clean up thread pools
                for pool in self._thread_pools:
                    try:
                        pool.shutdown(wait=True)
                        logger.debug(f"Shut down thread pool: {pool}")
                    except Exception as e:
                        logger.error(f"Error shutting down thread pool: {str(e)}")
                
                # Clean up event loops
                for loop in self._event_loops:
                    try:
                        if not loop.is_closed():
                            loop.stop()
                            loop.close()
                            logger.debug(f"Closed event loop: {loop}")
                    except Exception as e:
                        logger.error(f"Error closing event loop: {str(e)}")
                
                # Clean up GUI
                await self.cleanup_gui()
                
                # Clean up browser
                await self.cleanup_browser()
                
                # Force garbage collection
                gc.collect()
                
                # Notify signal manager of cleanup completion if available
                try:
                    signal_manager = container.get('signal_manager', initialize=False)
                    if signal_manager:
                        await signal_manager.emit(Signal(
                            type=SignalType.CLEANUP_REQUESTED,
                            data={'cleanup_type': 'completed'},
                            source='compactor'
                        ))
                except Exception as e:
                    logger.debug(f"Error notifying cleanup completion: {str(e)}")
                
                logger.info("Full cleanup completed")
                
            except Exception as e:
                logger.error(f"Error during cleanup: {str(e)}")
                raise
            finally:
                self._is_shutting_down = False
                
    def force_garbage_collection(self) -> None:
        """Force immediate garbage collection."""
        try:
            logger.debug("Forcing garbage collection")
            gc.collect()
        except Exception as e:
            logger.error(f"Error during forced garbage collection: {str(e)}")
            
    @property
    def is_shutting_down(self) -> bool:
        """Check if cleanup/shutdown is in progress."""
        return self._is_shutting_down
    
    @property
    def should_shutdown(self) -> bool:
        """Check if shutdown has been triggered."""
        return self._shutdown_event.is_set()

# Create global instance
compactor = Compactor()

# Initialize dependencies after registration
async def _initialize_dependencies():
    """Initialize compactor dependencies with proper error handling."""
    try:
        signal_manager = await get_signal_manager()
        if signal_manager:
            # Ensure compactor is registered with container
            container.register_sync('compactor', compactor)
            
            # Schedule signal handler initialization if not already done
            if not compactor._signal_handlers_registered:
                loop = event_loop_manager.get_event_loop()
                loop.create_task(compactor._safe_initialize_signal_handlers())
            else:
                def run_init():
                    asyncio.run(compactor._safe_initialize_signal_handlers())
                threading.Thread(target=run_init).start()
                
            logger.debug("Compactor dependencies initialized")
    except Exception as e:
        logger.error(f"Error initializing compactor dependencies: {str(e)}")
        # Schedule retry
        loop = event_loop_manager.get_event_loop()
        if not loop.is_closed():
            loop.call_later(1.0, lambda: asyncio.create_task(_initialize_dependencies()))

async def get_compactor() -> Compactor:
    """
    Get the global compactor instance in a thread-safe way.
    Ensures the compactor is properly initialized before returning.
    
    Returns:
        The global compactor instance
        
    Raises:
        RuntimeError: If initialization fails
    """
    try:
        # Initialize dependencies if needed
        if not compactor._initialized:
            await _initialize_dependencies()
            
        return compactor
    except Exception as e:
        logger.error(f"Error getting compactor: {str(e)}")
        raise RuntimeError(f"Failed to get compactor: {str(e)}") 