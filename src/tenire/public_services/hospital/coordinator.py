"""
Health monitoring coordinator for the Tenire framework.

This module provides the main coordinator that orchestrates health checks,
automated responses, and system monitoring.
"""

# Standard library imports
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional, Any, List, Tuple

# Core framework imports
from tenire.utils.logger import get_logger
from tenire.core.codex import Signal, SignalType
from tenire.core.container import container
from tenire.structure.dependency_graph import dependency_graph
from tenire.organizers.concurrency.monitor import monitor, MonitorMetrics

# Health monitoring imports
from tenire.public_services.hospital.charts import (
    HealthStatus,
    HealthCheckLevel,
    HealthCheckResult,
    HealthCheck,
    AutoResponse,
    AutoResponseAction
)
from tenire.public_services.hospital.registry import HealthCheckRegistry
from tenire.public_services.hospital.executor import HealthCheckExecutor
from tenire.public_services.hospital.responder import ResponseExecutor
from tenire.servicers import get_signal_manager

logger = get_logger(__name__)

class MonitoringState:
    """State management for health monitoring."""
    def __init__(self):
        self.is_monitoring: bool = False
        self.monitor_task: Optional[asyncio.Task] = None
        self.component_status: Dict[str, HealthStatus] = {}
        self.last_diagnostics: Dict[str, Dict[str, Any]] = {}
        self.check_history: Dict[str, List[HealthCheckResult]] = {}
        self.history_limit: int = 100
        self.auto_responses: Dict[str, Dict[str, AutoResponse]] = {}
        self.response_history: Dict[str, List[Dict[str, Any]]] = {}
        self.error_count: int = 0
        self.last_error_time: Optional[datetime] = None
        self.backoff_delay: float = 1.0
        self.cleanup_registered: bool = False
        self.signal_handlers_registered: bool = False

class HealthCoordinator:
    """
    Main coordinator for health monitoring system.
    
    This class follows the Single Responsibility Principle by focusing solely
    on orchestrating the health monitoring system components.
    
    Features:
    - Component health checks
    - Dependency state monitoring
    - Performance metrics tracking
    - Issue detection and diagnosis
    - Health status reporting
    - Integration with signal system
    - Automated response handling
    """
    
    def __init__(self):
        """Initialize the coordinator."""
        self._registry = HealthCheckRegistry()
        self._executor = HealthCheckExecutor()
        self._responder = ResponseExecutor()
        self._lock = asyncio.Lock()
        self._state = MonitoringState()
        
        # Register core health checks and responses
        self._register_core_checks()
        self._register_core_responses()
        
        logger.debug("Initialized HealthCoordinator")

    async def initialize(self) -> None:
        """Initialize async components of the coordinator."""
        if not self._state.cleanup_registered:
            await self._register_cleanup()
        if not self._state.signal_handlers_registered:
            await self._register_signal_handlers()
            
        logger.debug("Initialized HealthCoordinator async components")

    def _register_core_checks(self) -> None:
        """Register health checks for core components."""
        # Dependency Graph Health
        self._registry.register_check(
            component="dependency_graph",
            name="cycle_detection",
            check_fn=self._check_dependency_cycles,
            interval=timedelta(minutes=1),
            level=HealthCheckLevel.CRITICAL
        )
        
        self._registry.register_check(
            component="dependency_graph",
            name="initialization_status",
            check_fn=self._check_initialization_status,
            interval=timedelta(minutes=1),
            level=HealthCheckLevel.CRITICAL
        )
        
        # Container Health
        self._registry.register_check(
            component="container",
            name="component_registration",
            check_fn=self._check_component_registration,
            interval=timedelta(minutes=1),
            level=HealthCheckLevel.CRITICAL
        )
        
        self._registry.register_check(
            component="container",
            name="weak_references",
            check_fn=self._check_weak_references,
            interval=timedelta(minutes=5),
            level=HealthCheckLevel.WARNING
        )
        
        # Concurrency Manager Health
        self._registry.register_check(
            component="concurrency_manager",
            name="thread_pool_health",
            check_fn=self._check_thread_pool_health,
            interval=timedelta(minutes=1),
            level=HealthCheckLevel.WARNING
        )
        
        self._registry.register_check(
            component="concurrency_manager",
            name="task_queue_health",
            check_fn=self._check_task_queue_health,
            interval=timedelta(minutes=1),
            level=HealthCheckLevel.WARNING
        )
        
    def _register_core_responses(self) -> None:
        """Register automated responses for core health checks."""
        # Dependency Graph Responses
        self._registry.register_auto_response(
            component="dependency_graph",
            check_name="cycle_detection",
            action=AutoResponseAction.RESOLVE_CYCLE,
            conditions=[
                lambda r: r.status == HealthStatus.UNHEALTHY
            ]
        )
        
        # Thread Pool Responses
        self._registry.register_auto_response(
            component="concurrency_manager",
            check_name="thread_pool_health",
            action=AutoResponseAction.RESET_THREAD_POOL,
            conditions=[
                lambda r: r.status == HealthStatus.DEGRADED,
                lambda r: r.details.get('queue_size', 0) > r.details.get('workers', 1) * 2
            ]
        )
        
        # Task Queue Responses
        self._registry.register_auto_response(
            component="concurrency_manager",
            check_name="task_queue_health",
            action=AutoResponseAction.CLEAR_QUEUE,
            conditions=[
                lambda r: r.status == HealthStatus.DEGRADED,
                lambda r: any(size > 1000 for size in r.details.get('metrics', {}).get('queue_sizes', {}).values())
            ]
        )
        
        # Weak References Response
        self._registry.register_auto_response(
            component="container",
            check_name="weak_references",
            action=AutoResponseAction.CLEANUP_REFS,
            conditions=[
                lambda r: r.status == HealthStatus.DEGRADED,
                lambda r: len(r.details.get('dead_refs', [])) > 0
            ]
        )

    # Core Health Check Implementations
    async def _check_dependency_cycles(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check for dependency cycles."""
        cycles = dependency_graph.get_cycles()
        if not cycles:
            return (
                HealthStatus.HEALTHY,
                "No dependency cycles detected",
                {'cycles': []}
            )
            
        return (
            HealthStatus.UNHEALTHY,
            f"Detected {len(cycles)} dependency cycles",
            {'cycles': [' -> '.join(cycle) for cycle in cycles]}
        )
        
    async def _check_initialization_status(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check component initialization status."""
        status = dependency_graph.get_initialization_status()
        error_nodes = status['error_nodes']
        unregistered = status['unregistered']
        
        if error_nodes:
            return (
                HealthStatus.UNHEALTHY,
                f"Found {len(error_nodes)} components with initialization errors",
                {'error_nodes': error_nodes, 'status': status}
            )
            
        if unregistered:
            return (
                HealthStatus.DEGRADED,
                f"Found {len(unregistered)} unregistered components",
                {'unregistered': unregistered, 'status': status}
            )
            
        return (
            HealthStatus.HEALTHY,
            f"All {status['total_nodes']} components initialized successfully",
            {'status': status}
        )
        
    async def _check_component_registration(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check component registration status."""
        missing_components = set()
        for name in dependency_graph._core_components:
            if not container.has(name):
                missing_components.add(name)
                
        if missing_components:
            return (
                HealthStatus.UNHEALTHY,
                f"Missing {len(missing_components)} core components",
                {'missing': list(missing_components)}
            )
            
        return (
            HealthStatus.HEALTHY,
            "All core components registered",
            {'registered': list(container._components.keys())}
        )
        
    async def _check_weak_references(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check weak references health."""
        dead_refs = []
        for name, ref in container._weak_refs.items():
            if ref() is None:
                dead_refs.append(name)
                
        if dead_refs:
            return (
                HealthStatus.DEGRADED,
                f"Found {len(dead_refs)} dead weak references",
                {'dead_refs': dead_refs}
            )
            
        return (
            HealthStatus.HEALTHY,
            "All weak references are valid",
            {'total_refs': len(container._weak_refs)}
        )
        
    async def _check_thread_pool_health(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check thread pool health."""
        concurrency_manager = container.get('concurrency_manager')
        if not concurrency_manager:
            return (
                HealthStatus.UNKNOWN,
                "Concurrency manager not available",
                {}
            )
            
        pool = getattr(concurrency_manager.thread_pool, '_pool', None)
        if not pool:
            return (
                HealthStatus.DEGRADED,
                "Thread pool not initialized",
                {}
            )
            
        workers = pool._max_workers
        active = len([t for t in pool._threads if t.is_alive()])
        queue_size = pool._work_queue.qsize()
        
        if queue_size > workers * 2:
            return (
                HealthStatus.DEGRADED,
                f"Thread pool queue size ({queue_size}) exceeds worker capacity",
                {
                    'workers': workers,
                    'active': active,
                    'queue_size': queue_size
                }
            )
            
        return (
            HealthStatus.HEALTHY,
            f"Thread pool healthy: {active}/{workers} workers active",
            {
                'workers': workers,
                'active': active,
                'queue_size': queue_size
            }
        )
        
    async def _check_task_queue_health(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check task queue health."""
        concurrency_manager = container.get('concurrency_manager')
        if not concurrency_manager:
            return (
                HealthStatus.UNKNOWN,
                "Concurrency manager not available",
                {}
            )
            
        try:
            metrics = concurrency_manager.data_manager.get_metrics()
            queue_sizes = metrics.get('queue_sizes', {})
            
            if any(size > 1000 for size in queue_sizes.values()):
                return (
                    HealthStatus.DEGRADED,
                    "Task queue size exceeds threshold",
                    {'metrics': metrics}
                )
                
            return (
                HealthStatus.HEALTHY,
                "Task queues within normal limits",
                {'metrics': metrics}
            )
            
        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking task queues: {str(e)}",
                {'error': str(e)}
            )

    async def start_monitoring(self) -> None:
        """Start the health monitoring system."""
        if not self._state.is_monitoring:
            # Register with monitor
            monitor.register_component("health_coordinator")
            
            # Ensure core checks are registered
            self._register_core_checks()
            self._register_core_responses()
            
            # Initialize async components if needed
            await self.initialize()
            
            # Start monitoring
            self._state.is_monitoring = True
            
            # Run initial health check cycle immediately
            await self._run_monitoring_cycle()
            
            # Update initial component statuses
            for component in self._registry.get_all_components():
                await self._update_component_status(component)
            
            # Start the monitoring loop task after initial checks
            self._state.monitor_task = asyncio.create_task(self._monitor_loop())
            logger.info("Started health monitoring")
            
            # Emit monitoring started signal
            await self._emit_monitoring_signal("started")
            
    async def stop_monitoring(self) -> None:
        """Stop the health monitoring system."""
        if self._state.is_monitoring:
            self._state.is_monitoring = False
            if self._state.monitor_task:
                self._state.monitor_task.cancel()
                try:
                    await self._state.monitor_task
                except asyncio.CancelledError:
                    pass
            logger.info("Stopped health monitoring")
            
            # Unregister from monitor
            monitor.unregister_component("health_coordinator")
            
            # Emit monitoring stopped signal
            await self._emit_monitoring_signal("stopped")
            
    async def _monitor_loop(self) -> None:
        """Main monitoring loop with improved error handling and backoff."""
        while self._state.is_monitoring:
            try:
                await self._run_monitoring_cycle()
                # Reset error state on successful cycle
                self._state.error_count = 0
                self._state.backoff_delay = 1.0
                await asyncio.sleep(1)  # Base check interval
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                await self._handle_monitoring_error(e)
                
    async def _run_monitoring_cycle(self) -> None:
        """Run a single monitoring cycle."""
        async with self._lock:
            # Get components to check
            components = self._registry.get_all_components()
            
            # Run checks in parallel with timeout protection
            check_tasks = []
            for component in components:
                checks = self._registry.get_component_checks(component)
                for check in checks.values():
                    if self._should_run_check(check):
                        check_tasks.append(self._run_protected_check(component, check))
                        
            if check_tasks:
                # Run checks with global timeout
                try:
                    results = await asyncio.wait_for(
                        asyncio.gather(*check_tasks, return_exceptions=True),
                        timeout=60  # Global timeout for all checks
                    )
                    await self._process_check_results(results)
                    
                    # Send results to monitor
                    self._send_health_metrics_to_monitor(results)
                    
                except asyncio.TimeoutError:
                    logger.error("Global check timeout exceeded")
                    monitor.register_component("health_coordinator")
                    monitor._emit_alert("error", "Health check global timeout exceeded")
                    
    def _should_run_check(self, check: HealthCheck) -> bool:
        """Determine if a check should be run based on its interval."""
        now = datetime.now()
        return (not check.last_run or 
                now - check.last_run >= check.interval)
                
    async def _run_protected_check(
        self,
        component: str,
        check: HealthCheck
    ) -> Tuple[str, HealthCheck, HealthCheckResult]:
        """Run a health check with error protection."""
        try:
            result = await self._executor.run_check(component, check)
            return component, check, result
        except Exception as e:
            logger.error(f"Error in check {component}.{check.name}: {str(e)}")
            return component, check, self._create_error_result(component, check, e)
            
    async def _process_check_results(self, results: List[Tuple[str, HealthCheck, HealthCheckResult]]) -> None:
        """Process results from parallel check execution."""
        for component, check, result in results:
            if isinstance(result, Exception):
                continue
                
            # Update component status
            await self._update_component_status(component)
            
            # Handle automated response if needed
            await self._responder.handle_check_result(component, check, result)
            
    async def _handle_monitoring_error(self, error: Exception) -> None:
        """Handle errors in the monitoring loop with exponential backoff."""
        self._state.error_count += 1
        self._state.last_error_time = datetime.now()
        
        # Calculate backoff delay (max 30 seconds)
        self._state.backoff_delay = min(
            self._state.backoff_delay * 2,
            30.0
        )
        
        logger.error(
            f"Error in monitoring loop (attempt {self._state.error_count}): {str(error)}"
        )
        
        # Emit error signal
        await self._emit_monitoring_signal(
            "error",
            {
                'error': str(error),
                'attempt': self._state.error_count,
                'backoff': self._state.backoff_delay
            }
        )
        
        await asyncio.sleep(self._state.backoff_delay)
        
    async def _emit_monitoring_signal(self, status: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Emit a monitoring status signal."""
        try:
            signal_data = {
                'status': status,
                'timestamp': datetime.now().isoformat(),
                'components': list(self._registry.get_all_components())
            }
            if data:
                signal_data.update(data)
                
            signal_manager = get_signal_manager()
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.SYSTEM_HEALTH_REPORT,
                    data=signal_data,
                    source="health_coordinator"
                ))
                
        except Exception as e:
            logger.error(f"Error emitting monitoring signal: {str(e)}")
        
    def _create_error_result(
        self,
        component: str,
        check: HealthCheck,
        error: Exception
    ) -> HealthCheckResult:
        """Create an error result for a failed check."""
        return HealthCheckResult(
            component=component,
            status=HealthStatus.UNHEALTHY,
            message=f"Check failed: {str(error)}",
            level=check.level,
            error=error,
            timestamp=datetime.now(),
            latency=0.0
        )
        
    async def _update_component_status(self, component: str) -> None:
        """Update overall status for a component."""
        checks = self._registry.get_component_checks(component)
        if not checks:
            return
            
        # Get worst status from critical checks
        critical_status = HealthStatus.HEALTHY
        for check in checks.values():
            if check.level == HealthCheckLevel.CRITICAL and check.last_result:
                if check.last_result.status.value > critical_status.value:
                    critical_status = check.last_result.status
                    
        # Get worst status from warning checks
        warning_status = HealthStatus.HEALTHY
        for check in checks.values():
            if check.level == HealthCheckLevel.WARNING and check.last_result:
                if check.last_result.status.value > warning_status.value:
                    warning_status = check.last_result.status
                    
        # Critical status takes precedence
        status = critical_status
        if status == HealthStatus.HEALTHY:
            status = warning_status
            
        # Update status
        self._state.component_status[component] = status
        
        # Emit status update signal
        await self._emit_status_signal(component, status)
        
    async def _emit_status_signal(self, component: str, status: HealthStatus) -> None:
        """Emit a component status change signal."""
        try:
            signal_manager = get_signal_manager()
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.HEALTH_STATUS_UPDATED,
                    data={
                        'component': component,
                        'old_status': self._state.component_status.get(component, HealthStatus.UNKNOWN).value,
                        'new_status': status.value,
                        'timestamp': datetime.now().isoformat()
                    },
                    source="health_coordinator"
                ))
                
        except Exception as e:
            logger.error(f"Error emitting status signal: {str(e)}")
        
    async def get_component_health(self, component: str) -> Dict[str, Any]:
        """Get detailed health information for a component."""
        checks = self._registry.get_component_checks(component)
        if not checks:
            return {}
            
        results = []
        for name, check in checks.items():
            if check.last_result:
                results.append({
                    'name': name,
                    'status': check.last_result.status.value,
                    'message': check.last_result.message,
                    'level': check.level.value,
                    'last_run': check.last_run.isoformat() if check.last_run else None,
                    'latency': check.last_result.latency,
                    'details': check.last_result.details
                })
                
        return {
            'status': self._state.component_status.get(component, HealthStatus.UNKNOWN).value,
            'checks': results,
            'responses': self._responder.get_response_history(component)
        }
        
    async def get_system_health(self) -> Dict[str, Any]:
        """Get health information for the entire system."""
        system_health = {}
        overall_status = HealthStatus.HEALTHY  # Start with healthy

        # Get health for each component
        for component in self._registry.get_all_components():
            component_health = await self.get_component_health(component)
            system_health[component] = component_health
            
            # Update overall status based on component status
            component_status = HealthStatus(component_health.get('status', HealthStatus.UNKNOWN.value))
            if component_status.value > overall_status.value:
                overall_status = component_status

        return {
            'components': system_health,
            'overall_status': overall_status,
            'timestamp': datetime.now().isoformat()
        }

    @property
    def registry(self) -> HealthCheckRegistry:
        """Get the health check registry."""
        return self._registry

    @property
    def executor(self) -> HealthCheckExecutor:
        """Get the health check executor."""
        return self._executor

    @property
    def responder(self) -> ResponseExecutor:
        """Get the response executor."""
        return self._responder

    @property
    def monitoring_state(self) -> Dict[str, Any]:
        """Get current monitoring state information."""
        return {
            'is_monitoring': self._state.is_monitoring,
            'error_count': self._state.error_count,
            'last_error': self._state.last_error_time.isoformat() if self._state.last_error_time else None,
            'backoff_delay': self._state.backoff_delay,
            'components': len(self._registry.get_all_components()),
            'active_checks': sum(
                len(self._registry.get_component_checks(c))
                for c in self._registry.get_all_components()
            )
        }

    async def _register_cleanup(self) -> None:
        """Register cleanup tasks with the compactor."""
        if self._state.cleanup_registered:
            return
            
        try:
            # Get compactor through container to avoid circular imports
            compactor = container.get('compactor')
            if compactor:
                # Register coordinator cleanup
                compactor.register_cleanup_task(
                    name="health_coordinator_cleanup",
                    cleanup_func=self.cleanup,
                    priority=80,  # High priority but after core systems
                    is_async=True,
                    metadata={
                        "tags": ["health", "monitoring"],
                        "description": "Cleanup health monitoring system"
                    }
                )
                
                # Register executor cleanup
                compactor.register_cleanup_task(
                    name="health_executor_cleanup",
                    cleanup_func=self._executor.cleanup,
                    priority=81,
                    is_async=True,
                    metadata={
                        "tags": ["health", "monitoring"],
                        "description": "Cleanup health check executor"
                    }
                )
                
                # Register responder cleanup
                compactor.register_cleanup_task(
                    name="health_responder_cleanup",
                    cleanup_func=self._responder.cleanup,
                    priority=82,
                    is_async=True,
                    metadata={
                        "tags": ["health", "monitoring"],
                        "description": "Cleanup health response executor"
                    }
                )
                
                self._state.cleanup_registered = True
                logger.debug("Registered health system cleanup tasks")
                
        except Exception as e:
            logger.error(f"Error registering cleanup tasks: {str(e)}")

    async def cleanup(self) -> None:
        """Clean up the health monitoring system."""
        try:
            # Stop monitoring
            await self.stop_monitoring()
            
            # Clear state
            self._state = MonitoringState()
            
            # Clean up components
            await self._executor.cleanup()
            await self._responder.cleanup()
            
            # Clear registries
            self._registry.clear()
            
            logger.info("Cleaned up health monitoring system")
            
        except Exception as e:
            logger.error(f"Error during health system cleanup: {str(e)}")
            raise 

    async def _register_signal_handlers(self) -> None:
        """Register signal handlers for health monitoring events."""
        if self._state.signal_handlers_registered:
            return
            
        try:
            signal_manager = get_signal_manager()
            if signal_manager:
                # Register handlers for health-related signals
                signal_manager.register_handler(
                    SignalType.HEALTH_CHECK_STARTED,
                    self._handle_check_started_signal,
                    priority=10
                )
                
                signal_manager.register_handler(
                    SignalType.HEALTH_CHECK_COMPLETED,
                    self._handle_check_completed_signal,
                    priority=10
                )
                
                signal_manager.register_handler(
                    SignalType.HEALTH_CHECK_ERROR,
                    self._handle_check_error_signal,
                    priority=10
                )
                
                signal_manager.register_handler(
                    SignalType.COMPONENT_STATUS_CHANGED,
                    self._handle_status_changed_signal,
                    priority=10
                )
                
                signal_manager.register_handler(
                    SignalType.AUTO_RESPONSE_TRIGGERED,
                    self._handle_response_triggered_signal,
                    priority=10
                )
                
                self._state.signal_handlers_registered = True
                logger.debug("Registered health monitoring signal handlers")
                
        except Exception as e:
            logger.error(f"Error registering signal handlers: {str(e)}")

    async def _handle_check_started_signal(self, signal: Signal) -> None:
        """Handle health check started signal."""
        component = signal.data.get('component')
        check_name = signal.data.get('check')
        if component and check_name:
            logger.debug(f"Health check started: {component}.{check_name}")

    async def _handle_check_completed_signal(self, signal: Signal) -> None:
        """Handle health check completed signal."""
        component = signal.data.get('component')
        check_name = signal.data.get('check')
        status = signal.data.get('status')
        if component and check_name and status:
            logger.debug(f"Health check completed: {component}.{check_name} - {status}")
            await self._update_component_status(component)

    async def _handle_check_error_signal(self, signal: Signal) -> None:
        """Handle health check error signal."""
        component = signal.data.get('component')
        check_name = signal.data.get('check')
        error = signal.data.get('error')
        if component and check_name:
            logger.error(f"Health check error: {component}.{check_name} - {error}")
            await self._handle_monitoring_error(Exception(error))

    async def _handle_status_changed_signal(self, signal: Signal) -> None:
        """Handle component status changed signal."""
        component = signal.data.get('component')
        new_status = signal.data.get('new_status')
        if component and new_status:
            logger.info(f"Component status changed: {component} -> {new_status}")
            await self._emit_status_signal(component, HealthStatus(new_status))

    async def _handle_response_triggered_signal(self, signal: Signal) -> None:
        """Handle automated response triggered signal."""
        component = signal.data.get('component')
        action = signal.data.get('action')
        if component and action:
            logger.info(f"Automated response triggered: {component} - {action}")

    def _send_health_metrics_to_monitor(self, results: List[Tuple[str, HealthCheck, HealthCheckResult]]) -> None:
        """Send health check results to the monitor."""
        try:
            health_metrics = {
                'components': {},
                'overall_status': HealthStatus.HEALTHY,
                'timestamp': datetime.now().isoformat()
            }
            
            for component, check, result in results:
                if isinstance(result, Exception):
                    continue
                    
                if component not in health_metrics['components']:
                    health_metrics['components'][component] = {
                        'status': result.status.value,
                        'checks': [],
                        'errors': [],
                        'warnings': []
                    }
                    
                check_info = {
                    'name': check.name,
                    'status': result.status.value,
                    'message': result.message,
                    'level': check.level.value,
                    'latency': result.latency,
                    'details': result.details
                }
                
                health_metrics['components'][component]['checks'].append(check_info)
                
                if result.status == HealthStatus.UNHEALTHY:
                    health_metrics['components'][component]['errors'].append(result.message)
                    if health_metrics['overall_status'].value < HealthStatus.UNHEALTHY.value:
                        health_metrics['overall_status'] = HealthStatus.UNHEALTHY
                        
                elif result.status == HealthStatus.DEGRADED:
                    health_metrics['components'][component]['warnings'].append(result.message)
                    if health_metrics['overall_status'] == HealthStatus.HEALTHY:
                        health_metrics['overall_status'] = HealthStatus.DEGRADED
                        
            # Register health coordinator with monitor
            monitor.register_component("health_coordinator")
            
            # Store metrics in monitor
            monitor._store_metrics(MonitorMetrics(
                timestamp=datetime.now(),
                component_metrics={'health_system': health_metrics['components']},
                system_metrics={'overall_health': health_metrics['overall_status'].value},
                warnings=[w for c in health_metrics['components'].values() for w in c.get('warnings', [])],
                errors=[e for c in health_metrics['components'].values() for e in c.get('errors', [])]
            ))
            
        except Exception as e:
            logger.error(f"Error sending health metrics to monitor: {str(e)}")
            monitor._emit_alert("error", f"Failed to send health metrics: {str(e)}") 