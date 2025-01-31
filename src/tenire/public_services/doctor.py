"""
Health monitoring system for the Tenire framework.

This module provides comprehensive health monitoring for framework components,
integrating with the dependency graph to track component states, detect issues,
and provide diagnostic information.
"""

# Standard library imports
from typing import Dict, Any
from datetime import datetime
import threading
import asyncio
import queue

# Core framework imports
from tenire.core.container import container
from tenire.core.codex import Signal, SignalType
from tenire.organizers.concurrency.monitor import monitor, MonitorMetrics
from tenire.organizers.scheduler import (
    universal_timer,
    orchestrator,
    TimingConfig
)

# Health monitoring imports
from tenire.public_services.hospital.coordinator import HealthCoordinator
from tenire.servicers import get_signal_manager

# Utility imports
from tenire.utils.logger import get_logger
logger = get_logger(__name__)

class ComponentDoctor(HealthCoordinator):
    """
    Health monitoring system for framework components.
    
    This class extends HealthCoordinator to maintain backward compatibility
    while leveraging the modular hospital system and thread-based scheduling.
    
    Features:
    - Component health checks in separate thread
    - Dependency state monitoring
    - Performance metrics tracking
    - Issue detection and diagnosis
    - Health status reporting
    - Integration with signal system
    - Automated response handling
    """
    
    def __init__(self):
        """Initialize the component doctor."""
        super().__init__()
        
        # Thread control
        self._monitor_thread: threading.Thread = None
        self._stop_event = threading.Event()
        self._metrics_queue = queue.Queue(maxsize=1000)
        
        # State tracking
        self._is_monitoring = False
        self._last_check_time = 0
        self._error_count = 0
        self._backoff_delay = 1.0
        
        # Register with monitor
        monitor.register_component("component_doctor")
        logger.debug("Initialized ComponentDoctor with thread-based monitoring")
        
        # Register with universal timer
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(self._register_with_timer())

    async def _register_with_timer(self):
        """Register with universal timer for coordinated timing."""
        try:
            # Register timing configuration
            await orchestrator.register_handler(
                "component_doctor",
                TimingConfig(
                    min_interval=1.0,    # Base check interval
                    max_interval=5.0,    # Max check interval
                    burst_limit=5,       # Limit consecutive checks
                    cooldown=2.0,        # Cooldown after burst
                    priority=80          # Priority level
                )
            )
            
            # Register monitoring callback
            await universal_timer.register_timer(
                name="component_doctor_monitor",
                interval=1.0,  # Initial interval
                callback=self._monitor_callback
            )
            
            logger.debug("Registered ComponentDoctor with universal timer")
            
        except Exception as e:
            logger.error(f"Error registering with timer: {str(e)}")

    async def start_monitoring(self) -> None:
        """Start the health monitoring system in a separate thread."""
        if self._is_monitoring:
            logger.warning("Monitoring already active")
            return
            
        try:
            # Start hospital components first
            await super().start_monitoring()
            
            # Start thread-based monitoring
            self._stop_event.clear()
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                name="ComponentDoctorThread",
                daemon=True
            )
            self._monitor_thread.start()
            self._is_monitoring = True
            
            logger.info("Started ComponentDoctor monitoring thread")
            
            # Emit monitoring started signal
            await self._emit_monitoring_signal("started")
            
        except Exception as e:
            logger.error(f"Error starting monitoring: {str(e)}")
            raise

    async def stop_monitoring(self) -> None:
        """Stop the health monitoring system."""
        if not self._is_monitoring:
            return
            
        try:
            # Stop thread-based monitoring
            self._stop_event.set()
            if self._monitor_thread:
                self._monitor_thread.join(timeout=5.0)
                if self._monitor_thread.is_alive():
                    logger.warning("Monitor thread did not stop gracefully")
                self._monitor_thread = None
                
            self._is_monitoring = False
            
            # Stop hospital components
            await super().stop_monitoring()
            
            logger.info("Stopped ComponentDoctor monitoring")
            
            # Emit monitoring stopped signal
            await self._emit_monitoring_signal("stopped")
            
        except Exception as e:
            logger.error(f"Error stopping monitoring: {str(e)}")
            raise

    def _monitor_loop(self) -> None:
        """Main monitoring loop running in separate thread."""
        while not self._stop_event.is_set():
            try:
                # Get next check interval from orchestrator
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                interval = loop.run_until_complete(
                    orchestrator.get_next_interval("component_doctor")
                )
                
                # Run monitoring cycle
                loop.run_until_complete(self._run_monitoring_cycle())
                
                # Reset error state on success
                self._error_count = 0
                self._backoff_delay = 1.0
                
                # Sleep for the interval
                self._stop_event.wait(interval)
                
            except Exception as e:
                self._handle_monitoring_error(e)
                self._stop_event.wait(self._backoff_delay)
                
            finally:
                loop.close()

    async def _monitor_callback(self) -> None:
        """Callback for universal timer to trigger monitoring."""
        if not self._is_monitoring:
            return
            
        try:
            # Check if enough time has passed since last check
            current_time = self.time.monotonic()
            if current_time - self._last_check_time < 1.0:  # Minimum 1 second between checks
                return
                
            # Run monitoring cycle
            await self._run_monitoring_cycle()
            self._last_check_time = current_time
            
        except Exception as e:
            logger.error(f"Error in monitor callback: {str(e)}")

    def _handle_monitoring_error(self, error: Exception) -> None:
        """Handle errors in the monitoring loop with exponential backoff."""
        self._error_count += 1
        self._backoff_delay = min(self._backoff_delay * 2, 30.0)  # Max 30 second backoff
        
        logger.error(
            f"Error in monitoring loop (attempt {self._error_count}): {str(error)}"
        )
        
        # Store error in metrics queue
        try:
            self._metrics_queue.put_nowait({
                'timestamp': datetime.now().isoformat(),
                'error': str(error),
                'attempt': self._error_count,
                'backoff': self._backoff_delay
            })
        except queue.Full:
            pass

    async def cleanup(self) -> None:
        """Clean up the component doctor and its threads."""
        try:
            # Stop monitoring first
            await self.stop_monitoring()
            
            # Clean up hospital components
            await super().cleanup()
            
            # Clear metrics queue
            while not self._metrics_queue.empty():
                try:
                    self._metrics_queue.get_nowait()
                except queue.Empty:
                    break
                    
            # Unregister from monitor
            monitor.unregister_component("component_doctor")
            
            # Unregister from timer
            try:
                await universal_timer.unregister_timer("component_doctor_monitor")
            except Exception as e:
                logger.error(f"Error unregistering from timer: {str(e)}")
            
            logger.info("Cleaned up ComponentDoctor")
            
        except Exception as e:
            logger.error(f"Error during ComponentDoctor cleanup: {str(e)}")
            raise

    @property
    def monitoring_state(self) -> Dict[str, Any]:
        """Get current monitoring state information."""
        return {
            'is_monitoring': self._is_monitoring,
            'error_count': self._error_count,
            'backoff_delay': self._backoff_delay,
            'thread_active': bool(self._monitor_thread and self._monitor_thread.is_alive()),
            'metrics_queue_size': self._metrics_queue.qsize(),
            'last_check_time': self._last_check_time
        }

# Create global instance
component_doctor = ComponentDoctor()

# Register with container using lazy initialization to avoid circular dependencies
container.register_lazy_sync(
    'component_doctor',
    lambda: component_doctor,
    dependencies={
        'compactor',  # Ensure compactor is available for cleanup registration
        'signal_manager'  # Ensure signal manager is available for monitoring
    }
)

# Register cleanup task after container is ready
def _register_cleanup():
    try:
        from tenire.organizers.compactor import compactor
        if compactor:
            compactor.register_cleanup_task(
                name="component_doctor_cleanup",
                cleanup_func=component_doctor.cleanup,
                priority=90,  # High priority for health system cleanup
                is_async=True,
                metadata={
                    "tags": ["health", "monitoring", "core"],
                    "description": "Cleanup component health monitoring system"
                }
            )
    except ImportError:
        logger.debug("Compactor not available for cleanup registration")

container.register_lazy_sync(
    'component_doctor_cleanup',
    _register_cleanup,
    dependencies={'compactor'}
) 