"""
Data flow agent for overseeing and managing data flow processes.

This agent is responsible for:
1. Monitoring data flow health
2. Managing data cleaning pipelines
3. Coordinating between components
4. Handling errors and recovery
5. Optimizing flow performance
"""

import asyncio
from typing import Dict, List, Any, Optional
from datetime import datetime
from pathlib import Path

from tenire.utils.logger import get_logger
from tenire.core.codex import Signal, SignalType
from tenire.servicers import SignalManager
from tenire.organizers.compactor import compactor
from tenire.core.container import container
from tenire.data.seive import (
    DataRoute, DataStream
)

logger = get_logger(__name__)

class DataFlowAgent:
    """
    Agent for managing and monitoring data flow processes.
    
    This agent provides:
    1. Health monitoring and diagnostics
    2. Automatic error recovery
    3. Performance optimization
    4. Flow coordination
    5. Pipeline management
    """
    
    def __init__(
        self,
        data_dir: str = 'data',
        check_interval: int = 60,
        error_threshold: int = 10,
        cleanup_interval: int = 3600,
        max_retry_attempts: int = 3
    ):
        """
        Initialize the data flow agent.
        
        Args:
            data_dir: Base directory for data
            check_interval: Interval between health checks in seconds
            error_threshold: Number of errors before intervention
            cleanup_interval: Interval between cleanups in seconds
            max_retry_attempts: Maximum number of retry attempts
        """
        self.data_dir = Path(data_dir)
        self.check_interval = check_interval
        self.error_threshold = error_threshold
        self.cleanup_interval = cleanup_interval
        self.max_retry_attempts = max_retry_attempts
        
        # Get seive instance
        self.seive = container.get('data_seive')
        if not self.seive:
            raise RuntimeError("Data seive not initialized")
        
        # Monitoring state
        self._health_check_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None
        self._is_monitoring = False
        self._last_cleanup = datetime.now()
        
        # Error tracking
        self._error_counts: Dict[str, int] = {}
        self._retry_counts: Dict[str, int] = {}
        self._last_errors: Dict[str, datetime] = {}
        
        # Performance metrics
        self._metrics = {
            'total_processed': 0,
            'total_errors': 0,
            'total_retries': 0,
            'avg_processing_time': 0.0,
            'routes_health': {},
            'streams_health': {}
        }
        
        # Register cleanup
        compactor.register_cleanup_task(
            name="data_flow_agent_cleanup",
            cleanup_func=self.cleanup,
            priority=98,  # Higher than seive
            is_async=True,
            metadata={"tags": ["agent", "data_flow"]}
        )
        
        # Register signal handlers
        SignalManager().register_handler(
            SignalType.DATA_FLOW_METRICS,
            self._handle_metrics_update,
            priority=95
        )
        SignalManager().register_handler(
            SignalType.DATA_FLOW_ROUTE_CREATED,
            self._handle_route_created,
            priority=95
        )
        SignalManager().register_handler(
            SignalType.DATA_FLOW_STREAM_CREATED,
            self._handle_stream_created,
            priority=95
        )
        
        logger.info("Initialized DataFlowAgent")
        
    async def start(self) -> None:
        """Start the agent's monitoring tasks."""
        if not self._is_monitoring:
            self._is_monitoring = True
            
            # Start seive if not started
            await self.seive.start()
            
            # Start monitoring tasks
            self._health_check_task = asyncio.create_task(self._monitor_health())
            self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
            
            logger.info("Started DataFlowAgent monitoring")
            
    async def stop(self) -> None:
        """Stop the agent's monitoring tasks."""
        self._is_monitoring = False
        
        # Cancel monitoring tasks
        if self._health_check_task:
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
                
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
                
        logger.info("Stopped DataFlowAgent monitoring")
        
    async def _monitor_health(self) -> None:
        """Monitor health of data flow components."""
        while self._is_monitoring:
            try:
                # Check route health
                for route_id, route in self.seive.routes.items():
                    await self._check_route_health(route_id, route)
                    
                # Check stream health
                for stream_name, stream in self.seive.streams.items():
                    await self._check_stream_health(stream_name, stream)
                    
                # Update metrics
                await self._update_metrics()
                
                # Emit health status
                await SignalManager().emit(Signal(
                    type=SignalType.DATA_FLOW_HEALTH_STATUS,
                    data={
                        'metrics': self._metrics,
                        'routes_health': self._metrics['routes_health'],
                        'streams_health': self._metrics['streams_health']
                    },
                    source='data_flow_agent'
                ))
                
                await asyncio.sleep(self.check_interval)
                
            except Exception as e:
                logger.error(f"Error in health monitor: {str(e)}")
                await asyncio.sleep(5)  # Back off on error
                
    async def _check_route_health(self, route_id: str, route: DataRoute) -> None:
        """Check health of a specific route."""
        try:
            # Check error rate
            error_rate = route.metrics['errors'] / max(route.metrics['processed'], 1)
            
            # Check processing time
            if route.metrics['last_processed']:
                time_since_last = (datetime.now() - route.metrics['last_processed']).total_seconds()
            else:
                time_since_last = float('inf')
                
            # Update health status
            health_status = {
                'error_rate': error_rate,
                'time_since_last': time_since_last,
                'queue_size': route.queue.qsize(),
                'is_healthy': error_rate < 0.1 and time_since_last < 300  # 5 minutes
            }
            
            self._metrics['routes_health'][route_id] = health_status
            
            # Handle unhealthy route
            if not health_status['is_healthy']:
                await self._handle_unhealthy_route(route_id, route, health_status)
                
        except Exception as e:
            logger.error(f"Error checking route health for {route_id}: {str(e)}")
            
    async def _check_stream_health(self, stream_name: str, stream: DataStream) -> None:
        """Check health of a specific stream."""
        try:
            # Check buffer utilization
            buffer_usage = stream.buffer.qsize() / stream.max_buffer
            
            # Update health status
            health_status = {
                'buffer_usage': buffer_usage,
                'is_active': stream.is_active,
                'cleaning_stage': stream.current_stage.value if stream.cleaning_required else None,
                'is_healthy': buffer_usage < 0.9 and stream.is_active
            }
            
            self._metrics['streams_health'][stream_name] = health_status
            
            # Handle unhealthy stream
            if not health_status['is_healthy']:
                await self._handle_unhealthy_stream(stream_name, stream, health_status)
                
        except Exception as e:
            logger.error(f"Error checking stream health for {stream_name}: {str(e)}")
            
    async def _handle_unhealthy_route(
        self,
        route_id: str,
        route: DataRoute,
        health_status: Dict[str, Any]
    ) -> None:
        """Handle an unhealthy route."""
        try:
            # Increment error count
            self._error_counts[route_id] = self._error_counts.get(route_id, 0) + 1
            
            # Check if we should retry
            if self._error_counts[route_id] >= self.error_threshold:
                if self._retry_counts.get(route_id, 0) < self.max_retry_attempts:
                    # Attempt recovery
                    await self._recover_route(route_id, route)
                else:
                    # Stop route if max retries exceeded
                    logger.warning(f"Max retries exceeded for route {route_id}, stopping route")
                    await self.seive.stop_route(route_id)
                    
        except Exception as e:
            logger.error(f"Error handling unhealthy route {route_id}: {str(e)}")
            
    async def _handle_unhealthy_stream(
        self,
        stream_name: str,
        stream: DataStream,
        health_status: Dict[str, Any]
    ) -> None:
        """Handle an unhealthy stream."""
        try:
            # Check buffer overflow
            if health_status['buffer_usage'] >= 0.9:
                logger.warning(f"Stream {stream_name} buffer near capacity, applying backpressure")
                # Apply backpressure by temporarily stopping stream
                await self.seive.stop_stream(stream_name)
                
                # Wait for buffer to clear
                while stream.buffer.qsize() > stream.max_buffer * 0.5:
                    await asyncio.sleep(1)
                    
                # Restart stream
                stream.is_active = True
                
        except Exception as e:
            logger.error(f"Error handling unhealthy stream {stream_name}: {str(e)}")
            
    async def _recover_route(self, route_id: str, route: DataRoute) -> None:
        """Attempt to recover a failed route."""
        try:
            logger.info(f"Attempting to recover route {route_id}")
            
            # Increment retry count
            self._retry_counts[route_id] = self._retry_counts.get(route_id, 0) + 1
            
            # Stop current processing
            await self.seive.stop_route(route_id)
            
            # Clear queue
            while not route.queue.empty():
                try:
                    route.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                    
            # Reset metrics
            route.metrics['errors'] = 0
            route.metrics['processed'] = 0
            
            # Restart route
            route.is_active = True
            self._processing_tasks[route_id] = asyncio.create_task(
                self.seive._process_route(route)
            )
            
            logger.info(f"Recovered route {route_id}")
            
        except Exception as e:
            logger.error(f"Error recovering route {route_id}: {str(e)}")
            
    async def _periodic_cleanup(self) -> None:
        """Perform periodic cleanup of resources."""
        while self._is_monitoring:
            try:
                if (datetime.now() - self._last_cleanup).total_seconds() >= self.cleanup_interval:
                    logger.info("Starting periodic cleanup")
                    
                    # Reset error tracking
                    self._error_counts.clear()
                    self._retry_counts.clear()
                    self._last_errors.clear()
                    
                    # Clean up route queues
                    for route in self.seive.routes.values():
                        if route.queue.qsize() > route.buffer_size * 0.9:
                            while route.queue.qsize() > route.buffer_size * 0.5:
                                try:
                                    route.queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    break
                                    
                    # Clean up stream buffers
                    for stream in self.seive.streams.values():
                        if stream.buffer.qsize() > stream.max_buffer * 0.9:
                            while stream.buffer.qsize() > stream.max_buffer * 0.5:
                                try:
                                    stream.buffer.get_nowait()
                                except asyncio.QueueEmpty:
                                    break
                                    
                    self._last_cleanup = datetime.now()
                    logger.info("Completed periodic cleanup")
                    
                await asyncio.sleep(60)  # Check every minute
                
            except Exception as e:
                logger.error(f"Error in periodic cleanup: {str(e)}")
                await asyncio.sleep(5)  # Back off on error
                
    async def _update_metrics(self) -> None:
        """Update agent metrics."""
        try:
            total_processed = sum(
                route.metrics['processed']
                for route in self.seive.routes.values()
            )
            total_errors = sum(
                route.metrics['errors']
                for route in self.seive.routes.values()
            )
            
            self._metrics.update({
                'total_processed': total_processed,
                'total_errors': total_errors,
                'total_retries': sum(self._retry_counts.values()),
                'avg_processing_time': sum(
                    route.metrics['last_processed'].timestamp() - datetime.now().timestamp()
                    for route in self.seive.routes.values()
                    if route.metrics['last_processed']
                ) / len(self.seive.routes) if self.seive.routes else 0.0
            })
            
        except Exception as e:
            logger.error(f"Error updating metrics: {str(e)}")
            
    async def _handle_metrics_update(self, signal: Signal) -> None:
        """Handle metrics update signals."""
        try:
            metrics = signal.data.get('metrics', {})
            # Update relevant metrics
            for route_id, route_metrics in metrics.items():
                if route_id in self.seive.routes:
                    self._metrics['routes_health'][route_id] = route_metrics
                    
        except Exception as e:
            logger.error(f"Error handling metrics update: {str(e)}")
            
    async def _handle_route_created(self, signal: Signal) -> None:
        """Handle route created signals."""
        route_id = signal.data.get('route')
        if route_id:
            # Initialize health tracking for new route
            self._metrics['routes_health'][route_id] = {
                'error_rate': 0.0,
                'time_since_last': 0.0,
                'queue_size': 0,
                'is_healthy': True
            }
            
    async def _handle_stream_created(self, signal: Signal) -> None:
        """Handle stream created signals."""
        stream_name = signal.data.get('stream')
        if stream_name:
            # Initialize health tracking for new stream
            self._metrics['streams_health'][stream_name] = {
                'buffer_usage': 0.0,
                'is_active': True,
                'cleaning_stage': None,
                'is_healthy': True
            }
            
    async def cleanup(self) -> None:
        """Cleanup agent resources."""
        try:
            logger.info("Cleaning up DataFlowAgent")
            
            # Stop monitoring
            await self.stop()
            
            # Clear all tracking data
            self._error_counts.clear()
            self._retry_counts.clear()
            self._last_errors.clear()
            self._metrics.clear()
            
            logger.info("DataFlowAgent cleanup complete")
            
        except Exception as e:
            logger.error(f"Error during agent cleanup: {str(e)}")
            raise

# Create singleton instance
data_flow_agent = DataFlowAgent()

# Register with container
container.register_sync('data_flow_agent', data_flow_agent) 