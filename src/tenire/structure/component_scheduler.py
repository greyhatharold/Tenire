"""
Component scheduler module for managing component initialization and scheduling.

This module provides a robust scheduler that handles component initialization
with proper dependency ordering, concurrency, and error handling.
"""

import asyncio
import enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Callable
from collections import defaultdict
import heapq

from tenire.core.container import container as core_container
from tenire.core.codex import TimingConfig
from tenire.organizers.scheduler import orchestrator, BatchInitializationGroup
from tenire.utils.logger import get_logger
from tenire.core.event_loop import event_loop_manager

logger = get_logger(__name__)

class InitializationStatus(enum.Enum):
    """Status of component initialization."""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    TIMEOUT = "timeout"
    RETRY = "retry"

@dataclass
class InitializationTask:
    """Task for initializing a component."""
    component: str
    dependencies: Set[str]
    priority: int
    init_fn: Callable[[], Any]
    timeout: float = 30.0
    retry_count: int = 3
    retry_delay: float = 1.0
    health_check: Optional[Callable[[], bool]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    _status: InitializationStatus = field(default=InitializationStatus.PENDING)
    _attempts: int = field(default=0)
    _error: Optional[Exception] = field(default=None)

    def __lt__(self, other: 'InitializationTask') -> bool:
        """Compare tasks for priority queue."""
        return (
            # Critical components first
            self.metadata.get('is_critical', False) > other.metadata.get('is_critical', False)
            or (
                self.metadata.get('is_critical', False) == other.metadata.get('is_critical', False)
                # Then by priority
                and self.priority > other.priority
            )
        )

class ComponentScheduler:
    """
    Manages component initialization and scheduling with proper dependency handling.
    
    Features:
    - Concurrent initialization of independent components
    - Proper dependency ordering
    - Retry mechanism with backoff
    - Health checks
    - Priority-based scheduling
    - Timeout handling
    - Progress tracking
    """
    
    def __init__(self):
        """Initialize the component scheduler."""
        self._tasks: Dict[str, InitializationTask] = {}
        self._status: Dict[str, InitializationStatus] = {}
        self._dependencies: Dict[str, Set[str]] = {}
        self._dependents: Dict[str, Set[str]] = defaultdict(set)
        self._initialized: Set[str] = set()
        self._failed: Set[str] = set()
        self._in_progress: Set[str] = set()
        self._task_queue: List[InitializationTask] = []
        self._initialization_lock = asyncio.Lock()
        self._batch_size = 5  # Number of concurrent initializations
        self._initialized_event = asyncio.Event()
        self._logger = get_logger(__name__)
        self._batch_groups: List[BatchInitializationGroup] = []
        self._container = core_container  # Store container reference
        
        # Pre-register signal manager as initialized if it exists
        signal_manager = self._container.get('signal_manager')
        if signal_manager and hasattr(signal_manager, '_processing_task'):
            self._initialized.add('signal_manager')
            self._status['signal_manager'] = InitializationStatus.COMPLETED
            self._logger.debug("Signal manager pre-registered as initialized")

    async def _get_container(self):
        """Get the container instance."""
        return self._container

    async def _is_component_initialized(self, component: str) -> bool:
        """Check if a component is initialized."""
        # Special case for event_loop_manager
        if component == 'event_loop_manager':
            return event_loop_manager.is_initialized
            
        # Check in initialized components
        if component in self._initialized:
            return True
            
        # Check in container
        instance = self._container.get(component)
        if instance:
            # If found in container, consider it initialized
            self._initialized.add(component)
            return True
            
        return False

    def _get_event_loop(self) -> asyncio.AbstractEventLoop:
        """Get the current event loop."""
        try:
            return asyncio.get_running_loop()
        except RuntimeError:
            # If no loop is running, create one
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    async def schedule_component(self, task: InitializationTask) -> None:
        """Schedule a component for initialization."""
        try:
            # Special handling for signal manager
            if task.component == 'signal_manager':
                # Ensure signal manager has no dependencies
                task.dependencies = set()
                task.priority = 1000  # Highest priority
                task.metadata['is_critical'] = True
                
            # Special handling for async_task_manager
            elif task.component == 'async_task_manager':
                # Ensure proper timing configuration
                await orchestrator.register_handler(
                    task.component,
                    TimingConfig(
                        min_interval=0.01,  # Fast response time
                        max_interval=0.1,   # Maximum delay
                        burst_limit=100,    # High burst limit for task management
                        cooldown=0.2,       # Short cooldown
                        priority=95         # High priority
                    )
                )
                task.metadata['is_critical'] = True
                
            # Special handling for thread_pool_manager
            elif task.component == 'thread_pool_manager':
                # Ensure dependency on async_task_manager
                task.dependencies.add('async_task_manager')
                task.metadata['requires_async'] = True
                
            # Store task
            self._tasks[task.component] = task
            self._status[task.component] = InitializationStatus.PENDING
            self._dependencies[task.component] = task.dependencies
            
            # Update dependents
            for dep in task.dependencies:
                self._dependents[dep].add(task.component)
                
            # Add to priority queue
            heapq.heappush(self._task_queue, task)
            
            # Register with orchestrator for timing control
            if task.component not in {'signal_manager', 'event_loop_manager', 'async_task_manager'}:
                await orchestrator.register_handler(
                    task.component,
                    TimingConfig(
                        min_interval=0.1,
                        max_interval=1.0,
                        burst_limit=50,
                        cooldown=1.0,
                        priority=task.priority
                    )
                )
            
            self._logger.debug(f"Scheduled component {task.component} for initialization")
            
        except Exception as e:
            self._logger.error(f"Error scheduling component {task.component}: {str(e)}")
            raise
            
    async def initialize_components(self) -> bool:
        """Initialize all scheduled components with proper concurrency."""
        try:
            async with self._initialization_lock:
                self._logger.info("Starting component initialization")
                
                # Create optimized batch groups
                self._create_batch_groups()
                
                # Initialize each batch group
                for batch_group in self._batch_groups:
                    if not await self._initialize_batch_group(batch_group):
                        return False
                        
                self._logger.info("Component initialization completed successfully")
                self._initialized_event.set()
                return True
                
        except Exception as e:
            self._logger.error(f"Error during component initialization: {str(e)}")
            return False
            
    def _create_batch_groups(self) -> None:
        """Create optimized batch groups for parallel initialization."""
        # Critical core components in strict order
        core_order = [
            'signal_manager',      # Must be first - no dependencies
            'event_loop_manager',  # Must be second - no dependencies
            'async_task_manager',  # Depends on event_loop_manager
            'compactor',          # Depends on signal_manager
            'thread_pool_manager', # Depends on async_task_manager
            'data_manager',       # Depends on thread_pool_manager
            'monitor',            # Depends on signal_manager and compactor
            'gui_process'         # Depends on multiple core components
        ]
        
        # Create initial groups for core components
        for component in core_order:
            if component in self._tasks and component not in self._initialized:
                task = self._tasks[component]
                
                # Set critical dependencies
                if component == 'async_task_manager':
                    task.dependencies.add('event_loop_manager')
                    task.metadata['is_critical'] = True
                elif component == 'compactor':
                    task.dependencies.add('signal_manager')
                    task.metadata['is_critical'] = True
                elif component == 'thread_pool_manager':
                    task.dependencies.add('async_task_manager')
                    task.metadata['is_critical'] = True
                elif component == 'data_manager':
                    task.dependencies.add('thread_pool_manager')
                    task.metadata['is_critical'] = True
                elif component == 'monitor':
                    task.dependencies.update({'signal_manager', 'compactor'})
                elif component == 'gui_process':
                    task.dependencies.update({
                        'signal_manager',
                        'event_loop_manager',
                        'async_task_manager',
                        'compactor',
                        'thread_pool_manager'
                    })
                    task.metadata['requires_main_thread'] = True
                
                # Set signal manager and event loop manager as critical
                if component in {'signal_manager', 'event_loop_manager'}:
                    task.metadata['is_critical'] = True
                    task.priority = 1000  # Highest priority
                    task.dependencies = set()  # No dependencies for these core components
                
                core_group = BatchInitializationGroup(
                    components=[task],
                    priority=task.priority,
                    dependencies=task.dependencies,
                    max_concurrent=1  # Initialize core components sequentially
                )
                self._batch_groups.append(core_group)
                
        # Sort remaining components by priority and dependencies
        priority_sorted = sorted(
            [(name, task) for name, task in self._tasks.items() 
             if name not in core_order],
            key=lambda x: (
                x[1].metadata.get('is_critical', False),  # Critical first
                x[1].priority,  # Then by priority
                -len(x[1].dependencies)  # Then by fewer dependencies
            ),
            reverse=True
        )
        
        current_group = BatchInitializationGroup(
            components=[],
            priority=0,
            dependencies=set(),
            max_concurrent=self._batch_size
        )
        
        for name, task in priority_sorted:
            # Special handling for components that require main thread
            if task.metadata.get('requires_main_thread', False) and current_group.components:
                # Start new group for main thread components
                if current_group.components:
                    self._batch_groups.append(current_group)
                current_group = BatchInitializationGroup(
                    components=[],
                    priority=task.priority,
                    dependencies=set(),
                    max_concurrent=1  # Single component for main thread
                )
            
            # Start new group if current is full or has conflicting dependencies
            if (len(current_group.components) >= self._batch_size or
                task.dependencies & current_group.dependencies):
                if current_group.components:
                    self._batch_groups.append(current_group)
                    current_group = BatchInitializationGroup(
                        components=[],
                        priority=task.priority,
                        dependencies=set(),
                        max_concurrent=self._batch_size
                    )
            
            current_group.components.append(task)
            current_group.dependencies.update(task.dependencies)
            current_group.priority = max(current_group.priority, task.priority)
            
        # Add final group
        if current_group.components:
            self._batch_groups.append(current_group)
            
    async def _initialize_batch_group(self, batch_group: BatchInitializationGroup) -> bool:
        """Initialize a group of components in parallel."""
        try:
            # Get current event loop
            loop = asyncio.get_running_loop()
            
            # Create initialization tasks
            tasks = []
            for task in batch_group.components:
                # Special handling for thread_pool_manager
                if task.component == 'thread_pool_manager':
                    # Ensure async_task_manager is initialized first
                    if not await self._is_component_initialized('async_task_manager'):
                        self._logger.error("async_task_manager must be initialized before thread_pool_manager")
                        continue
                        
                if not await self._can_initialize(task):
                    self._logger.error(f"Dependencies not met for {task.component}")
                    continue
                    
                # Create task in current loop
                init_task = asyncio.create_task(
                    self._initialize_component(task),
                    name=f"init_{task.component}"
                )
                tasks.append(init_task)
                
            if not tasks:
                return True  # No tasks to run is not a failure
                
            # Wait for batch completion
            results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # Process results
            success = True
            for task, result in zip(batch_group.components, results):
                if isinstance(result, Exception):
                    self._logger.error(f"Failed to initialize {task.component}: {str(result)}")
                    self._failed.add(task.component)
                    self._status[task.component] = InitializationStatus.FAILED
                    if task.metadata.get('is_critical', False):
                        success = False
                elif result is False:
                    self._logger.error(f"Component {task.component} initialization returned False")
                    self._failed.add(task.component)
                    self._status[task.component] = InitializationStatus.FAILED
                    if task.metadata.get('is_critical', False):
                        success = False
                else:
                    self._initialized.add(task.component)
                    self._status[task.component] = InitializationStatus.COMPLETED
                    
            return success
            
        except Exception as e:
            self._logger.error(f"Error initializing batch group: {str(e)}")
            return False
            
    async def _initialize_component(self, task: InitializationTask) -> bool:
        """Initialize a single component with retries and health checks."""
        if not task:
            return False
            
        try:
            # Special handling for event_loop_manager
            if task.component == "event_loop_manager":
                try:
                    # Use ensure_initialized instead of initialize
                    if await event_loop_manager.ensure_initialized():
                        self._initialized.add(task.component)
                        self._status[task.component] = InitializationStatus.COMPLETED
                        self._logger.info(f"Component {task.component} initialized successfully")
                        return True
                    else:
                        self._logger.error("Failed to ensure event loop manager initialization")
                        return False
                except Exception as e:
                    self._logger.error(f"Failed to initialize event loop manager: {str(e)}")
                    return False
                    
            # Special handling for async_task_manager and other concurrency components
            if task.component in {"async_task_manager", "thread_pool_manager", "data_manager"}:
                try:
                    # Get concurrency manager
                    concurrency_manager = self._container.get('concurrency_manager')
                    if not concurrency_manager:
                        self._logger.error("Failed to get concurrency manager")
                        return False
                        
                    # Ensure concurrency manager is initialized
                    await concurrency_manager.ensure_initialized()
                    
                    # Mark component as initialized since concurrency manager handles it
                    self._initialized.add(task.component)
                    self._status[task.component] = InitializationStatus.COMPLETED
                    self._logger.info(f"Component {task.component} initialized through concurrency manager")
                    return True
                    
                except Exception as e:
                    self._logger.error(f"Failed to initialize {task.component} through concurrency manager: {str(e)}")
                    return False
                    
            # Check dependencies
            for dep in task.dependencies:
                if not await self._is_component_initialized(dep):
                    self._logger.error(f"Dependencies not met for {task.component}")
                    return False
                    
            # Initialize with retries
            for attempt in range(task.retry_count):
                try:
                    async with asyncio.timeout(task.timeout):
                        if task.init_fn:
                            instance = await task.init_fn()
                            if instance:
                                # Register with container
                                await self._container.register(task.component, instance)
                                
                                # Mark as initialized
                                self._initialized.add(task.component)
                                self._status[task.component] = InitializationStatus.COMPLETED
                                self._logger.info(f"Component {task.component} initialized successfully")
                                return True
                                
                except asyncio.TimeoutError:
                    self._logger.warning(f"Timeout initializing {task.component} (attempt {attempt + 1})")
                except Exception as e:
                    self._logger.error(f"Error initializing {task.component}: {str(e)}")
                    
                if attempt < task.retry_count - 1:
                    await asyncio.sleep(task.retry_delay)
                    
            return False
            
        except Exception as e:
            self._logger.error(f"Failed to initialize {task.component}: {str(e)}")
            return False
        
    async def _can_initialize(self, task: InitializationTask) -> bool:
        """Check if a component can be initialized."""
        # Check if already initialized or in progress
        if (
            task.component in self._initialized
            or task.component in self._in_progress
            or task.component in self._failed
        ):
            return False
            
        # Special case for signal manager - always allow initialization
        if task.component == 'signal_manager':
            return True
            
        # Check dependencies
        for dep in task.dependencies:
            if dep not in self._initialized:
                self._logger.debug(f"Dependency {dep} not initialized for {task.component}")
                return False
                
        return True
        
    def get_initialization_status(self) -> Dict[str, Dict[str, Any]]:
        """Get detailed initialization status for all components."""
        status = {}
        for component, task in self._tasks.items():
            status[component] = {
                'status': self._status[component].value,
                'attempts': task._attempts,
                'is_critical': task.metadata.get('is_critical', False),
                'priority': task.priority,
                'dependencies': list(task.dependencies),
                'error': str(task._error) if task._error else None,
                'timing': orchestrator.get_handler_status(component)
            }
        return status
        
    async def wait_for_initialization(self, timeout: Optional[float] = None) -> bool:
        """Wait for all components to be initialized."""
        try:
            await asyncio.wait_for(self._initialized_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
            
    def is_initialized(self, component: str) -> bool:
        """Check if a component is initialized."""
        return component in self._initialized
        
    def get_component_status(self, component: str) -> Optional[InitializationStatus]:
        """Get the initialization status of a component."""
        return self._status.get(component)
        
    def get_failed_components(self) -> Set[str]:
        """Get the set of failed components."""
        return self._failed.copy()
        
    def get_initialized_components(self) -> Set[str]:
        """Get the set of successfully initialized components."""
        return self._initialized.copy()
        
    def get_in_progress_components(self) -> Set[str]:
        """Get the set of components currently being initialized."""
        return self._in_progress.copy()

# Create global instance
component_scheduler = ComponentScheduler()

# Register with container
core_container.register_sync('component_scheduler', component_scheduler)

__all__ = [
    'InitializationStatus',
    'InitializationTask',
    'ComponentScheduler',
    'component_scheduler'
] 