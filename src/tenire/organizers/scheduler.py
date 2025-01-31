"""
Component initialization scheduler for the Tenire framework.

This module provides scheduling and timing control for component initialization 
and thread safety, ensuring proper sequencing and timing of component startups 
and universal timing.
"""

import asyncio
from typing import Dict, List, Optional, Set, Any, Callable, Union
from dataclasses import dataclass
from datetime import datetime
import time
import weakref

from tenire.utils.logger import get_logger
from tenire.core.codex import (
    Signal, SignalType, HealthStatus
)
from tenire.core.container import container
from tenire.organizers.concurrency.types import InitializationTask, TimingConfig
from tenire.core.event_loop import event_loop_manager

logger = get_logger(__name__)

@dataclass
class BatchInitializationGroup:
    """Group of components that can be initialized together."""
    components: List[InitializationTask]
    priority: int
    dependencies: Set[str]
    max_concurrent: int = 5  # Maximum number of components to initialize concurrently

class UniversalTimer:
    """
    Universal timer for coordinating timing across all concurrency handlers.
    
    Features:
    - Monotonic time tracking
    - Timer registration and cleanup
    - Interval-based timing coordination
    - Weak references to prevent memory leaks
    - Thread-safe timing operations
    """
    
    def __init__(self):
        """Initialize the universal timer."""
        self._start_time = time.monotonic()
        self._timers: Dict[str, weakref.ref] = {}
        self._intervals: Dict[str, float] = {}
        self._last_ticks: Dict[str, float] = {}
        self._lock = asyncio.Lock()
        self._running = True
        self._task: Optional[asyncio.Task] = None
        
    def get_elapsed(self) -> float:
        """Get elapsed time since timer start in seconds."""
        return time.monotonic() - self._start_time
        
    async def register_timer(self, name: str, interval: float, callback: Callable[[], Any]) -> None:
        """Register a new timer with callback."""
        async with self._lock:
            self._timers[name] = weakref.ref(callback)
            self._intervals[name] = interval
            self._last_ticks[name] = self.get_elapsed()
            logger.debug(f"Registered timer: {name} with interval {interval}s")
            
    async def unregister_timer(self, name: str) -> None:
        """Unregister a timer."""
        async with self._lock:
            self._timers.pop(name, None)
            self._intervals.pop(name, None)
            self._last_ticks.pop(name, None)
            logger.debug(f"Unregistered timer: {name}")
            
    async def start(self) -> None:
        """Start the timer coordination loop."""
        if self._task is not None:
            return
            
        self._running = True
        self._task = asyncio.create_task(self._coordination_loop())
        logger.info("Universal timer started")
        
    async def stop(self) -> None:
        """Stop the timer coordination loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Universal timer stopped")
        
    async def _coordination_loop(self) -> None:
        """Main timer coordination loop."""
        while self._running:
            try:
                current_time = self.get_elapsed()
                
                async with self._lock:
                    # Check each timer
                    for name, timer_ref in list(self._timers.items()):
                        interval = self._intervals.get(name)
                        last_tick = self._last_ticks.get(name)
                        
                        if interval is None or last_tick is None:
                            continue
                            
                        # Check if timer should trigger
                        if current_time - last_tick >= interval:
                            callback = timer_ref()
                            if callback is None:
                                # Callback was garbage collected
                                self._timers.pop(name, None)
                                self._intervals.pop(name, None)
                                self._last_ticks.pop(name, None)
                                continue
                                
                            try:
                                result = callback()
                                if asyncio.iscoroutine(result):
                                    await result
                            except Exception as e:
                                logger.error(f"Error in timer callback {name}: {str(e)}")
                                
                            self._last_ticks[name] = current_time
                            
                await asyncio.sleep(0.01)  # Small sleep to prevent CPU hogging
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in timer coordination loop: {str(e)}")
                await asyncio.sleep(1)  # Longer sleep on error
                
    def get_timer_status(self) -> Dict[str, Dict[str, Union[float, bool]]]:
        """Get status of all registered timers."""
        current_time = self.get_elapsed()
        return {
            name: {
                'interval': self._intervals.get(name, 0),
                'last_tick': self._last_ticks.get(name, 0),
                'next_tick': self._last_ticks.get(name, 0) + self._intervals.get(name, 0),
                'active': bool(self._timers.get(name) and self._timers[name]() is not None),
                'time_until_next': max(0, (self._last_ticks.get(name, 0) + self._intervals.get(name, 0)) - current_time)
            }
            for name in self._timers
        }

# Create global instance
universal_timer = UniversalTimer()

class Orchestrator:
    """
    Orchestrates timing across all concurrency handlers.
    
    Features:
    - Adaptive timing based on system load
    - Priority-based resource allocation
    - Burst handling with cooldown periods
    - Component-specific timing configurations
    - Health-aware scheduling adjustments
    - Container-aware initialization
    - Compactor integration for cleanup
    """
    
    _instance = None
    _lock = asyncio.Lock()
    
    def __new__(cls):
        """Ensure singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        """Initialize the orchestrator if not already initialized."""
        if not hasattr(self, '_initialized'):
            self._initialized = False
            self._timer = universal_timer
            self._configs: Dict[str, TimingConfig] = {}
            self._last_operations: Dict[str, List[float]] = {}
            self._burst_states: Dict[str, Dict[str, Any]] = {}
            self._is_active = False
            self._task: Optional[asyncio.Task] = None
            self._compactor = None
            self._signal_manager = None
            
            # Initialize default configurations
            self._init_default_configs()
            
            # Register with container
            container.register_sync('orchestrator', self)
            
            # Schedule initialization
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._initialize())
            
    async def _initialize(self):
        """Initialize the orchestrator."""
        if self._initialized:
            return
            
        try:
            # Get signal manager first
            from tenire.servicers import get_signal_manager
            self._signal_manager = await get_signal_manager()
            
            # Get compactor
            from tenire.organizers.compactor import get_compactor
            self._compactor = await get_compactor()
            
            # Start timer coordination
            self._is_active = True
            self._task = asyncio.create_task(self._coordinate_timers())
            await event_loop_manager.track_task(self._task)
            
            # Mark as initialized
            self._initialized = True
            
            logger.info("Orchestrator initialization completed")
            
        except Exception as e:
            logger.error(f"Error initializing orchestrator: {str(e)}")
            raise
            
    async def _handle_timer(self, handler: str):
        """Handle timer callback for a handler."""
        try:
            # Record the operation
            await self.record_operation(handler)
            
            # Emit timing signal using lazy import
            if self._signal_manager:
                await self._signal_manager.emit(Signal(
                    type=SignalType.HANDLER_TIMING,
                    data={
                        'handler': handler,
                        'timestamp': datetime.now().isoformat(),
                        'interval': await self.get_next_interval(handler)
                    },
                    source="orchestrator"
                ))
            
        except Exception as e:
            logger.error(f"Error in timer handler for {handler}: {str(e)}")
            
    def _init_default_configs(self):
        """Initialize default timing configurations for each component."""
        self._configs = {
            'async_task_manager': TimingConfig(
                min_interval=0.01,   # Fast task checking
                max_interval=0.1,    # Max delay for task processing
                burst_limit=100,     # High burst for task processing
                cooldown=0.5,        # Short cooldown
                priority=90          # High priority
            ),
            'data_manager': TimingConfig(
                min_interval=0.1,    # Moderate processing interval
                max_interval=1.0,    # Longer max interval for data ops
                burst_limit=50,      # Moderate burst for data processing
                cooldown=1.0,        # Longer cooldown for data ops
                priority=70          # Medium-high priority
            ),
            'gui_process': TimingConfig(
                min_interval=0.016,  # ~60 FPS
                max_interval=0.033,  # ~30 FPS minimum
                burst_limit=10,      # Limited burst for UI
                cooldown=0.1,        # Quick UI cooldown
                priority=85          # High priority for responsiveness
            ),
            'monitor': TimingConfig(
                min_interval=1.0,    # Regular monitoring interval
                max_interval=5.0,    # Max monitoring delay
                burst_limit=5,       # Limited monitoring burst
                cooldown=2.0,        # Longer cooldown
                priority=50          # Lower priority
            ),
            'thread_pool': TimingConfig(
                min_interval=0.05,   # Quick thread checking
                max_interval=0.5,    # Max thread scheduling delay
                burst_limit=20,      # Moderate burst
                cooldown=0.2,        # Short cooldown
                priority=80          # High priority
            ),
            'semaphore': TimingConfig(
                min_interval=0.01,   # Fast semaphore operations
                max_interval=0.1,    # Max semaphore delay
                burst_limit=50,      # High burst for quick ops
                cooldown=0.2,        # Short cooldown
                priority=95          # Very high priority
            )
        }
        
    async def start(self):
        """Start the orchestrator."""
        if self._is_active:
            return
            
        logger.info("Starting orchestrator")
        self._is_active = True
        await self._timer.start()
        self._task = asyncio.create_task(self._orchestration_loop())
        
    async def stop(self):
        """Stop the orchestrator."""
        if not self._is_active:
            return
            
        logger.info("Stopping orchestrator")
        self._is_active = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._timer.stop()
        
    async def register_handler(self, name: str, config: TimingConfig):
        """Register a handler with custom timing configuration."""
        async with self._lock:
            self._configs[name] = config
            self._last_operations[name] = []
            self._burst_states[name] = {
                'count': 0,
                'last_burst': 0,
                'in_cooldown': False
            }
            logger.debug(f"Registered handler: {name}")
            
    async def get_next_interval(self, handler: str) -> float:
        """Calculate the next operation interval for a handler."""
        async with self._lock:
            config = self._configs.get(handler)
            if not config:
                return 0.1  # Default interval
                
            # Get operation history
            operations = self._last_operations.get(handler, [])
            burst_state = self._burst_states.get(handler, {})
            current_time = time.monotonic()
            
            # Clean old operations
            operations = [t for t in operations if current_time - t <= config.max_interval]
            self._last_operations[handler] = operations
            
            # Check cooldown
            if burst_state.get('in_cooldown', False):
                if current_time - burst_state['last_burst'] >= config.cooldown:
                    burst_state['in_cooldown'] = False
                    burst_state['count'] = 0
                else:
                    return config.max_interval
                    
            # Calculate load-based interval
            if operations:
                op_rate = len(operations) / (current_time - operations[0] if operations else 1)
                load_factor = min(1.0, op_rate / config.burst_limit)
                interval = config.min_interval + (config.max_interval - config.min_interval) * load_factor
            else:
                interval = config.min_interval
                
            # Update burst state
            if len(operations) >= config.burst_limit:
                burst_state['in_cooldown'] = True
                burst_state['last_burst'] = current_time
                interval = config.max_interval
                
            return interval
            
    async def record_operation(self, handler: str):
        """Record an operation for a handler."""
        async with self._lock:
            current_time = time.monotonic()
            operations = self._last_operations.get(handler, [])
            operations.append(current_time)
            
            # Keep only recent operations
            config = self._configs.get(handler)
            if config:
                operations = [t for t in operations if current_time - t <= config.max_interval]
                
            self._last_operations[handler] = operations
            
            # Update burst state
            burst_state = self._burst_states.get(handler, {})
            burst_state['count'] = burst_state.get('count', 0) + 1
            
    async def _orchestration_loop(self):
        """Main orchestration loop."""
        while self._is_active:
            try:
                # Get all handlers sorted by priority
                handlers = sorted(
                    self._configs.items(),
                    key=lambda x: x[1].priority,
                    reverse=True
                )
                
                # Process each handler
                for handler, config in handlers:
                    try:
                        interval = await self.get_next_interval(handler)
                        
                        # Register timing callback
                        await self._timer.register_timer(
                            name=f"{handler}_timer",
                            interval=interval,
                            callback=lambda h=handler: self._handle_timer(h)
                        )
                        
                    except Exception as e:
                        logger.error(f"Error processing handler {handler}: {str(e)}")
                        
                await asyncio.sleep(0.1)  # Small sleep to prevent CPU hogging
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in orchestration loop: {str(e)}")
                await asyncio.sleep(1.0)  # Longer sleep on error
                
    def get_handler_status(self, handler: str) -> Dict[str, Any]:
        """Get current status for a handler."""
        config = self._configs.get(handler)
        if not config:
            return {}
            
        operations = self._last_operations.get(handler, [])
        burst_state = self._burst_states.get(handler, {})
        current_time = time.monotonic()
        
        return {
            'config': {
                'min_interval': config.min_interval,
                'max_interval': config.max_interval,
                'burst_limit': config.burst_limit,
                'cooldown': config.cooldown,
                'priority': config.priority
            },
            'state': {
                'operation_count': len(operations),
                'last_operation': current_time - operations[-1] if operations else None,
                'in_cooldown': burst_state.get('in_cooldown', False),
                'burst_count': burst_state.get('count', 0)
            }
        }
        
    def get_system_status(self) -> Dict[str, Any]:
        """Get overall system timing status."""
        return {
            handler: self.get_handler_status(handler)
            for handler in self._configs
        }

# Create global instance and register with container
orchestrator = Orchestrator()

# Register lazy initialization
async def _initialize_orchestrator():
    """Initialize orchestrator after container is ready."""
    try:
        await orchestrator._initialize()
    except Exception as e:
        logger.error(f"Error in lazy orchestrator initialization: {str(e)}")

# Register lazy initialization with container
container.register_lazy_sync(
    'orchestrator_init',
    lambda: asyncio.create_task(_initialize_orchestrator()),
    dependencies={'compactor', 'signal_manager'}
)

# Export for module access
__all__ = [
    'UniversalTimer',
    'TimingConfig',
    'Orchestrator',
    'BatchInitializationGroup',
    'universal_timer',
    'orchestrator'
]
