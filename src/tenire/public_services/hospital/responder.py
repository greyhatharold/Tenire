"""
Automated response executor for health check results.

This module handles the execution of automated responses to health check
results, following the Single Responsibility Principle.
"""

# Standard library imports
import asyncio
from datetime import datetime
from typing import Dict, List, Optional, Any

# Core framework imports
from tenire.core.codex import Signal, SignalType
from tenire.core.container import container
from tenire.organizers.concurrency.monitor import monitor, MonitorMetrics

# Structure imports
from tenire.structure.dependency_graph import dependency_graph

# Health monitoring imports
from tenire.public_services.hospital.charts import (
    AutoResponse,
    HealthCheckResult,
    AutoResponseAction,
    HealthCheck
)

# Utility imports
from tenire.utils.logger import get_logger
logger = get_logger(__name__)

class ResponseExecutor:
    """
    Executes automated responses to health check results.
    
    This class follows the Single Responsibility Principle by focusing solely
    on executing and tracking automated responses.
    """
    
    def __init__(self):
        """Initialize the executor."""
        self._lock = asyncio.Lock()
        self._response_history: Dict[str, List[Dict[str, Any]]] = {}
        
        # Register with monitor
        monitor.register_component("health_responder")
        
    async def execute_response(
        self,
        component: str,
        check_name: str,
        response: AutoResponse,
        result: HealthCheckResult
    ) -> None:
        """
        Execute an automated response if conditions are met.
        
        Args:
            component: Component name
            check_name: Name of the triggering check
            response: Response to execute
            result: Health check result that triggered the response
        """
        # Check if response should be executed
        should_execute = all(condition(result) for condition in response.conditions)
        if not should_execute:
            return
            
        # Check cooldown and attempt limits
        now = datetime.now()
        if response.last_attempt:
            if (now - response.last_attempt) < response.cooldown:
                logger.debug(f"Response {component}.{check_name} in cooldown")
                return
                
        if response.attempt_count >= response.max_attempts:
            logger.warning(
                f"Response {component}.{check_name} exceeded max attempts "
                f"({response.max_attempts})"
            )
            monitor._emit_alert(
                "warning",
                f"Response {component}.{check_name} exceeded max attempts"
            )
            return
            
        # Execute response action
        try:
            await self._execute_action(component, response.action, result.details)
            
            # Update response tracking
            response.last_attempt = now
            response.attempt_count += 1
            
            # Record in history
            response_info = {
                'check_name': check_name,
                'action': response.action.value,
                'timestamp': now.isoformat(),
                'success': True,
                'attempt': response.attempt_count,
                'details': result.details
            }
            
            await self._update_history(component, response_info)
            
            # Send metrics to monitor
            self._send_response_metrics(component, response_info, None)
            
        except Exception as e:
            logger.error(
                f"Error executing response {component}.{check_name}: {str(e)}"
            )
            
            # Record failure in history
            response_info = {
                'check_name': check_name,
                'action': response.action.value,
                'timestamp': now.isoformat(),
                'success': False,
                'error': str(e),
                'attempt': response.attempt_count,
                'details': result.details
            }
            
            await self._update_history(component, response_info)
            
            # Send error metrics to monitor
            self._send_response_metrics(component, response_info, e)
            
    async def _execute_action(
        self,
        component: str,
        action: AutoResponseAction,
        details: Dict[str, Any]
    ) -> None:
        """Execute a specific response action."""
        if action == AutoResponseAction.RESTART_COMPONENT:
            await self._restart_component(component)
            
        elif action == AutoResponseAction.CLEAR_QUEUE:
            await self._clear_component_queues(component)
            
        elif action == AutoResponseAction.RESET_THREAD_POOL:
            await self._reset_thread_pool(component)
            
        elif action == AutoResponseAction.RESOLVE_CYCLE:
            await self._resolve_dependency_cycle(details.get('cycles', []))
            
        elif action == AutoResponseAction.CLEANUP_REFS:
            await self._cleanup_weak_refs(details.get('dead_refs', []))
            
        elif action == AutoResponseAction.REINITIALIZE:
            await self._reinitialize_component(component)
            
    async def _restart_component(self, component: str) -> None:
        """Restart a component through the dependency graph."""
        try:
            node = dependency_graph.get_node(component)
            if node and node.instance:
                if hasattr(node.instance, 'stop'):
                    await node.instance.stop()
                if hasattr(node.instance, 'start'):
                    await node.instance.start()
                logger.info(f"Restarted component: {component}")
        except Exception as e:
            logger.error(f"Error restarting component {component}: {str(e)}")
            raise
            
    async def _clear_component_queues(self, component: str) -> None:
        """Clear queues associated with a component."""
        try:
            instance = container.get(component)
            if instance:
                if hasattr(instance, 'clear_queues'):
                    await instance.clear_queues()
                logger.info(f"Cleared queues for component: {component}")
        except Exception as e:
            logger.error(f"Error clearing queues for {component}: {str(e)}")
            raise
            
    async def _reset_thread_pool(self, component: str) -> None:
        """Reset a component's thread pool."""
        try:
            instance = container.get(component)
            if instance and hasattr(instance, 'thread_pool'):
                instance.thread_pool.shutdown(wait=True)
                instance.thread_pool.initialize()
                logger.info(f"Reset thread pool for component: {component}")
        except Exception as e:
            logger.error(f"Error resetting thread pool for {component}: {str(e)}")
            raise
            
    async def _resolve_dependency_cycle(self, cycles: List[List[str]]) -> None:
        """Resolve dependency cycles in the dependency graph."""
        try:
            for cycle in cycles:
                resolved = dependency_graph.resolve_cycle(cycle)
                if resolved:
                    logger.info(f"Resolved dependency cycle: {' -> '.join(cycle)}")
        except Exception as e:
            logger.error(f"Error resolving dependency cycles: {str(e)}")
            raise
            
    async def _cleanup_weak_refs(self, dead_refs: List[str]) -> None:
        """Clean up dead weak references in the container."""
        try:
            for ref in dead_refs:
                container._weak_refs.pop(ref, None)
            logger.info(f"Cleaned up {len(dead_refs)} dead references")
        except Exception as e:
            logger.error(f"Error cleaning up weak refs: {str(e)}")
            raise
            
    async def _reinitialize_component(self, component: str) -> None:
        """Reinitialize a component through the dependency graph."""
        try:
            success = await dependency_graph.initialize_node(component)
            if success:
                logger.info(f"Reinitialized component: {component}")
            else:
                raise RuntimeError(f"Failed to reinitialize component: {component}")
        except Exception as e:
            logger.error(f"Error reinitializing component {component}: {str(e)}")
            raise
            
    async def _update_history(self, component: str, entry: Dict[str, Any]) -> None:
        """Update response history for a component."""
        async with self._lock:
            if component not in self._response_history:
                self._response_history[component] = []
            self._response_history[component].append(entry)
            
    def get_response_history(
        self,
        component: Optional[str] = None
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Get response history for a component or all components."""
        if component:
            return {component: self._response_history.get(component, [])}
        return self._response_history 

    def _send_response_metrics(
        self,
        component: str,
        response_info: Dict[str, Any],
        error: Optional[Exception]
    ) -> None:
        """Send response metrics to the monitor."""
        try:
            # Get component history
            history = self._response_history.get(component, [])
            
            # Calculate metrics
            total_responses = len(history)
            successful = sum(1 for r in history if r.get('success', False))
            failed = total_responses - successful
            
            # Group by action type
            action_counts = {}
            for r in history:
                action = r.get('action')
                if action:
                    action_counts[action] = action_counts.get(action, 0) + 1
            
            monitor._store_metrics(MonitorMetrics(
                timestamp=datetime.now(),
                component_metrics={
                    'health_responder': {
                        component: {
                            'total_responses': total_responses,
                            'successful_responses': successful,
                            'failed_responses': failed,
                            'success_rate': successful / total_responses if total_responses > 0 else 0,
                            'action_counts': action_counts,
                            'last_response': response_info
                        }
                    }
                },
                system_metrics={
                    'total_components_responded': len(self._response_history),
                    'total_responses_executed': sum(len(h) for h in self._response_history.values())
                },
                warnings=[],
                errors=[str(error)] if error else []
            ))
            
        except Exception as e:
            logger.error(f"Error sending response metrics to monitor: {str(e)}")
            monitor._emit_alert("error", f"Failed to send response metrics: {str(e)}")

    async def cleanup(self) -> None:
        """Clean up executor resources and state."""
        try:
            async with self._lock:
                # Clear response history
                self._response_history.clear()
                
                # Unregister from monitor
                monitor.unregister_component("health_responder")
                
                logger.info("Cleaned up response executor")
                
        except Exception as e:
            logger.error(f"Error during response executor cleanup: {str(e)}")
            monitor._emit_alert("error", f"Error during response executor cleanup: {str(e)}")
            raise

    async def handle_check_result(
        self,
        component: str,
        check: HealthCheck,
        result: HealthCheckResult
    ) -> None:
        """
        Handle a health check result and execute any automated responses.
        
        Args:
            component: Component name
            check: Health check that was run
            result: Result of the health check
        """
        async with self._lock:
            # Record result in history
            if component not in self._response_history:
                self._response_history[component] = []
                
            self._response_history[component].append({
                'timestamp': datetime.now().isoformat(),
                'check': check.name,
                'status': result.status.value,
                'message': result.message,
                'details': result.details
            })
            
            # Trim history if needed
            if len(self._response_history[component]) > 100:  # Keep last 100 entries
                self._response_history[component] = self._response_history[component][-100:]
                
    def get_response_history(self, component: str) -> List[Dict[str, Any]]:
        """Get response history for a component."""
        return self._response_history.get(component, [])
        
    async def cleanup(self) -> None:
        """Clean up resources."""
        self._response_history.clear() 