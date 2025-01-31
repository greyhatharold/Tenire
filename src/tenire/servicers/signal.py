"""
Signal management module for the Tenire framework.

This module provides a robust signal/event system for communication between
different components of the framework, with special support for GUI interactions.
"""

# Standard library imports
import asyncio
from typing import Any, Dict, List, Optional, Union, Callable, Tuple, Set
import time
from datetime import datetime, timedelta
from collections import defaultdict
import weakref

# Local imports
from tenire.utils.logger import get_logger
from tenire.core.codex import (
    Signal, SignalHandler, SignalType, HealthStatus, HealthCheckLevel,
    AutoResponseAction, HealthCheck, HealthCheckResult, AutoResponse, HandlerFunc, FilterFunc,
    DataFlowStatus, DataFlowPriority, DataCleaningStage, DataFlowMetrics
)
from tenire.structure.dependency_graph import DependencyState

logger = get_logger(__name__)

# Global instance and lock
_signal_manager_instance = None
_signal_manager_lock = asyncio.Lock()
_bootstrap_lock = asyncio.Lock()  # New lock for bootstrap initialization

async def _get_container():
    """Lazy import and get container to avoid circular dependencies."""
    from tenire.core.container import container
    return container

async def _get_dependency_graph():
    """Lazy import and get dependency graph to avoid circular dependencies."""
    from tenire.structure.dependency_graph import dependency_graph
    return dependency_graph

async def _get_monitor():
    """Lazy import and get monitor to avoid circular dependencies."""
    from tenire.organizers.concurrency.monitor import monitor
    return monitor

class SignalManager:
    """
    Enhanced signal manager with improved handler management and error handling.
    
    Features:
    1. Priority-based handler execution
    2. Automatic cleanup of dead references
    3. Handler filtering support
    4. Comprehensive error tracking
    5. Async and sync handler support
    6. GUI-specific optimizations
    7. Health monitoring integration
    """
    
    _instance = None
    _lock = asyncio.Lock()

    def __new__(cls):
        if not cls._instance:
            cls._instance = super(SignalManager, cls).__new__(cls)
            # Initialize core attributes during instance creation
            cls._instance._bootstrap_initialized = False
            cls._instance._fully_initialized = False
            cls._instance._processing_task = None
            cls._instance._monitor_task = None
            cls._instance._shutdown_event = asyncio.Event()
            cls._instance._signal_queue = asyncio.PriorityQueue()
            cls._instance._handlers = defaultdict(list)
            cls._instance._gui_handlers = defaultdict(list)
            cls._instance._health_checks = {}
            cls._instance._auto_responses = defaultdict(list)
            cls._instance._is_processing = False
            cls._instance._gui_state = {}
            cls._instance._error_handlers = []
            cls._instance._component_status = {}
            cls._instance._last_diagnostics = {}
            cls._instance._check_history = {}
            cls._instance._history_limit = 100
            cls._instance._is_monitoring = False
            cls._instance._response_history = {}
            cls._instance._signal_metrics = defaultdict(
                lambda: {'count': 0, 'first_seen': datetime.now(), 'last_seen': datetime.now()}
            )
            cls._instance._signal_history = []
            cls._instance._error_counts = defaultdict(int)
            cls._instance._handler_priorities = {}
            cls._instance._weak_handlers = defaultdict(list)
            cls._instance._dependency_graph = {}
            logger.debug("Initialized SignalManager core attributes")
        return cls._instance

    @property
    def is_initialized(self) -> bool:
        """Check if the signal manager is fully initialized."""
        return self._fully_initialized

    async def _bootstrap_initialize(self) -> None:
        """
        Perform minimal initialization required for basic functionality.
        This allows other components to use the signal manager during their initialization.
        """
        if self._bootstrap_initialized:
            return

        async with _bootstrap_lock:
            if self._bootstrap_initialized:  # Double check
                return
                
            try:
                # Initialize only the essential components needed for basic operation
                if not self._processing_task or self._processing_task.done():
                    self._processing_task = asyncio.create_task(self._process_signals())
                self._bootstrap_initialized = True
                logger.debug("Signal manager bootstrap initialization completed")
            except Exception as e:
                logger.error(f"Error during signal manager bootstrap: {str(e)}")
                raise

    async def initialize(self) -> None:
        """Initialize the signal manager with full functionality."""
        try:
            # Ensure bootstrap initialization is done first
            await self._bootstrap_initialize()
            
            if self._fully_initialized:
                return

            from tenire.core.event_loop import event_loop_manager
            
            # Get event loop through manager
            loop = event_loop_manager.get_event_loop()
            
            # Register with monitor if available
            monitor = await _get_monitor()
            if monitor:
                monitor.register_component("signal_manager")
            
            # Initialize health monitoring
            if not self._monitor_task or self._monitor_task.done():
                await self.start_health_monitoring()
                if self._monitor_task:
                    await event_loop_manager.track_task(self._monitor_task)
            
            self._fully_initialized = True
            logger.info("Signal manager full initialization completed")
            
        except Exception as e:
            logger.error(f"Error during signal manager initialization: {str(e)}")
            raise

    async def start(self) -> None:
        """Start the signal manager and health monitoring."""
        try:
            # Register with monitor
            monitor = await _get_monitor()
            monitor.register_component("signal_manager")
            
            if not self._processing_task:
                self._processing_task = asyncio.create_task(self._process_signals())
                logger.info("Started signal processing")
                
            await self.start_health_monitoring()
        except Exception as e:
            logger.error(f"Error starting signal manager: {str(e)}")
            raise
        
    async def stop(self) -> None:
        """Stop the signal manager and health monitoring."""
        await self.stop_health_monitoring()
        
        if self._processing_task:
            self._shutdown_event.set()
            self._processing_task.cancel()
            try:
                await self._processing_task
            except asyncio.CancelledError:
                pass
            self._processing_task = None
            logger.info("Stopped signal processing")
            
    async def cleanup(self) -> None:
        """Cleanup resources and stop all processing."""
        try:
            await self.stop()
            
            # Clear queues and handlers
            self._signal_queue = asyncio.PriorityQueue()
            self._handlers.clear()
            self._gui_handlers.clear()
            self._health_checks.clear()
            self._auto_responses.clear()
            
            # Unregister from monitor
            monitor = await _get_monitor()
            monitor.unregister_component("signal_manager")
            
            logger.info("Cleaned up signal manager resources")
            
        except Exception as e:
            logger.error(f"Error during signal manager cleanup: {str(e)}")
            # Get monitor lazily for error alert
            try:
                monitor = await _get_monitor()
                monitor._emit_alert("error", f"Error during signal manager cleanup: {str(e)}")
            except Exception:
                pass
            raise
            
    async def emit(self, signal: Signal) -> None:
        """
        Emit a signal with enhanced error handling and metrics tracking.
        
        Args:
            signal: The signal to emit
        """
        try:
            # Track signal metrics
            self._track_signal_metrics(signal)
            
            # Check for critical patterns
            await self._check_signal_patterns(signal)
            
            # Get handlers sorted by priority
            handlers = self._get_sorted_handlers(signal.type)
            
            # Execute handlers with timeout protection
            for handler, priority in handlers:
                try:
                    async with asyncio.timeout(5.0):  # 5 second timeout for handlers
                        await handler(signal)
                except asyncio.TimeoutError:
                    logger.error(f"Handler timeout for signal {signal.type}")
                    self._error_counts[handler.__name__] += 1
                except Exception as e:
                    logger.error(f"Handler error for signal {signal.type}: {str(e)}")
                    self._error_counts[handler.__name__] += 1
                    
            # Archive signal
            self._signal_history.append(signal)
            
        except Exception as e:
            logger.error(f"Error emitting signal: {str(e)}")
            
    def _track_signal_metrics(self, signal: Signal) -> None:
        """Track signal metrics for pattern detection."""
        # Use signal type name as key to avoid enum serialization issues
        signal_type_name = signal.type.name
        metrics = self._signal_metrics[signal_type_name]
        metrics['count'] += 1
        metrics['last_seen'] = datetime.now()
        
        # Check for high frequency signals
        if metrics['count'] > 100 and (metrics['last_seen'] - metrics['first_seen']).seconds < 60:
            logger.warning(f"High frequency signal detected: {signal_type_name}")
            
    async def _check_signal_patterns(self, signal: Signal) -> None:
        """Check for concerning signal patterns."""
        # Check for error cascades
        if signal.type.name.endswith('_ERROR'):
            error_count = sum(1 for s in self._signal_history
                            if s.type.name.endswith('_ERROR')
                            and (datetime.now() - s.timestamp).seconds < 60)
            if error_count > 10:
                logger.error("Error cascade detected")
                await self.emit(Signal(
                    type=SignalType.COMPONENT_DEGRADED,
                    data={'reason': 'error_cascade'},
                    source='signal_manager'
                ))
                
        # Check for dependency cycles
        if signal.type == SignalType.COMPONENT_STATE_CHANGED:
            component = signal.data.get('component')
            if component and self._has_dependency_cycle(component):
                logger.error(f"Dependency cycle detected for {component}")
                await self.emit(Signal(
                    type=SignalType.COMPONENT_DEPENDENCY_CYCLE,
                    data={'component': component},
                    source='signal_manager'
                ))
                
    def _has_dependency_cycle(self, component: str, visited: Optional[Set[str]] = None) -> bool:
        """Check for dependency cycles."""
        if visited is None:
            visited = set()
            
        if component in visited:
            return True
            
        visited.add(component)
        
        for dep in self._dependency_graph.get(component, []):
            if self._has_dependency_cycle(dep, visited):
                return True
                
        visited.remove(component)
        return False
        
    def _get_sorted_handlers(self, signal_type: SignalType) -> List[Tuple[Callable, int]]:
        """Get handlers sorted by priority."""
        handlers = []
        
        # Get regular handlers
        if signal_type in self._handlers:
            for handler in self._handlers[signal_type]:
                if isinstance(handler, SignalHandler):
                    handlers.append((handler.func, handler.priority))
                else:
                    priority = self._handler_priorities.get(handler, 0)
                    handlers.append((handler, priority))
            
        # Get weak handlers that are still alive
        if signal_type in self._weak_handlers:
            for handler_ref in self._weak_handlers[signal_type]:
                handler = handler_ref()
                if handler is not None:
                    if isinstance(handler, SignalHandler):
                        handlers.append((handler.func, handler.priority))
                    else:
                        priority = self._handler_priorities.get(handler, 0)
                        handlers.append((handler, priority))
                
        # Sort by priority (higher priority first)
        return sorted(handlers, key=lambda x: x[1], reverse=True)
        
    async def _process_signals(self) -> None:
        """Process signals from the queue with enhanced error handling."""
        while not self._shutdown_event.is_set():
            try:
                # Get next signal with timeout
                try:
                    _, signal = await asyncio.wait_for(
                        self._signal_queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue
                
                # Process regular handlers
                if signal.type in self._handlers:
                    await self._process_handler_list(
                        self._handlers[signal.type],
                        signal,
                        "regular"
                    )
                
                # Process GUI handlers
                if signal.type in self._gui_handlers:
                    await self._process_handler_list(
                        self._gui_handlers[signal.type],
                        signal,
                        "GUI"
                    )
                
                # Update GUI state if needed
                if signal.type in [SignalType.GUI_STATE, SignalType.GUI_UPDATE]:
                    self._update_gui_state(signal)
                
                self._signal_queue.task_done()
                
            except Exception as e:
                logger.error(f"Error processing signals: {str(e)}")
                await asyncio.sleep(1)  # Back off on error
                
    async def _monitor_loop(self) -> None:
        """Main monitoring loop with improved error handling and backoff."""
        consecutive_errors = 0
        while self._is_monitoring and not self._shutdown_event.is_set():
            try:
                await self._run_due_checks()
                consecutive_errors = 0  # Reset error count on success
                await asyncio.sleep(1)  # Check every second
                
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Error in monitor loop: {str(e)}")
                
                # Exponential backoff with max delay of 30 seconds
                delay = min(2 ** consecutive_errors, 30)
                await asyncio.sleep(delay)
                
    async def _run_due_checks(self) -> None:
        """Run all checks that are due with improved concurrency."""
        now = datetime.now()
        check_tasks = []
        
        async with self._lock:  # Prevent modification during iteration
            for component, checks in self._health_checks.items():
                for check in checks.values():
                    if (not check.last_run or 
                        now - check.last_run >= check.interval):
                        check_tasks.append(
                            self._run_check(component, check)
                        )
        
        if check_tasks:
            # Run checks concurrently with timeout
            try:
                await asyncio.wait_for(
                    asyncio.gather(*check_tasks, return_exceptions=True),
                    timeout=60  # Global timeout for all checks
                )
            except asyncio.TimeoutError:
                logger.error("Health checks timed out")

    async def start_health_monitoring(self) -> None:
        """Start the health monitoring system."""
        if self._is_monitoring:
            return
            
        self._is_monitoring = True
        
        from tenire.core.event_loop import event_loop_manager
        loop = event_loop_manager.get_event_loop()
        
        self._monitor_task = loop.create_task(self._monitor_loop())
        await event_loop_manager.track_task(self._monitor_task)
        logger.info("Started health monitoring")
        
        try:
            # Emit system health report signal
            await self.emit(Signal(
                type=SignalType.SYSTEM_HEALTH_REPORT,
                data={
                    'status': 'started',
                    'components': list(self._health_checks.keys())
                }
            ))
        except Exception as e:
            logger.warning(f"Could not emit health report signal: {str(e)}")
            # Continue even if signal emission fails

    async def stop_health_monitoring(self) -> None:
        """Stop the health monitoring system."""
        self._is_monitoring = False
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.error(f"Error canceling monitor task: {str(e)}")
                
        try:
            # Emit system health report signal
            await self.emit(Signal(
                type=SignalType.SYSTEM_HEALTH_REPORT,
                data={
                    'status': 'stopped',
                    'components': list(self._health_checks.keys())
                }
            ))
        except Exception as e:
            logger.warning(f"Could not emit health report signal: {str(e)}")
            # Continue even if signal emission fails
            
        logger.info("Stopped health monitoring")
        
    async def register_health_check(
        self,
        component: str,
        name: str,
        check_fn: Callable[[], Any],
        interval: timedelta,
        level: HealthCheckLevel = HealthCheckLevel.INFO,
        timeout: float = 5.0
    ) -> None:
        """Register a health check with signal integration."""
        if component not in self._health_checks:
            self._health_checks[component] = {}
            
        self._health_checks[component][name] = HealthCheck(
            name=name,
            check_fn=check_fn,
            interval=interval,
            level=level,
            timeout=timeout,
            is_async=asyncio.iscoroutinefunction(check_fn)
        )
        
        logger.debug(f"Registered health check: {component}.{name}")
        
        # Emit registration signal
        await self.emit(Signal(
            type=SignalType.RAG_COMPONENT_INITIALIZED,
            data={
                'component': component,
                'check': name,
                'level': level.value,
                'interval': interval.total_seconds()
            }
        ))
        
    async def register_auto_response(
        self,
        component: str,
        check_name: str,
        action: AutoResponseAction,
        conditions: List[Callable[[HealthCheckResult], bool]],
        max_attempts: int = 3,
        cooldown: timedelta = timedelta(minutes=5)
    ) -> None:
        """Register an automated response with signal integration."""
        if component not in self._auto_responses:
            self._auto_responses[component] = {}
            
        self._auto_responses[component][check_name] = AutoResponse(
            action=action,
            conditions=conditions,
            max_attempts=max_attempts,
            cooldown=cooldown,
            is_async=action != AutoResponseAction.LOG_ONLY
        )
        
        logger.debug(f"Registered auto-response for {component}.{check_name}: {action.value}")
        
    async def get_component_health(self, component: str) -> Dict[str, Any]:
        """Get detailed health information for a component."""
        if component not in self._health_checks:
            return {
                'status': HealthStatus.UNKNOWN.value,
                'checks': [],
                'last_updated': None
            }
            
        checks = self._health_checks[component]
        check_results = []
        
        for check in checks.values():
            if check.last_result:
                result = check.last_result
                check_results.append({
                    'name': check.name,
                    'status': result.status.value,
                    'message': result.message,
                    'level': result.level.value,
                    'latency': result.latency,
                    'last_run': check.last_run.isoformat() if check.last_run else None,
                    'details': result.details
                })
                
        return {
            'status': self._component_status.get(component, HealthStatus.UNKNOWN).value,
            'checks': check_results,
            'last_updated': max(
                (c.last_run for c in checks.values() if c.last_run),
                default=None
            )
        }
        
    async def get_system_health(self) -> Dict[str, Any]:
        """Get overall system health status."""
        component_health = {}
        system_status = HealthStatus.HEALTHY
        
        for component in self._health_checks:
            health = await self.get_component_health(component)
            component_health[component] = health
            
            status = HealthStatus(health['status'])
            if status == HealthStatus.UNHEALTHY:
                system_status = HealthStatus.UNHEALTHY
            elif status == HealthStatus.DEGRADED and system_status == HealthStatus.HEALTHY:
                system_status = HealthStatus.DEGRADED
                
        health_report = {
            'status': system_status.value,
            'components': component_health,
            'timestamp': datetime.now().isoformat()
        }
        
        # Emit system health report signal
        await self.emit(Signal(
            type=SignalType.SYSTEM_HEALTH_REPORT,
            data=health_report
        ))
        
        return health_report
        
    def get_response_history(self, component: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
        """Get automated response history."""
        if component:
            return {component: self._response_history.get(component, [])}
        return self._response_history

    def register_handler(self, 
                        signal_type: SignalType,
                        handler: Union[HandlerFunc, SignalHandler],
                        filter_func: Optional[FilterFunc] = None,
                        priority: int = 0,
                        is_gui: bool = False,
                        is_async: Optional[bool] = None) -> None:
        """
        Register a handler for a specific signal type with enhanced metadata.
        
        Args:
            signal_type: Type of signal to handle
            handler: Handler function or SignalHandler instance
            filter_func: Optional function to filter signals
            priority: Handler priority (higher = earlier execution)
            is_gui: Whether this is a GUI-specific handler
            is_async: Optional override for async detection (if None, auto-detected)
        """
        if isinstance(handler, SignalHandler):
            handler_obj = handler
        else:
            handler_obj = SignalHandler(
                func=handler,
                filter_func=filter_func,
                priority=priority,
                is_gui=is_gui,
                is_async=is_async
            )
        
        target_handlers = self._gui_handlers if is_gui else self._handlers
        if signal_type not in target_handlers:
            target_handlers[signal_type] = []
            
        target_handlers[signal_type].append(handler_obj)
        # Sort handlers by priority (highest first)
        target_handlers[signal_type].sort(key=lambda h: h.priority, reverse=True)
        
        logger.debug(f"Registered {'GUI ' if is_gui else ''}handler for {signal_type}")

    def register_error_handler(self, handler: Callable[[Exception, Signal], None]) -> None:
        """Register a handler for signal processing errors."""
        self._error_handlers.append(handler)

    def _update_gui_state(self, signal: Signal) -> None:
        """Update the GUI state based on a signal."""
        if signal.gui_context:
            self._gui_state.update(signal.gui_context)

    def get_gui_state(self) -> Dict[str, Any]:
        """Get the current GUI state."""
        return self._gui_state.copy()

    async def emit_gui_update(self, update_type: str, data: Dict[str, Any], priority: int = 0) -> None:
        """Emit a GUI update signal with priority support."""
        signal = Signal(
            type=SignalType.GUI_UPDATE,
            data=data,
            source="gui",
            gui_context={"update_type": update_type},
            priority=priority
        )
        await self.emit(signal)

    async def emit_gui_action(self, action_type: str, data: Dict[str, Any], priority: int = 0) -> None:
        """Emit a GUI action signal with priority support."""
        signal = Signal(
            type=SignalType.GUI_ACTION,
            data=data,
            source="gui",
            gui_context={"action_type": action_type},
            priority=priority
        )
        await self.emit(signal)

    async def _run_check(self, component: str, check: HealthCheck) -> None:
        """Run a single health check with automated response handling."""
        # Emit check started signal
        signal_manager = await _get_container()
        if signal_manager:
            await signal_manager.emit(Signal(
                type=SignalType.HEALTH_CHECK_STARTED,
                data={
                    'component': component,
                    'check': check.name,
                    'level': check.level.value
                }
            ))
            
        start_time = time.time()
        try:
            # Run check with timeout
            if check.is_async:
                result = await asyncio.wait_for(
                    check.check_fn(),
                    timeout=check.timeout
                )
            else:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    check.check_fn
                )
                
            latency = time.time() - start_time
            
            # Process result
            if isinstance(result, tuple):
                status, message, details = result
            else:
                status, message, details = result, "", {}
                
            check_result = HealthCheckResult(
                component=component,
                status=status,
                message=message,
                details=details,
                level=check.level,
                latency=latency
            )
            
        except asyncio.TimeoutError:
            check_result = HealthCheckResult(
                component=component,
                status=HealthStatus.UNHEALTHY,
                message=f"Health check timed out after {check.timeout}s",
                level=check.level,
                latency=check.timeout,
                error=asyncio.TimeoutError()
            )
            
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.HEALTH_CHECK_ERROR,
                    data={
                        'component': component,
                        'check': check.name,
                        'error': 'timeout',
                        'message': check_result.message
                    }
                ))
            
        except Exception as e:
            check_result = HealthCheckResult(
                component=component,
                status=HealthStatus.UNHEALTHY,
                message=str(e),
                level=check.level,
                latency=time.time() - start_time,
                error=e
            )
            
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.HEALTH_CHECK_ERROR,
                    data={
                        'component': component,
                        'check': check.name,
                        'error': str(e),
                        'message': check_result.message
                    }
                ))
            
        # Update check state
        check.last_run = datetime.now()
        check.last_result = check_result
        
        # Update history
        if component not in self._check_history:
            self._check_history[component] = []
        history = self._check_history[component]
        history.append(check_result)
        
        # Trim history
        if len(history) > self._history_limit:
            history[:] = history[-self._history_limit:]
            
        # Update component status and emit status change signals
        old_status = self._component_status.get(component)
        await self._update_component_status(component)
        new_status = self._component_status.get(component)
        
        if signal_manager and old_status != new_status:
            await signal_manager.emit(Signal(
                type=SignalType.COMPONENT_STATUS_CHANGED,
                data={
                    'component': component,
                    'old_status': old_status.value if old_status else None,
                    'new_status': new_status.value,
                    'check': check.name
                }
            ))
            
            # Emit specific status signals
            if new_status == HealthStatus.DEGRADED:
                await signal_manager.emit(Signal(
                    type=SignalType.COMPONENT_DEGRADED,
                    data={
                        'component': component,
                        'message': check_result.message,
                        'details': check_result.details
                    }
                ))
            elif new_status == HealthStatus.UNHEALTHY:
                await signal_manager.emit(Signal(
                    type=SignalType.COMPONENT_UNHEALTHY,
                    data={
                        'component': component,
                        'message': check_result.message,
                        'details': check_result.details
                    }
                ))
            elif old_status in [HealthStatus.DEGRADED, HealthStatus.UNHEALTHY] and new_status == HealthStatus.HEALTHY:
                await signal_manager.emit(Signal(
                    type=SignalType.COMPONENT_RECOVERED,
                    data={
                        'component': component,
                        'previous_status': old_status.value,
                        'message': check_result.message
                    }
                ))
        
        # Emit check completed signal
        if signal_manager:
            await signal_manager.emit(Signal(
                type=SignalType.HEALTH_CHECK_COMPLETED,
                data={
                    'component': component,
                    'check': check.name,
                    'status': check_result.status.value,
                    'message': check_result.message,
                    'latency': check_result.latency,
                    'level': check_result.level.value
                }
            ))
            
        # Handle automated response
        await self._handle_check_result(component, check, check_result)

    async def _execute_response(
        self,
        component: str,
        check_name: str,
        response: AutoResponse,
        result: HealthCheckResult
    ) -> None:
        """Execute an automated response action."""
        logger.info(f"Executing {response.action.value} for {component}.{check_name}")
        
        # Emit response triggered signal
        signal_manager = await _get_container()
        if signal_manager:
            await signal_manager.emit(Signal(
                type=SignalType.AUTO_RESPONSE_TRIGGERED,
                data={
                    'component': component,
                    'check': check_name,
                    'action': response.action.value,
                    'attempt': response.attempt_count + 1
                }
            ))
        
        try:
            if response.action == AutoResponseAction.RESTART_COMPONENT:
                await self._restart_component(component)
                
            elif response.action == AutoResponseAction.CLEAR_QUEUE:
                await self._clear_component_queues(component)
                
            elif response.action == AutoResponseAction.RESET_THREAD_POOL:
                await self._reset_thread_pool(component)
                
            elif response.action == AutoResponseAction.RESOLVE_CYCLE:
                await self._resolve_dependency_cycle(result.details.get('cycles', []))
                
            elif response.action == AutoResponseAction.CLEANUP_REFS:
                await self._cleanup_weak_refs(result.details.get('dead_refs', []))
                
            elif response.action == AutoResponseAction.REINITIALIZE:
                await self._reinitialize_component(component)
                
            # Emit response completed signal
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.AUTO_RESPONSE_COMPLETED,
                    data={
                        'component': component,
                        'check': check_name,
                        'action': response.action.value,
                        'attempt': response.attempt_count + 1,
                        'success': True
                    }
                ))
                
        except Exception as e:
            logger.error(f"Error executing response for {component}.{check_name}: {str(e)}")
            
            # Emit response error signal
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.AUTO_RESPONSE_ERROR,
                    data={
                        'component': component,
                        'check': check_name,
                        'action': response.action.value,
                        'attempt': response.attempt_count + 1,
                        'error': str(e)
                    }
                ))
            raise

    def emit_sync(self, signal: Signal) -> None:
        """
        Synchronous version of emit for non-async contexts.
        
        Args:
            signal: Signal instance to emit
        """
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(self.emit(signal))
        else:
            loop.run_until_complete(self.emit(signal))

    async def _process_handler_list(self,
                                  handlers: List[SignalHandler],
                                  signal: Signal,
                                  handler_type: str) -> None:
        """Process a list of handlers for a signal with enhanced error handling."""
        for handler in handlers:
            try:
                if handler.filter_func and not handler.filter_func(signal):
                    continue
                    
                if handler.is_async:
                    await handler.handler(signal)
                else:
                    # Run sync handlers in thread pool for GUI handlers
                    if handler_type == "GUI" and not handler.is_async:
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(None, handler.handler, signal)
                    else:
                        handler.handler(signal)
                        
            except Exception as e:
                logger.error(
                    f"Error in {handler_type} handler for {signal.type}: {str(e)}"
                )
                for error_handler in self._error_handlers:
                    try:
                        error_handler(e, signal)
                    except Exception as err:
                        logger.error(f"Error in error handler: {str(err)}")

    async def _update_component_status(self, component: str) -> None:
        """Update overall component status based on check results."""
        if component not in self._health_checks:
            return
            
        checks = self._health_checks[component]
        results = [check.last_result for check in checks.values() 
                  if check.last_result is not None]
        
        if not results:
            self._component_status[component] = HealthStatus.UNKNOWN
            return
            
        # Determine status based on check levels
        critical_results = [r for r in results if r.level == HealthCheckLevel.CRITICAL]
        warning_results = [r for r in results if r.level == HealthCheckLevel.WARNING]
        
        if any(r.status == HealthStatus.UNHEALTHY for r in critical_results):
            status = HealthStatus.UNHEALTHY
        elif any(r.status == HealthStatus.UNHEALTHY for r in warning_results):
            status = HealthStatus.DEGRADED
        elif all(r.status == HealthStatus.HEALTHY for r in results):
            status = HealthStatus.HEALTHY
        else:
            status = HealthStatus.DEGRADED
            
        old_status = self._component_status.get(component)
        self._component_status[component] = status
        
        # Emit status change signal if needed
        if old_status != status:
            await self.emit(Signal(
                type=SignalType.HEALTH_STATUS_UPDATED,
                data={
                    'component': component,
                    'old_status': old_status.value if old_status else None,
                    'new_status': status.value
                }
            ))

    async def _handle_check_result(self, component: str, check: HealthCheck, result: HealthCheckResult) -> None:
        """Handle a health check result and trigger automated response if needed."""
        if component not in self._auto_responses or check.name not in self._auto_responses[component]:
            return
            
        response = self._auto_responses[component][check.name]
        
        # Check if response should be triggered
        if not all(condition(result) for condition in response.conditions):
            return
            
        # Check cooldown and attempt limits
        now = datetime.now()
        if (response.last_attempt and 
            now - response.last_attempt < response.cooldown):
            return
            
        if response.attempt_count >= response.max_attempts:
            logger.warning(
                f"Max response attempts ({response.max_attempts}) reached for {component}.{check.name}"
            )
            return
            
        # Execute response
        try:
            await self._execute_response(component, check.name, response, result)
            
            # Update response state
            response.last_attempt = now
            response.attempt_count += 1
            
            # Record response in history
            if component not in self._response_history:
                self._response_history[component] = []
            self._response_history[component].append({
                'timestamp': now.isoformat(),
                'check': check.name,
                'action': response.action.value,
                'result': result.status.value,
                'attempt': response.attempt_count
            })
            
        except Exception as e:
            logger.error(f"Error executing response for {component}.{check.name}: {str(e)}")

    async def _restart_component(self, component: str) -> None:
        """Restart a component."""
        try:
            # Get component instance
            container = await _get_container()
            instance = container.get(component)
            if not instance:
                raise ValueError(f"Component not found: {component}")
                
            # Stop component if it has a stop method
            if hasattr(instance, 'stop'):
                await instance.stop()
                
            # Reinitialize component
            await self._reinitialize_component(component)
            
        except Exception as e:
            logger.error(f"Error restarting component {component}: {str(e)}")
            raise

    async def _clear_component_queues(self, component: str) -> None:
        """Clear queues for a component."""
        try:
            container = await _get_container()
            instance = container.get(component)
            if not instance:
                raise ValueError(f"Component not found: {component}")
                
            # Clear queues based on component type
            if component == 'concurrency_manager':
                instance.data_manager._document_queue = asyncio.Queue()
                instance.data_manager._embedding_queue = asyncio.Queue()
                instance.data_manager._prefetch_queue = asyncio.Queue()
                
        except Exception as e:
            logger.error(f"Error clearing queues for {component}: {str(e)}")
            raise

    async def _reset_thread_pool(self, component: str) -> None:
        """Reset a component's thread pool."""
        try:
            container = await _get_container()
            instance = container.get(component)
            if not instance or not hasattr(instance, 'thread_pool'):
                raise ValueError(f"Thread pool not found for {component}")
                
            # Shutdown existing pool
            instance.thread_pool.shutdown(wait=True)
            
            # Create new pool with same settings
            workers = getattr(instance.thread_pool, '_max_workers', 4)
            instance.configure_thread_pool(workers)
            
        except Exception as e:
            logger.error(f"Error resetting thread pool for {component}: {str(e)}")
            raise

    async def _reinitialize_component(self, component: str) -> None:
        """Reinitialize a component."""
        try:
            # Get component node from dependency graph
            dependency_graph = await _get_dependency_graph()
            node = dependency_graph.get_node(component)
            if not node:
                raise ValueError(f"Component not found in dependency graph: {component}")
                
            # Reset component state
            node.state = DependencyState.REGISTERED
            node.instance = None
            
            # Reinitialize
            success = await dependency_graph.initialize_node(component)
            if not success:
                raise RuntimeError(f"Failed to reinitialize component: {component}")
                
        except Exception as e:
            logger.error(f"Error reinitializing component {component}: {str(e)}")
            raise

    async def _resolve_dependency_cycle(self, cycles: List[List[str]]) -> None:
        """Attempt to resolve dependency cycles."""
        try:
            dependency_graph = await _get_dependency_graph()
            for cycle in cycles:
                if resolved_at := dependency_graph.resolve_cycle(cycle):
                    logger.info(f"Resolved dependency cycle at: {resolved_at}")
                    
        except Exception as e:
            logger.error(f"Error resolving dependency cycles: {str(e)}")
            raise

    async def _cleanup_weak_refs(self, dead_refs: List[str]) -> None:
        """Clean up dead weak references."""
        try:
            container = await _get_container()
            for ref in dead_refs:
                if ref in container._weak_refs:
                    del container._weak_refs[ref]
                    logger.info(f"Cleaned up dead reference: {ref}")
                    
        except Exception as e:
            logger.error(f"Error cleaning up weak references: {str(e)}")
            raise

    async def emit_data_flow_status(
        self,
        component: str,
        status: DataFlowStatus,
        metrics: Optional[DataFlowMetrics] = None,
        details: Optional[Dict[str, Any]] = None
    ) -> None:
        """Emit data flow status signal."""
        await self.emit(Signal(
            type=SignalType.DATA_FLOW_AGENT_STATUS,
            data={
                'component': component,
                'status': status.value,
                'metrics': metrics.__dict__ if metrics else {},
                'details': details or {}
            },
            source='data_flow_agent'
        ))
        
    async def emit_data_flow_metrics(
        self,
        component: str,
        metrics: DataFlowMetrics,
        route_id: Optional[str] = None,
        stream_name: Optional[str] = None
    ) -> None:
        """Emit data flow metrics signal."""
        signal_type = (
            SignalType.DATA_FLOW_ROUTE_METRICS if route_id
            else SignalType.DATA_FLOW_STREAM_METRICS if stream_name
            else SignalType.DATA_FLOW_METRICS
        )
        
        await self.emit(Signal(
            type=signal_type,
            data={
                'component': component,
                'metrics': metrics.__dict__,
                'route_id': route_id,
                'stream_name': stream_name
            },
            source='data_flow_agent'
        ))
        
    async def emit_data_flow_error(
        self,
        component: str,
        error: str,
        details: Optional[Dict[str, Any]] = None,
        severity: str = 'error'
    ) -> None:
        """Emit data flow error signal."""
        await self.emit(Signal(
            type=SignalType.DATA_FLOW_ERROR,
            data={
                'component': component,
                'error': error,
                'details': details or {},
                'severity': severity
            },
            source='data_flow_agent',
            priority=2 if severity == 'error' else 1
        ))
        
    async def emit_cleaning_stage_update(
        self,
        component: str,
        stage: DataCleaningStage,
        status: str,
        details: Optional[Dict[str, Any]] = None
    ) -> None:
        """Emit cleaning stage update signal."""
        await self.emit(Signal(
            type=SignalType.DATA_FLOW_CLEANING_STAGE,
            data={
                'component': component,
                'stage': stage.value,
                'status': status,
                'details': details or {}
            },
            source='data_flow_agent'
        ))
        
    async def emit_route_status(
        self,
        route_id: str,
        status: DataFlowStatus,
        metrics: Optional[DataFlowMetrics] = None,
        priority: Optional[DataFlowPriority] = None
    ) -> None:
        """Emit route status signal."""
        await self.emit(Signal(
            type=SignalType.DATA_FLOW_ROUTE_STATUS,
            data={
                'route_id': route_id,
                'status': status.value,
                'metrics': metrics.__dict__ if metrics else {},
                'priority': priority.value if priority else None
            },
            source='data_flow_agent'
        ))
        
    async def emit_stream_status(
        self,
        stream_name: str,
        status: DataFlowStatus,
        metrics: Optional[DataFlowMetrics] = None,
        buffer_usage: Optional[float] = None
    ) -> None:
        """Emit stream status signal."""
        await self.emit(Signal(
            type=SignalType.DATA_FLOW_STREAM_STATUS,
            data={
                'stream_name': stream_name,
                'status': status.value,
                'metrics': metrics.__dict__ if metrics else {},
                'buffer_usage': buffer_usage
            },
            source='data_flow_agent'
        ))
        
    async def emit_backpressure_event(
        self,
        component: str,
        reason: str,
        threshold: float,
        current_value: float
    ) -> None:
        """Emit backpressure event signal."""
        await self.emit(Signal(
            type=SignalType.DATA_FLOW_BACKPRESSURE,
            data={
                'component': component,
                'reason': reason,
                'threshold': threshold,
                'current_value': current_value,
                'timestamp': datetime.now().isoformat()
            },
            source='data_flow_agent',
            priority=2
        ))
        
    async def emit_batch_processed(
        self,
        component: str,
        batch_size: int,
        processing_time: float,
        success_count: int,
        error_count: int
    ) -> None:
        """Emit batch processed signal."""
        await self.emit(Signal(
            type=SignalType.DATA_FLOW_BATCH_PROCESSED,
            data={
                'component': component,
                'batch_size': batch_size,
                'processing_time': processing_time,
                'success_count': success_count,
                'error_count': error_count,
                'timestamp': datetime.now().isoformat()
            },
            source='data_flow_agent'
        ))
        
    async def emit_queue_status(
        self,
        component: str,
        queue_name: str,
        size: int,
        capacity: int,
        is_full: bool
    ) -> None:
        """Emit queue status signal."""
        await self.emit(Signal(
            type=SignalType.DATA_FLOW_QUEUE_STATUS,
            data={
                'component': component,
                'queue_name': queue_name,
                'size': size,
                'capacity': capacity,
                'is_full': is_full,
                'usage_percent': (size / capacity) * 100 if capacity > 0 else 0
            },
            source='data_flow_agent'
        ))
        
    async def emit_buffer_status(
        self,
        component: str,
        buffer_name: str,
        usage: float,
        threshold: float
    ) -> None:
        """Emit buffer status signal."""
        await self.emit(Signal(
            type=SignalType.DATA_FLOW_BUFFER_STATUS,
            data={
                'component': component,
                'buffer_name': buffer_name,
                'usage': usage,
                'threshold': threshold,
                'is_near_capacity': usage >= threshold
            },
            source='data_flow_agent'
        ))

async def get_signal_manager() -> Optional[SignalManager]:
    """Get or create the signal manager instance with robust initialization."""
    global _signal_manager_instance
    
    # Fast path - return existing initialized instance
    if _signal_manager_instance is not None and _signal_manager_instance._bootstrap_initialized:
        return _signal_manager_instance

    async with _signal_manager_lock:
        try:
            # Check container first
            container = await _get_container()
            signal_manager = container.get('signal_manager')
            
            # If found in container and initialized, use that
            if signal_manager is not None and signal_manager._bootstrap_initialized:
                _signal_manager_instance = signal_manager
                return signal_manager

            # Create new instance if needed
            if signal_manager is None:
                signal_manager = SignalManager()
                # Register with container immediately to prevent cycles
                container.register_sync('signal_manager', signal_manager)

            # Ensure bootstrap initialization
            await signal_manager._bootstrap_initialize()
            
            # Store the instance globally
            _signal_manager_instance = signal_manager
            return signal_manager

        except Exception as e:
            logger.error(f"Error in get_signal_manager: {str(e)}")
            return None

async def initialize_signal_manager() -> None:
    """Initialize the signal manager with proper dependency handling."""
    try:
        # Get container
        container = await _get_container()
        
        # Create signal manager instance if needed
        signal_manager = container.get('signal_manager')
        if signal_manager is None:
            signal_manager = SignalManager()
            
            # Register with container with highest priority and minimal dependencies
            await container.register(
                'signal_manager',
                signal_manager,
                dependencies=set(),  # No initial dependencies for bootstrap
                priority=1000,  # Highest priority
                metadata={
                    'is_core': True,
                    'is_critical': True,
                    'tags': {'core', 'messaging', 'critical'}
                }
            )
        
        # Ensure bootstrap initialization is done
        await signal_manager._bootstrap_initialize()
        
        # Perform full initialization if not already done
        if not signal_manager._fully_initialized:
            await signal_manager.initialize()
            
        # Register with dependency graph as critical core component
        dependency_graph = await _get_dependency_graph()
        if dependency_graph:
            dependency_graph.register_node(
                name='signal_manager',
                dependencies=set(),  # No initial dependencies
                init_fn=signal_manager.initialize,
                is_core=True,
                priority=1000,  # Highest priority
                metadata={
                    'is_critical': True,
                    'tags': {'core', 'messaging', 'critical'}
                }
            )
            
        logger.info("Signal manager initialized with critical priority")
            
    except Exception as e:
        logger.error(f"Error during signal manager initialization: {str(e)}")
        raise