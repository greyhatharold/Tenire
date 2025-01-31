"""
Concurrency management module.

This module provides the ConcurrencyManager class for centralized management
of all concurrency-related operations.
"""

# Standard library imports
import asyncio
import multiprocessing
import queue
from abc import ABC, abstractmethod
from typing import Any, Callable, Optional, Tuple, TypeVar
import threading

# Local imports
from tenire.utils.logger import get_logger
from tenire.utils.optimizer import optimizer
from tenire.organizers.concurrency.thread_pool_manager import ThreadPoolManager
from tenire.organizers.concurrency.async_task_manager import AsyncTaskManager
from tenire.organizers.concurrency.semaphore_manager import SemaphoreManager
from tenire.organizers.concurrency.data_manager import DataManager
from tenire.organizers.concurrency.gui_process import GUIProcess
from tenire.organizers.concurrency.monitor import monitor
from tenire.organizers.scheduler import orchestrator, TimingConfig
from tenire.core.event_loop import event_loop_manager
from tenire.core.container import container

# Configure logging
logger = get_logger(__name__)

# Type variables for generics
T = TypeVar('T')

class AbstractConcurrencyManager(ABC):
    """Abstract base class for concurrency management."""
    
    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the concurrency manager and its dependencies."""
        pass
        
    @abstractmethod
    async def ensure_initialized(self) -> None:
        """Ensure dependencies are initialized."""
        pass
        
    @abstractmethod
    def configure_thread_pool(self, max_workers: Optional[int] = None) -> None:
        """Configure the thread pool."""
        pass
        
    @abstractmethod
    async def start_gui(self, width: int) -> None:
        """Start the GUI process."""
        pass
        
    @abstractmethod
    async def cleanup(self) -> None:
        """Clean up all resources."""
        pass
        
    @abstractmethod
    async def run_in_thread(self, func: Callable[..., T], *args, **kwargs) -> T:
        """Run a function in a thread pool."""
        pass

class ConcurrencyManager(AbstractConcurrencyManager):
    """
    Central manager for all concurrency-related operations with improved task management.
    
    This class follows the Facade pattern by providing a simple interface
    to the various concurrency management components.
    """
    
    def __init__(self):
        """Initialize concurrency management components."""
        if hasattr(self, '_initialized'):
            return
            
        self._initialized = False
        self._initialization_lock = asyncio.Lock()
        self._initialization_scheduled = False
        self._initialization_complete = asyncio.Event()
        
        # Initialize core components
        self.thread_pool = ThreadPoolManager()
        self.async_tasks = AsyncTaskManager()
        self.semaphores = SemaphoreManager()
        self._gui_process_lock = asyncio.Lock()
        self._cleanup_lock = asyncio.Lock()
        self._is_shutting_down = False
        self._gui_instance_check = threading.Lock()
        self._gui_running = False
        
        # Initialize DataManager with optimized settings
        self.data_manager = DataManager(
            max_workers=optimizer.num_workers,
            batch_size=optimizer.batch_size,
            memory_config=optimizer.get_memory_config(),
            cache_size=1000,
            prefetch_size=5,
            use_tfidf=True,
            embedding_dim=384
        )
        
        # Initialize state variables
        self._main_event_loop = None
        self._gui_process = None
        self._command_queue = multiprocessing.Queue()
        self._response_queue = multiprocessing.Queue()
        self._browser_session = None
        self._browser_contexts = set()
        self._shutdown_event = asyncio.Event()
        self._gui_state = {
            'status': 'initialized',
            'is_ready': False,
            'error': None,
            'last_command': None,
            'last_response': None
        }
        
        # Initialize dependency placeholders
        self._compactor = None
        self._signal_manager = None
        
        logger.debug("Initialized ConcurrencyManager")

    async def _schedule_initialization(self):
        """Schedule dependency initialization with improved flow control."""
        if self._initialization_scheduled:
            return

        try:
            # Get dependencies from container first
            container_instance = await self._get_container()
            event_loop_manager = container_instance.get('event_loop_manager')
            signal_manager = container_instance.get('signal_manager')

            # Validate critical dependencies
            if not event_loop_manager or not event_loop_manager.is_initialized:
                logger.error("Event loop manager must be initialized first")
                return
                
            if not signal_manager:
                logger.error("Signal manager must be initialized first")
                return

            # Get the main thread's event loop
            main_loop = None
            try:
                # First try getting the current loop
                main_loop = asyncio.get_running_loop()
            except RuntimeError:
                # If no loop is running, create one
                main_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(main_loop)

            # Store the main loop reference
            self._main_event_loop = main_loop

            # Register with event loop manager
            event_loop_manager.register_process_main_loop(main_loop)

            # Create initialization task in the main loop
            if main_loop.is_running():
                # If loop is running, create task directly
                init_task = main_loop.create_task(self.initialize())
                init_task.add_done_callback(self._handle_init_completion)
            else:
                # If loop isn't running, run in new thread with proper loop handling
                def run_init():
                    try:
                        # Set the loop for this thread
                        asyncio.set_event_loop(main_loop)
                        # Run initialization
                        main_loop.run_until_complete(self.initialize())
                    except Exception as e:
                        logger.error(f"Initialization failed in thread: {str(e)}")
                    finally:
                        # Clean up
                        try:
                            main_loop.close()
                        except Exception:
                            pass

                # Start initialization thread
                init_thread = threading.Thread(target=run_init, daemon=True)
                init_thread.start()

            self._initialization_scheduled = True
            logger.info("Scheduled concurrency manager initialization")

        except Exception as e:
            logger.error(f"Error scheduling initialization: {str(e)}")
            self._initialization_scheduled = False
            raise

    def _handle_init_completion(self, future):
        """Handle initialization task completion with proper loop handling."""
        try:
            # Ensure we're in the right loop context
            loop = self._main_event_loop or asyncio.get_event_loop()
            if future.get_loop() != loop:
                logger.warning("Future attached to different loop, reattaching...")
                # Create new future in correct loop
                new_future = asyncio.Future(loop=loop)
                future.add_done_callback(lambda f: new_future.set_result(f.result()))
                future = new_future

            result = future.result()
            logger.info("Concurrency manager initialization completed successfully")
        except Exception as e:
            logger.error(f"Initialization task failed: {str(e)}")
            self._initialization_scheduled = False

    async def _get_container(self):
        """Get container instance with lazy loading."""
        from tenire.core.container import container
        return container

    async def _initialize_core_components(self):
        """Initialize core components with proper error handling."""
        try:
            # Initialize thread pool first
            await self.thread_pool.ensure_initialized()
            logger.info("Thread pool initialized")

            # Initialize async task manager
            await self.async_tasks.initialize()
            logger.info("Async task manager initialized")

            # Initialize data manager with optimized settings
            await self.data_manager.initialize()
            logger.info("Data manager initialized")

            # Start the monitor in background
            monitor.start()
            logger.info("Started monitoring thread")

            # Initialize timing configs
            await self._initialize_timing_configs()
            logger.info("Timing configs initialized")

        except Exception as e:
            logger.error(f"Error initializing core components: {str(e)}")
            raise RuntimeError("Core component initialization failed") from e

    async def _initialize_timing_configs(self):
        """Initialize timing configurations for core components."""
        try:
            await orchestrator.register_handler(
                "thread_pool",
                TimingConfig(
                    min_interval=0.05,
                    max_interval=0.5,
                    burst_limit=20,
                    cooldown=0.2,
                    priority=80
                )
            )
            
            await orchestrator.register_handler(
                "async_tasks",
                TimingConfig(
                    min_interval=0.01,
                    max_interval=0.1,
                    burst_limit=100,
                    cooldown=0.5,
                    priority=90
                )
            )
            
            await orchestrator.register_handler(
                "data_manager",
                TimingConfig(
                    min_interval=0.1,
                    max_interval=1.0,
                    burst_limit=50,
                    cooldown=1.0,
                    priority=70
                )
            )
        except Exception as e:
            logger.error(f"Error initializing timing configs: {str(e)}")
            raise

    async def _initialize_signal_manager(self):
        """Initialize signal manager integration when available."""
        try:
            from tenire.servicers import get_signal_manager
            
            # Get or create event loop in a thread-safe way
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
            # Run get_signal_manager in the appropriate context
            if loop.is_running():
                self._signal_manager = await get_signal_manager()
            else:
                self._signal_manager = loop.run_until_complete(get_signal_manager())
                
            if not self._signal_manager:
                logger.warning("Signal manager not available during initialization")
                return
                
            # Register any signal-dependent configurations
            if loop.is_running():
                loop.create_task(self._register_signal_handlers())
            else:
                loop.run_until_complete(self._register_signal_handlers())
                
            logger.info("Signal manager integration initialized successfully")
            
        except ImportError:
            logger.debug("Signal manager module not available")
        except Exception as e:
            logger.error(f"Error initializing signal manager integration: {str(e)}")
            # Don't raise here to allow degraded operation
            self._signal_manager = None

    async def _register_signal_handlers(self):
        """Register signal handlers once signal manager is available."""
        if not self._signal_manager:
            return
            
        try:
            # Register cleanup handler
            if self._compactor:
                self._compactor.register_cleanup_task(
                    name="monitor_cleanup",
                    cleanup_func=monitor.stop,
                    priority=90,
                    is_async=False,
                    metadata={"tags": ["monitor", "concurrency"]}
                )
        except Exception as e:
            logger.error(f"Error registering signal handlers: {str(e)}")
            raise

    @property
    def is_initialized(self) -> bool:
        """Check if the concurrency manager is initialized."""
        return self._initialized and self._initialization_complete.is_set()
        
    def check_initialized(self) -> bool:
        """
        Legacy method for checking initialization status.
        Provided for backward compatibility.
        
        Returns:
            bool: True if initialized, False otherwise
        """
        return self.is_initialized

    async def initialize(self) -> None:
        """Initialize the concurrency manager with improved dependency handling."""
        async with self._initialization_lock:
            if self.is_initialized or self._initialization_complete.is_set():
                return

            try:
                # Ensure we're in the right event loop
                current_loop = asyncio.get_running_loop()
                if self._main_event_loop and current_loop != self._main_event_loop:
                    logger.warning("Switching to main event loop...")
                    asyncio.set_event_loop(self._main_event_loop)

                # Get container and validate dependencies
                container_instance = await self._get_container()
                
                # Initialize core components first
                await self._initialize_core_components()

                # Register with container with proper dependencies
                await container_instance.register(
                    'concurrency_manager',
                    self,
                    dependencies={'event_loop_manager', 'signal_manager'},
                    priority=900,  # High priority, just below signal_manager
                    metadata={
                        'is_critical': True,
                        'provides': ['thread_pool', 'async_tasks', 'data_manager'],
                        'tags': {'core', 'concurrency'}
                    }
                )

                # Mark as initialized
                self._initialized = True
                self._initialization_complete.set()

                # Schedule signal manager integration in background
                loop = self._main_event_loop or asyncio.get_event_loop()
                loop.create_task(self._initialize_signal_manager())

                logger.info("Concurrency manager initialized successfully")

            except Exception as e:
                logger.error(f"Error during initialization: {str(e)}")
                self._initialized = False
                self._initialization_complete.clear()
                raise

    async def ensure_initialized(self) -> None:
        """Ensure the concurrency manager is initialized with proper loop handling."""
        if not self.is_initialized:
            # Ensure we're in the right event loop
            if self._main_event_loop:
                current_loop = asyncio.get_event_loop()
                if current_loop != self._main_event_loop:
                    asyncio.set_event_loop(self._main_event_loop)
            
            await self.initialize()
            try:
                await asyncio.wait_for(self._initialization_complete.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.error("Timeout waiting for initialization to complete")
                raise RuntimeError("Initialization timeout")

    @property
    def compactor(self):
        """Get the compactor instance, initializing if necessary."""
        if self._compactor is None:
            try:
                from tenire.organizers.compactor import compactor
                self._compactor = compactor
                if not self._initialization_scheduled:
                    self._schedule_initialization()
            except ImportError:
                logger.debug("Compactor not available")
        return self._compactor

    @property
    def signal_manager(self):
        """Get the signal manager instance, initializing if necessary."""
        if self._signal_manager is None:
            try:
                from tenire.servicers import get_signal_manager
                # Create a new event loop if one doesn't exist
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    
                # Run get_signal_manager in the event loop
                if loop.is_running():
                    self._signal_manager = asyncio.run_coroutine_threadsafe(
                        get_signal_manager(), loop
                    ).result()
                else:
                    self._signal_manager = loop.run_until_complete(get_signal_manager())
                    
                if not self._initialization_scheduled:
                    self._schedule_initialization()
            except ImportError:
                logger.debug("Signal manager not available")
            except Exception as e:
                logger.error(f"Error getting signal manager: {str(e)}")
                return None
        return self._signal_manager

    def configure_thread_pool(self, max_workers: Optional[int] = None) -> None:
        """Configure the thread pool with the specified number of workers."""
        if max_workers is not None:
            self.thread_pool.configure(max_workers)
            logger.debug(f"Configured thread pool with {max_workers} workers")
            
    def update_from_config(self) -> None:
        """Update settings from configuration after it has been initialized."""
        try:
            from tenire.core.config import config_manager
            config = config_manager.config
            
            # Update thread pool configuration based on ChatGPT config
            chatgpt_config = config_manager.get_chatgpt_config()
            if chatgpt_config and chatgpt_config.max_concurrent_requests:
                self.configure_thread_pool(chatgpt_config.max_concurrent_requests)
                
            # Update other settings from config
            if config.max_workers:
                self.configure_thread_pool(config.max_workers)
                
            logger.debug("Updated ConcurrencyManager configuration")
                
        except Exception as e:
            logger.error(f"Failed to update settings from config: {str(e)}")
            raise

    @property
    def gui_state(self) -> dict:
        """Get current GUI state."""
        return self._gui_state.copy()

    def _update_gui_state(self, **kwargs):
        """Update GUI state and notify observers."""
        self._gui_state.update(kwargs)
        if self._gui_process and self._gui_process.is_alive:
            self._command_queue.put(("update_state", self._gui_state.copy()))

    def register_browser_session(self, browser_session) -> None:
        """Register a browser session for lifecycle management."""
        self._browser_session = browser_session
        logger.debug("Registered browser session for lifecycle management")

    def register_browser_context(self, browser_context) -> None:
        """Register a browser context for lifecycle management."""
        self._browser_contexts.add(browser_context)
        logger.debug(f"Registered browser context: {browser_context}")

    async def cleanup_browser(self) -> None:
        """Clean up browser-related resources."""
        try:
            logger.info("Starting browser cleanup")
            
            # Clean up browser contexts
            for context in list(self._browser_contexts):
                try:
                    await context.close()
                    self._browser_contexts.remove(context)
                except Exception as e:
                    logger.error(f"Error closing browser context: {str(e)}")
            
            # Clean up browser session
            if self._browser_session:
                try:
                    await self._browser_session.close_browser_session()
                except Exception as e:
                    logger.error(f"Error closing browser session: {str(e)}")
                finally:
                    self._browser_session = None
            
            logger.info("Browser cleanup completed")
            
        except Exception as e:
            logger.error(f"Error during browser cleanup: {str(e)}")
            raise

    async def start_gui(self, width: int) -> None:
        """Start the GUI process with improved singleton handling."""
        # Use a lock to ensure thread safety
        async with self._gui_process_lock:
            # Check if GUI is already running
            if self._gui_running:
                logger.info("GUI already running")
                return
                
            try:
                # Create GUI process if needed
                if not self._gui_process:
                    self._gui_process = GUIProcess(
                        width=width,
                        command_queue=self._command_queue,
                        response_queue=self._response_queue
                    )
                
                # Start the process
                logger.info("Starting GUI process initialization")
                await self._gui_process.start()
                
                # Wait for GUI to be ready
                ready = await self._wait_for_gui_ready()
                if not ready:
                    raise RuntimeError("GUI failed to start properly")
                
                # Mark GUI as running
                self._gui_running = True
                logger.info("GUI process started successfully")
                
            except Exception as e:
                logger.error(f"Failed to start GUI process: {str(e)}")
                # Clean up on failure
                await self.cleanup_gui()
                raise RuntimeError(f"Failed to start GUI process: {str(e)}")
                
    async def _wait_for_gui_ready(self) -> bool:
        """Wait for GUI to be ready with timeout."""
        try:
            start_time = asyncio.get_event_loop().time()
            timeout = 30  # 30 second timeout
            
            while True:
                if asyncio.get_event_loop().time() - start_time > timeout:
                    logger.error("Timeout waiting for GUI to be ready")
                    return False
                    
                try:
                    response = await self.get_gui_response(timeout=0.5)
                    if response and response[0] == "state_update":
                        state = response[1]
                        if state.get("status") == "ready":
                            return True
                        elif state.get("status") == "error":
                            logger.error(f"GUI error: {state.get('error', 'Unknown error')}")
                            return False
                except asyncio.TimeoutError:
                    await asyncio.sleep(0.1)
                    continue
                    
        except Exception as e:
            logger.error(f"Error waiting for GUI: {str(e)}")
            return False
            
    async def cleanup_gui(self) -> None:
        """Clean up GUI process with improved error handling."""
        async with self._gui_process_lock:
            try:
                if self._gui_process:
                    logger.info("Starting GUI cleanup")
                    await self._gui_process.stop()
                    self._gui_process = None
                    self._gui_running = False
                    self._clear_queues()
                    logger.info("GUI cleanup completed")
            except Exception as e:
                logger.error(f"Error during GUI cleanup: {str(e)}")
                # Force cleanup in case of errors
                self._gui_process = None
                self._gui_running = False
                self._clear_queues()

    def _clear_queues(self):
        """Clear command and response queues with improved error handling."""
        try:
            # Clear command queue
            while True:
                try:
                    self._command_queue.get_nowait()
                except queue.Empty:
                    break
                except Exception as e:
                    logger.error(f"Error clearing command queue: {str(e)}")
                    break
                    
            # Clear response queue
            while True:
                try:
                    self._response_queue.get_nowait()
                except queue.Empty:
                    break
                except Exception as e:
                    logger.error(f"Error clearing response queue: {str(e)}")
                    break
        except Exception as e:
            logger.error(f"Error clearing queues: {str(e)}")

    def send_gui_command(self, command: str, data: Any = None) -> None:
        """
        Send a command to the GUI with state tracking.
        
        Args:
            command: The command to send
            data: Optional data to send with the command
        """
        if self._gui_process and self._gui_process.is_alive:
            self._command_queue.put((command, data))
            self._update_gui_state(last_command=command)

    def update_gui_status(self, status: str) -> None:
        """Update GUI status with state tracking."""
        self.send_gui_command("update_status", status)
        self._update_gui_state(status=status)

    def add_gui_message(self, text: str, is_user: bool = False) -> None:
        """Add a message to the GUI with state tracking."""
        self.send_gui_command("add_message", (text, is_user))
        self._update_gui_state(last_response=text if not is_user else None)

    async def get_gui_response(self, timeout: float = 0.1) -> Optional[Tuple[str, Any]]:
        """
        Get a response from the GUI with timeout and state tracking.
        
        Args:
            timeout: Timeout in seconds
            
        Returns:
            Tuple of (command, data) or None if no response
        """
        if not self._gui_process or not self._gui_process.is_alive:
            return None
            
        try:
            loop = asyncio.get_event_loop()
            response = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    lambda: self._response_queue.get(timeout=timeout)
                ),
                timeout=timeout
            )
            if response:
                cmd, data = response
                self._update_gui_state(last_response=data)
            return response
        except (asyncio.TimeoutError, queue.Empty):
            return None
        except Exception as e:
            logger.error(f"Error getting GUI response: {str(e)}")
            return None

    async def run_in_thread(self, func: Callable[..., T], *args, **kwargs) -> T:
        """
        Run a function in a thread pool.
        
        Args:
            func: The function to execute
            *args: Positional arguments for the function
            **kwargs: Keyword arguments for the function
            
        Returns:
            The result of the function execution
        """
        return await self.thread_pool.execute(func, *args, **kwargs)
            
    def trigger_shutdown(self) -> None:
        """Trigger shutdown of all managed resources."""
        logger.info("Triggering concurrency manager shutdown")
        self._shutdown_event.set()
        self.async_tasks.trigger_shutdown()
        
        # Trigger compactor shutdown
        self.compactor.register_cleanup_task(
            name="shutdown_cleanup",
            cleanup_func=self.cleanup,
            priority=100,
            is_async=True,
            metadata={"tags": ["shutdown"]}
        )
        asyncio.create_task(self.cleanup_gui())

    def stop_gui(self):
        """Stop the GUI process if it's running."""
        if self._gui_process is not None:
            logger.debug("Stopping GUI process")
            try:
                self._command_queue.put(("shutdown", None))
                self._gui_process.join(timeout=5)
            except Exception as e:
                logger.error(f"Error stopping GUI process: {str(e)}")
            finally:
                if self._gui_process.is_alive:
                    self._gui_process.terminate()
                self._gui_process = None
                self._update_gui_state(status='stopped', is_ready=False)

    async def cleanup(self) -> None:
        """Clean up all concurrency-related resources with improved synchronization."""
        if self._is_shutting_down:
            return
            
        async with self._cleanup_lock:
            try:
                self._is_shutting_down = True
                logger.info("Starting concurrency cleanup")
                
                # Stop the monitor first
                monitor.stop()
                
                # Clean up GUI before other resources
                await self.cleanup_gui()
                
                # Stop accepting new tasks
                self.async_tasks.trigger_shutdown()
                
                # Cancel all running tasks
                logger.info("Cancelling running tasks")
                self.async_tasks.cancel_tasks()
                
                # Wait for tasks to complete with timeout
                try:
                    await asyncio.wait_for(
                        self.async_tasks.wait_for_tasks(),
                        timeout=5.0
                    )
                except asyncio.TimeoutError:
                    logger.warning("Timeout waiting for tasks during cleanup")
                
                # Clean up browser resources
                await self.cleanup_browser()
                
                # Shutdown data manager
                logger.info("Shutting down DataManager")
                await self.data_manager.shutdown()
                
                # Shutdown thread pool
                logger.info("Shutting down thread pool")
                self.thread_pool.shutdown(wait=True)
                
                # Clear all state
                self._command_queue = None
                self._response_queue = None
                self._browser_session = None
                self._browser_contexts.clear()
                self._gui_state.clear()
                
                logger.info("Concurrency cleanup completed")
                
            except Exception as e:
                logger.error(f"Error during concurrency cleanup: {str(e)}", exc_info=True)
                raise
            finally:
                self._is_shutting_down = False


# Create global instance and register with container
concurrency_manager = ConcurrencyManager()

# Register with container using sync method
container.register_sync('concurrency_manager', concurrency_manager)

# Initialize dependencies after registration
def _initialize_dependencies():
    try:
        signal_manager = container.get('signal_manager')
        compactor = container.get('compactor')
        if signal_manager and compactor:
            # Register cleanup tasks with compactor
            compactor.register_cleanup_task(
                name="concurrency_cleanup",
                cleanup_func=concurrency_manager.cleanup,
                priority=100,  # Highest priority for concurrency cleanup
                is_async=True,
                metadata={"tags": ["core", "concurrency"]}
            )
            
            # Register thread pool with compactor
            if hasattr(concurrency_manager.thread_pool, '_pool'):
                compactor.register_thread_pool(concurrency_manager.thread_pool._pool)
            
            # Update configuration
            try:
                concurrency_manager.update_from_config()
            except Exception as e:
                logger.error(f"Error updating concurrency manager config: {str(e)}")
            
            logger.debug("ConcurrencyManager dependencies initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing ConcurrencyManager dependencies: {str(e)}")
        # Retry after a short delay
        loop = asyncio.get_event_loop()
        loop.call_later(0.5, _initialize_dependencies)

# Register lazy initialization using sync method
container.register_lazy_sync(
    'concurrency_manager_init',
    _initialize_dependencies,
    dependencies={'signal_manager', 'compactor'}
)
