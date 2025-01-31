"""
Subgraph management module for the Tenire framework.

This module provides functionality to create and manage subgraphs within the main
dependency graph, allowing for isolated component management and specialized
initialization sequences.
"""
# Standard library imports
import asyncio
import weakref
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Any, Callable, DefaultDict, Tuple
from collections import defaultdict

# Local imports
from tenire.structure.dependency_graph import DependencyNode, DependencyState, DependencyGraph, DependencyLocks
from tenire.core.container import container
from tenire.utils.logger import get_logger
from tenire.organizers.concurrency.types import InitializationTask, TimingConfig

logger = get_logger(__name__)

async def _get_component_scheduler():
    """Lazy import and get component scheduler to avoid circular dependencies."""
    from tenire.organizers.scheduler import component_scheduler
    return component_scheduler

class SubgraphType(Enum):
    """Types of subgraphs that can be created."""
    
    # Core subgraphs
    CORE = "core"  # Core system components
    
    # Data subgraphs
    DATA_CORE = "data_core"  # Core data management components
    DATA_VALIDATION = "data_validation"  # Data validation components
    DATA_CLEANUP = "data_cleanup"  # Data cleanup and maintenance
    DATA_CACHING = "data_caching"  # Caching and performance components
    DATA_STORAGE = "data_storage"  # Storage and persistence
    
    # RAG subgraphs
    RAG = "data_rag"  # Retrieval and generation components
    
    # Betting subgraphs
    BETTING = "betting"  # Betting-related components
    
    # GUI subgraphs
    GUI = "gui"  # GUI-related components

    # Toolkit subgraphs
    TOOLKIT = "toolkit"  # Toolkit components and extensions

@dataclass
class SubgraphMetadata:
    """Metadata for a subgraph."""
    type: SubgraphType
    priority: int
    description: str
    tags: Set[str] = field(default_factory=set)
    is_isolated: bool = False
    auto_initialize: bool = True
    parent_type: Optional[SubgraphType] = None  # For hierarchical organization
    dependencies: Set[str] = field(default_factory=set)  # Dependencies on other subgraphs
    health_checks: Optional[Dict[str, Callable]] = field(default_factory=dict)
    timing_config: Optional[TimingConfig] = None
    cleanup_priority: int = field(default=50)
    is_critical: bool = field(default=False)

class Subgraph:
    """
    Manages a subset of the dependency graph with its own initialization sequence.
    
    Features:
    1. Isolated component management
    2. Custom initialization order
    3. Resource cleanup handling
    4. Health monitoring integration
    5. Signal system integration
    6. Enhanced error handling
    7. Resource management
    8. Weak references for memory management
    """
    
    def __init__(
        self,
        name: str,
        metadata: SubgraphMetadata,
        parent_graph: DependencyGraph
    ):
        """Initialize the subgraph."""
        self.name = name
        self.metadata = metadata
        self._parent = parent_graph
        self._nodes: Dict[str, DependencyNode] = {}
        self._node_refs: Dict[str, weakref.ref] = {}
        self._locks: DefaultDict[str, DependencyLocks] = defaultdict(DependencyLocks)
        self._initialization_order: List[str] = []
        self._initialized = set()
        self._error_nodes = set()
        self._cleanup_tasks: List[Tuple[Callable, int]] = []
        self._shutdown_event = asyncio.Event()
        
        logger.debug(f"Created subgraph: {name} ({metadata.type.value})")
        
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

    def add_node(
        self,
        name: str,
        dependencies: Optional[Set[str]] = None,
        init_fn: Optional[Callable[[], Any]] = None,
        priority: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
        health_checks: Optional[Dict[str, Callable]] = None,
        timing_config: Optional[TimingConfig] = None,
        cleanup_priority: int = 50,
        tags: Optional[Set[str]] = None,
        is_critical: bool = False
    ) -> None:
        """Add a node to the subgraph with enhanced metadata."""
        # Create node in parent graph with full metadata
        self._parent.register_node(
            name=name,
            dependencies=dependencies,
            init_fn=init_fn,
            priority=priority,
            metadata={
                **(metadata or {}),
                'subgraph': self.name,
                'subgraph_type': self.metadata.type.value
            },
            health_checks=health_checks,
            timing_config=timing_config,
            cleanup_priority=cleanup_priority,
            tags=tags or set(),
            is_critical=is_critical
        )
        
        # Track in subgraph
        self._nodes[name] = self._parent.get_node(name)
        
    async def initialize(self) -> bool:
        """Initialize all nodes in the subgraph with enhanced error handling."""
        if self._shutdown_event.is_set():
            return False
            
        try:
            success = True
            failed_nodes = []
            
            # Get initialization order from parent
            full_order = self._parent.get_initialization_order()
            
            # Filter for our nodes
            our_nodes = set(self._nodes.keys())
            our_order = [n for n in full_order if n in our_nodes]
            
            # Get component scheduler
            component_scheduler = await _get_component_scheduler()
            
            # Schedule initialization tasks
            for name in our_order:
                try:
                    async with await self._get_node_lock(name, 'init'):
                        node = self._nodes[name]
                        
                        # Create initialization task with enhanced configuration
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
                                'subgraph': self.name,
                                'subgraph_type': self.metadata.type.value,
                                'is_critical': node.is_critical,
                                'tags': list(node.tags)
                            }
                        )
                        
                        # Schedule initialization
                        await component_scheduler.schedule_component(init_task)
                        
                except Exception as e:
                    logger.error(f"Error scheduling node {name}: {str(e)}")
                    failed_nodes.append(name)
                    success = False
                    continue
                    
            # Initialize using scheduler
            if not await component_scheduler.initialize_components():
                success = False
                failed_nodes = [
                    name for name in our_order 
                    if name not in component_scheduler.get_initialization_status()['initialized']
                ]
            else:
                self._initialized.update(our_order)
                
                # Store weak references to instances
                for name in our_order:
                    node = self._nodes[name]
                    if node.instance:
                        self._node_refs[name] = weakref.ref(node.instance)
                    
            # Emit initialization status
            await self._emit_status(success, failed_nodes)
            
            return success
            
        except Exception as e:
            logger.error(f"Error initializing subgraph {self.name}: {str(e)}")
            await self._emit_status(False, list(our_nodes))
            return False
            
    async def cleanup(self) -> None:
        """Clean up subgraph resources with enhanced error handling."""
        if self._shutdown_event.is_set():
            return
            
        try:
            self._shutdown_event.set()
            logger.info(f"Starting cleanup for subgraph: {self.name}")
            
            # Clean up nodes in reverse initialization order
            for name in reversed(self._initialization_order):
                try:
                    async with await self._get_node_lock(name, 'cleanup'):
                        node = self._nodes[name]
                        if node.instance:
                            # Get instance from weak reference
                            instance = self._node_refs[name]() if name in self._node_refs else None
                            if instance and hasattr(instance, 'cleanup'):
                                node.state = DependencyState.CLEANUP
                                if asyncio.iscoroutinefunction(instance.cleanup):
                                    await instance.cleanup()
                                else:
                                    instance.cleanup()
                            
                            # Clear instance reference
                            node.instance = None
                            self._node_refs.pop(name, None)
                        
                        # Reset node state
                        node.state = DependencyState.UNREGISTERED
                        node.error = None
                        
                except Exception as e:
                    logger.error(f"Error cleaning up node {name}: {str(e)}")
                    
            # Clear collections
            self._initialized.clear()
            self._error_nodes.clear()
            self._initialization_order.clear()
            self._cleanup_tasks.clear()
            
            # Emit cleanup signal
            await self._emit_cleanup()
            
            logger.info(f"Completed cleanup for subgraph: {self.name}")
            
        except Exception as e:
            logger.error(f"Error during subgraph cleanup: {str(e)}")
            raise
        finally:
            self._shutdown_event.clear()
            
    def get_nodes(self) -> Dict[str, DependencyNode]:
        """Get all nodes in the subgraph."""
        return self._nodes.copy()
        
    def get_node(self, name: str) -> Optional[DependencyNode]:
        """Get a specific node from the subgraph."""
        return self._nodes.get(name)
        
    def get_initialization_status(self) -> Dict[str, Any]:
        """Get initialization status of the subgraph."""
        return {
            'name': self.name,
            'type': self.metadata.type.value,
            'initialized': list(self._initialized),
            'total_nodes': len(self._nodes),
            'failed_nodes': [
                name for name, node in self._nodes.items()
                if node.state == DependencyState.ERROR
            ],
            'is_critical': self.metadata.is_critical,
            'priority': self.metadata.priority,
            'dependencies': list(self.metadata.dependencies)
        }
        
    async def _emit_status(self, success: bool, failed_nodes: List[str]) -> None:
        """Emit subgraph status signal with enhanced signal manager handling."""
        try:
            # Get signal manager with retries
            retry_count = 0
            max_retries = 3
            signal_manager = None
            
            while retry_count < max_retries:
                try:
                    from tenire.servicers.signal import get_signal_manager
                    signal_manager = await get_signal_manager()
                    if signal_manager and hasattr(signal_manager, '_processing_task'):
                        break
                except Exception as e:
                    logger.debug(f"Attempt {retry_count + 1} to get signal manager: {str(e)}")
                retry_count += 1
                if retry_count < max_retries:
                    await asyncio.sleep(2 ** retry_count)
            
            if signal_manager and hasattr(signal_manager, '_processing_task'):
                # Import signal types lazily
                from tenire.core.codex import Signal, SignalType
                try:
                    await signal_manager.emit(Signal(
                        type=SignalType.RAG_SUBGRAPH_STATUS,
                        data={
                            'subgraph': self.name,
                            'type': self.metadata.type.value,
                            'success': success,
                            'failed_nodes': failed_nodes,
                            'status': self.get_initialization_status()
                        }
                    ))
                except Exception as e:
                    logger.error(f"Error emitting subgraph status signal: {str(e)}")
        except Exception as e:
            logger.error(f"Error in _emit_status: {str(e)}")
            
    async def _emit_cleanup(self) -> None:
        """Emit subgraph cleanup signal with enhanced signal manager handling."""
        try:
            # Get signal manager with retries
            retry_count = 0
            max_retries = 3
            signal_manager = None
            
            while retry_count < max_retries:
                try:
                    from tenire.servicers.signal import get_signal_manager
                    signal_manager = await get_signal_manager()
                    if signal_manager and hasattr(signal_manager, '_processing_task'):
                        break
                except Exception as e:
                    logger.debug(f"Attempt {retry_count + 1} to get signal manager: {str(e)}")
                retry_count += 1
                if retry_count < max_retries:
                    await asyncio.sleep(2 ** retry_count)
            
            if signal_manager and hasattr(signal_manager, '_processing_task'):
                # Import signal types lazily
                from tenire.core.codex import Signal, SignalType
                try:
                    await signal_manager.emit(Signal(
                        type=SignalType.RAG_CLEANUP_COMPLETED,
                        data={
                            'component': f"subgraph_{self.name}",
                            'type': self.metadata.type.value
                        }
                    ))
                except Exception as e:
                    logger.error(f"Error emitting cleanup signal: {str(e)}")
        except Exception as e:
            logger.error(f"Error in _emit_cleanup: {str(e)}")

class SubgraphManager:
    """
    Manages multiple subgraphs within the system.
    
    Features:
    1. Subgraph creation and registration
    2. Dependency tracking between subgraphs
    3. Coordinated initialization
    4. Health monitoring integration
    5. Enhanced error handling
    6. Resource management
    """
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
        
    def __init__(self):
        if not hasattr(self, '_initialized'):
            self._initialized = True
            self._subgraphs: Dict[str, Subgraph] = {}
            self._locks: DefaultDict[str, DependencyLocks] = defaultdict(DependencyLocks)
            self._cleanup_tasks: List[Tuple[Callable, int]] = []
            self._shutdown_event = asyncio.Event()
            
            # Create default subgraphs
            self._create_default_subgraphs()
            
    async def _get_subgraph_lock(self, name: str, operation: str) -> asyncio.Lock:
        """Get the appropriate granular lock for a subgraph operation."""
        locks = self._locks[name]
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

    def _create_default_subgraphs(self) -> None:
        """Create default system subgraphs with enhanced metadata."""
        from tenire.structure.dependency_graph import dependency_graph
        
        # Core components subgraph
        self.create_subgraph(
            "core",
            SubgraphMetadata(
                type=SubgraphType.CORE,
                priority=100,
                description="Core system components",
                auto_initialize=True,
                is_critical=True,
                tags={'core', 'system'},
                health_checks={'system_health': self._check_core_health}
            ),
            dependency_graph
        )
        
        # Data management subgraphs
        data_core = self.create_subgraph(
            "data_core",
            SubgraphMetadata(
                type=SubgraphType.DATA_CORE,
                priority=90,
                description="Core data management components",
                tags={"data", "core"},
                dependencies={"core"},
                is_critical=True,
                health_checks={'data_health': self._check_data_health}
            ),
            dependency_graph
        )
        
        self.create_subgraph(
            "data_validation",
            SubgraphMetadata(
                type=SubgraphType.DATA_VALIDATION,
                priority=85,
                description="Data validation components",
                tags={"data", "validation"},
                parent_type=SubgraphType.DATA_CORE,
                dependencies={"data_core"},
                health_checks={'validation_health': self._check_validation_health}
            ),
            dependency_graph
        )
        
        self.create_subgraph(
            "data_cleanup",
            SubgraphMetadata(
                type=SubgraphType.DATA_CLEANUP,
                priority=85,
                description="Data cleanup and maintenance components",
                tags={"data", "cleanup"},
                parent_type=SubgraphType.DATA_CORE,
                dependencies={"data_core"},
                health_checks={'cleanup_health': self._check_cleanup_health}
            ),
            dependency_graph
        )
        
        self.create_subgraph(
            "data_caching",
            SubgraphMetadata(
                type=SubgraphType.DATA_CACHING,
                priority=85,
                description="Data caching and performance components",
                tags={"data", "caching"},
                parent_type=SubgraphType.DATA_CORE,
                dependencies={"data_core"},
                health_checks={'cache_health': self._check_cache_health}
            ),
            dependency_graph
        )
        
        self.create_subgraph(
            "data_storage",
            SubgraphMetadata(
                type=SubgraphType.DATA_STORAGE,
                priority=85,
                description="Data storage and persistence components",
                tags={"data", "storage"},
                parent_type=SubgraphType.DATA_CORE,
                dependencies={"data_core"},
                health_checks={'storage_health': self._check_storage_health}
            ),
            dependency_graph
        )
        
        # RAG components subgraph
        self.create_subgraph(
            "data_rag",
            SubgraphMetadata(
                type=SubgraphType.RAG,
                priority=80,
                description="Retrieval and generation components",
                tags={"rag", "ml", "data"},
                dependencies={"data_core"},
                health_checks={'rag_health': self._check_rag_health}
            ),
            dependency_graph
        )
        
        # Betting components subgraph
        self.create_subgraph(
            "betting",
            SubgraphMetadata(
                type=SubgraphType.BETTING,
                priority=70,
                description="Betting-related components",
                tags={"betting", "actions"},
                dependencies={"core", "data_core"},
                health_checks={'betting_health': self._check_betting_health}
            ),
            dependency_graph
        )
        
        # GUI components subgraph
        self.create_subgraph(
            "gui",
            SubgraphMetadata(
                type=SubgraphType.GUI,
                priority=60,
                description="GUI-related components",
                tags={"gui", "frontend"},
                dependencies={"core"},
                health_checks={'gui_health': self._check_gui_health}
            ),
            dependency_graph
        )
        
    def create_subgraph(
        self,
        name: str,
        metadata: SubgraphMetadata,
        parent_graph: DependencyGraph
    ) -> Subgraph:
        """Create a new subgraph."""
        if name in self._subgraphs:
            return self._subgraphs[name]
            
        subgraph = Subgraph(name, metadata, parent_graph)
        self._subgraphs[name] = subgraph
        return subgraph
        
    def get_subgraph(self, name: str) -> Optional[Subgraph]:
        """Get a subgraph by name."""
        return self._subgraphs.get(name)
        
    async def initialize_subgraphs(self) -> bool:
        """Initialize all subgraphs in priority order with enhanced error handling."""
        if self._shutdown_event.is_set():
            return False
            
        try:
            success = True
            
            # Sort by priority and dependencies
            ordered_subgraphs = self._get_initialization_order()
            
            # Initialize each subgraph
            for subgraph in ordered_subgraphs:
                try:
                    async with await self._get_subgraph_lock(subgraph.name, 'init'):
                        if subgraph.metadata.auto_initialize:
                            if not await subgraph.initialize():
                                success = False
                                if subgraph.metadata.type == SubgraphType.CORE:
                                    # Core initialization failure is fatal
                                    logger.error("Core subgraph initialization failed")
                                    return False
                                elif subgraph.metadata.is_critical:
                                    # Critical subgraph failure is fatal
                                    logger.error(f"Critical subgraph {subgraph.name} initialization failed")
                                    return False
                except Exception as e:
                    logger.error(f"Error initializing subgraph {subgraph.name}: {str(e)}")
                    success = False
                    
            return success
            
        except Exception as e:
            logger.error(f"Error during subgraph initialization: {str(e)}")
            return False
            
    def _get_initialization_order(self) -> List[Subgraph]:
        """Get subgraphs ordered by priority and dependencies."""
        ordered = []
        visited = set()
        
        def visit(subgraph: Subgraph):
            if subgraph.name in visited:
                return
            visited.add(subgraph.name)
            
            # Visit dependencies first
            for dep_name in subgraph.metadata.dependencies:
                dep = self._subgraphs.get(dep_name)
                if dep:
                    visit(dep)
                
            ordered.append(subgraph)
        
        # Sort by priority first
        priority_sorted = sorted(
            self._subgraphs.values(),
            key=lambda x: x.metadata.priority,
            reverse=True
        )
        
        # Visit each subgraph
        for subgraph in priority_sorted:
            visit(subgraph)
        
        return ordered
        
    async def cleanup_subgraphs(self) -> None:
        """Clean up all subgraphs with enhanced error handling."""
        if self._shutdown_event.is_set():
            return
            
        try:
            self._shutdown_event.set()
            logger.info("Starting subgraph cleanup")
            
            # Clean up in reverse priority order
            ordered_subgraphs = sorted(
                self._subgraphs.values(),
                key=lambda x: x.metadata.priority
            )
            
            for subgraph in ordered_subgraphs:
                try:
                    async with await self._get_subgraph_lock(subgraph.name, 'cleanup'):
                        await subgraph.cleanup()
                except Exception as e:
                    logger.error(f"Error cleaning up subgraph {subgraph.name}: {str(e)}")
                    
            # Clear cleanup tasks
            self._cleanup_tasks.clear()
            
            logger.info("Subgraph cleanup completed")
            
        except Exception as e:
            logger.error(f"Error during subgraph cleanup: {str(e)}")
            raise
        finally:
            self._shutdown_event.clear()
            
    def get_initialization_status(self) -> Dict[str, Any]:
        """Get initialization status of all subgraphs."""
        return {
            name: subgraph.get_initialization_status()
            for name, subgraph in self._subgraphs.items()
        }
        
    async def _check_core_health(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Check health of core subgraph."""
        subgraph = self.get_subgraph("core")
        if not subgraph:
            return False, "Core subgraph not found", {}
        return True, "Core subgraph healthy", subgraph.get_initialization_status()
        
    async def _check_data_health(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Check health of data subgraphs."""
        data_subgraphs = [
            sg for sg in self._subgraphs.values()
            if sg.metadata.type in {
                SubgraphType.DATA_CORE,
                SubgraphType.DATA_VALIDATION,
                SubgraphType.DATA_CLEANUP,
                SubgraphType.DATA_CACHING,
                SubgraphType.DATA_STORAGE
            }
        ]
        status = {sg.name: sg.get_initialization_status() for sg in data_subgraphs}
        return True, "Data subgraphs healthy", status
        
    async def _check_validation_health(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Check health of validation subgraph."""
        subgraph = self.get_subgraph("data_validation")
        if not subgraph:
            return False, "Validation subgraph not found", {}
        return True, "Validation subgraph healthy", subgraph.get_initialization_status()
        
    async def _check_cleanup_health(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Check health of cleanup subgraph."""
        subgraph = self.get_subgraph("data_cleanup")
        if not subgraph:
            return False, "Cleanup subgraph not found", {}
        return True, "Cleanup subgraph healthy", subgraph.get_initialization_status()
        
    async def _check_cache_health(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Check health of cache subgraph."""
        subgraph = self.get_subgraph("data_caching")
        if not subgraph:
            return False, "Cache subgraph not found", {}
        return True, "Cache subgraph healthy", subgraph.get_initialization_status()
        
    async def _check_storage_health(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Check health of storage subgraph."""
        subgraph = self.get_subgraph("data_storage")
        if not subgraph:
            return False, "Storage subgraph not found", {}
        return True, "Storage subgraph healthy", subgraph.get_initialization_status()
        
    async def _check_rag_health(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Check health of RAG subgraph."""
        subgraph = self.get_subgraph("data_rag")
        if not subgraph:
            return False, "RAG subgraph not found", {}
        return True, "RAG subgraph healthy", subgraph.get_initialization_status()
        
    async def _check_betting_health(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Check health of betting subgraph."""
        subgraph = self.get_subgraph("betting")
        if not subgraph:
            return False, "Betting subgraph not found", {}
        return True, "Betting subgraph healthy", subgraph.get_initialization_status()
        
    async def _check_gui_health(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Check health of GUI subgraph."""
        subgraph = self.get_subgraph("gui")
        if not subgraph:
            return False, "GUI subgraph not found", {}
        return True, "GUI subgraph healthy", subgraph.get_initialization_status()

# Create global instance
subgraph_manager = SubgraphManager()

def register_subgraph_manager():
    """Register subgraph manager with container after imports are resolved."""
    from tenire.core.container import container
    container.register_sync('subgraph_manager', subgraph_manager)

# Register lazily to avoid circular imports
import atexit
atexit.register(register_subgraph_manager)

# Export for module access
__all__ = [
    'SubgraphType',
    'SubgraphMetadata',
    'Subgraph',
    'SubgraphManager',
    'subgraph_manager'
] 