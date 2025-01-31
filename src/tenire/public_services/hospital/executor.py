"""
Health check executor for running health checks and processing results.

This module handles the execution of health checks and processing of their
results, following the Single Responsibility Principle.
"""

import asyncio
import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple, Set

from tenire.utils.logger import get_logger
from tenire.core.codex import Signal, SignalType
from tenire.organizers.concurrency.monitor import monitor, MonitorMetrics
from tenire.public_services.hospital.charts import (
    HealthCheck,
    HealthCheckResult,
    HealthStatus,
    HealthCheckLevel
)
from tenire.servicers import get_signal_manager

logger = get_logger(__name__)

class CheckExecutionMetrics:
    """Metrics tracking for check execution."""
    def __init__(self):
        self.total_executions: int = 0
        self.successful_executions: int = 0
        self.failed_executions: int = 0
        self.timeout_count: int = 0
        self.total_latency: float = 0.0
        self.max_latency: float = 0.0
        self.last_execution: Optional[datetime] = None
        self.error_types: Dict[str, int] = {}

class ExecutorState:
    """State management for the executor."""
    def __init__(self):
        self.metrics: Dict[str, CheckExecutionMetrics] = {}
        self.active_checks: Set[str] = set()
        self.check_history: Dict[str, List[HealthCheckResult]] = {}
        self.history_limit: int = 100
        self.error_handlers: List[callable] = []
        self.is_shutting_down: bool = False

class HealthCheckExecutor:
    """
    Executes health checks and processes their results.
    
    This class follows the Single Responsibility Principle by focusing solely
    on executing health checks and handling their results.
    
    Features:
    - Async and sync check execution
    - Timeout handling
    - Result processing and validation
    - History tracking
    - Error handling and recovery
    - Parallel execution support
    - Execution metrics tracking
    """
    
    def __init__(self):
        """Initialize the executor."""
        self._lock = asyncio.Lock()
        self._state = ExecutorState()
        self._semaphore = asyncio.Semaphore(10)  # Limit concurrent checks
        
        # Register with monitor
        monitor.register_component("health_executor")
        
    async def cleanup(self) -> None:
        """Clean up the executor."""
        try:
            async with self._lock:
                # Mark as shutting down
                self._state.is_shutting_down = True
                
                # Wait for active checks to complete (with timeout)
                if self._state.active_checks:
                    try:
                        await asyncio.wait_for(
                            self._wait_for_active_checks(),
                            timeout=30.0
                        )
                    except asyncio.TimeoutError:
                        logger.warning("Timeout waiting for active checks to complete")
                        monitor._emit_alert("warning", "Timeout waiting for active health checks to complete")
                
                # Clear state
                self._state = ExecutorState()
                
                # Unregister from monitor
                monitor.unregister_component("health_executor")
                
            logger.info("Cleaned up health check executor")
            
        except Exception as e:
            logger.error(f"Error during executor cleanup: {str(e)}")
            monitor._emit_alert("error", f"Error during health executor cleanup: {str(e)}")
            raise
            
    async def _wait_for_active_checks(self) -> None:
        """Wait for all active checks to complete."""
        while self._state.active_checks:
            await asyncio.sleep(0.1)
            
    async def run_check(self, component: str, check: HealthCheck) -> HealthCheckResult:
        """
        Execute a health check and process its result.
        
        Args:
            component: Component name
            check: Health check to execute
            
        Returns:
            Result of the health check
        """
        # Skip if shutting down
        if self._state.is_shutting_down:
            return self._create_skipped_result(
                component,
                check,
                "Executor is shutting down"
            )
            
        check_id = f"{component}.{check.name}"
        
        # Skip if check is already running
        if check_id in self._state.active_checks:
            return self._create_skipped_result(
                component,
                check,
                "Check is already running"
            )
            
        try:
            self._state.active_checks.add(check_id)
            
            # Use semaphore to limit concurrent checks
            async with self._semaphore:
                return await self._execute_check(component, check)
                
        finally:
            self._state.active_checks.remove(check_id)
            
    async def _execute_check(self, component: str, check: HealthCheck) -> HealthCheckResult:
        """Execute a single check with metrics tracking."""
        start_time = time.time()
        error = None
        status = HealthStatus.UNKNOWN
        message = ""
        details = {}
        
        try:
            # Emit check started signal
            signal_manager = get_signal_manager()
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.HEALTH_CHECK_STARTED,
                    data={
                        'component': component,
                        'check': check.name,
                        'level': check.level.value,
                        'timestamp': datetime.now().isoformat()
                    },
                    source="health_executor"
                ))
            
            # Execute check with timeout
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
                
            # Process result
            if isinstance(result, tuple) and len(result) == 3:
                status, message, details = result
            elif isinstance(result, tuple) and len(result) == 2:
                status, message = result
                details = {}
            elif isinstance(result, HealthStatus):
                status = result
                message = f"Check returned status: {status.value}"
                details = {}
            else:
                raise ValueError(f"Invalid check result format: {result}")
                
        except asyncio.TimeoutError:
            status = HealthStatus.DEGRADED
            message = f"Health check timed out after {check.timeout}s"
            error = asyncio.TimeoutError(message)
            await self._update_metrics(component, False, time.time() - start_time, error)
            
            # Emit timeout error signal
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.HEALTH_CHECK_ERROR,
                    data={
                        'component': component,
                        'check': check.name,
                        'error': 'timeout',
                        'message': message,
                        'timestamp': datetime.now().isoformat()
                    },
                    source="health_executor"
                ))
            
        except Exception as e:
            status = HealthStatus.UNHEALTHY
            message = f"Health check failed: {str(e)}"
            error = e
            logger.error(f"Error running health check {component}.{check.name}: {str(e)}")
            await self._update_metrics(component, False, time.time() - start_time, error)
            
            # Emit error signal
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=SignalType.HEALTH_CHECK_ERROR,
                    data={
                        'component': component,
                        'check': check.name,
                        'error': str(e),
                        'message': message,
                        'timestamp': datetime.now().isoformat()
                    },
                    source="health_executor"
                ))
            
            # Notify error handlers
            for handler in self._state.error_handlers:
                try:
                    handler(e, component, check)
                except Exception as err:
                    logger.error(f"Error in error handler: {str(err)}")
                    
        else:
            # Update metrics on success
            await self._update_metrics(component, True, time.time() - start_time)
            
        finally:
            latency = time.time() - start_time
            
        # Create result
        result = HealthCheckResult(
            component=component,
            status=status,
            message=message,
            details=details,
            timestamp=datetime.now(),
            level=check.level,
            latency=latency,
            error=error
        )
        
        # Update check history
        await self._update_history(component, result)
        
        # Update check's last run info
        check.last_run = result.timestamp
        check.last_result = result
        
        # Emit check completed signal
        if signal_manager:
            await signal_manager.emit(Signal(
                type=SignalType.HEALTH_CHECK_COMPLETED,
                data={
                    'component': component,
                    'check': check.name,
                    'status': result.status.value,
                    'message': result.message,
                    'latency': result.latency,
                    'level': result.level.value,
                    'timestamp': datetime.now().isoformat(),
                    'details': result.details
                },
                source="health_executor"
            ))
        
        return result
        
    async def _update_metrics(
        self,
        component: str,
        success: bool,
        latency: float,
        error: Optional[Exception] = None
    ) -> None:
        """Update execution metrics for a component."""
        async with self._lock:
            if component not in self._state.metrics:
                self._state.metrics[component] = CheckExecutionMetrics()
                
            metrics = self._state.metrics[component]
            metrics.total_executions += 1
            metrics.total_latency += latency
            metrics.max_latency = max(metrics.max_latency, latency)
            metrics.last_execution = datetime.now()
            
            if success:
                metrics.successful_executions += 1
            else:
                metrics.failed_executions += 1
                if isinstance(error, asyncio.TimeoutError):
                    metrics.timeout_count += 1
                if error:
                    error_type = type(error).__name__
                    metrics.error_types[error_type] = metrics.error_types.get(error_type, 0) + 1
                    
            # Send metrics to monitor
            monitor._store_metrics(MonitorMetrics(
                timestamp=datetime.now(),
                component_metrics={
                    'health_executor': {
                        component: {
                            'total_executions': metrics.total_executions,
                            'successful_executions': metrics.successful_executions,
                            'failed_executions': metrics.failed_executions,
                            'timeout_count': metrics.timeout_count,
                            'avg_latency': metrics.total_latency / metrics.total_executions if metrics.total_executions > 0 else 0,
                            'max_latency': metrics.max_latency,
                            'error_types': metrics.error_types
                        }
                    }
                },
                system_metrics={
                    'active_checks': len(self._state.active_checks),
                    'total_components': len(self._state.metrics)
                },
                warnings=[],
                errors=[str(error)] if error else []
            ))
                    
    def _create_skipped_result(
        self,
        component: str,
        check: HealthCheck,
        reason: str
    ) -> HealthCheckResult:
        """Create a result for a skipped check."""
        return HealthCheckResult(
            component=component,
            status=HealthStatus.UNKNOWN,
            message=f"Check skipped - {reason}",
            level=check.level,
            timestamp=datetime.now(),
            latency=0.0
        )
        
    async def run_checks_parallel(
        self,
        checks: List[Tuple[str, HealthCheck]]
    ) -> List[HealthCheckResult]:
        """
        Run multiple checks in parallel.
        
        Args:
            checks: List of (component, check) tuples to execute
            
        Returns:
            List of check results
        """
        tasks = [
            self.run_check(component, check)
            for component, check in checks
        ]
        
        if not tasks:
            return []
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # Convert exceptions to error results
        processed_results = []
        for (component, check), result in zip(checks, results):
            if isinstance(result, Exception):
                processed_results.append(
                    self._create_error_result(component, check, result)
                )
            else:
                processed_results.append(result)
                
        return processed_results
        
    def get_component_metrics(self, component: str) -> Dict[str, Any]:
        """
        Get execution metrics for a component.
        
        Args:
            component: Component name
            
        Returns:
            Dictionary of execution metrics
        """
        metrics = self._state.metrics.get(component)
        if not metrics:
            return {}
            
        return {
            'total_executions': metrics.total_executions,
            'success_rate': (metrics.successful_executions / metrics.total_executions 
                           if metrics.total_executions > 0 else 0),
            'avg_latency': (metrics.total_latency / metrics.total_executions 
                          if metrics.total_executions > 0 else 0),
            'max_latency': metrics.max_latency,
            'timeout_rate': (metrics.timeout_count / metrics.total_executions 
                           if metrics.total_executions > 0 else 0),
            'last_execution': metrics.last_execution.isoformat() if metrics.last_execution else None,
            'error_types': metrics.error_types
        }
        
    def get_active_checks(self) -> Set[str]:
        """Get currently running check IDs."""
        return self._state.active_checks.copy()
        
    def get_execution_summary(self) -> Dict[str, Any]:
        """Get overall execution summary."""
        total_executions = sum(m.total_executions for m in self._state.metrics.values())
        total_success = sum(m.successful_executions for m in self._state.metrics.values())
        total_timeouts = sum(m.timeout_count for m in self._state.metrics.values())
        
        return {
            'total_executions': total_executions,
            'overall_success_rate': (total_success / total_executions 
                                   if total_executions > 0 else 0),
            'total_timeouts': total_timeouts,
            'active_checks': len(self._state.active_checks),
            'components_tracked': len(self._state.metrics)
        }
        
    async def _update_history(self, component: str, result: HealthCheckResult) -> None:
        """
        Update check history for a component.
        
        Args:
            component: Component name
            result: Check result to add to history
        """
        async with self._lock:
            if component not in self._state.check_history:
                self._state.check_history[component] = []
                
            history = self._state.check_history[component]
            history.append(result)
            
            # Trim history if needed
            if len(history) > self._state.history_limit:
                history = history[-self._state.history_limit:]
                self._state.check_history[component] = history
                
    def get_check_history(self, component: str) -> List[HealthCheckResult]:
        """
        Get check history for a component.
        
        Args:
            component: Component name
            
        Returns:
            List of check results
        """
        return self._state.check_history.get(component, [])
        
    def register_error_handler(self, handler: callable) -> None:
        """
        Register an error handler for check execution errors.
        
        Args:
            handler: Error handler function
        """
        self._state.error_handlers.append(handler)
        
    def get_component_stats(self, component: str) -> Dict[str, Any]:
        """
        Get execution statistics for a component's checks.
        
        Args:
            component: Component name
            
        Returns:
            Dictionary of execution statistics
        """
        history = self._state.check_history.get(component, [])
        if not history:
            return {}
            
        total_checks = len(history)
        errors = sum(1 for r in history if r.error is not None)
        timeouts = sum(1 for r in history if isinstance(r.error, asyncio.TimeoutError))
        avg_latency = sum(r.latency for r in history) / total_checks
        
        return {
            'total_checks': total_checks,
            'error_rate': errors / total_checks if total_checks > 0 else 0,
            'timeout_rate': timeouts / total_checks if total_checks > 0 else 0,
            'avg_latency': avg_latency,
            'last_check': history[-1].timestamp if history else None
        } 