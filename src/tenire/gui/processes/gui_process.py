"""
Process management for running the GUI in a separate process.
"""

# Standard library imports
import asyncio
import multiprocessing
import queue
from importlib import import_module
from typing import Any, Dict, List, Optional, Callable, TYPE_CHECKING

# Third-party imports
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Signal as QtSignal
import qasync

# Local imports
from tenire.organizers.compactor import compactor
from tenire.servicers import SignalManager, get_signal_manager
from tenire.core.codex import Signal, SignalType
from tenire.core.event_loop import event_loop_manager
from tenire.utils.logger import get_logger
from tenire.actions.command_processor import CommandProcessor
from tenire.organizers.scheduler import InitializationTask

if TYPE_CHECKING:
    from tenire.gui.gui import SidebarGUI

logger = get_logger(__name__)

class AsyncSignalEmitter:
    """Helper class to safely emit signals in Qt context with improved thread safety."""
    
    def __init__(self) -> None:
        """Initialize the signal emitter."""
        self._loop: Optional[qasync.QEventLoop] = None
        self._loop_lock: asyncio.Lock = asyncio.Lock()
        
    async def _ensure_loop(self) -> qasync.QEventLoop:
        """
        Ensure we have a qasync event loop with proper thread safety.
        
        Returns:
            qasync.QEventLoop: The event loop instance
            
        Raises:
            RuntimeError: If unable to obtain or create a valid event loop
        """
        async with self._loop_lock:
            if self._loop is None:
                try:
                    # First try to get existing Qt event loop
                    loop = event_loop_manager.get_event_loop(for_qt=True)
                    if isinstance(loop, qasync.QEventLoop):
                        self._loop = loop
                    else:
                        # Create new qasync loop
                        self._loop = qasync.QEventLoop()
                        asyncio.set_event_loop(self._loop)
                except Exception as e:
                    raise RuntimeError(f"Failed to create qasync event loop: {str(e)}")
                    
            if not self._loop:
                raise RuntimeError("Could not obtain or create qasync event loop")
                
            return self._loop
            
    async def emit_signal(self, signal: Signal) -> None:
        """
        Safely emit a signal with proper error handling.
        
        Args:
            signal: The signal to emit
            
        Raises:
            RuntimeError: If signal emission fails
        """
        try:
            loop = await self._ensure_loop()
            if not loop.is_running():
                logger.warning("Event loop not running during signal emission")
                return
            
            # Get current loop for comparison
            try:
                current_loop = asyncio.get_event_loop()
            except RuntimeError:
                current_loop = None
            
            # Ensure signal emission happens in the correct event loop
            if current_loop is not loop:
                future = asyncio.run_coroutine_threadsafe(
                    SignalManager().emit(signal), 
                    loop
                )
                await asyncio.wrap_future(future)
            else:
                await SignalManager().emit(signal)
                
        except Exception as e:
            error_msg = f"Error emitting signal: {str(e)}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

class GUIProcess(multiprocessing.Process):
    """Process for running the GUI application."""
    
    def __init__(
        self,
        command_queue: multiprocessing.Queue,
        response_queue: multiprocessing.Queue,
        width: int = 400
    ) -> None:
        """
        Initialize the GUI process.
        
        Args:
            command_queue: Queue for receiving commands
            response_queue: Queue for sending responses
            width: Width of the GUI window
        """
        super().__init__()
        self.command_queue = command_queue
        self.response_queue = response_queue
        self.width = width
        self._state: Dict[str, Any] = {
            'status': 'initialized',
            'is_ready': False,
            'error': None,
            'last_command': None,
            'last_response': None,
            'cleanup_tasks': set(),
            'initialization_attempts': 0
        }
        self.daemon = True  # Process will be terminated when main process exits
        self._signal_emitter = AsyncSignalEmitter()
        self._initialized = False
        self._shutdown_event = asyncio.Event()
        self._startup_event = asyncio.Event()
        self._cleanup_handlers: List[Callable[[], None]] = []
        self._qt_app: Optional[QApplication] = None
        self._event_loop: Optional[qasync.QEventLoop] = None
        
        # Register with component registry
        self._register_with_registry()

    async def _connect_window_signals(self, window: 'SidebarGUI', signal_manager: SignalManager) -> None:
        """Connect window signals to signal manager."""
        try:
            from tenire.organizers.scheduler import component_scheduler, TimingConfig
            
            # Register GUI timing config
            await component_scheduler.schedule_component(
                InitializationTask(
                    component="gui_signal_handler",
                    dependencies=set(),
                    priority=85,  # High priority for UI responsiveness
                    init_fn=lambda: None,
                    timeout=5.0,
                    retry_count=3,
                    retry_delay=0.1,
                    metadata={"type": "gui", "purpose": "signal_handling"}
                )
            )

            def create_signal_wrapper(signal_type: SignalType, data_transformer: Optional[Callable] = None):
                """Create a signal wrapper function that properly handles coroutines."""
                def wrapper(*args, **kwargs):
                    try:
                        data = {}
                        if data_transformer:
                            data = data_transformer(*args, **kwargs)
                            
                        # Get the event loop
                        loop = asyncio.get_event_loop()
                        if loop.is_running():
                            # Schedule coroutine execution using component scheduler
                            async def emit_signal():
                                try:
                                    await signal_manager.emit(Signal(
                                        type=signal_type,
                                        data=data,
                                        source="gui"
                                    ))
                                except Exception as e:
                                    logger.error(f"Error emitting signal: {str(e)}")

                            component_scheduler.schedule_component(
                                InitializationTask(
                                    component=f"gui_signal_{signal_type.name}",
                                    dependencies=set(),
                                    priority=80,
                                    init_fn=emit_signal,
                                    timeout=5.0,
                                    retry_count=2,
                                    retry_delay=0.1,
                                    metadata={"type": "gui", "signal": signal_type.name}
                                )
                            )
                        else:
                            logger.warning(f"Event loop not running for signal {signal_type}")
                    except Exception as e:
                        logger.error(f"Error in signal wrapper: {str(e)}")
                return wrapper

            # Connect main widget signals with proper async handling
            window.main_widget.message_received.connect(
                create_signal_wrapper(SignalType.GUI_MESSAGE_RECEIVED, 
                    lambda text, is_user: {"text": text, "is_user": is_user})
            )
            
            window.main_widget.status_updated.connect(
                create_signal_wrapper(SignalType.GUI_STATUS_UPDATED,
                    lambda status: {"status": status})
            )
            
            window.main_widget.command_processed.connect(
                create_signal_wrapper(SignalType.COMMAND_PROCESSED)
            )
            
            window.main_widget.state_updated.connect(
                create_signal_wrapper(SignalType.GUI_STATE)
            )
            
            window.main_widget.error_occurred.connect(
                create_signal_wrapper(SignalType.GUI_ERROR,
                    lambda error: {"error": error})
            )

            # Add browser-specific signal connections with proper async handling
            def create_browser_signal_wrapper(signal_type: SignalType):
                def wrapper(command, params):
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        async def emit_browser_signal():
                            try:
                                await signal_manager.emit(Signal(
                                    type=signal_type,
                                    data={"command": command, "params": params},
                                    source="gui"
                                ))
                            except Exception as e:
                                logger.error(f"Error emitting browser signal: {str(e)}")

                        component_scheduler.schedule_component(
                            InitializationTask(
                                component=f"gui_browser_{signal_type.name}",
                                dependencies=set(),
                                priority=80,
                                init_fn=emit_browser_signal,
                                timeout=5.0,
                                retry_count=2,
                                retry_delay=0.1,
                                metadata={"type": "gui", "browser_signal": signal_type.name}
                            )
                        )
                return wrapper
            
            window.main_widget.browser_command.connect(
                create_browser_signal_wrapper(SignalType.BROWSER_COMMAND)
            )
            
            window.main_widget.browser_action.connect(
                create_browser_signal_wrapper(SignalType.BROWSER_ACTION)
            )
            
            logger.info("Successfully connected window signals")
            
        except Exception as e:
            error_msg = f"Failed to connect window signals: {str(e)}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

    def _handle_future_result(self, future: asyncio.Future) -> None:
        """Handle the result of an async future."""
        try:
            if future.done():
                if future.exception():
                    logger.error(f"Error in async operation: {future.exception()}")
                else:
                    result = future.result()
                    if result:
                        logger.debug(f"Async operation completed: {result}")
        except Exception as e:
            logger.error(f"Error handling future result: {str(e)}")

    async def _register_gui_handlers(self, signal_manager: SignalManager, window: 'SidebarGUI') -> None:
        """Register GUI-specific signal handlers."""
        try:
            # Get command processor instance
            command_processor = CommandProcessor()

            # Register core GUI handlers using the main widget's handlers
            handlers = [
                (SignalType.GUI_UPDATE, window.main_widget._handle_gui_update, 10),
                (SignalType.GUI_ACTION, window.main_widget._handle_gui_action, 10),
                (SignalType.COMMAND_RECEIVED, window.main_widget._handle_command_received, 10),
                (SignalType.COMMAND_PROCESSED, window.main_widget._handle_command_processed, 10),
                (SignalType.COMMAND_COMPLETED, window.main_widget._handle_command_completed, 10),
                (SignalType.COMMAND_ERROR, window.main_widget._handle_command_error, 10),
                # Add browser-specific handlers
                (SignalType.BROWSER_COMMAND_COMPLETED, window.main_widget._handle_browser_command_completed, 10),
                (SignalType.BROWSER_ACTION_COMPLETED, window.main_widget._handle_browser_action_completed, 10),
                (SignalType.BROWSER_ERROR, window.main_widget._handle_browser_error, 10),
                (SignalType.BROWSER_STATUS, window.main_widget._handle_browser_status, 8),
                (SignalType.BROWSER_NAVIGATION, window.main_widget._handle_browser_navigation, 8),
                (SignalType.BROWSER_INTERACTION, window.main_widget._handle_browser_interaction, 8)
            ]
            
            # Register command processor handlers
            command_handlers = [
                (SignalType.GUI_ACTION, command_processor._handle_gui_action_signal),
                (SignalType.GUI_UPDATE, command_processor._handle_gui_update_signal),
                (SignalType.COMMAND_CONFIRMATION, command_processor._handle_command_confirmation),
                # Add browser command handlers
                (SignalType.BROWSER_COMMAND, command_processor._handle_browser_command_signal),
                (SignalType.BROWSER_ACTION, command_processor._handle_browser_action_signal)
            ]

            # Register all handlers
            for signal_type, handler, priority in handlers:
                try:
                    if not hasattr(handler.__self__, handler.__name__):
                        logger.warning(f"Handler {handler.__name__} not found on {handler.__self__.__class__.__name__}")
                        continue
                        
                    signal_manager.register_handler_sync(
                        signal_type,
                        handler,
                        priority=priority,
                        is_gui=True
                    )
                    logger.debug(f"Registered handler {handler.__name__} for signal {signal_type}")
                except Exception as e:
                    logger.error(f"Failed to register handler {handler.__name__} for signal {signal_type}: {str(e)}")
                    raise

            # Register command processor handlers
            for signal_type, handler in command_handlers:
                try:
                    signal_manager.register_handler_sync(
                        signal_type,
                        handler,
                        priority=5,  # Lower priority than GUI handlers
                        is_gui=True
                    )
                    logger.debug(f"Registered command processor handler for signal {signal_type}")
                except Exception as e:
                    logger.error(f"Failed to register command processor handler for signal {signal_type}: {str(e)}")
                    raise

            # Register GUI signal handlers with command processor
            command_processor.register_signal_handler("gui_update", window.main_widget._handle_gui_update)
            command_processor.register_signal_handler("gui_action", window.main_widget._handle_gui_action)
            command_processor.register_signal_handler("browser_command", window.main_widget._handle_browser_command)
            command_processor.register_signal_handler("browser_action", window.main_widget._handle_browser_action)
            
            logger.info("Successfully registered GUI signal handlers")
            
        except Exception as e:
            error_msg = f"Failed to register GUI signal handlers: {str(e)}"
            logger.error(error_msg)
            raise RuntimeError(error_msg) from e

    def _register_with_registry(self) -> None:
        """Register the GUI process with the component registry."""
        try:
            # Define timing config for the GUI process
            from tenire.organizers.scheduler import TimingConfig
            timing_config = TimingConfig(
                min_interval=0.1,
                max_interval=1.0,
                burst_limit=25,
                cooldown=0.5,
                priority=85
            )
            
            # Register with component registry
            from tenire.structure.component_registry import component_registry, ComponentType
            
            # Create a future for async registration
            loop = asyncio.get_event_loop()
            if loop.is_running():
                future = asyncio.run_coroutine_threadsafe(
                    component_registry.register_component(
                        name="gui_process",
                        component=self,
                        component_type=ComponentType.GUI,
                        provides=["gui_interface", "command_processing"],
                        dependencies={
                            "signal_manager",      # For signal handling
                            "event_loop_manager",  # For event loop management
                            "async_task_manager",  # For task scheduling
                            "compactor",           # For cleanup
                            "thread_pool_manager"  # For background operations
                        },
                        priority=60,
                        health_checks={
                            "process_health": self._check_process_health,
                            "queue_health": self._check_queue_health
                        },
                        timing_config=timing_config,
                        cleanup_priority=90,
                        tags={"gui", "process", "frontend"},
                        is_critical=False,
                        metadata={
                            "requires_main_thread": True,
                            "requires_qt": True
                        }
                    ),
                    loop
                )
                # Wait for registration to complete with timeout
                try:
                    future.result(timeout=30.0)  # 30 second timeout
                    logger.debug("Registered GUI process with component registry")
                except Exception as e:
                    logger.error(f"Error during async component registration: {str(e)}")
            else:
                logger.error("No running event loop for GUI process registration")
            
        except Exception as e:
            logger.error(f"Error registering with component registry: {str(e)}")

    async def _check_process_health(self) -> tuple[bool, str]:
        """Check the health of the GUI process."""
        try:
            if not self.is_alive():
                return False, "GUI process is not running"
            if self._state.get('error'):
                return False, f"GUI process error: {self._state['error']}"
            return True, "GUI process is healthy"
        except Exception as e:
            return False, f"Error checking process health: {str(e)}"
            
    async def _check_queue_health(self) -> tuple[bool, str]:
        """Check the health of command and response queues."""
        try:
            if self.command_queue.full():
                return False, "Command queue is full"
            if self.response_queue.full():
                return False, "Response queue is full"
            return True, "Queues are healthy"
        except Exception as e:
            return False, f"Error checking queue health: {str(e)}"

    def _register_cleanup_tasks(self):
        """Register cleanup tasks with the compactor."""
        try:
            if not compactor:
                logger.warning("Compactor not available for cleanup task registration")
                return
                
            cleanup_tasks = [
                ("process_cleanup", self._cleanup_process, 98),
                ("queues_cleanup", self._cleanup_queues, 97),
                ("event_loop_cleanup", self._cleanup_event_loop, 96)
            ]
            
            for name, func, priority in cleanup_tasks:
                task_name = f"gui_process_{name}"
                compactor.register_cleanup_task(
                    name=task_name,
                    cleanup_func=func,
                    priority=priority,
                    is_async=False,
                    metadata={"tags": ["gui", "process"]}
                )
                self._state['cleanup_tasks'].add(task_name)
                
        except Exception as e:
            logger.error(f"Error registering cleanup tasks: {str(e)}")
            
    def _cleanup_queues(self):
        """Clean up command and response queues."""
        try:
            # Clear command queue
            while not self.command_queue.empty():
                try:
                    self.command_queue.get_nowait()
                except queue.Empty:
                    break
                    
            # Clear response queue
            while not self.response_queue.empty():
                try:
                    self.response_queue.get_nowait()
                except queue.Empty:
                    break
                    
            self._update_state(status='queues_cleaned')
        except Exception as e:
            logger.error(f"Error cleaning up queues: {str(e)}")
            
    def _cleanup_event_loop(self) -> None:
        """Clean up event loop resources."""
        try:
            if self._event_loop:
                if not self._event_loop.is_closed():
                    self._event_loop.stop()
                    self._event_loop.close()
                self._event_loop = None
                
            if self._qt_app:
                self._qt_app.quit()
                self._qt_app = None
                
        except Exception as e:
            logger.error(f"Error cleaning up event loop: {str(e)}")

    def _update_state(self, **kwargs):
        """Update internal state and notify parent process."""
        try:
            self._state.update(kwargs)
            if hasattr(self, 'response_queue') and self.response_queue is not None:
                try:
                    self.response_queue.put(("state_update", self._state.copy()))
                except Exception as e:
                    logger.error(f"Error sending state update: {str(e)}")
                    
            # Notify concurrency manager if available
            try:
                from tenire.organizers.concurrency import concurrency_manager
                if concurrency_manager:
                    concurrency_manager._update_gui_state(**kwargs)
            except Exception as e:
                logger.debug(f"Could not notify concurrency manager of state update: {str(e)}")
                
        except Exception as e:
            logger.error(f"Error updating state: {str(e)}")

    async def _initialize_gui(self) -> None:
        """Initialize the GUI with proper signal handling and error recovery."""
        try:
            # Wait for required dependencies
            signal_manager = None
            for _ in range(10):  # Try for 10 seconds
                signal_manager = await get_signal_manager()
                if signal_manager:
                    break
                await asyncio.sleep(1)
                
            if not signal_manager:
                raise RuntimeError("Failed to initialize signal manager")
                
            # Ensure event loop manager is initialized
            if not event_loop_manager.is_initialized():
                raise RuntimeError("Event loop manager not initialized")
                
            # Ensure async task manager is available
            from tenire.organizers.concurrency import get_async_task_manager
            async_task_manager = await get_async_task_manager()
            if not async_task_manager:
                raise RuntimeError("Async task manager not initialized")
                
            # Create QApplication instance if needed
            if not QApplication.instance():
                self._qt_app = QApplication([])
                self._qt_app.setStyle('Fusion')  # Use Fusion style for better performance
            else:
                self._qt_app = QApplication.instance()
            
            # Create qasync event loop with optimized settings
            self._event_loop = qasync.QEventLoop(self._qt_app)
            asyncio.set_event_loop(self._event_loop)
            
            # Create main window using lazy import and async initialization
            try:
                # Import the GUI class only when needed
                gui_module = import_module('tenire.gui.gui')
                SidebarGUI = getattr(gui_module, 'SidebarGUI')
                
                # Create window with minimal initial setup
                window = SidebarGUI(
                    width=self.width,
                    command_queue=self.command_queue,
                    response_queue=self.response_queue
                )
                
                # Ensure window is properly initialized
                if not hasattr(window, 'main_widget') or not window.main_widget:
                    raise RuntimeError("GUI window not properly initialized")
                
                # Show window early and notify ready state
                window.show()
                self._update_state(status='ready', is_ready=True)
                self.response_queue.put(("status", "ready"))
                self._startup_event.set()
                
                # Initialize remaining components asynchronously
                async def init_remaining():
                    try:
                        # Connect window to signal manager
                        await self._connect_window_signals(window, signal_manager)
                        # Register GUI signal handlers
                        await self._register_gui_handlers(signal_manager, window)
                    except Exception as e:
                        logger.error(f"Error in async initialization: {str(e)}")
                
                # Schedule async initialization
                self._event_loop.create_task(init_remaining())
                
                # Start Qt event loop with qasync
                try:
                    logger.info("Starting Qt event loop with qasync")
                    with self._event_loop:
                        self._event_loop.run_forever()
                except Exception as e:
                    logger.error(f"Error in Qt event loop: {str(e)}")
                    raise
                
            except ImportError as e:
                logger.error(f"Failed to import GUI module: {str(e)}")
                raise RuntimeError(f"GUI module import failed: {str(e)}")
            except Exception as e:
                logger.error(f"Error creating GUI window: {str(e)}")
                raise
            
        except Exception as e:
            error_msg = f"Error initializing GUI: {str(e)}"
            logger.error(error_msg)
            self._update_state(status='error', error=error_msg)
            self.response_queue.put(("error", error_msg))
            raise

    def run(self) -> None:
        """Run the GUI process with improved error handling and cleanup."""
        try:
            # Create qasync event loop
            self._event_loop = qasync.QEventLoop()
            asyncio.set_event_loop(self._event_loop)
            
            # Register cleanup handlers
            def cleanup_handler():
                try:
                    self._cleanup_event_loop()
                except Exception as e:
                    logger.error(f"Error in cleanup handler: {str(e)}")
                    
            self._cleanup_handlers.append(cleanup_handler)
            
            # Register process main loop as Qt loop
            event_loop_manager.register_process_main_loop(self._event_loop, is_qt=True)
            
            # Register cleanup tasks
            self._register_cleanup_tasks()
            
            # Initialize GUI with proper async handling
            try:
                with self._event_loop:
                    self._event_loop.run_until_complete(self._initialize_gui())
            except Exception as e:
                raise RuntimeError(f"Failed to initialize GUI: {str(e)}")
            
        except Exception as e:
            error_msg = f"Error in GUI process: {str(e)}"
            logger.error(error_msg, exc_info=True)
            self._update_state(status='error', error=error_msg)
            try:
                self.response_queue.put(("error", error_msg))
            except Exception:
                pass
            
        finally:
            # Ensure cleanup
            try:
                self._cleanup_process()
            except Exception as e:
                logger.error(f"Error during final cleanup: {str(e)}")

    def _cleanup_process(self):
        """Clean up process resources."""
        try:
            self._update_state(status='cleaning_up')
            
            # Run cleanup handlers
            for handler in self._cleanup_handlers:
                try:
                    handler()
                except Exception as e:
                    logger.error(f"Error in cleanup handler: {str(e)}")
                    
            # Clean up queues
            self._cleanup_queues()
            
            # Final state update
            self._update_state(
                status='cleaned_up',
                is_ready=False,
                error=None,
                last_command=None,
                last_response=None
            )
            
        except Exception as e:
            error_msg = f"Error during process cleanup: {str(e)}"
            logger.error(error_msg)
            self._update_state(status='error', error=error_msg)
            try:
                self.response_queue.put(("error", error_msg))
            except Exception:
                pass 