"""
Centralized event loop management for the Tenire framework.

This module provides a unified way to manage event loops across the framework,
preventing common issues like multiple loops, closed loops, and cross-process
loop access.
"""

import asyncio
import threading
import weakref
from typing import Optional, Dict, Set, TYPE_CHECKING
from contextlib import contextmanager, asynccontextmanager
import os
import importlib.util
import sys
import time

from tenire.utils.logger import get_logger

# Initialize logger first
logger = get_logger(__name__)

def import_qasync():
    """Import qasync dynamically when needed."""
    try:
        # First check if already imported
        if 'qasync' in sys.modules:
            import qasync
            return qasync.QEventLoop
            
        # Then try to find without installing
        if importlib.util.find_spec('qasync'):
            import qasync
            logger.info("Successfully initialized qasync")
            return qasync.QEventLoop
            
        # Only attempt install if not found
        logger.warning("qasync not found, attempting to install...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "qasync"])
        
        # Import after install
        import qasync
        logger.info("Successfully initialized qasync")
        return qasync.QEventLoop
    except Exception as e:
        logger.warning(f"qasync not available: {str(e)}. Please ensure qasync is installed: pip install qasync")
        return None

# Only import qasync when type checking to avoid runtime import
if TYPE_CHECKING:
    from qasync import QEventLoop
else:
    QEventLoop = import_qasync()

class EventLoopManager:
    """
    Manages event loops across the framework.
    
    Features:
    1. Thread-safe event loop access
    2. Process-aware loop management
    3. Automatic cleanup of closed loops
    4. Safe loop creation and reuse
    5. Context managers for temporary loops
    6. Qt-specific event loop handling
    """
    
    _instance = None
    _lock = threading.Lock()
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    # Initialize immediately upon instance creation
                    cls._instance._early_init()
        return cls._instance
    
    @property
    def is_initialized(self) -> bool:
        """Check if the event loop manager is fully initialized."""
        return self._initialized
    
    def _early_init(self):
        """Early initialization to ensure event loop exists."""
        # Core event loop attributes
        self._loop = None
        self._qt_loop = None
        self._initialized = False
        self._qt_initialized = False
        
        # Task management
        self._tasks = set()
        self._task_lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        
        # Thread and process management
        self._thread_lock = threading.Lock()
        self._process_main_loops = {}  # pid -> loop mapping
        self._loops = {}  # thread_id -> loop mapping
        self._qt_loops = {}  # thread_id -> qt_loop mapping
        self._active_loops = set()  # Set of active thread IDs
        self._loop_policies = {}  # thread_id -> policy mapping
        
        # Registry management
        self._registry_initialized = {}  # thread_id -> bool mapping
        self._deferred_registry_inits = {}  # thread_id -> bool mapping
        
        # Logging
        self._logger = get_logger(__name__)
        
        # Ensure we have a running event loop immediately
        self._ensure_loop_running()
        
    def _ensure_loop_running(self):
        """Ensure we have a running event loop."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
        if not loop.is_running():
            def run_loop():
                asyncio.set_event_loop(loop)
                loop.run_forever()
                
            thread = threading.Thread(target=run_loop, daemon=True, name="EventLoop")
            thread.start()
            
            # Wait for loop to be running
            start_time = time.time()
            while not loop.is_running() and time.time() - start_time < 1.0:
                time.sleep(0.01)
                
        self._loop = loop
        self._initialized = True
        
    def __init__(self):
        """Initialize the event loop manager with optimized settings."""
        # Skip if already initialized
        if hasattr(self, '_initialized'):
            return
            
        # Core initialization was done in _early_init
        
    async def ensure_initialized(self) -> bool:
        """Ensure the event loop manager is fully initialized."""
        if not self._initialized:
            try:
                async with self._init_lock:
                    if not self._initialized:
                        self._ensure_loop_running()
            except Exception as e:
                self._logger.error(f"Failed to initialize event loop: {str(e)}")
                return False
        return True

    def get_event_loop(self, for_qt: bool = False) -> Optional[asyncio.AbstractEventLoop]:
        """Get the appropriate event loop with lazy initialization."""
        try:
            if for_qt:
                if not self._qt_initialized:
                    self._initialize_qt_loop()
                return self._qt_loop
            else:
                if not self._initialized:
                    self._initialize_loop()
                return self._loop
        except Exception as e:
            self._logger.error(f"Error getting event loop: {str(e)}")
            return None
            
    def _initialize_loop(self) -> None:
        """Initialize the main event loop with optimized settings."""
        try:
            if self._initialized:
                return
                
            # Create new event loop with optimized settings
            policy = asyncio.get_event_loop_policy()
            loop = policy.new_event_loop()
            
            # Configure loop for better performance
            if hasattr(loop, 'set_debug'):
                loop.set_debug(False)  # Disable debug mode for better performance
            if hasattr(loop, 'slow_callback_duration'):
                loop.slow_callback_duration = 0.1  # Reduce slow callback threshold
                
            # Set as current event loop
            asyncio.set_event_loop(loop)
            
            # Start loop in background if not running
            if not loop.is_running():
                def run_loop():
                    try:
                        asyncio.set_event_loop(loop)
                        if not loop.is_closed():
                            loop.run_forever()
                    except Exception as e:
                        self._logger.error(f"Error in event loop thread: {str(e)}")
                        
                thread = threading.Thread(target=run_loop, daemon=True, name="EventLoop")
                thread.start()
                
                # Wait for loop to be running with timeout
                start_time = time.time()
                while not loop.is_running() and time.time() - start_time < 1.0:
                    time.sleep(0.01)
                    
                if not loop.is_running():
                    self._logger.error("Failed to start event loop")
                    return
                    
            self._loop = loop
            self._initialized = True
            
            # Register this loop with the process
            try:
                pid = os.getpid()
                thread_id = threading.get_ident()
                with self._thread_lock:
                    self._process_main_loops[pid] = weakref.ref(loop)
                    self._loops[thread_id] = weakref.ref(loop)
                    self._active_loops.add(thread_id)
            except Exception as e:
                self._logger.error(f"Error registering loop: {str(e)}")
                
            self._logger.debug("Initialized main event loop with optimized settings")
            
        except Exception as e:
            self._logger.error(f"Error initializing main event loop: {str(e)}")
            raise  # Re-raise to allow proper error handling
            
    def _initialize_qt_loop(self) -> None:
        """Initialize the Qt event loop with optimized settings."""
        try:
            if self._qt_initialized:
                return
                
            # Import Qt dependencies only when needed
            try:
                from PySide6.QtWidgets import QApplication
                import qasync
            except ImportError:
                self._logger.warning("Qt dependencies not available, falling back to regular event loop")
                self._initialize_loop()
                return
            
            # Create QApplication if needed
            app = QApplication.instance()
            if app is None:
                try:
                    app = QApplication([])
                    app.setStyle('Fusion')  # Use Fusion style for better performance
                except Exception as e:
                    self._logger.error(f"Failed to create QApplication: {str(e)}")
                    self._initialize_loop()
                    return
            
            # Verify QApplication is properly initialized
            if not app.thread():
                self._logger.error("QApplication thread not properly initialized")
                self._initialize_loop()
                return
                
            # Create optimized qasync loop
            try:
                loop = qasync.QEventLoop(app)
                
                # Configure loop for better performance
                if hasattr(loop, 'set_debug'):
                    loop.set_debug(False)
                if hasattr(loop, 'slow_callback_duration'):
                    loop.slow_callback_duration = 0.1
                    
                # Start loop in background if not running
                if not loop.is_running():
                    def run_loop():
                        try:
                            asyncio.set_event_loop(loop)
                            if not loop.is_closed():
                                loop.run_forever()
                        except Exception as e:
                            self._logger.error(f"Error in Qt event loop thread: {str(e)}")
                            
                    thread = threading.Thread(target=run_loop, daemon=True, name="QtEventLoop")
                    thread.start()
                    
                    # Wait for loop to be running with timeout
                    start_time = time.time()
                    while not loop.is_running() and time.time() - start_time < 1.0:
                        time.sleep(0.01)
                        
                    if not loop.is_running():
                        self._logger.error("Failed to start Qt event loop")
                        self._initialize_loop()
                        return
                        
                # Register this loop with the process
                try:
                    pid = os.getpid()
                    thread_id = threading.get_ident()
                    with self._thread_lock:
                        self._process_main_loops[pid] = weakref.ref(loop)
                        self._loops[thread_id] = weakref.ref(loop)
                        self._qt_loops[thread_id] = weakref.ref(loop)
                        self._active_loops.add(thread_id)
                except Exception as e:
                    self._logger.error(f"Error registering Qt loop: {str(e)}")
                    
                self._qt_loop = loop
                self._qt_initialized = True
                self._logger.debug("Initialized Qt event loop with optimized settings")
                
            except Exception as e:
                self._logger.error(f"Failed to create QEventLoop: {str(e)}")
                self._initialize_loop()
                return
                
        except Exception as e:
            self._logger.error(f"Error initializing Qt event loop: {str(e)}")
            self._initialize_loop()
            
    async def track_task(self, task: asyncio.Task) -> None:
        """Track a task with proper cleanup."""
        try:
            async with self._task_lock:
                self._tasks.add(task)
                task.add_done_callback(lambda t: self._remove_task(t))
        except Exception as e:
            self._logger.error(f"Error tracking task: {str(e)}")
            
    def _remove_task(self, task: asyncio.Task) -> None:
        """Remove a completed task."""
        try:
            self._tasks.discard(task)
        except Exception as e:
            self._logger.error(f"Error removing task: {str(e)}")

    def _schedule_registry_init(self, loop: asyncio.AbstractEventLoop, thread_id: int) -> None:
        """Safely schedule registry initialization."""
        try:
            if not loop.is_running():
                self._deferred_registry_inits[thread_id] = True
                return

            if not self._registry_initialized.get(thread_id, False):
                loop.create_task(self._ensure_registry(loop, thread_id))
        except Exception as e:
            logger.debug(f"Registry initialization scheduling deferred: {str(e)}")
            self._deferred_registry_inits[thread_id] = True

    async def _ensure_registry(self, loop: asyncio.AbstractEventLoop, thread_id: int) -> None:
        """Ensure component registry is initialized for a loop."""
        if not self._registry_initialized.get(thread_id, False):
            try:
                # Import registry lazily to avoid circular imports
                from tenire.structure.component_registry import initialize_registry
                
                # Create a task with timeout protection
                async def init_with_timeout():
                    try:
                        if loop.is_running():
                            # Use asyncio.shield to protect from cancellation
                            await asyncio.shield(
                                asyncio.wait_for(
                                    initialize_registry(),
                                    timeout=5.0  # Reduced timeout from 30s to 5s
                                )
                            )
                        else:
                            await initialize_registry()
                        self._registry_initialized[thread_id] = True
                        self._deferred_registry_inits.pop(thread_id, None)
                        logger.debug("Component registry initialized for thread")
                    except asyncio.TimeoutError:
                        logger.error("Registry initialization timed out after 5 seconds")
                        self._deferred_registry_inits[thread_id] = True
                    except Exception as e:
                        logger.error(f"Registry initialization failed: {str(e)}")
                        self._deferred_registry_inits[thread_id] = True

                # Create and track the initialization task
                init_task = loop.create_task(init_with_timeout())
                self.track_task(init_task)
                
                # Wait for initialization with a shorter timeout
                try:
                    await asyncio.wait_for(init_task, timeout=5.0)
                except asyncio.TimeoutError:
                    logger.warning("Registry initialization deferred due to timeout")
                    self._deferred_registry_inits[thread_id] = True
                except Exception as e:
                    logger.error(f"Error during registry initialization: {str(e)}")
                    self._deferred_registry_inits[thread_id] = True
                    
            except Exception as e:
                logger.error(f"Registry initialization error: {str(e)}")
                self._deferred_registry_inits[thread_id] = True

    def register_process_main_loop(self, loop: asyncio.AbstractEventLoop, is_qt: bool = False) -> None:
        """
        Register the main event loop for a process.
        
        Args:
            loop: The event loop to register
            is_qt: Whether this is a Qt event loop
            
        Raises:
            RuntimeError: If registration fails
        """
        try:
            pid = os.getpid()
            thread_id = threading.get_ident()
            
            with self._thread_lock:
                # Register process loop
                self._process_main_loops[pid] = weakref.ref(loop)
                
                # Register thread-specific loop
                self._loops[thread_id] = weakref.ref(loop)
                self._active_loops.add(thread_id)
                
                # Register Qt-specific loop if needed
                if is_qt:
                    self._qt_loops[thread_id] = weakref.ref(loop)
                    
                # Store current event loop policy
                self._loop_policies[thread_id] = asyncio.get_event_loop_policy()
                
                self._logger.debug(
                    f"Registered {'Qt ' if is_qt else ''}event loop for process {pid} "
                    f"thread {thread_id}"
                )
                
        except Exception as e:
            error_msg = f"Failed to register process main loop: {str(e)}"
            self._logger.error(error_msg)
            raise RuntimeError(error_msg) from e

    def get_process_main_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        """Get the main event loop for the current process."""
        pid = os.getpid()
        with self._thread_lock:
            loop_ref = self._process_main_loops.get(pid)
            if loop_ref:
                return loop_ref()
        return None

    def cleanup_thread(self, thread_id: Optional[int] = None) -> None:
        """Clean up event loop resources for a thread."""
        if thread_id is None:
            thread_id = threading.get_ident()
            
        with self._thread_lock:
            # Clean up loop
            loop_ref = self._loops.pop(thread_id, None)
            if loop_ref:
                loop = loop_ref()
                if loop and not loop.is_closed():
                    loop.stop()
                    loop.close()
                    
            # Clean up Qt loop
            qt_loop_ref = self._qt_loops.pop(thread_id, None)
            if qt_loop_ref:
                qt_loop = qt_loop_ref()
                if qt_loop and not qt_loop.is_closed():
                    qt_loop.stop()
                    qt_loop.close()
                    
            # Clean up other resources
            self._active_loops.discard(thread_id)
            self._loop_policies.pop(thread_id, None)
            self._registry_initialized.pop(thread_id, None)
            self._deferred_registry_inits.pop(thread_id, None)

    @contextmanager
    def loop_context(self, for_qt: bool = False):
        """
        Context manager for managing event loop lifecycle.
        
        Args:
            for_qt: Whether to use Qt event loop
            
        Yields:
            asyncio.AbstractEventLoop: The managed event loop
        """
        loop = None
        try:
            # Get appropriate loop
            loop = self.get_event_loop(for_qt=for_qt)
            if loop is None:
                raise RuntimeError("Failed to get event loop")
                
            # Set as current loop for this context
            asyncio.set_event_loop(loop)
            yield loop
            
        except Exception as e:
            self._logger.error(f"Error in loop context: {str(e)}")
            raise
        finally:
            if loop and not loop.is_closed():
                try:
                    # Cancel pending tasks
                    pending = asyncio.all_tasks(loop)
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                except Exception as e:
                    self._logger.error(f"Error cleaning up loop tasks: {str(e)}")

    @asynccontextmanager
    async def aloop_scope(self, for_qt: bool = False):
        """
        Async context manager for managing event loop scope.
        
        Args:
            for_qt: Whether to use Qt event loop
            
        Yields:
            asyncio.AbstractEventLoop: The managed event loop
        """
        loop = None
        try:
            # Get appropriate loop
            loop = self.get_event_loop(for_qt=for_qt)
            if loop is None:
                raise RuntimeError("Failed to get event loop")
                
            # Register with process/thread tracking
            pid = os.getpid()
            thread_id = threading.get_ident()
            with self._thread_lock:
                self._process_main_loops[pid] = weakref.ref(loop)
                self._loops[thread_id] = weakref.ref(loop)
                if for_qt:
                    self._qt_loops[thread_id] = weakref.ref(loop)
                self._active_loops.add(thread_id)
                
            yield loop
            
        except Exception as e:
            self._logger.error(f"Error in loop scope: {str(e)}")
            raise
        finally:
            if loop and not loop.is_closed():
                try:
                    # Clean up tasks
                    pending = asyncio.all_tasks(loop)
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                except Exception as e:
                    self._logger.error(f"Error cleaning up loop tasks: {str(e)}")
                    
            # Clean up registrations
            try:
                with self._thread_lock:
                    self._active_loops.discard(thread_id)
                    self._loops.pop(thread_id, None)
                    if for_qt:
                        self._qt_loops.pop(thread_id, None)
                    if pid in self._process_main_loops:
                        self._process_main_loops.pop(pid)
            except Exception as e:
                self._logger.error(f"Error cleaning up loop registrations: {str(e)}")

    @contextmanager
    def watch_tasks(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        """
        Context manager for watching tasks in a loop.
        
        Args:
            loop: Optional specific loop to watch, uses current if None
            
        Yields:
            Set[asyncio.Task]: Set of tasks being watched
        """
        if loop is None:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = self.get_event_loop()
                
        if loop is None:
            raise RuntimeError("No event loop available")
            
        tasks = set()
        try:
            # Track existing tasks
            tasks.update(asyncio.all_tasks(loop))
            yield tasks
            
        finally:
            try:
                # Check for uncompleted tasks
                current = asyncio.all_tasks(loop)
                new_tasks = current - tasks
                if new_tasks:
                    self._logger.warning(f"Uncompleted tasks detected: {len(new_tasks)}")
                    for task in new_tasks:
                        if not task.done():
                            task.cancel()
            except Exception as e:
                self._logger.error(f"Error cleaning up watched tasks: {str(e)}")

    @asynccontextmanager
    async def watch_async_tasks(self, loop: Optional[asyncio.AbstractEventLoop] = None):
        """
        Async context manager for watching tasks with async cleanup.
        
        Args:
            loop: Optional specific loop to watch, uses current if None
            
        Yields:
            Set[asyncio.Task]: Set of tasks being watched
        """
        if loop is None:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = self.get_event_loop()
                
        if loop is None:
            raise RuntimeError("No event loop available")
            
        tasks = set()
        try:
            # Track existing tasks
            tasks.update(asyncio.all_tasks(loop))
            yield tasks
            
        finally:
            try:
                # Check for uncompleted tasks
                current = asyncio.all_tasks(loop)
                new_tasks = current - tasks
                if new_tasks:
                    self._logger.warning(f"Uncompleted tasks detected: {len(new_tasks)}")
                    # Cancel and await completion
                    for task in new_tasks:
                        if not task.done():
                            task.cancel()
                    if new_tasks:
                        await asyncio.gather(*new_tasks, return_exceptions=True)
            except Exception as e:
                self._logger.error(f"Error cleaning up watched tasks: {str(e)}")

# Create global instance
event_loop_manager = EventLoopManager() 