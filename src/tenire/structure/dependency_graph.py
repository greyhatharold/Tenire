"""
Dependency graph management module for the Tenire framework.

This module provides a robust dependency graph implementation that integrates
with the container system, signal manager, and compactor to manage component
lifecycles and dependencies.
"""

import asyncio
import enum
import weakref
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Any, Callable, Tuple, DefaultDict
from collections import defaultdict

from tenire.utils.logger import get_logger
from tenire.core.codex import TimingConfig

logger = get_logger(__name__)

async def _get_event_loop_manager():
    """Lazy import and get event loop manager to avoid circular dependencies."""
    from tenire.core.event_loop import event_loop_manager
    return event_loop_manager

async def _get_container():
    """Lazy import and get container to avoid circular dependencies."""
    from tenire.core.container import container
    return container

@dataclass
class InitializationTask:
    """Task for initializing a component."""
    component: str
    dependencies: Set[str]
    priority: int
    init_fn: Optional[Callable[[], Any]]
    timeout: float = 30.0
    retry_count: int = 3
    retry_delay: float = 1.0
    health_check: Optional[Callable[[], bool]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

class DependencyError(Exception):
    """Exception raised for dependency-related errors."""
    pass

class DependencyState(enum.Enum):
    """Enumeration of possible dependency states."""
    UNREGISTERED = "unregistered"
    REGISTERED = "registered"
    INITIALIZING = "initializing"
    INITIALIZED = "initialized"
    ERROR = "error"
    CYCLIC = "cyclic"
    CLEANUP = "cleanup"  # New state for cleanup phase

@dataclass
class DependencyNode:
    """Node in the dependency graph representing a component."""
    name: str
    dependencies: Set[str] = field(default_factory=set)
    dependents: Set[str] = field(default_factory=set)
    state: DependencyState = field(default=DependencyState.UNREGISTERED)
    error: Optional[Exception] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    init_order: int = field(default=-1)
    init_fn: Optional[Callable[[], Any]] = None
    instance: Optional[Any] = None
    is_core: bool = field(default=False)
    priority: int = field(default=0)
    health_checks: Optional[Dict[str, Callable]] = field(default_factory=dict)
    timing_config: Optional[TimingConfig] = None
    cleanup_priority: int = field(default=50)
    tags: Set[str] = field(default_factory=set)
    is_critical: bool = field(default=False)

@dataclass
class DependencyLocks:
    """Granular locks for dependency operations."""
    init_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    health_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timing_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

class DependencyGraph:
    """
    Central manager for all dependency graph operations with improved task management.
    """
    
    _instance = None  # Class variable to hold singleton instance
    
    @classmethod
    def get_instance(cls) -> 'DependencyGraph':
        """Get or create the singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    def __init__(self):
        """Initialize the dependency graph manager."""
        if hasattr(self, '_initialized'):
            return
            
        self._nodes: Dict[str, DependencyNode] = {}
        self._node_refs: Dict[str, weakref.ref] = {}
        self._locks: DefaultDict[str, DependencyLocks] = defaultdict(DependencyLocks)
        self._initialization_order: List[str] = []
        self._initialized = set()
        self._initializing = set()
        self._error_nodes = set()
        self._cyclic_nodes = set()
        self._cleanup_tasks: List[Tuple[Callable, int]] = []
        self._shutdown_event = asyncio.Event()
        self._global_init_lock = asyncio.Lock()
        self._health_coordinator = None
        self._signal_manager = None
        self._orchestrator = None
        
        # Core component definitions with enhanced metadata
        self._core_components = {
            'signal_manager': {
                'dependencies': set(),  # No dependencies for signal manager
                'priority': 1000,  # Highest priority
                'is_critical': True,
                'tags': {'core', 'messaging', 'critical'}
            },
            'event_loop_manager': {
                'dependencies': set(),  # No dependencies
                'priority': 950,  # Very high priority
                'is_critical': True,
                'tags': {'core', 'event_loop', 'critical'}
            },
            'async_task_manager': {
                'dependencies': {'event_loop_manager'},  # Only depends on event_loop_manager
                'priority': 900,  # High priority after event_loop_manager
                'is_critical': True,
                'tags': {'core', 'async', 'critical'}
            },
            'compactor': {
                'dependencies': {'signal_manager', 'async_task_manager'},  # Updated dependencies
                'priority': 95,
                'is_critical': True,
                'tags': {'core', 'cleanup'}
            },
            'concurrency_manager': {
                'dependencies': {'signal_manager', 'async_task_manager'},  # Updated dependencies
                'priority': 90,
                'is_critical': True,
                'tags': {'core', 'concurrency'}
            },
            'data_manager': {
                'dependencies': {'signal_manager', 'concurrency_manager'},
                'priority': 85,
                'is_critical': True,
                'tags': {'core', 'data'}
            },
            'agent_manager': {
                'dependencies': {'concurrency_manager', 'data_manager'},
                'priority': 80,
                'is_critical': False,
                'tags': {'core', 'agent'}
            },
            'browser_integration': {
                'dependencies': {'concurrency_manager'},
                'priority': 75,
                'is_critical': False,
                'tags': {'core', 'browser'}
            }
        }
        
        # Register core components
        for name, info in self._core_components.items():
            self.register_node(
                name=name,
                dependencies=info['dependencies'],
                is_core=True,
                priority=info['priority'],
                metadata={
                    'tags': info['tags'],
                    'is_critical': info['is_critical']
                }
            )
            
        logger.debug("Initialized DependencyGraph")

    async def _ensure_health_coordinator(self) -> None:
        """Ensure health coordinator is initialized lazily when needed."""
        if self._health_coordinator is None:
            try:
                # Use asyncio.shield to prevent cancellation during critical initialization
                async def init_coordinator():
                    from tenire.public_services.hospital import get_health_coordinator
                    self._health_coordinator = await get_health_coordinator()
                    if self._health_coordinator:
                        logger.debug("Health coordinator initialized lazily")
                    else:
                        logger.warning("Health coordinator initialization returned None")
                
                # Shield the initialization to prevent cancellation
                await asyncio.shield(asyncio.wait_for(init_coordinator(), timeout=5.0))
            except asyncio.TimeoutError:
                logger.warning("Health coordinator initialization timed out, will retry later")
                self._health_coordinator = None
            except Exception as e:
                logger.warning(f"Failed to initialize health coordinator: {str(e)}")
                self._health_coordinator = None

    async def register_health_check(self, component: str, check_fn: Callable, **kwargs) -> None:
        """Register a health check for a component."""
        try:
            # Use asyncio.shield to prevent cancellation during critical registration
            async with asyncio.timeout(5.0):  # 5 second timeout
                await asyncio.shield(self._ensure_health_coordinator())
                if self._health_coordinator:
                    await asyncio.shield(
                        self._health_coordinator.registry.register_check(
                            component=component,
                            check_fn=check_fn,
                            **kwargs
                        )
                    )
        except asyncio.TimeoutError:
            logger.warning(f"Health check registration timed out for {component}")
        except Exception as e:
            logger.warning(f"Failed to register health check for {component}: {str(e)}")

    def _calculate_initialization_order(self) -> None:
        """
        Calculate the initialization order based on dependencies and priorities.
        Ensures signal manager is always first.
        """
        self._initialization_order = []
        self._cyclic_nodes = set()
        visited = set()
        temp_visited = set()
        
        def visit(name: str) -> None:
            """DFS visit with cycle detection."""
            if name in self._cyclic_nodes:
                return
            if name in temp_visited:
                # Cycle detected - but don't mark as cyclic if it's signal manager
                if name != 'signal_manager':
                    cycle = [name]
                    self._cyclic_nodes.update(cycle)
                    for node_name in cycle:
                        if node_name in self._nodes:
                            self._nodes[node_name].state = DependencyState.CYCLIC
                return
            if name in visited:
                return
                
            temp_visited.add(name)
            
            # Get dependencies sorted by priority
            if name in self._nodes:
                deps = sorted(
                    self._nodes[name].dependencies,
                    key=lambda x: self._nodes[x].priority if x in self._nodes else 0,
                    reverse=True
                )
                
                # Visit dependencies
                for dep in deps:
                    visit(dep)
                    
            visited.add(name)
            temp_visited.remove(name)
            self._initialization_order.append(name)
            
        # Visit signal manager first if present
        if 'signal_manager' in self._nodes and 'signal_manager' not in visited:
            visit('signal_manager')
            
        # Then visit remaining core components in priority order
        core_components = sorted(
            [n for n in self._core_components.keys() if n != 'signal_manager'],
            key=lambda x: self._core_components[x]['priority'],
            reverse=True
        )
        
        for name in core_components:
            if name not in visited:
                visit(name)
                
        # Then visit remaining nodes in priority order
        remaining = sorted(
            [n for n in self._nodes.keys() if n not in self._core_components],
            key=lambda x: self._nodes[x].priority,
            reverse=True
        )
        
        for name in remaining:
            if name not in visited:
                visit(name)
                
        logger.debug(f"Calculated initialization order: {self._initialization_order}")
        if self._cyclic_nodes:
            logger.warning(f"Detected cycles in dependencies: {self._cyclic_nodes}")

    async def _get_node_lock(self, node: str, operation: str) -> asyncio.Lock:
        """Get the appropriate granular lock for a node operation."""
        locks = self._locks[node]
        if operation == 'init':
            return locks.init_lock
        elif operation == 'cleanup':
            return locks.cleanup_lock
        elif operation == 'health':
            return locks.health_lock
        elif operation == 'timing':
            return locks.timing_lock
        else:
            raise ValueError(f"Unknown operation type: {operation}")

    def register_node(
        self,
        name: str,
        dependencies: Optional[Set[str]] = None,
        init_fn: Optional[Callable[[], Any]] = None,
        is_core: bool = False,
        priority: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
        health_checks: Optional[Dict[str, Callable]] = None,
        timing_config: Optional[TimingConfig] = None,
        cleanup_priority: int = 50,
        tags: Optional[Set[str]] = None,
        is_critical: bool = False
    ) -> None:
        """Register a node with improved task handling."""
        try:
            logger.debug(f"Registering node: {name}")
            if name in self._nodes:
                node = self._nodes[name]
                # Update existing node
                if dependencies is not None:
                    node.dependencies.update(dependencies)
                if init_fn is not None:
                    node.init_fn = init_fn
                node.is_core = is_core or node.is_core
                node.priority = max(priority, node.priority)
                if metadata:
                    node.metadata.update(metadata)
                if health_checks:
                    # Store health checks for async registration
                    node.health_checks.update(health_checks)
                    # Schedule health check registration for later
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # Create task with proper error handling and task tracking
                        async def register_health_checks():
                            try:
                                await self._register_node_health_checks(name, health_checks, is_critical)
                            except Exception as e:
                                logger.error(f"Error registering health checks for {name}: {str(e)}")
                        task = loop.create_task(register_health_checks())
                        # Store task reference to prevent premature cleanup
                        if not hasattr(self, '_pending_tasks'):
                            self._pending_tasks = set()
                        self._pending_tasks.add(task)
                        task.add_done_callback(self._pending_tasks.discard)
                if timing_config:
                    # Ensure orchestrator is available before registering timing config
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        task = loop.create_task(self._register_timing_config(name, timing_config))
                        if not hasattr(self, '_pending_tasks'):
                            self._pending_tasks = set()
                        self._pending_tasks.add(task)
                        task.add_done_callback(self._pending_tasks.discard)
                    node.timing_config = timing_config
                node.cleanup_priority = max(cleanup_priority, node.cleanup_priority)
                if tags:
                    node.tags.update(tags)
                node.is_critical = is_critical or node.is_critical
                logger.debug(f"Updated existing node: {name}")
            else:
                # Create new node
                node = DependencyNode(
                    name=name,
                    dependencies=dependencies or set(),
                    state=DependencyState.REGISTERED,
                    init_fn=init_fn,
                    is_core=is_core,
                    priority=priority,
                    metadata=metadata or {},
                    health_checks=health_checks or {},
                    timing_config=timing_config,
                    cleanup_priority=cleanup_priority,
                    tags=tags or set(),
                    is_critical=is_critical
                )
                self._nodes[name] = node
                
                # Schedule health check registration if needed
                if health_checks:
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # Create task with proper error handling and task tracking
                        async def register_health_checks():
                            try:
                                await self._register_node_health_checks(name, health_checks, is_critical)
                            except Exception as e:
                                logger.error(f"Error registering health checks for {name}: {str(e)}")
                        task = loop.create_task(register_health_checks())
                        if not hasattr(self, '_pending_tasks'):
                            self._pending_tasks = set()
                        self._pending_tasks.add(task)
                        task.add_done_callback(self._pending_tasks.discard)
                
                # Register with orchestrator if available
                if timing_config and self._orchestrator:
                    try:
                        self._orchestrator.register_handler_sync(name, timing_config)
                    except Exception as e:
                        logger.warning(f"Failed to register timing config for {name}: {str(e)}")
                
                logger.debug(f"Created new node: {name}")
            
            # Update dependencies
            for dep_name in node.dependencies:
                if dep_name in self._nodes:
                    self._nodes[dep_name].dependents.add(name)
                    logger.debug(f"Added dependency {dep_name} -> {name}")
                
            # Recalculate initialization order
            self._calculate_initialization_order()
            logger.debug(f"Updated initialization order after registering {name}")
            
        except Exception as e:
            logger.error(f"Error registering node {name}: {str(e)}")
            raise

    async def _register_node_health_checks(self, name: str, health_checks: Dict[str, Callable], is_critical: bool) -> None:
        """Register health checks for a node asynchronously with proper task handling."""
        try:
            # Shield the health check registration to prevent cancellation
            async with asyncio.timeout(5.0):  # 5 second timeout
                await asyncio.shield(self._ensure_health_coordinator())
                if self._health_coordinator:
                    for check_name, check_fn in health_checks.items():
                        try:
                            await asyncio.shield(
                                self.register_health_check(
                                    component=name,
                                    check_fn=check_fn,
                                    name=check_name,
                                    is_critical=is_critical
                                )
                            )
                        except Exception as e:
                            logger.warning(f"Failed to register health check {check_name} for {name}: {str(e)}")
                            continue
                    logger.debug(f"Registered health checks for {name}")
        except asyncio.TimeoutError:
            logger.warning(f"Health check registration timed out for {name}")
        except Exception as e:
            logger.warning(f"Failed to register health checks for {name}: {str(e)}")

    def get_node(self, name: str) -> Optional[DependencyNode]:
        """
        Get a node from the dependency graph by name.
        
        Args:
            name: The name of the node to retrieve
            
        Returns:
            The node if found, None otherwise
        """
        return self._nodes.get(name)

    async def initialize_all(self) -> Tuple[bool, List[str]]:
        """Initialize all nodes in the dependency graph in the correct order."""
        async with self._global_init_lock:
            if self._shutdown_event.is_set():
                return False, []
                
            success = True
            failed_nodes = []
            
            try:
                # Initialize core components first in strict order
                core_order = sorted(
                    self._core_components.keys(),
                    key=lambda x: self._core_components[x]['priority'],
                    reverse=True
                )
                
                for name in core_order:
                    if name not in self._initialized:
                        if not await self.initialize_node(name):
                            failed_nodes.append(name)
                            if self._core_components[name]['is_critical']:
                                logger.error(f"Critical core component {name} failed to initialize")
                                return False, failed_nodes
                
                # Then initialize remaining nodes in dependency order
                remaining = [n for n in self._initialization_order if n not in core_order]
                
                for name in remaining:
                    if name not in self._initialized:
                        if not await self.initialize_node(name):
                            failed_nodes.append(name)
                            node = self._nodes[name]
                            if node.is_critical:
                                success = False
                                logger.error(f"Critical node {name} failed to initialize")
                                break
                
                return success, failed_nodes
                
            except Exception as e:
                logger.error(f"Error during full initialization: {str(e)}")
                return False, self._initialization_order

    async def initialize_node(self, name: str) -> bool:
        """Initialize a node and its dependencies with enhanced error handling."""
        if name not in self._nodes:
            logger.error(f"Node not found: {name}")
            return False
            
        node = self._nodes[name]
        
        # Check if already initialized
        if node.state == DependencyState.INITIALIZED:
            return True
            
        # Use node-specific lock for initialization
        async with await self._get_node_lock(name, 'init'):
            # Recheck state after acquiring lock
            if node.state == DependencyState.INITIALIZED:
                return True
                
            # Check for initialization in progress
            if node.state == DependencyState.INITIALIZING:
                logger.warning(f"Circular initialization detected for node: {name}")
                return False
                
            # Initialize dependencies first
            for dep_name in node.dependencies:
                if dep_name not in self._initialized:
                    if not await self.initialize_node(dep_name):
                        logger.error(f"Failed to initialize dependency {dep_name} for {name}")
                        return False
            
            # Mark as initializing
            node.state = DependencyState.INITIALIZING
            self._initializing.add(name)
            
            try:
                # Get event loop manager using lazy import
                event_loop_manager = await _get_event_loop_manager()
                
                # Create initialization task
                init_task = InitializationTask(
                    component=name,
                    dependencies=node.dependencies,
                    priority=node.priority,
                    init_fn=node.init_fn,
                    timeout=30.0 if not node.is_critical else 60.0,
                    retry_count=3 if not node.is_critical else 5,
                    retry_delay=1.0,
                    health_check=node.health_checks.get('health_check'),
                    metadata={
                        'is_core': node.is_core,
                        'is_critical': node.is_critical,
                        'tags': list(node.tags)
                    }
                )
                
                # Track task using lazy-loaded event loop manager
                task = asyncio.create_task(self._initialize_task(init_task))
                event_loop_manager.track_task(task)
                
                try:
                    await asyncio.wait_for(task, timeout=init_task.timeout)
                    return True
                except asyncio.TimeoutError:
                    logger.error(f"Initialization timed out for {name}")
                    return False
                    
            except Exception as e:
                logger.error(f"Error initializing node {name}: {str(e)}")
                return False

    async def cleanup(self) -> None:
        """Cleanup with proper task handling."""
        try:
            # Wait for any pending tasks to complete before cleanup
            if hasattr(self, '_pending_tasks') and self._pending_tasks:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*self._pending_tasks, return_exceptions=True),
                        timeout=5.0
                    )
                except asyncio.TimeoutError:
                    logger.warning("Some pending tasks did not complete during cleanup")
                except Exception as e:
                    logger.warning(f"Error waiting for pending tasks during cleanup: {str(e)}")
                
            # Clear pending tasks
            if hasattr(self, '_pending_tasks'):
                self._pending_tasks.clear()
                
            # Proceed with normal cleanup
            for task, priority in sorted(self._cleanup_tasks, key=lambda x: x[1]):
                try:
                    await asyncio.shield(task())
                except Exception as e:
                    logger.error(f"Error during cleanup task: {str(e)}")
                    
        except Exception as e:
            logger.error(f"Error during dependency graph cleanup: {str(e)}")
            raise

    async def _emit_node_signal(self, name: str, status: str, data: Dict[str, Any]) -> None:
        """Emit a signal for node status changes."""
        try:
            signal_manager = await _get_container().get('signal_manager')
            if signal_manager:
                from tenire.core.codex import Signal, SignalType
                signal_type = getattr(SignalType, f"RAG_COMPONENT_{status.upper()}")
                await signal_manager.emit(Signal(
                    type=signal_type,
                    data={
                        'component': name,
                        'status': status,
                        **data
                    }
                ))
        except Exception as e:
            logger.error(f"Error emitting node signal: {str(e)}")

    async def _emit_cleanup_signal(self, status: str, data: Optional[Dict[str, Any]] = None) -> None:
        """Emit a cleanup status signal."""
        try:
            signal_manager = await _get_container().get('signal_manager')
            if signal_manager:
                from tenire.core.codex import Signal, SignalType
                signal_type = getattr(SignalType, f"RAG_CLEANUP_{status.upper()}")
                await signal_manager.emit(Signal(
                    type=signal_type,
                    data={
                        'component': 'dependency_graph',
                        'status': status,
                        **(data or {})
                    }
                ))
        except Exception as e:
            logger.error(f"Error emitting cleanup signal: {str(e)}")

    async def _initialize_non_core_components(self) -> None:
        """Initialize non-core components in the background."""
        try:
            # Get non-core components
            non_core = [name for name in self._nodes.keys() if name not in self._core_components]
            
            # Initialize in dependency order
            for name in non_core:
                try:
                    node = self._nodes[name]
                    if node.init_fn and name not in self._initialized:
                        logger.debug(f"Initializing non-core component: {name}")
                        instance = await node.init_fn()
                        if instance:
                            node.instance = instance
                            node.state = DependencyState.INITIALIZED
                            self._initialized.add(name)
                except Exception as e:
                    logger.warning(f"Failed to initialize non-core component {name}: {str(e)}")
                
        except Exception as e:
            logger.error(f"Error in background component initialization: {str(e)}")

    async def _ensure_orchestrator(self) -> None:
        """Ensure orchestrator is initialized lazily when needed."""
        if self._orchestrator is None:
            try:
                from tenire.core.container import container
                self._orchestrator = container.get('orchestrator')
                if self._orchestrator is None:
                    # If not in container, try direct import
                    from tenire.organizers.scheduler import orchestrator
                    self._orchestrator = orchestrator
                logger.debug("Orchestrator reference initialized lazily")
            except Exception as e:
                logger.warning(f"Failed to initialize orchestrator reference: {str(e)}")

    async def _register_timing_config(self, name: str, timing_config: TimingConfig) -> None:
        """Register timing configuration for a node with the orchestrator."""
        try:
            await self._ensure_orchestrator()
            if self._orchestrator:
                await self._orchestrator.register_handler(name, timing_config)
                logger.debug(f"Registered timing config for {name}")
        except Exception as e:
            logger.warning(f"Failed to register timing config for {name}: {str(e)}")

    async def _initialize_core_components(self) -> None:
        """Initialize core components with proper task handling."""
        try:
            # Initialize critical core components in sequence
            critical_components = {name: info for name, info in self._core_components.items() 
                                 if info.get('is_critical', False)}
            
            # Initialize non-critical components in parallel
            non_critical = {name: info for name, info in self._core_components.items() 
                           if not info.get('is_critical', False)}
            
            # Initialize critical components first
            for name, info in critical_components.items():
                logger.info(f"Initializing critical core component: {name}")
                try:
                    async with asyncio.timeout(20):  # Reduced timeout
                        node = self.get_node(name)
                        if node and node.init_fn:
                            instance = await node.init_fn()
                            if instance:
                                node.instance = instance
                                node.state = DependencyState.INITIALIZED
                                self._initialized.add(name)
                                logger.info(f"Initialized critical core component: {name}")
                except asyncio.TimeoutError:
                    logger.error(f"Timeout initializing critical core component: {name}")
                    raise
                except Exception as e:
                    logger.error(f"Failed to initialize critical core component {name}: {str(e)}")
                    raise
                
            # Initialize non-critical components in parallel
            tasks = []
            for name, info in non_critical.items():
                tasks.append(asyncio.create_task(self._initialize_single_component(name, info)))
                
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            
            logger.info("Core component initialization completed")
            
        except Exception as e:
            logger.error(f"Error during core component initialization: {str(e)}")
            raise

    async def _initialize_single_component(self, name: str, info: dict) -> None:
        """Initialize a single non-critical component."""
        try:
            async with asyncio.timeout(10):  # Shorter timeout for non-critical components
                node = self.get_node(name)
                if node and node.init_fn:
                    try:
                        instance = await node.init_fn()
                        if instance:
                            node.instance = instance
                            node.state = DependencyState.INITIALIZED
                            self._initialized.add(name)
                            logger.info(f"Initialized non-critical core component: {name}")
                    except Exception as e:
                        logger.warning(f"Failed to initialize non-critical component {name}: {str(e)}")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout initializing non-critical component: {name}")

# Create global instance for module-level access
dependency_graph = DependencyGraph.get_instance()

async def initialize_dependency_graph() -> DependencyGraph:
    """Initialize the dependency graph."""
    try:
        logger.info("Starting dependency graph initialization")
        
        # Get container and event loop manager using lazy imports
        container = await _get_container()
        event_loop_manager = await _get_event_loop_manager()
        
        # Get the singleton instance
        global dependency_graph
        dependency_graph = DependencyGraph.get_instance()
        
        # Register with container using proper task handling
        loop = asyncio.get_event_loop()
        register_task = loop.create_task(
            container.register(
                "dependency_graph",
                dependency_graph,
                dependencies=set()
            )
        )
        
        # Track the task and await it properly
        await event_loop_manager.track_task(register_task)
        
        # Wait for registration with timeout
        try:
            await asyncio.wait_for(register_task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.error("Dependency graph registration timed out")
            raise
            
        logger.info("Registered dependency graph with container")
        
        # Initialize core components with proper task handling
        init_core_task = loop.create_task(
            dependency_graph._initialize_core_components()
        )
        await event_loop_manager.track_task(init_core_task)
        
        try:
            await asyncio.wait_for(init_core_task, timeout=10.0)
        except asyncio.TimeoutError:
            logger.error("Core component initialization timed out")
            raise
            
        logger.info("Core component initialization completed")
        
        # Initialize non-core components with proper task handling
        init_non_core_task = loop.create_task(
            dependency_graph._initialize_non_core_components()
        )
        await event_loop_manager.track_task(init_non_core_task)
        
        try:
            await asyncio.wait_for(init_non_core_task, timeout=10.0)
        except asyncio.TimeoutError:
            logger.error("Non-core component initialization timed out")
            raise
            
        logger.info("Dependency graph initialization completed")
        return dependency_graph
        
    except Exception as e:
        logger.error(f"Error during dependency graph initialization: {str(e)}")
        raise

# Export for module access
__all__ = [
    'DependencyGraph',
    'DependencyNode',
    'DependencyState',
    'DependencyLocks',
    'initialize_dependency_graph',
    'dependency_graph'
] 