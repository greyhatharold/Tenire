"""
Thread-based monitoring system for the concurrency framework.

This module provides a monitoring system that runs in a separate thread to track
and report on the health and performance of various concurrency components.
"""

# Standard library imports
import threading
import queue
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Any, Optional, List, Set
import asyncio

# Local imports
from tenire.utils.logger import get_logger
from tenire.core.container import container
from tenire.organizers.scheduler import orchestrator, TimingConfig

logger = get_logger(__name__)

@dataclass
class MonitorMetrics:
    """Data structure for monitoring metrics."""
    timestamp: datetime
    component_metrics: Dict[str, Dict[str, Any]]
    system_metrics: Dict[str, Any]
    warnings: List[str]
    errors: List[str]

class AbstractMonitor(ABC):
    """Abstract base class for monitoring system."""
    
    @abstractmethod
    def start(self) -> None:
        """Start the monitoring thread."""
        pass
        
    @abstractmethod
    def stop(self) -> None:
        """Stop the monitoring thread."""
        pass
        
    @abstractmethod
    def get_metrics(self) -> MonitorMetrics:
        """Get current monitoring metrics."""
        pass

class ThreadedMonitor(AbstractMonitor):
    """
    Thread-based monitoring system for concurrency components.
    
    This class runs monitoring operations in a separate thread to avoid
    blocking the main event loop. It collects metrics from various components
    and reports on system health.
    """
    
    def __init__(
        self,
        interval: float = 1.0,
        metrics_queue_size: int = 1000,
        alert_threshold: int = 3
    ):
        """
        Initialize the threaded monitor.
        
        Args:
            interval: Monitoring interval in seconds
            metrics_queue_size: Size of metrics history queue
            alert_threshold: Number of consecutive warnings before alerting
        """
        self._interval = interval
        self._metrics_queue = queue.Queue(maxsize=metrics_queue_size)
        self._alert_threshold = alert_threshold
        
        # Thread control
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._metrics_lock = threading.Lock()
        
        # State tracking
        self._warning_count = 0
        self._error_count = 0
        self._monitored_components: Set[str] = set()
        self._last_metrics: Optional[MonitorMetrics] = None
        self._is_monitoring = False
        self._initialized = False
        
        # Component references (will be initialized later)
        self._signal_manager = None
        self._compactor = None
        
        logger.debug("Initialized ThreadedMonitor")
        
        # Register with container
        container.register_sync('monitor', self)
        
        # Register with orchestrator when available
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(self._register_with_orchestrator())
        
    async def _register_with_orchestrator(self):
        """Register monitor timing with orchestrator."""
        try:
            await orchestrator.register_handler(
                "monitor",
                TimingConfig(
                    min_interval=1.0,    # Regular monitoring interval
                    max_interval=5.0,    # Max monitoring delay
                    burst_limit=5,       # Limited monitoring burst
                    cooldown=2.0,        # Longer cooldown
                    priority=50          # Lower priority
                )
            )
            logger.debug("Registered monitor timing configuration")
        except Exception as e:
            logger.error(f"Error registering monitor timing: {str(e)}")
        
    async def ensure_initialized(self) -> bool:
        """Ensure monitor is properly initialized with dependencies."""
        if self._initialized:
            return True
            
        try:
            # Get required dependencies
            self._signal_manager = container.get('signal_manager')
            self._compactor = container.get('compactor')
            
            if not self._signal_manager or not self._compactor:
                logger.error("Required dependencies not available")
                return False
                
            # Register with orchestrator
            await self._register_with_orchestrator()
            
            self._initialized = True
            return True
            
        except Exception as e:
            logger.error(f"Error initializing monitor: {str(e)}")
            return False
            
    async def start(self) -> bool:
        """Start the monitoring thread."""
        if not await self.ensure_initialized():
            return False
            
        if self._is_monitoring:
            logger.warning("Monitor already running")
            return True
            
        logger.info("Starting monitoring thread")
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="MonitorThread",
            daemon=True
        )
        self._monitor_thread.start()
        self._is_monitoring = True
        return True
        
    def stop(self) -> None:
        """Stop the monitoring thread."""
        if not self._is_monitoring:
            return
            
        logger.info("Stopping monitoring thread")
        self._stop_event.set()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5.0)
            if self._monitor_thread.is_alive():
                logger.warning("Monitor thread did not stop gracefully")
            self._monitor_thread = None
        self._is_monitoring = False
        
    def get_metrics(self) -> MonitorMetrics:
        """Get the most recent metrics."""
        with self._metrics_lock:
            return self._last_metrics if self._last_metrics else MonitorMetrics(
                timestamp=datetime.now(),
                component_metrics={},
                system_metrics={},
                warnings=[],
                errors=[]
            )
            
    def _monitor_loop(self) -> None:
        """Main monitoring loop running in separate thread."""
        # Create a new event loop for this thread
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # Ensure thread pool is initialized before starting monitoring
            try:
                loop.run_until_complete(self._ensure_thread_pool())
            except Exception as e:
                logger.error(f"Failed to initialize thread pool for monitoring: {str(e)}")
                return
                
            while not self._stop_event.is_set():
                try:
                    metrics = self._collect_metrics()
                    self._process_metrics(metrics)
                    self._store_metrics(metrics)
                    
                    # Run emit_metrics in the event loop
                    try:
                        loop.run_until_complete(self._emit_metrics_async(metrics))
                    except Exception as e:
                        logger.error(f"Error emitting metrics: {str(e)}")
                        self._error_count += 1
                    
                    # Get next interval from orchestrator if available
                    try:
                        interval = loop.run_until_complete(
                            orchestrator.get_next_interval("monitor")
                        )
                    except Exception:
                        interval = self._interval
                        
                    # Sleep for the monitoring interval
                    self._stop_event.wait(interval)
                    
                except Exception as e:
                    logger.error(f"Error in monitoring loop: {str(e)}")
                    self._error_count += 1
                    if self._error_count >= self._alert_threshold:
                        try:
                            loop.run_until_complete(self._emit_alert_async("error", str(e)))
                        except Exception as alert_error:
                            logger.error(f"Failed to emit alert: {str(alert_error)}")
                        
        finally:
            # Clean up the event loop
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception as e:
                logger.error(f"Error shutting down async generators: {str(e)}")
            try:
                loop.close()
            except Exception as e:
                logger.error(f"Error closing event loop: {str(e)}")
                
    async def _ensure_thread_pool(self) -> None:
        """Ensure thread pool is properly initialized."""
        try:
            concurrency_manager = container.get('concurrency_manager')
            if concurrency_manager and concurrency_manager.thread_pool:
                await concurrency_manager.thread_pool.ensure_initialized()
        except Exception as e:
            logger.error(f"Error ensuring thread pool: {str(e)}")
            raise

    def _collect_metrics(self) -> MonitorMetrics:
        """Collect metrics from all monitored components."""
        component_metrics = {}
        system_metrics = {}
        warnings = []
        errors = []
        
        try:
            # Get concurrency manager metrics
            concurrency_manager = container.get('concurrency_manager')
            if concurrency_manager:
                # Thread pool metrics
                if hasattr(concurrency_manager.thread_pool, '_pool'):
                    pool = concurrency_manager.thread_pool._pool
                    component_metrics['thread_pool'] = {
                        'active_threads': len([t for t in pool._threads if t.is_alive()]),
                        'max_workers': pool._max_workers,
                        'queue_size': pool._work_queue.qsize()
                    }
                
                # Data manager metrics
                if hasattr(concurrency_manager, 'data_manager'):
                    component_metrics['data_manager'] = concurrency_manager.data_manager.get_metrics()
                
                # GUI metrics
                if hasattr(concurrency_manager, '_gui_state'):
                    component_metrics['gui'] = concurrency_manager.gui_state
                    
            # Get system-wide metrics
            system_metrics.update({
                'cpu_percent': self._get_cpu_usage(),
                'memory_percent': self._get_memory_usage(),
                'thread_count': threading.active_count(),
                'timestamp': datetime.now().isoformat()
            })
            
        except Exception as e:
            errors.append(f"Error collecting metrics: {str(e)}")
            
        return MonitorMetrics(
            timestamp=datetime.now(),
            component_metrics=component_metrics,
            system_metrics=system_metrics,
            warnings=warnings,
            errors=errors
        )
        
    def _process_metrics(self, metrics: MonitorMetrics) -> None:
        """Process collected metrics and check for issues."""
        # Check thread pool health
        if 'thread_pool' in metrics.component_metrics:
            tp_metrics = metrics.component_metrics['thread_pool']
            if tp_metrics['queue_size'] > tp_metrics['max_workers'] * 2:
                metrics.warnings.append(
                    f"Thread pool queue size ({tp_metrics['queue_size']}) exceeds worker capacity"
                )
                
        # Check data manager health
        if 'data_manager' in metrics.component_metrics:
            dm_metrics = metrics.component_metrics['data_manager']
            if dm_metrics.get('failed_batches', 0) > 0:
                metrics.warnings.append(
                    f"Data manager has {dm_metrics['failed_batches']} failed batches"
                )
                
        # Check GUI health
        if 'gui' in metrics.component_metrics:
            gui_metrics = metrics.component_metrics['gui']
            if gui_metrics.get('error'):
                metrics.errors.append(f"GUI error: {gui_metrics['error']}")
                
        # Update warning/error counts
        if metrics.warnings:
            self._warning_count += 1
            if self._warning_count >= self._alert_threshold:
                self._emit_alert("warning", "\n".join(metrics.warnings))
        else:
            self._warning_count = 0
            
        if metrics.errors:
            self._error_count += 1
            if self._error_count >= self._alert_threshold:
                self._emit_alert("error", "\n".join(metrics.errors))
        else:
            self._error_count = 0
            
    def _store_metrics(self, metrics: MonitorMetrics) -> None:
        """Store metrics in the queue and update last metrics."""
        try:
            self._metrics_queue.put_nowait(metrics)
        except queue.Full:
            # Remove oldest metric if queue is full
            try:
                self._metrics_queue.get_nowait()
                self._metrics_queue.put_nowait(metrics)
            except queue.Empty:
                pass
                
        with self._metrics_lock:
            self._last_metrics = metrics
            
    async def _emit_metrics_async(self, metrics: MonitorMetrics) -> None:
        """Async version of emit metrics."""
        try:
            if not self._signal_manager:
                from tenire.core.container import container
                self._signal_manager = container.get('signal_manager')
                
            if self._signal_manager:
                # Import signal types lazily
                from tenire.core.codex import Signal, SignalType
                await self._signal_manager.emit(Signal(
                    type=SignalType.MONITOR_METRICS,
                    data={
                        'metrics': {
                            'components': metrics.component_metrics,
                            'system': metrics.system_metrics,
                            'warnings': metrics.warnings,
                            'errors': metrics.errors,
                            'timestamp': metrics.timestamp.isoformat()
                        }
                    },
                    source='threaded_monitor'
                ))
        except Exception as e:
            logger.error(f"Error emitting metrics: {str(e)}")
            
    async def _emit_alert_async(self, level: str, message: str) -> None:
        """Async version of emit alert."""
        try:
            if not self._signal_manager:
                from tenire.core.container import container
                self._signal_manager = container.get('signal_manager')
                
            if self._signal_manager:
                # Import signal types lazily
                from tenire.core.codex import Signal, SignalType
                await self._signal_manager.emit(Signal(
                    type=SignalType.MONITOR_ALERT,
                    data={
                        'level': level,
                        'message': message,
                        'timestamp': datetime.now().isoformat()
                    },
                    source='threaded_monitor'
                ))
        except Exception as e:
            logger.error(f"Error emitting alert: {str(e)}")
            
    def _get_cpu_usage(self) -> float:
        """Get CPU usage percentage."""
        try:
            import psutil
            return psutil.cpu_percent(interval=0.1)
        except ImportError:
            return 0.0
            
    def _get_memory_usage(self) -> float:
        """Get memory usage percentage."""
        try:
            import psutil
            return psutil.virtual_memory().percent
        except ImportError:
            return 0.0
            
    @property
    def is_monitoring(self) -> bool:
        """Check if monitoring is active."""
        return self._is_monitoring
        
    def register_component(self, component: str) -> None:
        """Register a component for monitoring."""
        self._monitored_components.add(component)
        logger.debug(f"Registered component for monitoring: {component}")
        
    def unregister_component(self, component: str) -> None:
        """Unregister a component from monitoring."""
        self._monitored_components.discard(component)
        logger.debug(f"Unregistered component from monitoring: {component}")
        
    def get_component_metrics(self, component: str) -> Dict[str, Any]:
        """Get metrics for a specific component."""
        with self._metrics_lock:
            if self._last_metrics:
                return self._last_metrics.component_metrics.get(component, {})
        return {}
        
    def get_system_metrics(self) -> Dict[str, Any]:
        """Get system-wide metrics."""
        with self._metrics_lock:
            if self._last_metrics:
                return self._last_metrics.system_metrics
        return {}
        
    def clear_metrics(self) -> None:
        """Clear stored metrics."""
        with self._metrics_lock:
            while not self._metrics_queue.empty():
                try:
                    self._metrics_queue.get_nowait()
                except queue.Empty:
                    break
            self._last_metrics = None
            
    def get_metrics_history(self) -> List[MonitorMetrics]:
        """Get historical metrics."""
        with self._metrics_lock:
            return list(self._metrics_queue.queue)

    def _emit_metrics(self, metrics: MonitorMetrics) -> None:
        """Synchronous wrapper for emit metrics - deprecated, use _emit_metrics_async."""
        logger.warning("Synchronous _emit_metrics called - use _emit_metrics_async instead")
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._emit_metrics_async(metrics))
        except Exception as e:
            logger.error(f"Error in synchronous emit metrics: {str(e)}")

    def _emit_alert(self, level: str, message: str) -> None:
        """Synchronous wrapper for emit alert - deprecated, use _emit_alert_async instead."""
        logger.warning("Synchronous _emit_alert called - use _emit_alert_async instead")
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._emit_alert_async(level, message))
        except Exception as e:
            logger.error(f"Error in synchronous emit alert: {str(e)}")


# Create global instance
monitor = ThreadedMonitor()

# Register with container
container.register_sync('monitor', monitor) 