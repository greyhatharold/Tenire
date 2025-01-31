"""
Signal manager implementation with enhanced error handling and health monitoring.
"""

import asyncio
import inspect
from typing import Dict, List, Any, Callable, TypeVar, Optional, Union
import threading
import time

from tenire.utils.logger import get_logger
from tenire.core.codex import (
    SignalType,
    Signal, SignalHandler,
    DataCleaningStage
)
from tenire.servicers.signal import SignalManager, get_signal_manager
from tenire.core.event_loop import event_loop_manager

logger = get_logger(__name__)

# Type variables for generics
T = TypeVar('T')

# Global lock for signal manager initialization
_signal_manager_lock = asyncio.Lock()
_signal_manager_instance = None
_initialization_complete = asyncio.Event()

async def _ensure_compactor_initialized():
    """Ensure compactor is properly initialized."""
    try:
        from tenire.organizers.compactor import compactor
        if compactor is None:
            logger.debug("Compactor is None")
            return None
            
        # Check if compactor needs initialization
        if hasattr(compactor, 'initialize') and callable(compactor.initialize):
            if inspect.iscoroutinefunction(compactor.initialize):
                try:
                    await compactor.initialize()
                except Exception as e:
                    logger.warning(f"Failed to initialize compactor (async): {str(e)}")
            else:
                try:
                    compactor.initialize()
                except Exception as e:
                    logger.warning(f"Failed to initialize compactor (sync): {str(e)}")
                    
        return compactor
    except ImportError:
        logger.debug("Compactor not available")
        return None
    except Exception as e:
        logger.error(f"Error ensuring compactor initialization: {str(e)}")
        return None

async def _register_cleanup_task(compactor, signal_manager):
    """Register cleanup task with proper async/sync handling."""
    if not hasattr(compactor, 'register_cleanup_task') or not callable(compactor.register_cleanup_task):
        logger.warning("Compactor does not have register_cleanup_task method")
        return
        
    try:
        if inspect.iscoroutinefunction(compactor.register_cleanup_task):
            await compactor.register_cleanup_task(
                name="signal_manager_cleanup",
                cleanup_func=signal_manager.cleanup,
                priority=100,
                is_async=True,
                metadata={"tags": ["core", "signal"]}
            )
        else:
            compactor.register_cleanup_task(
                name="signal_manager_cleanup",
                cleanup_func=signal_manager.cleanup,
                priority=100,
                is_async=True,
                metadata={"tags": ["core", "signal"]}
            )
    except Exception as e:
        logger.error(f"Error registering cleanup task: {str(e)}")
        raise

async def _ensure_loop_running(loop: asyncio.AbstractEventLoop) -> bool:
    """
    Ensure the given event loop is running.
    
    Args:
        loop: The event loop to ensure is running
        
    Returns:
        bool: True if loop is running, False otherwise
    """
    try:
        if not loop.is_running():
            # Start loop in a background thread if not running
            def run_loop():
                asyncio.set_event_loop(loop)
                if not loop.is_closed():
                    loop.run_forever()
                    
            thread = threading.Thread(target=run_loop, daemon=True)
            thread.start()
            
            # Give the loop time to start
            await asyncio.sleep(0.1)
            
            # Verify loop is running
            return loop.is_running()
            
        return True
        
    except Exception as e:
        logger.error(f"Error ensuring loop is running: {str(e)}")
        return False

async def _ensure_event_loop() -> Optional[asyncio.AbstractEventLoop]:
    """
    Ensure an event loop is available and properly initialized.
    
    Returns:
        Optional[asyncio.AbstractEventLoop]: The initialized event loop or None if initialization fails
    """
    try:
        from tenire.core.event_loop import event_loop_manager
        
        # Get the event loop through the manager
        loop = event_loop_manager.get_event_loop(for_qt=True)
        if loop is not None and loop.is_running():
            return loop
            
        # Try getting regular loop if Qt loop not available
        loop = event_loop_manager.get_event_loop(for_qt=False)
        if loop is not None and loop.is_running():
            return loop
            
        # If no running loop found, let event_loop_manager create one
        event_loop_manager._initialize_loop()
        loop = event_loop_manager.get_event_loop()
        
        if loop is not None and await _ensure_loop_running(loop):
            return loop
            
        logger.error("Failed to get or create event loop")
        return None
        
    except Exception as e:
        logger.error(f"Error ensuring event loop: {str(e)}")
        return None

async def ensure_signal_manager() -> Optional[SignalManager]:
    """
    Ensure signal manager is initialized and available.
    
    Returns:
        Optional[SignalManager]: Initialized signal manager instance or None if initialization fails
    """
    global _signal_manager_instance, _initialization_complete
    
    # Fast path - return existing initialized instance
    if _initialization_complete.is_set() and _signal_manager_instance is not None:
        return _signal_manager_instance
    
    try:
        from tenire.core.event_loop import event_loop_manager
        from tenire.core.container import container
        
        # Get event loop first
        loop = await _ensure_event_loop()
        if loop is None:
            logger.error("Failed to get event loop")
            return None
            
        # Use event loop manager's async context manager
        async with event_loop_manager.aloop_scope(for_qt=True) as loop:
            async with _signal_manager_lock:
                # Check if another task completed initialization while we were waiting
                if _initialization_complete.is_set() and _signal_manager_instance is not None:
                    return _signal_manager_instance
                
                # Check container first
                signal_manager = container.get('signal_manager')
                if signal_manager is not None and hasattr(signal_manager, '_processing_task'):
                    _signal_manager_instance = signal_manager
                    _initialization_complete.set()
                    return signal_manager
                
                # Get signal manager instance with retries
                retry_count = 0
                max_retries = 3
                signal_manager = None
                
                # Use task watching context manager
                async with event_loop_manager.watch_async_tasks(loop) as tasks:
                    while retry_count < max_retries:
                        try:
                            if signal_manager is None:
                                signal_manager = SignalManager()
                                # Register with container immediately
                                container.register_sync('signal_manager', signal_manager)
                            
                            # Initialize if needed
                            if not hasattr(signal_manager, '_processing_task'):
                                init_task = loop.create_task(signal_manager.initialize())
                                await event_loop_manager.track_task(init_task)
                                
                                try:
                                    await asyncio.wait_for(init_task, timeout=10.0)
                                    break
                                except asyncio.TimeoutError:
                                    logger.warning(f"Initialization attempt {retry_count + 1} timed out")
                                    retry_count += 1
                                    if retry_count < max_retries:
                                        await asyncio.sleep(2 ** retry_count)
                                    continue
                            else:
                                break
                                
                        except Exception as e:
                            logger.warning(f"Attempt {retry_count + 1} failed: {str(e)}")
                            retry_count += 1
                            if retry_count < max_retries:
                                await asyncio.sleep(2 ** retry_count)
                                
                    if signal_manager is None or not hasattr(signal_manager, '_processing_task'):
                        logger.error("Failed to initialize signal manager after retries")
                        return None
                        
                    # Store the instance and mark initialization complete
                    _signal_manager_instance = signal_manager
                    _initialization_complete.set()
                    
                    # Register cleanup task
                    try:
                        # Initialize compactor
                        compactor = await _ensure_compactor_initialized()
                        if compactor is not None:
                            try:
                                await _register_cleanup_task(compactor, signal_manager)
                            except Exception as e:
                                logger.error(f"Failed to register cleanup task: {str(e)}")
                                # Non-critical error, continue
                    except Exception as e:
                        logger.error(f"Error in cleanup registration process: {str(e)}")
                        # Non-critical error, continue
                        
                    return signal_manager
            
    except Exception as e:
        logger.error(f"Error ensuring signal manager: {str(e)}")
        return None

def initialize() -> None:
    """Initialize the signal manager synchronously."""
    import asyncio
    from tenire.core.event_loop import event_loop_manager
    
    try:
        # Get event loop through manager
        loop = event_loop_manager.get_event_loop(for_qt=True)
        if loop is None:
            # Fallback to regular loop
            loop = event_loop_manager.get_event_loop(for_qt=False)
            
        if loop is None:
            # If still no loop, let event_loop_manager create one
            event_loop_manager._initialize_loop()
            loop = event_loop_manager.get_event_loop()
            
        if loop is None:
            logger.error("Failed to get or create event loop")
            return
            
        # Register the loop with event_loop_manager if not already registered
        event_loop_manager.register_process_main_loop(loop)
        
        async def init_and_track():
            init_task = loop.create_task(ensure_signal_manager())
            await event_loop_manager.track_task(init_task)
            await init_task
            
        if loop.is_running():
            # Schedule initialization in the running loop
            loop.create_task(init_and_track())
        else:
            # Start the loop if it's not running
            def run_loop():
                asyncio.set_event_loop(loop)
                if not loop.is_closed():
                    loop.run_forever()
                    
            thread = threading.Thread(target=run_loop, daemon=True)
            thread.start()
            
            # Give the loop time to start
            time.sleep(0.1)
            
            # Now schedule our initialization
            loop.call_soon_threadsafe(
                lambda: loop.create_task(init_and_track())
            )
                
    except Exception as e:
        logger.error(f"Error in initialize: {str(e)}")
        # Don't raise to allow degraded operation

# Initialize on module import if possible
try:
    # Get event loop through manager
    loop = event_loop_manager.get_event_loop(for_qt=True)
    if loop is None:
        # Fallback to regular loop
        loop = event_loop_manager.get_event_loop(for_qt=False)
        
    if loop is None:
        # If still no loop, let event_loop_manager create one
        event_loop_manager._initialize_loop()
        loop = event_loop_manager.get_event_loop()
        
    if loop:
        # Register the loop
        event_loop_manager.register_process_main_loop(loop)
        
        if loop.is_running():
            loop.create_task(ensure_signal_manager())
        else:
            # Start the loop in a background thread
            def run_loop():
                asyncio.set_event_loop(loop)
                if not loop.is_closed():
                    loop.run_forever()
                    
            thread = threading.Thread(target=run_loop, daemon=True)
            thread.start()
            
            # Give the loop time to start
            time.sleep(0.1)
            
            # Schedule initialization
            loop.call_soon_threadsafe(
                lambda: loop.create_task(ensure_signal_manager())
            )
    else:
        logger.warning("Could not initialize event loop, deferring initialization")
except Exception as e:
    logger.warning(f"Deferred initialization due to: {str(e)}")

class SignalServicer:
    """
    Signal servicer for managing signal routing and processing.
    
    This class provides methods for:
    1. Registering signal handlers
    2. Processing signals
    3. Managing signal routing
    4. Handling signal priorities
    5. Managing signal queues
    """
    
    def __init__(self) -> None:
        """Initialize signal servicer."""
        self._signal_manager = None
        self._data_flow_handlers: Dict[str, List[SignalHandler]] = {}
        self._route_handlers: Dict[str, List[SignalHandler]] = {}
        self._stream_handlers: Dict[str, List[SignalHandler]] = {}
        self._cleaning_stage_handlers: Dict[DataCleaningStage, List[SignalHandler]] = {}
        self._backpressure_handlers: List[SignalHandler] = []
        self._batch_handlers: List[SignalHandler] = []
        self._metrics_handlers: List[SignalHandler] = []
        self._browser_handlers: Dict[str, List[SignalHandler]] = {}  # New browser handlers
        
    async def initialize(self) -> None:
        """Initialize the signal manager."""
        try:
            from tenire.core.event_loop import event_loop_manager
            
            # Get event loop through manager with proper initialization
            loop = event_loop_manager.get_event_loop()
            
            # If no loop available, create one with proper initialization
            if loop is None:
                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                logger.debug("Created new event loop for signal manager")
                
                # Ensure the loop is properly initialized
                if not hasattr(loop, '_running'):
                    loop._running = False
                    
            # Ensure loop is running
            if not getattr(loop, '_running', False):
                if not loop.is_closed():
                    try:
                        # Start the loop if it's not running
                        if not loop.is_running():
                            def run_loop():
                                asyncio.set_event_loop(loop)
                                loop.run_forever()
                            threading.Thread(target=run_loop, daemon=True).start()
                            await asyncio.sleep(0.1)  # Give loop time to start
                        loop._running = True
                        logger.debug("Set event loop as running")
                    except Exception as e:
                        logger.warning(f"Could not set loop running flag: {str(e)}")
                else:
                    logger.error("Event loop is closed")
                    return
            
            # Register with monitor if available
            monitor, _ = await _get_monitor()
            if monitor:
                monitor.register_component("signal_manager")
            
            # Start signal processing if not already running
            if not hasattr(self, '_processing_task') or not self._processing_task or self._processing_task.done():
                self._processing_task = loop.create_task(self._process_signals())
                await event_loop_manager.track_task(self._processing_task)
                logger.info("Started signal processing")
            
            # Initialize health monitoring
            if not hasattr(self, '_monitor_task') or not self._monitor_task or self._monitor_task.done():
                await self.start_health_monitoring()
                if hasattr(self, '_monitor_task') and self._monitor_task:
                    await event_loop_manager.track_task(self._monitor_task)
            
            logger.info("Signal manager initialization completed")
            
        except Exception as e:
            logger.error(f"Error in initialize: {str(e)}")
            raise
        
    async def _register_data_flow_handlers(self) -> None:
        """Register handlers for data flow signals."""
        if self._signal_manager is None:
            logger.warning("Cannot register data flow handlers: signal manager not available")
            return
            
        try:
            # Register handlers for data flow status
            await self._signal_manager.register_handler(
                SignalType.DATA_FLOW_AGENT_STATUS,
                self._handle_data_flow_status,
                priority=2,
                is_async=True
            )
            
            # Register browser-related handlers
            browser_handlers = [
                (SignalType.BROWSER_COMMAND, self._handle_browser_command, 3),
                (SignalType.BROWSER_COMMAND_COMPLETED, self._handle_browser_command_completed, 3),
                (SignalType.BROWSER_ACTION, self._handle_browser_action, 3),
                (SignalType.BROWSER_ACTION_COMPLETED, self._handle_browser_action_completed, 3),
                (SignalType.BROWSER_ERROR, self._handle_browser_error, 4),
                (SignalType.BROWSER_STATUS, self._handle_browser_status, 2),
                (SignalType.BROWSER_NAVIGATION, self._handle_browser_navigation, 2),
                (SignalType.BROWSER_INTERACTION, self._handle_browser_interaction, 2)
            ]
            
            for signal_type, handler, priority in browser_handlers:
                await self._signal_manager.register_handler(
                    signal_type,
                    handler,
                    priority=priority,
                    is_async=True
                )
                
            logger.debug("Registered all browser handlers successfully")
            
            # Register handlers for metrics
            await self._signal_manager.register_handler(
                SignalType.DATA_FLOW_METRICS,
                self._handle_data_flow_metrics,
                priority=1,
                is_async=True
            )
            
            # Register handlers for errors
            await self._signal_manager.register_handler(
                SignalType.DATA_FLOW_ERROR,
                self._handle_data_flow_error,
                priority=3,
                is_async=True
            )
            
            # Register handler for timing signals
            await self._signal_manager.register_handler(
                SignalType.HANDLER_TIMING,
                self._handle_handler_timing,
                priority=2,
                is_async=True
            )
            
            # Register handlers for cleaning stages
            await self._signal_manager.register_handler(
                SignalType.DATA_FLOW_CLEANING_STAGE,
                self._handle_cleaning_stage,
                priority=1,
                is_async=True
            )
            
            # Register handlers for routes and streams
            await self._signal_manager.register_handler(
                SignalType.DATA_FLOW_ROUTE_STATUS,
                self._handle_route_status,
                priority=2,
                is_async=True
            )
            await self._signal_manager.register_handler(
                SignalType.DATA_FLOW_STREAM_STATUS,
                self._handle_stream_status,
                priority=2,
                is_async=True
            )
            
            # Register handlers for backpressure
            await self._signal_manager.register_handler(
                SignalType.DATA_FLOW_BACKPRESSURE,
                self._handle_backpressure,
                priority=3,
                is_async=True
            )
            
            # Register handlers for batch processing
            await self._signal_manager.register_handler(
                SignalType.DATA_FLOW_BATCH_PROCESSED,
                self._handle_batch_processed,
                priority=1,
                is_async=True
            )
            
            logger.debug("Registered all data flow handlers successfully")
        except Exception as e:
            logger.error(f"Error registering data flow handlers: {str(e)}")
            raise
        
    async def register_data_flow_handler(
        self,
        component: str,
        handler: Callable[[Signal], Any],
        priority: int = 0
    ) -> None:
        """
        Register a handler for data flow signals.
        
        Args:
            component: Component identifier
            handler: Handler function
            priority: Handler priority (default: 0)
        """
        if self._signal_manager is None:
            logger.warning(f"Cannot register data flow handler for {component}: signal manager not available")
            return
            
        if component not in self._data_flow_handlers:
            self._data_flow_handlers[component] = []
            
        self._data_flow_handlers[component].append(
            SignalHandler(handler, priority=priority)
        )
        self._data_flow_handlers[component].sort(key=lambda h: h.priority, reverse=True)
        
    async def register_route_handler(
        self,
        route_id: str,
        handler: Callable[[Signal], Any],
        priority: int = 0
    ) -> None:
        """
        Register a handler for route signals.
        
        Args:
            route_id: Route identifier
            handler: Handler function
            priority: Handler priority (default: 0)
        """
        if self._signal_manager is None:
            logger.warning(f"Cannot register route handler for {route_id}: signal manager not available")
            return
            
        if route_id not in self._route_handlers:
            self._route_handlers[route_id] = []
            
        self._route_handlers[route_id].append(
            SignalHandler(handler, priority=priority)
        )
        self._route_handlers[route_id].sort(key=lambda h: h.priority, reverse=True)
        
    async def register_stream_handler(
        self,
        stream_name: str,
        handler: Callable[[Signal], Any],
        priority: int = 0
    ) -> None:
        """
        Register a handler for stream signals.
        
        Args:
            stream_name: Stream identifier
            handler: Handler function
            priority: Handler priority (default: 0)
        """
        if self._signal_manager is None:
            logger.warning(f"Cannot register stream handler for {stream_name}: signal manager not available")
            return
            
        if stream_name not in self._stream_handlers:
            self._stream_handlers[stream_name] = []
            
        self._stream_handlers[stream_name].append(
            SignalHandler(handler, priority=priority)
        )
        self._stream_handlers[stream_name].sort(key=lambda h: h.priority, reverse=True)
        
    async def register_cleaning_stage_handler(
        self,
        stage: DataCleaningStage,
        handler: Callable[[Signal], Any],
        priority: int = 0
    ) -> None:
        """
        Register a handler for cleaning stage signals.
        
        Args:
            stage: Cleaning stage
            handler: Handler function
            priority: Handler priority (default: 0)
        """
        if self._signal_manager is None:
            logger.warning(f"Cannot register cleaning stage handler for {stage}: signal manager not available")
            return
            
        if stage not in self._cleaning_stage_handlers:
            self._cleaning_stage_handlers[stage] = []
            
        self._cleaning_stage_handlers[stage].append(
            SignalHandler(handler, priority=priority)
        )
        self._cleaning_stage_handlers[stage].sort(key=lambda h: h.priority, reverse=True)
        
    async def register_backpressure_handler(
        self,
        handler: Callable[[Signal], Any],
        priority: int = 0
    ) -> None:
        """
        Register a handler for backpressure signals.
        
        Args:
            handler: Handler function
            priority: Handler priority (default: 0)
        """
        if self._signal_manager is None:
            logger.warning("Cannot register backpressure handler: signal manager not available")
            return
            
        self._backpressure_handlers.append(
            SignalHandler(handler, priority=priority)
        )
        self._backpressure_handlers.sort(key=lambda h: h.priority, reverse=True)
        
    async def register_batch_handler(
        self,
        handler: Callable[[Signal], Any],
        priority: int = 0
    ) -> None:
        """
        Register a handler for batch processing signals.
        
        Args:
            handler: Handler function
            priority: Handler priority (default: 0)
        """
        if self._signal_manager is None:
            logger.warning("Cannot register batch handler: signal manager not available")
            return
            
        self._batch_handlers.append(
            SignalHandler(handler, priority=priority)
        )
        self._batch_handlers.sort(key=lambda h: h.priority, reverse=True)
        
    async def register_metrics_handler(
        self,
        handler: Callable[[Signal], Any],
        priority: int = 0
    ) -> None:
        """
        Register a handler for metrics signals.
        
        Args:
            handler: Handler function
            priority: Handler priority (default: 0)
        """
        if self._signal_manager is None:
            logger.warning("Cannot register metrics handler: signal manager not available")
            return
            
        self._metrics_handlers.append(
            SignalHandler(handler, priority=priority)
        )
        self._metrics_handlers.sort(key=lambda h: h.priority, reverse=True)
        
    async def register_browser_handler(
        self,
        command: str,
        handler: Callable[[Signal], Any],
        priority: int = 0
    ) -> None:
        """
        Register a handler for browser signals.
        
        Args:
            command: Browser command identifier
            handler: Handler function
            priority: Handler priority (default: 0)
        """
        if self._signal_manager is None:
            logger.warning(f"Cannot register browser handler for {command}: signal manager not available")
            return
            
        if command not in self._browser_handlers:
            self._browser_handlers[command] = []
            
        self._browser_handlers[command].append(
            SignalHandler(handler, priority=priority)
        )
        self._browser_handlers[command].sort(key=lambda h: h.priority, reverse=True)
        
    async def _handle_data_flow_status(self, signal: Signal) -> None:
        """
        Handle data flow status signals.
        
        Args:
            signal: Signal to handle
        """
        if self._signal_manager is None:
            logger.warning("Cannot handle data flow status: signal manager not available")
            return
            
        component = signal.data.get('component')
        if component in self._data_flow_handlers:
            for handler in self._data_flow_handlers[component]:
                try:
                    await handler(signal)
                except Exception as e:
                    logger.error(f"Error in data flow handler for {component}: {str(e)}")
                    
    async def _handle_data_flow_metrics(self, signal: Signal) -> None:
        """
        Handle data flow metrics signals.
        
        Args:
            signal: Signal to handle
        """
        if self._signal_manager is None:
            logger.warning("Cannot handle data flow metrics: signal manager not available")
            return
            
        for handler in self._metrics_handlers:
            try:
                await handler(signal)
            except Exception as e:
                logger.error(f"Error in metrics handler: {str(e)}")
                
    async def _handle_data_flow_error(self, signal: Signal) -> None:
        """
        Handle data flow error signals.
        
        Args:
            signal: Signal to handle
        """
        if self._signal_manager is None:
            logger.warning("Cannot handle data flow error: signal manager not available")
            return
            
        component = signal.data.get('component')
        if component in self._data_flow_handlers:
            for handler in self._data_flow_handlers[component]:
                try:
                    await handler(signal)
                except Exception as e:
                    logger.error(f"Error in error handler for {component}: {str(e)}")
                    
    async def _handle_cleaning_stage(self, signal: Signal) -> None:
        """
        Handle cleaning stage signals.
        
        Args:
            signal: Signal to handle
        """
        if self._signal_manager is None:
            logger.warning("Cannot handle cleaning stage: signal manager not available")
            return
            
        stage = DataCleaningStage(signal.data.get('stage'))
        if stage in self._cleaning_stage_handlers:
            for handler in self._cleaning_stage_handlers[stage]:
                try:
                    await handler(signal)
                except Exception as e:
                    logger.error(f"Error in cleaning stage handler for {stage}: {str(e)}")
                    
    async def _handle_route_status(self, signal: Signal) -> None:
        """
        Handle route status signals.
        
        Args:
            signal: Signal to handle
        """
        if self._signal_manager is None:
            logger.warning("Cannot handle route status: signal manager not available")
            return
            
        route_id = signal.data.get('route_id')
        if route_id in self._route_handlers:
            for handler in self._route_handlers[route_id]:
                try:
                    await handler(signal)
                except Exception as e:
                    logger.error(f"Error in route handler for {route_id}: {str(e)}")
                    
    async def _handle_stream_status(self, signal: Signal) -> None:
        """
        Handle stream status signals.
        
        Args:
            signal: Signal to handle
        """
        if self._signal_manager is None:
            logger.warning("Cannot handle stream status: signal manager not available")
            return
            
        stream_name = signal.data.get('stream_name')
        if stream_name in self._stream_handlers:
            for handler in self._stream_handlers[stream_name]:
                try:
                    await handler(signal)
                except Exception as e:
                    logger.error(f"Error in stream handler for {stream_name}: {str(e)}")
                    
    async def _handle_backpressure(self, signal: Signal) -> None:
        """
        Handle backpressure signals.
        
        Args:
            signal: Signal to handle
        """
        if self._signal_manager is None:
            logger.warning("Cannot handle backpressure: signal manager not available")
            return
            
        for handler in self._backpressure_handlers:
            try:
                await handler(signal)
            except Exception as e:
                logger.error(f"Error in backpressure handler: {str(e)}")
                
    async def _handle_batch_processed(self, signal: Signal) -> None:
        """
        Handle batch processed signals.
        
        Args:
            signal: Signal to handle
        """
        if self._signal_manager is None:
            logger.warning("Cannot handle batch processed: signal manager not available")
            return
            
        for handler in self._batch_handlers:
            try:
                await handler(signal)
            except Exception as e:
                logger.error(f"Error in batch handler: {str(e)}")

    async def _handle_handler_timing(self, signal: Signal) -> None:
        """
        Handle handler timing signals.
        
        Args:
            signal: Signal containing handler timing information
        """
        try:
            handler = signal.data.get('handler')
            timestamp = signal.data.get('timestamp')
            interval = signal.data.get('interval')
            
            if handler and timestamp and interval is not None:
                # Process timing information
                logger.debug(f"Handler timing update - {handler}: interval={interval}s at {timestamp}")
                
                # Update metrics if available
                if self._metrics_handlers:
                    for handler in self._metrics_handlers:
                        try:
                            await handler(signal)
                        except Exception as e:
                            logger.error(f"Error in metrics handler: {str(e)}")
                            
        except Exception as e:
            logger.error(f"Error handling timing signal: {str(e)}")

    async def _handle_browser_command(self, signal: Signal) -> None:
        """Handle browser command signals."""
        try:
            command = signal.data.get('command')
            if command in self._browser_handlers:
                for handler in self._browser_handlers[command]:
                    try:
                        await handler(signal)
                    except Exception as e:
                        logger.error(f"Error in browser handler for {command}: {str(e)}")
                        await self._signal_manager.emit(Signal(
                            type=SignalType.BROWSER_ERROR,
                            data={'command': command, 'error': str(e)},
                            source='signal_servicer'
                        ))
        except Exception as e:
            logger.error(f"Error handling browser command: {str(e)}")
            
    async def _handle_browser_command_completed(self, signal: Signal) -> None:
        """Handle browser command completed signals."""
        try:
            command = signal.data.get('command')
            result = signal.data.get('result')
            if command and result:
                await self._signal_manager.emit(Signal(
                    type=SignalType.BROWSER_STATUS,
                    data={'command': command, 'status': 'completed', 'result': result},
                    source='signal_servicer'
                ))
        except Exception as e:
            logger.error(f"Error handling browser command completion: {str(e)}")
            
    async def _handle_browser_action(self, signal: Signal) -> None:
        """Handle browser action signals."""
        try:
            action = signal.data.get('action')
            params = signal.data.get('params', {})
            if action:
                await self._signal_manager.emit(Signal(
                    type=SignalType.BROWSER_STATUS,
                    data={'action': action, 'status': 'started', 'params': params},
                    source='signal_servicer'
                ))
        except Exception as e:
            logger.error(f"Error handling browser action: {str(e)}")
            
    async def _handle_browser_action_completed(self, signal: Signal) -> None:
        """Handle browser action completed signals."""
        try:
            action = signal.data.get('action')
            result = signal.data.get('result')
            if action and result:
                await self._signal_manager.emit(Signal(
                    type=SignalType.BROWSER_STATUS,
                    data={'action': action, 'status': 'completed', 'result': result},
                    source='signal_servicer'
                ))
        except Exception as e:
            logger.error(f"Error handling browser action completion: {str(e)}")
            
    async def _handle_browser_error(self, signal: Signal) -> None:
        """Handle browser error signals."""
        try:
            error = signal.data.get('error')
            context = signal.data.get('context', 'browser')
            if error:
                logger.error(f"Browser error in {context}: {error}")
                # Propagate error to any registered error handlers
                if hasattr(self._signal_manager, 'emit_error'):
                    await self._signal_manager.emit_error(error, context)
        except Exception as e:
            logger.error(f"Error handling browser error: {str(e)}")
            
    async def _handle_browser_status(self, signal: Signal) -> None:
        """Handle browser status signals."""
        try:
            status = signal.data.get('status')
            details = signal.data.get('details', {})
            if status:
                logger.debug(f"Browser status update: {status} - {details}")
        except Exception as e:
            logger.error(f"Error handling browser status: {str(e)}")
            
    async def _handle_browser_navigation(self, signal: Signal) -> None:
        """Handle browser navigation signals."""
        try:
            url = signal.data.get('url')
            if url:
                logger.debug(f"Browser navigation: {url}")
        except Exception as e:
            logger.error(f"Error handling browser navigation: {str(e)}")
            
    async def _handle_browser_interaction(self, signal: Signal) -> None:
        """Handle browser interaction signals."""
        try:
            interaction = signal.data.get('interaction')
            if interaction:
                logger.debug(f"Browser interaction: {interaction}")
        except Exception as e:
            logger.error(f"Error handling browser interaction: {str(e)}")

async def get_signal_manager():
    """
    Get or create the signal manager instance with robust initialization.
    
    Returns:
        The signal manager instance
    """
    from tenire.servicers.signal import SignalManager
    
    global _signal_manager_instance
    
    # Fast path - return existing initialized instance
    if _signal_manager_instance is not None and hasattr(_signal_manager_instance, '_processing_task'):
        return _signal_manager_instance

    async with _signal_manager_lock:
        try:
            # Check container first
            from tenire.core.container import container
            signal_manager = container.get('signal_manager')
            
            # If found in container and initialized, use that
            if signal_manager is not None and hasattr(signal_manager, '_processing_task'):
                _signal_manager_instance = signal_manager
                return signal_manager

            # Create new instance if needed
            if signal_manager is None:
                signal_manager = SignalManager()
                # Register with container immediately to prevent cycles
                container.register_sync('signal_manager', signal_manager)

            # Initialize if not already initialized
            if not hasattr(signal_manager, '_processing_task'):
                try:
                    from tenire.core.event_loop import event_loop_manager
                    
                    # Get event loop through manager
                    loop = event_loop_manager.get_event_loop()
                    
                    # Create initialization task
                    init_task = loop.create_task(signal_manager.initialize())
                    await event_loop_manager.track_task(init_task)
                    try:
                        await init_task
                    except Exception as e:
                        logger.error(f"Signal manager initialization failed: {str(e)}")
                        raise
                    
                    # Register cleanup task
                    try:
                        # Initialize compactor
                        compactor = await _ensure_compactor_initialized()
                        if compactor is not None:
                            try:
                                await _register_cleanup_task(compactor, signal_manager)
                            except Exception as e:
                                logger.error(f"Failed to register cleanup task: {str(e)}")
                                # Non-critical error, continue
                    except Exception as e:
                        logger.error(f"Error in cleanup registration process: {str(e)}")
                        # Non-critical error, continue
                    
                    # Update dependencies
                    try:
                        await container.update_dependencies(
                            'signal_manager',
                            {'concurrency_manager'}
                        )
                    except Exception as e:
                        logger.error(f"Error updating dependencies: {str(e)}")
                        # Non-critical error, continue
                        
                except Exception as e:
                    logger.error(f"Error during signal manager initialization: {str(e)}")
                    raise  # Re-raise as initialization is critical

            # Store the instance globally
            _signal_manager_instance = signal_manager
            return signal_manager

        except Exception as e:
            logger.error(f"Error in get_signal_manager: {str(e)}")
            # Create a new instance as fallback if something went wrong
            if _signal_manager_instance is None:
                _signal_manager_instance = SignalManager()
            return _signal_manager_instance

async def initialize_signal_manager() -> None:
    """Initialize the signal manager instance."""
    try:
        from tenire.core.event_loop import event_loop_manager
        
        # Get event loop through manager
        loop = event_loop_manager.get_event_loop()
        
        # Get signal manager instance
        try:
            manager = await get_signal_manager()
            if not manager:
                logger.error("Failed to get signal manager instance")
                return
        except Exception as e:
            logger.error(f"Error getting signal manager: {str(e)}")
            return

        # Ensure initialization is complete
        if not hasattr(manager, '_processing_task') or not manager._processing_task:
            init_task = loop.create_task(manager.initialize())
            await event_loop_manager.track_task(init_task)
            try:
                await init_task
                logger.info("Signal manager initialized successfully")
            except Exception as e:
                logger.error(f"Signal manager initialization failed: {str(e)}")
                raise
        else:
            logger.debug("Signal manager already initialized")
            
    except Exception as e:
        logger.error(f"Error initializing signal manager: {str(e)}")
        raise

async def _get_monitor():
    """
    Get the monitor instance with proper error handling.
    
    Returns:
        Tuple[Optional[Any], bool]: Tuple of (monitor instance, success flag)
    """
    try:
        from tenire.core.container import container
        
        # Try to get monitor from container
        monitor = container.get('monitor', initialize=False)
        if monitor is not None:
            if not monitor.is_monitoring:
                monitor.start()
            return monitor, True
            
        # Try to import and initialize monitor directly
        try:
            from tenire.organizers.concurrency.monitor import monitor as threaded_monitor
            if not threaded_monitor.is_monitoring:
                threaded_monitor.start()
            return threaded_monitor, True
        except ImportError:
            logger.debug("ThreadedMonitor not available")
        except Exception as e:
            logger.warning(f"Error getting ThreadedMonitor: {str(e)}")
            
        # Try component doctor as fallback
        try:
            from tenire.public_services.doctor import ComponentDoctor
            doctor = ComponentDoctor()
            await doctor.start_monitoring()
            return doctor, True
        except ImportError:
            logger.debug("ComponentDoctor not available")
        except Exception as e:
            logger.warning(f"Error getting ComponentDoctor: {str(e)}")
            
        return None, False
        
    except Exception as e:
        logger.error(f"Error in _get_monitor: {str(e)}")
        return None, False