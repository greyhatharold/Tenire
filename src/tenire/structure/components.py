"""
Pipeline component registry for the Tenire framework.

This module handles registration of all system pipelines and components,
managing dependencies, initialization order, and lifecycle management. It deeply integrates with the scheduler and compactor for coordinated
timing and cleanup.
"""

# Standard library imports
import asyncio
from asyncio import Lock
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (
    Any, Callable, DefaultDict, Dict, List, Optional,
    Set, Tuple, TypeVar
)
import weakref

# Third-party imports
import numpy as np

# Core imports
from tenire.core.codex import (
    Signal, SignalType, HealthStatus
)
from tenire.core.config import ConfigManager, config_manager
from tenire.core.container import container

# Data imports
from tenire.data.cacher import (
    TieredCache, CachePolicy, cache_decorator
)

# GUI imports
from tenire.gui import GUIProcess, SidebarGUI, SidebarHandler

# Integration imports
from tenire.integrations.browser import BrowserIntegration

# Organizer imports
from tenire.organizers.concurrency import concurrency_manager
from tenire.organizers.concurrency.async_task_manager import AsyncTaskManager
from tenire.organizers.concurrency.concurrency_manager import ConcurrencyManager
from tenire.structure.component_scheduler import component_scheduler
from tenire.organizers.concurrency.monitor import (
    monitor, MonitorMetrics
)
from tenire.organizers.scheduler import (
    ComponentScheduler,
    InitializationTask,
    TimingConfig,
    orchestrator,
    universal_timer,
)
from tenire.organizers.concurrency.semaphore_manager import SemaphoreManager
from tenire.organizers.concurrency.thread_pool_manager import ThreadPoolManager

# Public service imports
from tenire.public_services.doctor import ComponentDoctor
from tenire.public_services.hospital.charts import (
    HealthStatus
)
from tenire.public_services.hospital.coordinator import HealthCoordinator

# RAG imports
from tenire.rag import (
    RAGConfig, DocumentStore, LocalEmbeddings,
    GamblingDataRetriever, RetrieverConfig
)

# Servicer imports
from tenire.servicers import SignalManager

# Structure imports
from tenire.structure.dependency_graph import (
    dependency_graph, initialize_dependency_graph
)
from tenire.structure.subgraph import subgraph_manager
from tenire.structure.component_scheduler import component_scheduler, InitializationStatus

# Utility imports
from tenire.utils.logger import get_logger

logger = get_logger(__name__)

# Type variables
T = TypeVar('T')
K = TypeVar('K')

class ComponentType(Enum):
    """Types of system components."""
    CORE = "core"
    DATA = "data"
    GUI = "gui"
    INTEGRATION = "integration"
    ORGANIZER = "organizer"
    PROFESSIONAL = "professional"
    PUBLIC_SERVICE = "public_service"
    RAG = "rag"
    SERVICER = "servicer"
    STRUCTURE = "structure"
    UTILITY = "utility"

@dataclass
class ComponentMetadata:
    """Metadata for registered components."""
    type: ComponentType
    provides: List[str]
    dependencies: Set[str]
    priority: int
    health_checks: Optional[Dict[str, Any]] = None
    timing_config: Optional[TimingConfig] = None
    cleanup_priority: int = field(default=50)  # Priority for cleanup tasks
    tags: Set[str] = field(default_factory=set)  # Tags for component categorization
    is_critical: bool = field(default=False)  # Whether component is critical

@dataclass
class ComponentLocks:
    """Granular locks for component operations."""
    init_lock: Lock = field(default_factory=Lock)
    cleanup_lock: Lock = field(default_factory=Lock)
    health_lock: Lock = field(default_factory=Lock)
    timing_lock: Lock = field(default_factory=Lock)
    
@dataclass
class BatchGroup:
    """Group of components that can be processed together."""
    components: Set[str] = field(default_factory=set)
    priority: int = 0
    dependencies: Set[str] = field(default_factory=set)

class ComponentRegistryError(Exception):
    """Base exception for component registry errors."""

class ComponentNotFoundError(ComponentRegistryError):
    """Raised when a component is not found."""

class DependencyError(ComponentRegistryError):
    """Raised when there are dependency issues."""

def _get_cache_decorator(method):
    """Wrapper to properly handle cache decorator with instance cache."""
    async def wrapper(self, *args, **kwargs):
        if not hasattr(self, '_component_cache') or not self._component_cache:
            return await method(self, *args, **kwargs)
        
        decorator = cache_decorator(
            cache=self._component_cache,
            key_prefix=f"{method.__name__}"
        )
        return await decorator(method)(self, *args, **kwargs)
    return wrapper

class PipelineRegistry:
    """
    Registers and manages all system pipelines and components.
    
    This class organizes components into logical pipelines based on their
    functionality and manages their dependencies, initialization order,
    and lifecycle management. It deeply integrates with the scheduler and
    compactor for coordinated timing and cleanup.
    
    Features:
    - Deep integration with universal timer for coordinated timing
    - Compactor integration for resource cleanup
    - Health monitoring and status tracking
    - Dynamic component timing adjustment
    - Graceful shutdown handling
    - Resource usage optimization
    """
    
    _instance = None
    _lock = asyncio.Lock()
    
    # RAG configurations
    rag_timing_configs = {
        "document_store": TimingConfig(
            min_interval=0.5,    # Slower for heavy vector operations
            max_interval=2.0,    # Allow longer intervals when idle
            burst_limit=10,      # Limited bursts for memory management
            cooldown=1.0,        # Longer cooldown for resource recovery
            priority=85          # High priority for core functionality
        ),
        "embeddings_manager": TimingConfig(
            min_interval=0.1,    # Fast for small embedding operations
            max_interval=1.0,    # Allow longer intervals for batching
            burst_limit=20,      # Higher bursts for batch processing
            cooldown=0.5,        # Moderate cooldown
            priority=80          # High priority for embeddings
        ),
        "gambling_data_retriever": TimingConfig(
            min_interval=0.2,    # Moderate for query processing
            max_interval=2.0,    # Allow longer intervals for complex queries
            burst_limit=15,      # Moderate bursts for query batching
            cooldown=1.0,        # Longer cooldown for LLM recovery
            priority=75          # Lower priority than core components
        )
    }
    
    rag_config = RAGConfig(
        model_name='all-MiniLM-L6-v2',
        model_type='sentence_transformers',
        embedding_dim=384,
        use_tfidf=True,
        chunk_fields=True,
        field_weights={
            'game': 1.0,
            'user': 0.8,
            'payout': 0.6,
            'full': 1.0
        }
    )
    
    retriever_config = RetrieverConfig(
        rag_config=rag_config,
        initial_k=20,
        final_k=5,
        cross_encoder_model="cross-encoder/ms-marco-MiniLM-L-6-v2"
    )
    
    def __new__(cls):
        """Ensure singleton pattern."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self) -> None:
        """Initialize the registry with granular locking and caching."""
        if hasattr(self, '_initialized'):
            return
            
        self._initialized = False
        self._thread_pool: Optional[ThreadPoolManager] = None
        self._initialization_lock = asyncio.Lock()
        self._cleanup_tasks: List[tuple] = []
        self._components: Dict[str, ComponentMetadata] = {}
        self._health_coordinator: Optional[HealthCoordinator] = None
        self._timer = universal_timer
        self._orchestrator = orchestrator
        self._component_refs: Dict[str, weakref.ref] = {}
        self._shutdown_event = asyncio.Event()
        self._signal_manager = None
        self._concurrency_manager = None
        
        # Add granular locking
        self._component_locks: DefaultDict[str, ComponentLocks] = defaultdict(ComponentLocks)
        self._type_locks: DefaultDict[ComponentType, Lock] = defaultdict(Lock)
        
        # Add batch processing
        self._batch_groups: DefaultDict[ComponentType, List[BatchGroup]] = defaultdict(list)
        self._batch_size = 5  # Configurable batch size
        
        # Initialize component cache with proper error handling
        try:
            self._component_cache = TieredCache[str, ComponentMetadata](
                memory_size=1000,  # Cache up to 1000 components in memory
                disk_size=10_000_000,  # 10MB disk cache
                disk_path=Path("data/cache/components"),
                policy=CachePolicy.LRU,
                ttl=None  # Components don't expire
            )
        except Exception as e:
            logger.error(f"Failed to initialize component cache: {str(e)}")
            self._component_cache = None  # Fallback to no caching
        
        # Ensure dependency graph is available
        if not dependency_graph:
            raise ComponentRegistryError("Dependency graph not initialized")
            
        # Register with container
        try:
            container.register_sync('pipeline_registry', self)
        except Exception as e:
            logger.error(f"Failed to register with container: {str(e)}")
            raise ComponentRegistryError(f"Container registration failed: {str(e)}")
        
        # Register cleanup with compactor
        try:
            from tenire.organizers.compactor import compactor
            compactor.register_cleanup_task(
                name="pipeline_registry_cleanup",
                cleanup_func=self.cleanup_pipelines,
                priority=95,
                is_async=True,
                metadata={"tags": ["core", "pipeline", "cleanup"]}
            )
        except Exception as e:
            logger.error(f"Failed to register cleanup task: {str(e)}")
            # Continue without cleanup registration as it's not critical for initialization
        
        # Initialize orchestrator integration
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(self._initialize_dependencies())

    async def _get_component_lock(self, component: str, operation: str) -> Lock:
        """Get the appropriate granular lock for a component operation."""
        locks = self._component_locks[component]
        if operation == 'init':
            return locks.init_lock
        elif operation == 'cleanup':
            return locks.cleanup_lock
        elif operation == 'health':
            return locks.health_lock
        elif operation == 'timing':
            return locks.timing_lock
        else:
            return self._lock  # Fallback to global lock

    async def _get_type_lock(self, component_type: ComponentType) -> Lock:
        """Get the lock for a component type."""
        return self._type_locks[component_type]

    async def _create_batch_groups(self, components: List[Tuple[str, ComponentMetadata]]) -> None:
        """Create optimized batch groups for parallel processing with enhanced concurrency control."""
        # Group by type and dependencies
        type_groups: DefaultDict[ComponentType, List[Tuple[str, ComponentMetadata]]] = defaultdict(list)
        for name, metadata in components:
            type_groups[metadata.type].append((name, metadata))
            
        # Create batch groups within each type
        for component_type, components in type_groups.items():
            # Sort by priority
            components.sort(key=lambda x: x[1].priority, reverse=True)
            
            current_group = BatchGroup()
            for name, metadata in components:
                # Start new group if current is full or has conflicting dependencies
                if (len(current_group.components) >= self._batch_size or
                    metadata.dependencies & current_group.dependencies):
                    if current_group.components:
                        self._batch_groups[component_type].append(current_group)
                        current_group = BatchGroup()
                
                current_group.components.add(name)
                current_group.dependencies.update(metadata.dependencies)
                current_group.priority = max(current_group.priority, metadata.priority)
                
            # Add final group
            if current_group.components:
                self._batch_groups[component_type].append(current_group)
            
            logger.debug(f"Created {len(self._batch_groups[component_type])} batch groups for {component_type.value}")

    async def register_pipelines(self) -> None:
        """Register all system pipelines with optimized batch processing."""
        try:
            # Get component scheduler
            scheduler = container.get('component_scheduler')
            if not scheduler:
                raise ComponentRegistryError("Component scheduler not available")
            
            # Configure batch size based on system resources
            from tenire.utils.optimizer import optimizer
            batch_size = min(optimizer.num_workers, 5)  # Cap at 5 for stability
            scheduler.set_batch_size(batch_size)
            
            # Group components by type for batch processing
            components = [(name, meta) for name, meta in self._components.items()]
            await self._create_batch_groups(components)
            
            # Process each component type in parallel
            tasks = []
            for component_type in ComponentType:
                tasks.append(self._schedule_pipeline_type(component_type))
            
            await asyncio.gather(*tasks)
            
            # Initialize all components using batch processing
            if not await scheduler.initialize_components():
                raise ComponentRegistryError("Failed to initialize components")
            
            logger.info(f"All pipelines registered and initialized successfully with batch size {batch_size}")
            
        except Exception as e:
            logger.error(f"Error registering pipelines: {str(e)}")
            raise ComponentRegistryError(f"Failed to register pipelines: {str(e)}")

    async def _schedule_pipeline_type(self, component_type: ComponentType) -> None:
        """Schedule all components of a given type with optimized batch processing."""
        async with self._get_type_lock(component_type):
            for batch_group in self._batch_groups[component_type]:
                # Process batch group in parallel with concurrency control
                tasks = []
                semaphore = asyncio.Semaphore(self._batch_size)
                
                for component_name in batch_group.components:
                    metadata = self._components[component_name]
                    async with semaphore:
                        tasks.append(self._schedule_component(component_name, metadata))
                
                # Wait for all components in batch to be scheduled
                await asyncio.gather(*tasks)
                
                # Register batch timing config with orchestrator
                await self._orchestrator.register_handler(
                    f"{component_type.value}_batch_{len(self._batch_groups[component_type])}",
                    TimingConfig(
                        min_interval=0.1,
                        max_interval=1.0,
                        burst_limit=len(batch_group.components),
                        cooldown=0.5,
                        priority=batch_group.priority
                    )
                )

    async def _schedule_component(self, name: str, metadata: ComponentMetadata) -> None:
        """Schedule a single component with enhanced batch processing support."""
        async with self._get_component_lock(name, 'init'):
            try:
                # Create initialization task with batch-aware configuration
                init_task = InitializationTask(
                    component=name,
                    dependencies=metadata.dependencies,
                    priority=metadata.priority,
                    init_fn=self._create_component_initializer(name),
                    timeout=30.0,
                    retry_count=3,
                    retry_delay=1.0,
                    health_check=metadata.health_checks.get('health_check') if metadata.health_checks else None,
                    metadata={
                        'type': metadata.type.value,
                        'is_critical': metadata.is_critical,
                        'tags': list(metadata.tags),
                        'cleanup_priority': metadata.cleanup_priority,
                        'timing_config': metadata.timing_config._asdict() if metadata.timing_config else None
                    }
                )
                
                # Schedule with component scheduler
                await component_scheduler.schedule_component(init_task)
                
                # Monitor initialization status
                status = component_scheduler.get_component_status(name)
                if status == InitializationStatus.FAILED:
                    raise ComponentRegistryError(f"Component {name} failed to initialize")
                
                logger.debug(f"Scheduled component {name} for batch initialization")
                
            except Exception as e:
                logger.error(f"Error scheduling component {name}: {str(e)}")
                raise ComponentRegistryError(f"Failed to schedule component {name}: {str(e)}")

    async def _initialize_dependencies(self) -> None:
        """Initialize required dependencies."""
        try:
            # Get signal manager
            from tenire.servicers import get_signal_manager
            self._signal_manager = await get_signal_manager()
            
            # Get concurrency manager
            from tenire.organizers.concurrency.concurrency_manager import concurrency_manager
            self._concurrency_manager = concurrency_manager
            
            # Initialize orchestrator
            await self._initialize_orchestrator()
            
            # Initialize health coordinator
            self._health_coordinator = container.get('health_coordinator')
            
            # Initialize and start monitor
            await self._initialize_monitor()
            
            self._initialized = True
            logger.info("Pipeline registry dependencies initialized")
            
            # Emit initialization signal
            if self._signal_manager:
                await self._signal_manager.emit(Signal(
                    type=SignalType.RAG_COMPONENT_INITIALIZED,
                    data={
                        'component': 'pipeline_registry',
                        'status': 'initialized'
                    }
                ))
                
        except Exception as e:
            logger.error(f"Error initializing dependencies: {str(e)}")
            # Schedule retry
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_later(1.0, lambda: loop.create_task(self._initialize_dependencies()))

    async def _initialize_orchestrator(self) -> None:
        """Initialize orchestrator integration."""
        try:
            # Register timing configurations
            timing_configs = {
                ComponentType.CORE: TimingConfig(
                    min_interval=0.01,
                    max_interval=0.1,
                    burst_limit=100,
                    cooldown=0.5,
                    priority=95
                ),
                ComponentType.DATA: TimingConfig(
                    min_interval=0.1,
                    max_interval=1.0,
                    burst_limit=50,
                    cooldown=1.0,
                    priority=80
                ),
                ComponentType.GUI: TimingConfig(
                    min_interval=0.016,
                    max_interval=0.033,
                    burst_limit=10,
                    cooldown=0.1,
                    priority=85
                ),
                ComponentType.INTEGRATION: TimingConfig(
                    min_interval=0.1,
                    max_interval=1.0,
                    burst_limit=20,
                    cooldown=0.5,
                    priority=75
                ),
                ComponentType.ORGANIZER: TimingConfig(
                    min_interval=0.05,
                    max_interval=0.5,
                    burst_limit=30,
                    cooldown=0.2,
                    priority=90
                ),
                ComponentType.PROFESSIONAL: TimingConfig(
                    min_interval=0.1,
                    max_interval=2.0,
                    burst_limit=10,
                    cooldown=1.0,
                    priority=70
                ),
                ComponentType.PUBLIC_SERVICE: TimingConfig(
                    min_interval=0.2,
                    max_interval=2.0,
                    burst_limit=5,
                    cooldown=1.0,
                    priority=60
                ),
                ComponentType.RAG: TimingConfig(
                    min_interval=0.5,
                    max_interval=5.0,
                    burst_limit=5,
                    cooldown=2.0,
                    priority=65
                ),
                ComponentType.SERVICER: TimingConfig(
                    min_interval=0.05,
                    max_interval=0.5,
                    burst_limit=50,
                    cooldown=0.2,
                    priority=85
                ),
                ComponentType.STRUCTURE: TimingConfig(
                    min_interval=0.1,
                    max_interval=1.0,
                    burst_limit=20,
                    cooldown=0.5,
                    priority=88
                ),
                ComponentType.UTILITY: TimingConfig(
                    min_interval=0.1,
                    max_interval=1.0,
                    burst_limit=30,
                    cooldown=0.5,
                    priority=75
                )
            }
            
            # Register with orchestrator
            for component_type, timing_config in timing_configs.items():
                await self._orchestrator.register_handler(
                    f"{component_type.value}_pipeline",
                    timing_config
                )
                
        except Exception as e:
            logger.error(f"Error initializing orchestrator integration: {str(e)}")
            
    async def _ensure_thread_pool(self) -> None:
        """Ensure thread pool is initialized with proper integration."""
        if not self._thread_pool:
            self._thread_pool = ThreadPoolManager(
                max_workers=concurrency_manager.data_manager.max_workers
            )
            self._thread_pool.initialize()
            
            # Register with compactor
            from tenire.organizers.compactor import compactor
            compactor.register_thread_pool(self._thread_pool)
            
            # Register timing with orchestrator
            await self._orchestrator.register_handler(
                "pipeline_thread_pool",
                TimingConfig(
                    min_interval=0.05,
                    max_interval=0.5,
                    burst_limit=20,
                    cooldown=0.2,
                    priority=85
                )
            )
            
    async def _ensure_health_monitoring(self) -> None:
        """Ensure health monitoring is initialized."""
        if not self._health_coordinator:
            try:
                from tenire.public_services.hospital import get_health_coordinator
                self._health_coordinator = get_health_coordinator()
                if self._health_coordinator:
                    await self._health_coordinator.initialize()
                    
                    # Register cleanup task
                    compactor = container.get('compactor')
                    if compactor:
                        compactor.register_cleanup_task(
                            name="health_coordinator_cleanup",
                            cleanup_func=self._health_coordinator.cleanup,
                            priority=80,
                            is_async=True,
                            metadata={
                                "tags": ["health", "monitoring"],
                                "description": "Cleanup health monitoring system"
                            }
                        )
                    logger.debug("Health monitoring initialized")
            except Exception as e:
                logger.warning(f"Failed to initialize health monitoring: {str(e)}")
                self._health_coordinator = None
            
            # Register timing with orchestrator
            await self._orchestrator.register_handler(
                "health_coordinator",
                TimingConfig(
                    min_interval=1.0,
                    max_interval=5.0,
                    burst_limit=5,
                    cooldown=2.0,
                    priority=70
                )
            )
            
    async def _schedule_core_pipeline(self) -> None:
        """Schedule core system components with proper timing."""
        core = subgraph_manager.get_subgraph("core")
        if not core:
            raise ComponentNotFoundError("Required core subgraph not found")
            
        # Define core timing config
        core_timing = TimingConfig(
            min_interval=0.01,
            max_interval=0.1,
            burst_limit=100,
            cooldown=0.5,
            priority=95
        )
            
        # Schedule signal manager first
        signal_task = InitializationTask(
            component="signal_manager",
            dependencies=set(),  # No dependencies as it's a core service
            priority=100,  # Highest priority as other components depend on it
            init_fn=lambda: SignalManager(
                max_workers=self._concurrency_manager.signal_manager.max_workers,
                batch_size=self._concurrency_manager.signal_manager.batch_size
            ),
            timeout=20.0,
            retry_count=3,
            retry_delay=1.0,
            health_check=self._check_signal_manager_health
        )
        await component_scheduler.schedule_component(signal_task)

        # Schedule container with timing
        container_task = InitializationTask(
            component="container",
            dependencies={"signal_manager"},  # Now depends on signal manager
            priority=95,
            init_fn=container,
            timeout=10.0,
            retry_count=3,
            retry_delay=1.0
        )
        await component_scheduler.schedule_component(container_task)

        # Schedule ConfigManager with timing
        config_task = InitializationTask(
            component="config_manager",
            dependencies={"container", "signal_manager"},
            priority=90,
            init_fn=ConfigManager,
            timeout=10.0,
            retry_count=3,
            retry_delay=1.0
        )
        await component_scheduler.schedule_component(config_task)

        # Register core cleanup tasks
        from tenire.organizers.compactor import compactor
        compactor.register_cleanup_task(
            name="core_pipeline_cleanup",
            cleanup_func=self._cleanup_core_pipeline,
            priority=100,
            is_async=True,
            metadata={"tags": ["core", "cleanup"]}
        )

    async def _check_signal_manager_health(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        try:
            signal_manager = container.get("signal_manager")
            if not signal_manager:
                return (HealthStatus.UNHEALTHY, "Signal manager not found", {})

            queue_size = signal_manager._signal_queue.qsize()
            processing_latency = signal_manager._get_processing_latency()

            if queue_size > 1000:  # Queue getting too large
                return (
                    HealthStatus.DEGRADED,
                    f"Signal queue size too large: {queue_size}",
                    {"queue_size": queue_size, "latency": processing_latency}
                )

            if processing_latency > 1.0:  # Processing taking too long
                return (
                    HealthStatus.DEGRADED,
                    f"Signal processing latency high: {processing_latency:.2f}s",
                    {"queue_size": queue_size, "latency": processing_latency}
                )

            return (
                HealthStatus.HEALTHY,
                "Signal manager healthy",
                {"queue_size": queue_size, "latency": processing_latency}
            )

        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking signal manager health: {str(e)}",
                {"error": str(e)}
            )

    async def _schedule_data_pipeline(self) -> None:
        """Schedule data pipeline components."""
        try:
            # Register data sieve first as it's the high-level orchestrator
            await self.register_component(
                name="data_sieve",
                component_type=ComponentType.DATA,
                provides=["data_orchestration", "data_management"],
                dependencies={"signal_manager", "compactor"},
                priority=95,  # High priority as it orchestrates other data components
                health_checks={
                    "cache_storage": self._check_cache_storage,
                    "embedding_cache": self._check_embedding_cache,
                    "vector_storage": self._check_vector_storage
                },
                timing_config=self._get_default_timing_config(ComponentType.DATA),
                cleanup_priority=95,
                tags={"data", "orchestration", "core"},
                is_critical=True
            )

            # Register data manager
            await self.register_component(
                name="data_manager",
                component_type=ComponentType.DATA,
                provides=["data_processing", "data_validation"],
                dependencies={"signal_manager", "compactor", "data_sieve"},
                priority=90,
                health_checks={
                    "cache_storage": self._check_cache_storage,
                    "bet_cache": self._check_bet_cache
                },
                timing_config=self._get_default_timing_config(ComponentType.DATA),
                cleanup_priority=90,
                tags={"data", "processing", "core"},
                is_critical=True
            )

            # Register head janitor
            await self.register_component(
                name="head_janitor",
                component_type=ComponentType.DATA,
                provides=["data_cleaning", "data_analysis"],
                dependencies={"signal_manager", "compactor", "data_sieve", "data_manager"},
                priority=85,
                health_checks={
                    "bet_patterns": self._check_bet_patterns,
                    "bet_cache": self._check_bet_cache
                },
                timing_config=self._get_default_timing_config(ComponentType.DATA),
                cleanup_priority=85,
                tags={"data", "cleaning", "analysis"},
                is_critical=True
            )

            # Register caching system
            await self.register_component(
                name="cache_system",
                component_type=ComponentType.DATA,
                provides=["caching", "data_persistence"],
                dependencies={"signal_manager", "compactor", "data_sieve"},
                priority=80,
                health_checks={
                    "cache_storage": self._check_cache_storage,
                    "cached_models": self._check_cached_models
                },
                timing_config=self._get_default_timing_config(ComponentType.DATA),
                cleanup_priority=80,
                tags={"data", "caching", "persistence"},
                is_critical=True
            )

            logger.info("Data pipeline components scheduled successfully")

        except Exception as e:
            logger.error(f"Error scheduling data pipeline: {str(e)}")
            raise

    async def _cleanup_data_pipeline(self) -> None:
        """Cleanup data pipeline components."""
        try:
            # Cleanup data sieve first
            data_sieve = container.get_sync('data_sieve')
            if data_sieve:
                await data_sieve.cleanup()

            # Cleanup data manager
            data_manager = container.get_sync('data_manager')
            if data_manager:
                await data_manager.cleanup()

            # Cleanup head janitor
            head_janitor = container.get_sync('head_janitor')
            if head_janitor:
                await head_janitor.cleanup()

            # Cleanup cache system
            cache_system = container.get_sync('cache_system')
            if cache_system:
                await cache_system.cleanup()

            logger.info("Data pipeline cleanup completed successfully")

        except Exception as e:
            logger.error(f"Error during data pipeline cleanup: {str(e)}")
            raise

    async def _schedule_gui_pipeline(self) -> None:
        """Schedule GUI system components with proper timing and dependencies."""
        gui = subgraph_manager.get_subgraph("gui")
        if not gui:
            raise ComponentNotFoundError("Required gui subgraph not found")
            
        try:
            # Schedule GUI process first
            await component_scheduler.schedule_component(
                InitializationTask(
                    component="gui_process",
                    dependencies={"signal_manager", "concurrency_manager"},
                    priority=85,
                    init_fn=lambda: GUIProcess(
                        command_queue=self._concurrency_manager.command_queue,
                        response_queue=self._concurrency_manager.response_queue,
                        width=400  # Default width
                    ),
                    timeout=30.0,
                    retry_count=3,
                    retry_delay=1.0,
                    health_check=self._check_gui_health
                )
            )
            
            # Schedule SidebarGUI
            await component_scheduler.schedule_component(
                InitializationTask(
                    component="gui_manager",
                    dependencies={"gui_process", "signal_manager"},
                    priority=84,
                    init_fn=lambda: SidebarGUI(
                        width=400,
                        command_queue=self._concurrency_manager.command_queue,
                        response_queue=self._concurrency_manager.response_queue
                    ),
                    timeout=20.0,
                    retry_count=3,
                    retry_delay=1.0
                )
            )
            
            # Schedule logging handler
            await component_scheduler.schedule_component(
                InitializationTask(
                    component="gui_logging_handler",
                    dependencies={"gui_manager"},
                    priority=83,
                    init_fn=lambda: SidebarHandler(container.get('gui_manager')),
                    timeout=10.0,
                    retry_count=3,
                    retry_delay=1.0
                )
            )
            
            # Register GUI components with orchestrator using appropriate timing configs
            gui_components = {
                "gui_process": TimingConfig(
                    min_interval=0.016,  # ~60 FPS
                    max_interval=0.033,  # ~30 FPS minimum
                    burst_limit=10,
                    cooldown=0.1,
                    priority=85
                ),
                "gui_manager": TimingConfig(
                    min_interval=0.033,  # ~30 FPS
                    max_interval=0.066,  # ~15 FPS minimum
                    burst_limit=5,
                    cooldown=0.2,
                    priority=84
                ),
                "gui_logging_handler": TimingConfig(
                    min_interval=0.1,
                    max_interval=1.0,
                    burst_limit=20,
                    cooldown=0.5,
                    priority=83
                )
            }
            
            for component, timing_config in gui_components.items():
                await self._orchestrator.register_handler(component, timing_config)
            
            # Register cleanup tasks
            self.compactor.register_cleanup_task(
                name="gui_pipeline_cleanup",
                cleanup_func=self._cleanup_gui_pipeline,
                priority=85,
                is_async=True,
                metadata={"tags": ["gui", "cleanup"]}
            )
            
            logger.info("Scheduled GUI pipeline components")
            
        except Exception as e:
            logger.error(f"Error scheduling GUI pipeline: {str(e)}")
            raise ComponentRegistryError(f"Failed to schedule GUI pipeline: {str(e)}")
            
    async def _cleanup_gui_pipeline(self) -> None:
        """Clean up GUI pipeline components."""
        try:
            # Get components
            gui_process = container.get('gui_process')
            gui_manager = container.get('gui_manager')
            
            # Clean up in reverse initialization order
            if gui_process:
                await gui_process._cleanup_process()
            
            if gui_manager:
                await gui_manager._cleanup_gui()
                
            # Clean up logging handler
            logging_handler = container.get('gui_logging_handler')
            if logging_handler:
                logging_handler.disable()
                self.logging.getLogger().removeHandler(logging_handler)
                
            logger.info("Cleaned up GUI pipeline components")
            
        except Exception as e:
            logger.error(f"Error during GUI pipeline cleanup: {str(e)}")
            raise
            
    async def _check_gui_health(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check health of GUI components."""
        try:
            gui_process = container.get('gui_process')
            if not gui_process or not gui_process.is_alive():
                return (
                    HealthStatus.UNHEALTHY,
                    "GUI process not running",
                    {"process_status": "not_running"}
                )
                
            # Check queue sizes
            cmd_queue_size = gui_process.command_queue.qsize() if gui_process.command_queue else 0
            resp_queue_size = gui_process.response_queue.qsize() if gui_process.response_queue else 0
            
            if cmd_queue_size > 100 or resp_queue_size > 100:
                return (
                    HealthStatus.DEGRADED,
                    f"GUI queues getting large: cmd={cmd_queue_size}, resp={resp_queue_size}",
                    {
                        "command_queue_size": cmd_queue_size,
                        "response_queue_size": resp_queue_size
                    }
                )
                
            return (
                HealthStatus.HEALTHY,
                "GUI components healthy",
                {
                    "process_status": "running",
                    "command_queue_size": cmd_queue_size,
                    "response_queue_size": resp_queue_size
                }
            )
            
        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking GUI health: {str(e)}",
                {"error": str(e)}
            )

    async def _schedule_integration_pipeline(self) -> None:
        """Schedule integration pipeline components."""
        integration = subgraph_manager.get_subgraph("integration")
        if not integration:
            raise ComponentNotFoundError("Required integration subgraph not found")
            
        # Schedule browser integration
        await component_scheduler.schedule_component(
            InitializationTask(
                component="browser_integration",
                dependencies={"signal_manager"},
                priority=85,
                init_fn=BrowserIntegration,
                timeout=45.0,
                retry_count=3,
                retry_delay=2.0
            )
        )

    async def _schedule_organizer_pipeline(self) -> None:
        """Schedule organizer pipeline components."""
        organizer = subgraph_manager.get_subgraph("organizer")
        if not organizer:
            raise ComponentNotFoundError("Required organizer subgraph not found")
            
        # Schedule async task manager first with minimal dependencies
        await component_scheduler.schedule_component(
            InitializationTask(
                component="async_task_manager",
                dependencies={"event_loop_manager"},  # Only depend on event_loop_manager initially
                priority=95,  # High priority as it's a critical dependency
                init_fn=AsyncTaskManager,
                timeout=30.0,
                retry_count=3,
                retry_delay=1.0,
                metadata={
                    'is_critical': True,
                    'tags': {'concurrency', 'core', 'async'}
                }
            )
        )
        
        # Schedule remaining concurrency components
        concurrency_components = [
            ("concurrency_manager", ConcurrencyManager, 90, {"async_task_manager"}),
            ("concurrency_monitor", MonitorMetrics, 85, {"async_task_manager"}),
            ("component_scheduler", ComponentScheduler, 85, {"async_task_manager"}),
            ("semaphore_manager", SemaphoreManager, 80, {"async_task_manager"}),
            ("thread_pool_manager", ThreadPoolManager, 80, {"async_task_manager"})
        ]
        
        for name, component_class, priority, deps in concurrency_components:
            await component_scheduler.schedule_component(
                InitializationTask(
                    component=name,
                    dependencies=deps,
                    priority=priority,
                    init_fn=component_class,
                    timeout=30.0,
                    retry_count=3,
                    retry_delay=1.0
                )
            )

    async def _schedule_professional_pipeline(self) -> None:
        """Schedule professional pipeline components."""
        try:
            # Register BetAnalyzer professional
            await self.register_component(
                name="bet_analyzer",
                component_type=ComponentType.PROFESSIONAL,
                provides=["bet_analysis", "pattern_detection", "prediction"],
                dependencies={"signal_manager", "concurrency_manager", "data_manager"},
                priority=75,
                timing_config=TimingConfig(
                    min_interval=0.1,
                    max_interval=2.0,
                    burst_limit=10,
                    cooldown=1.0,
                    priority=70
                ),
                health_checks={
                    "check_models": "_check_models_health",
                    "check_cache": "_check_cache_health"
                },
                tags={"betting", "analysis", "ml"},
                is_critical=False
            )
            
            logger.info("Scheduled professional pipeline components")
            
        except Exception as e:
            logger.error(f"Error scheduling professional pipeline: {str(e)}")
            raise ComponentRegistryError(f"Failed to schedule professional pipeline: {str(e)}")

    async def _schedule_servicer_pipeline(self) -> None:
        """Schedule servicer pipeline components."""
        try:
            # Register AgentManager servicer
            await self.register_component(
                name="agent_manager",
                component_type=ComponentType.SERVICER,
                provides=["agent_management", "task_execution"],
                dependencies={"signal_manager", "concurrency_manager", "browser_integration"},
                priority=85,
                timing_config=TimingConfig(
                    min_interval=0.05,
                    max_interval=0.5,
                    burst_limit=50,
                    cooldown=0.2,
                    priority=85
                ),
                health_checks={
                    "check_agent": "_check_agent_health",
                    "check_tasks": "_check_tasks_health"
                },
                tags={"agent", "automation"},
                is_critical=True
            )
            
            # Register BetManager servicer
            await self.register_component(
                name="bet_manager",
                component_type=ComponentType.SERVICER,
                provides=["bet_management", "bet_tracking"],
                dependencies={"signal_manager", "browser_integration", "agent_manager"},
                priority=85,
                timing_config=TimingConfig(
                    min_interval=0.05,
                    max_interval=0.5,
                    burst_limit=50,
                    cooldown=0.2,
                    priority=85
                ),
                health_checks={
                    "check_betting": "_check_betting_health",
                    "check_history": "_check_history_health"
                },
                tags={"betting", "tracking"},
                is_critical=True
            )
            
            logger.info("Scheduled servicer pipeline components")
            
        except Exception as e:
            logger.error(f"Error scheduling servicer pipeline: {str(e)}")
            raise ComponentRegistryError(f"Failed to schedule servicer pipeline: {str(e)}")

    async def _schedule_actions_pipeline(self) -> None:
        """Schedule actions pipeline components."""
        try:
            # Register CommandProcessor
            await self.register_component(
                name="command_processor",
                component_type=ComponentType.CORE,
                provides=["command_processing", "action_routing"],
                dependencies={"signal_manager", "concurrency_manager"},
                priority=90,
                timing_config=TimingConfig(
                    min_interval=0.01,
                    max_interval=0.1,
                    burst_limit=100,
                    cooldown=0.5,
                    priority=90
                ),
                health_checks={
                    "check_handlers": "_check_handlers_health"
                },
                tags={"commands", "actions", "core"},
                is_critical=True
            )
            
            # Register BettingActions controller
            await self.register_component(
                name="betting_controller",
                component_type=ComponentType.CORE,
                provides=["betting_actions", "browser_automation"],
                dependencies={"command_processor", "browser_integration"},
                priority=88,
                timing_config=TimingConfig(
                    min_interval=0.05,
                    max_interval=0.5,
                    burst_limit=50,
                    cooldown=0.2,
                    priority=88
                ),
                health_checks={
                    "check_actions": "_check_actions_health"
                },
                tags={"betting", "actions", "automation"},
                is_critical=True
            )
            
            logger.info("Scheduled actions pipeline components")
            
        except Exception as e:
            logger.error(f"Error scheduling actions pipeline: {str(e)}")
            raise ComponentRegistryError(f"Failed to schedule actions pipeline: {str(e)}")

    async def _schedule_public_service_pipeline(self) -> None:
        """
        Schedule the public service pipeline components.
        
        The ComponentDoctor is the main entry point for the health monitoring system.
        It internally manages its hospital components (coordinator, executor, responder)
        which run in their own threads.
        """
        try:
            # Initialize component doctor which manages the health system
            doctor_task = InitializationTask(
                component="component_doctor",
                dependencies={"compactor", "signal_manager"},
                priority=80,
                init_fn=lambda: ComponentDoctor(),
                timeout=60.0,
                retry_count=3,
                retry_delay=2.0,
                health_check=lambda: self._check_component_health()
            )
            await component_scheduler.schedule_component(doctor_task)

            # Register cleanup task with compactor
            from tenire.organizers.compactor import compactor
            if compactor:
                compactor.register_cleanup_task(
                    name="public_service_cleanup",
                    cleanup_func=self._cleanup_public_service_pipeline,
                    priority=90,
                    is_async=True,
                    metadata={"tags": ["health", "monitoring", "cleanup"]}
                )

            logger.info("Scheduled public service pipeline components")

        except Exception as e:
            logger.error(f"Error scheduling public service pipeline: {str(e)}")
            raise ComponentRegistryError(f"Failed to schedule public service pipeline: {str(e)}")

    async def _cleanup_public_service_pipeline(self) -> None:
        """Clean up public service pipeline components."""
        try:
            # ComponentDoctor will handle cleanup of all hospital components
            component_doctor = container.get('component_doctor')
            if component_doctor:
                await component_doctor.cleanup()

            logger.info("Cleaned up public service pipeline components")

        except Exception as e:
            logger.error(f"Error during public service pipeline cleanup: {str(e)}")
            raise

    async def _check_component_health(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check health of registered components."""
        try:
            component_doctor = container.get('component_doctor')
            if not component_doctor:
                return (
                    HealthStatus.DEGRADED,
                    "Component doctor not available",
                    {}
                )

            health_info = await component_doctor.get_system_health()
            return (
                HealthStatus(health_info['overall_status']),
                f"System health status: {health_info['overall_status']}",
                health_info
            )
        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking component health: {str(e)}",
                {'error': str(e)}
            )

    async def _check_system_health(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check overall system health."""
        try:
            component_doctor = container.get('component_doctor')
            if not component_doctor:
                return (
                    HealthStatus.DEGRADED,
                    "Component doctor not available",
                    {}
                )

            health_info = await component_doctor.get_system_health()
            total_components = len(health_info['components'])
            healthy_components = sum(1 for c in health_info['components'].values()
                                  if c.get('status') == 'healthy')

            health_ratio = healthy_components / total_components if total_components > 0 else 0

            if health_ratio >= 0.9:
                status = HealthStatus.HEALTHY
                message = f"System healthy: {healthy_components}/{total_components} components healthy"
            elif health_ratio >= 0.7:
                status = HealthStatus.DEGRADED
                message = f"System degraded: {healthy_components}/{total_components} components healthy"
            else:
                status = HealthStatus.UNHEALTHY
                message = f"System unhealthy: only {healthy_components}/{total_components} components healthy"

            return (status, message, health_info)
        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking system health: {str(e)}",
                {'error': str(e)}
            )

    def _create_component_initializer(self, name: str) -> Callable:
        """Create an enhanced initializer function for a component."""
        metadata = self._components[name]
        
        async def initializer():
            try:
                # Get component class from container
                component_class = container.get_class(name)
                if not component_class:
                    raise ComponentNotFoundError(f"Component class not found: {name}")
                    
                # Create instance with concurrency manager settings if needed
                if hasattr(component_class, 'max_workers') and hasattr(component_class, 'batch_size'):
                    instance = component_class(
                        max_workers=self._concurrency_manager.data_manager.max_workers,
                        batch_size=self._concurrency_manager.data_manager.batch_size
                    )
                else:
                    instance = component_class()
                    
                # Store weak reference
                self._component_refs[name] = weakref.ref(instance)
                
                # Register cleanup if available
                if hasattr(instance, 'cleanup'):
                    from tenire.organizers.compactor import compactor
                    compactor.register_cleanup_task(
                        name=f"{name}_cleanup",
                        cleanup_func=instance.cleanup,
                        priority=metadata.cleanup_priority,
                        is_async=asyncio.iscoroutinefunction(instance.cleanup),
                        metadata={"tags": list(metadata.tags)}
                    )
                    
                # Emit initialization signal
                if self._signal_manager:
                    await self._signal_manager.emit(Signal(
                        type=SignalType.COMPONENT_INITIALIZED,
                        data={
                            'component': name,
                            'type': metadata.type.value,
                            'priority': metadata.priority
                        }
                    ))
                    
                return instance
                
            except Exception as e:
                logger.error(f"Error initializing component {name}: {str(e)}")
                if self._signal_manager:
                    await self._signal_manager.emit(Signal(
                        type=SignalType.COMPONENT_ERROR,
                        data={
                            'component': name,
                            'error': str(e),
                            'type': 'initialization'
                        }
                    ))
                raise
                
        return initializer

    async def _register_cleanup_tasks(self) -> None:
        """Register cleanup tasks for all components."""
        subgraphs = [
            "core", "data", "gui", "integration", "organizer",
            "professional", "public_service", "rag", "servicer",
            "structure", "utility"
        ]
        
        for subgraph_name in subgraphs:
            subgraph = subgraph_manager.get_subgraph(subgraph_name)
            if subgraph:
                for node in subgraph.get_nodes().values():
                    if hasattr(node, 'cleanup_func'):
                        self._cleanup_tasks.append((node.cleanup_func, node.priority))

    async def cleanup_pipelines(self) -> None:
        """Clean up all pipelines with enhanced monitoring and cache cleanup."""
        if self._shutdown_event.is_set():
            return
            
        try:
            self._shutdown_event.set()
            logger.info("Starting pipeline cleanup")
            
            # Get final metrics snapshot
            final_metrics = monitor.get_metrics()
            
            # Clean up component cache
            await self._component_cache.cleanup()
            
            # Clean up components in reverse priority order
            components = sorted(
                self._components.items(),
                key=lambda x: x[1].priority,
                reverse=True
            )
            
            for name, metadata in components:
                try:
                    # Get component instance
                    ref = self._component_refs.get(name)
                    instance = ref() if ref else None
                    
                    if instance and hasattr(instance, 'cleanup'):
                        await instance.cleanup()
                        
                    # Unregister from monitor
                    monitor.unregister_component(name)
                        
                except Exception as e:
                    logger.error(f"Error cleaning up component {name}: {str(e)}")
                    
            # Clean up monitor last
            await self._cleanup_monitor()
                    
            # Clear state
            self._components.clear()
            self._component_refs.clear()
            self._cleanup_tasks.clear()
            
            # Emit cleanup completed signal
            if self._signal_manager:
                await self._signal_manager.emit(Signal(
                    type=SignalType.RAG_CLEANUP_COMPLETED,
                    data={'component': 'pipeline_registry'}
                ))
                
            logger.info("Pipeline cleanup completed")
            
        except Exception as e:
            logger.error(f"Error during pipeline cleanup: {str(e)}")
            if self._signal_manager:
                await self._signal_manager.emit(Signal(
                    type=SignalType.RAG_CLEANUP_ERROR,
                    data={
                        'component': 'pipeline_registry',
                        'error': str(e)
                    }
                ))
            raise

    async def _cache_operation(self, operation: str, *args, **kwargs) -> Optional[Any]:
        """Helper method for safe cache operations with fallback."""
        if not self._component_cache:
            return None
            
        try:
            if operation == 'get':
                return await self._component_cache.get(*args, **kwargs)
            elif operation == 'set':
                return await self._component_cache.set(*args, **kwargs)
            elif operation == 'cleanup':
                return await self._component_cache.cleanup(*args, **kwargs)
            elif operation == 'clear':
                return await self._component_cache.clear(*args, **kwargs)
        except Exception as e:
            logger.warning(f"Cache operation {operation} failed: {str(e)}")
            return None

    @_get_cache_decorator
    async def get_component_info(self) -> Dict[str, Dict[str, Any]]:
        """Get detailed information about registered components with metrics and caching."""
        try:
            component_info = {}
            
            # Get current metrics
            current_metrics = monitor.get_metrics()
            
            for component_name, metadata in self._components.items():
                try:
                    # Get health info
                    health_info = {}
                    if self._health_coordinator:
                        health_info = self._health_coordinator.get_component_health(component_name)
                        
                    # Get timing info
                    timing_info = {}
                    if self._orchestrator:
                        timing_info = self._orchestrator.get_handler_status(component_name)
                        
                    # Get monitoring metrics
                    monitor_metrics = {}
                    if current_metrics:
                        monitor_metrics = current_metrics.component_metrics.get(component_name, {})
                        
                    component_info[component_name] = {
                        "type": metadata.type.value,
                        "provides": metadata.provides,
                        "dependencies": list(metadata.dependencies),
                        "priority": metadata.priority,
                        "cleanup_priority": metadata.cleanup_priority,
                        "is_critical": metadata.is_critical,
                        "tags": list(metadata.tags),
                        "health": health_info,
                        "timing": timing_info,
                        "metrics": monitor_metrics
                    }
                except Exception as e:
                    logger.error(f"Error getting info for component {component_name}: {str(e)}")
                    # Continue with other components
                    continue
                    
            return component_info
            
        except Exception as e:
            logger.error(f"Error getting component info: {str(e)}")
            return {}

    @_get_cache_decorator
    async def get_pipeline_status(self) -> Dict[str, Any]:
        """Get overall pipeline status including monitoring metrics with caching."""
        current_metrics = monitor.get_metrics()
        
        return {
            "components": await self.get_component_info(),  # Use await here since it's async
            "timing": self._orchestrator.get_system_status() if self._orchestrator else {},
            "health": self._health_coordinator.get_system_health() if self._health_coordinator else {},
            "metrics": {
                "system": current_metrics.system_metrics,
                "warnings": current_metrics.warnings,
                "errors": current_metrics.errors
            } if current_metrics else {},
            "initialized": self._initialized,
            "shutting_down": self._shutdown_event.is_set()
        }

    async def register_component(
        self,
        name: str,
        component_type: ComponentType,
        provides: List[str],
        dependencies: Set[str],
        priority: int,
        health_checks: Optional[Dict[str, Any]] = None,
        timing_config: Optional[TimingConfig] = None,
        cleanup_priority: int = 50,
        tags: Set[str] = None,
        is_critical: bool = False
    ) -> None:
        """Register a component with enhanced monitoring and caching."""
        if not self._initialized:
            try:
                async with self._initialization_lock:
                    await self._initialize_dependencies()
            except Exception as e:
                logger.error(f"Failed to initialize dependencies for {name}: {str(e)}")
                raise ComponentRegistryError(f"Initialization failed for {name}: {str(e)}")
            
        metadata = ComponentMetadata(
            type=component_type,
            provides=provides,
            dependencies=dependencies,
            priority=priority,
            health_checks=health_checks,
            timing_config=timing_config,
            cleanup_priority=cleanup_priority,
            tags=tags or set(),
            is_critical=is_critical
        )
        
        try:
            # Store in both local dict and cache with proper error handling
            self._components[name] = metadata
            await self._cache_operation('set', f"metadata:{name}", metadata)
            
            logger.debug(f"Registered component: {name}")
            
            # Register with dependency graph
            try:
                dependency_graph.register_node(
                    name=name,
                    dependencies=dependencies,
                    metadata={
                        'type': component_type.value,
                        'priority': priority,
                        'is_critical': is_critical
                    }
                )
            except Exception as e:
                logger.error(f"Failed to register {name} with dependency graph: {str(e)}")
                raise ComponentRegistryError(f"Dependency graph registration failed for {name}: {str(e)}")
            
            # Register with health coordinator if health checks provided
            if health_checks and self._health_coordinator:
                try:
                    self._health_coordinator.register_component(
                        name,
                        health_checks,
                        is_critical=is_critical
                    )
                except Exception as e:
                    logger.warning(f"Failed to register health checks for {name}: {str(e)}")
                    # Continue as health monitoring is not critical
                
            # Register timing config with orchestrator
            if timing_config is None:
                # Use default timing config for component type
                timing_config = self._get_default_timing_config(component_type)
                
            if timing_config and self._orchestrator:
                try:
                    await self._orchestrator.register_handler(
                        name,
                        timing_config
                    )
                except Exception as e:
                    logger.warning(f"Failed to register timing config for {name}: {str(e)}")
                    # Continue as timing is not critical
                
            # Register with monitor
            try:
                monitor.register_component(name)
            except Exception as e:
                logger.warning(f"Failed to register {name} with monitor: {str(e)}")
                # Continue as monitoring is not critical
                
            # Schedule component initialization with proper error handling
            try:
                init_task = InitializationTask(
                    component=name,
                    dependencies=dependencies,
                    priority=priority,
                    init_fn=self._create_component_initializer(name),
                    timeout=30.0,
                    retry_count=3,
                    retry_delay=1.0,
                    health_check=health_checks.get('health_check') if health_checks else None
                )
                
                await component_scheduler.schedule_component(init_task)
            except Exception as e:
                logger.error(f"Failed to schedule initialization for {name}: {str(e)}")
                raise ComponentRegistryError(f"Initialization scheduling failed for {name}: {str(e)}")
            
            # Emit registration signal
            if self._signal_manager:
                try:
                    await self._signal_manager.emit(Signal(
                        type=SignalType.COMPONENT_REGISTERED,
                        data={
                            'component': name,
                            'type': component_type.value,
                            'priority': priority,
                            'is_critical': is_critical,
                            'timing_config': timing_config._asdict() if timing_config else None
                        }
                    ))
                except Exception as e:
                    logger.warning(f"Failed to emit registration signal for {name}: {str(e)}")
                    # Continue as signal emission is not critical
                    
        except Exception as e:
            logger.error(f"Component registration failed for {name}: {str(e)}")
            # Clean up any partial registration
            await self._cleanup_failed_registration(name)
            raise ComponentRegistryError(f"Registration failed for {name}: {str(e)}")

    async def _cleanup_failed_registration(self, name: str) -> None:
        """Clean up after a failed component registration."""
        try:
            # Remove from components dict
            self._components.pop(name, None)
            
            # Remove from cache
            await self._cache_operation('clear', f"metadata:{name}")
            
            # Remove from dependency graph
            try:
                dependency_graph.remove_node(name)
            except Exception:
                pass
                
            # Remove from health coordinator
            if self._health_coordinator:
                try:
                    self._health_coordinator.unregister_component(name)
                except Exception:
                    pass
                    
            # Remove from orchestrator
            if self._orchestrator:
                try:
                    await self._orchestrator.unregister_handler(name)
                except Exception:
                    pass
                    
            # Remove from monitor
            try:
                monitor.unregister_component(name)
            except Exception:
                pass
                
            logger.info(f"Cleaned up failed registration for {name}")
            
        except Exception as e:
            logger.error(f"Error cleaning up failed registration for {name}: {str(e)}")
            # Continue as this is already error handling

    def _get_default_timing_config(self, component_type: ComponentType) -> TimingConfig:
        """Get default timing configuration for a component type."""
        base_configs = {
            # Core systems (high priority, low latency)
            'core': dict(min_interval=0.01, max_interval=0.1, burst_limit=100, cooldown=0.5, priority=95),
            # Data processing systems (medium priority, medium latency)
            'data': dict(min_interval=0.1, max_interval=1.0, burst_limit=50, cooldown=1.0, priority=80),
            # User interface systems (high priority, consistent timing)
            'gui': dict(min_interval=0.016, max_interval=0.033, burst_limit=10, cooldown=0.1, priority=85),
            # Integration systems (medium priority, medium latency)
            'integration': dict(min_interval=0.1, max_interval=1.0, burst_limit=20, cooldown=0.5, priority=75),
            # Organization systems (high priority, medium latency)
            'organizer': dict(min_interval=0.05, max_interval=0.5, burst_limit=30, cooldown=0.2, priority=90),
            # Professional systems (low priority, high latency)
            'professional': dict(min_interval=0.1, max_interval=2.0, burst_limit=10, cooldown=1.0, priority=70),
            # Public service systems (low priority, high latency)
            'public_service': dict(min_interval=0.2, max_interval=2.0, burst_limit=5, cooldown=1.0, priority=60),
            # RAG systems (low priority, high latency)
            'rag': dict(min_interval=0.5, max_interval=5.0, burst_limit=5, cooldown=2.0, priority=65),
            # Service systems (high priority, medium latency)
            'servicer': dict(min_interval=0.05, max_interval=0.5, burst_limit=50, cooldown=0.2, priority=85),
            # Structure systems (high priority, medium latency)
            'structure': dict(min_interval=0.1, max_interval=1.0, burst_limit=20, cooldown=0.5, priority=88),
            # Utility systems (medium priority, medium latency)
            'utility': dict(min_interval=0.1, max_interval=1.0, burst_limit=30, cooldown=0.5, priority=75)
        }
        
        config_data = base_configs.get(component_type.value.lower())
        return TimingConfig(**config_data) if config_data else None

    async def _cleanup_component(self, name: str, instance: Any) -> None:
        """Clean up a component with enhanced resource management."""
        try:
            # Get compactor for resource tracking
            from tenire.organizers.compactor import compactor

            # Perform cleanup if available
            if hasattr(instance, 'cleanup'):
                if asyncio.iscoroutinefunction(instance.cleanup):
                    await instance.cleanup()
                else:
                    await concurrency_manager.run_in_thread(instance.cleanup)

            # Remove from resource registry
            if compactor:
                compactor._resource_registry.discard(instance)

            # Clear any cached references
            if hasattr(self, '_component_refs'):
                self._component_refs.pop(name, None)

            logger.debug(f"Cleaned up component: {name}")

        except Exception as e:
            logger.error(f"Error cleaning up component {name}: {str(e)}")
            if self._signal_manager:
                await self._signal_manager.emit(Signal(
                    type=SignalType.COMPONENT_ERROR,
                    data={
                        'component': name,
                        'error': str(e),
                        'type': 'cleanup'
                    }
                ))

    async def _cleanup_pipeline(self, pipeline_name: str, components: List[str]) -> None:
        """Generic pipeline cleanup."""
        try:
            logger.info(f"Starting {pipeline_name} pipeline cleanup")
            
            for component_name in components:
                instance = container.get(component_name)
                if instance:
                    await self._cleanup_component(component_name, instance)
                    
            if self._signal_manager:
                await self._signal_manager.emit(Signal(
                    type=SignalType.RAG_CLEANUP_COMPLETED,
                    data={'component': pipeline_name}
                ))
                
            logger.info(f"Cleaned up {pipeline_name} pipeline components")
            
        except Exception as e:
            logger.error(f"Error during {pipeline_name} pipeline cleanup: {str(e)}")
            if self._signal_manager:
                await self._signal_manager.emit(Signal(
                    type=SignalType.RAG_CLEANUP_ERROR,
                    data={
                        'component': pipeline_name,
                        'error': str(e)
                    }
                ))
            raise

    async def _cleanup_core_pipeline(self) -> None:
        """Clean up core pipeline components."""
        await self._cleanup_pipeline('core', ['container', 'config_manager'])

    async def _schedule_rag_pipeline(self) -> None:
        """Schedule RAG system components with optimized timing and concurrency."""
        try:
            # Get required subgraph
            rag = subgraph_manager.get_subgraph("rag")
            if not rag:
                raise ComponentNotFoundError("Required rag subgraph not found")

            # Prepare components with their metadata
            components = []
            for component_name, timing_config in self.rag_timing_configs.items():
                # Register with orchestrator
                await orchestrator.register_handler(component_name, timing_config)

                # Create component metadata
                metadata = ComponentMetadata(
                    type=ComponentType.RAG,
                    provides=self._get_component_provides(component_name),
                    dependencies=self._get_component_dependencies(component_name),
                    priority=timing_config.priority,
                    health_checks={'health_check': self._get_component_health_check(component_name)},
                    timing_config=timing_config,
                    cleanup_priority=85,
                    tags={'rag', component_name.split('_')[0]},
                    is_critical=True
                )
                
                components.append((component_name, metadata))

            # Schedule all components using the common pipeline scheduler
            await self._schedule_pipeline_components('rag', components)

            # Register cleanup tasks with compactor
            from tenire.organizers.compactor import compactor
            compactor.register_cleanup_task(
                name="rag_pipeline_cleanup",
                cleanup_func=self._cleanup_rag_pipeline,
                priority=85,
                is_async=True,
                metadata={"tags": ["rag", "cleanup"]}
            )

            # Register with monitor
            monitor.register_component("document_store")
            monitor.register_component("embeddings_manager")
            monitor.register_component("gambling_data_retriever")

            # Register signal handlers
            await self._register_rag_signal_handlers()

            # Emit pipeline initialized signal
            if self._signal_manager:
                await self._signal_manager.emit(Signal(
                    type=SignalType.RAG_COMPONENT_INITIALIZED,
                    data={
                        'component': 'rag_pipeline',
                        'num_components': 3,
                        'components': ['document_store', 'embeddings_manager', 'gambling_data_retriever']
                    }
                ))

            logger.info("Scheduled RAG pipeline components with optimized timing")

        except Exception as e:
            logger.error(f"Error scheduling RAG pipeline: {str(e)}")
            raise ComponentRegistryError(f"Failed to schedule RAG pipeline: {str(e)}")

    def _get_component_dependencies(self, component_name: str) -> Set[str]:
        """Get dependencies for a RAG component."""
        base_deps = {"signal_manager", "concurrency_manager"}
        if component_name == "document_store":
            return base_deps
        elif component_name == "embeddings_manager":
            return base_deps | {"document_store"}
        elif component_name == "gambling_data_retriever":
            return base_deps | {"document_store", "embeddings_manager", "data_manager"}
        return base_deps

    def _get_component_timeout(self, component_name: str) -> float:
        """Get initialization timeout for a RAG component."""
        if component_name == "gambling_data_retriever":
            return 45.0  # Longer timeout for LLM initialization
        return 30.0  # Default timeout

    def _get_component_health_check(self, component_name: str) -> Optional[Callable]:
        """Get health check function for a RAG component."""
        if component_name == "document_store":
            return self._check_docstore_health
        elif component_name == "embeddings_manager":
            return self._check_embedding_health
        elif component_name == "gambling_data_retriever":
            return self._check_retriever_health
        return None

    def _create_component_initializer(
        self,
        component_name: str,
        rag_config: RAGConfig,
        retriever_config: RetrieverConfig
    ) -> Callable:
        """Create initialization function for a RAG component."""
        if component_name == "document_store":
            return lambda: DocumentStore(rag_config)
        elif component_name == "embeddings_manager":
            return lambda: LocalEmbeddings(rag_config)
        elif component_name == "gambling_data_retriever":
            return lambda: GamblingDataRetriever(
                config=retriever_config,
                document_store=container.get('document_store'),
                data_manager=container.get('data_manager')
            )
        raise ValueError(f"Unknown component: {component_name}")

    async def _cleanup_rag_pipeline(self) -> None:
        """Clean up RAG pipeline components with proper resource management."""
        try:
            logger.info("Starting RAG pipeline cleanup")

            # Get compactor
            from tenire.organizers.compactor import compactor
            if not compactor:
                raise ComponentRegistryError("Compactor not available")

            # Clean up components in reverse initialization order
            components_to_cleanup = [
                "gambling_data_retriever",  # Clean up highest level first
                "embeddings_manager",
                "document_store"
            ]

            for component_name in components_to_cleanup:
                try:
                    # Get component instance
                    instance = container.get(component_name)
                    if not instance:
                        continue

                    # Perform component cleanup
                    await self._cleanup_component(component_name, instance)

                    # Unregister from orchestrator
                    await orchestrator.unregister_handler(component_name)

                    # Unregister from monitor
                    monitor.unregister_component(component_name)

                    # Force garbage collection for the component
                    compactor.force_garbage_collection()

                except Exception as e:
                    logger.error(f"Error cleaning up {component_name}: {str(e)}")
                    if self._signal_manager:
                        await self._signal_manager.emit(Signal(
                            type=SignalType.RAG_CLEANUP_ERROR,
                            data={
                                'component': component_name,
                                'error': str(e)
                            }
                        ))

            # Emit cleanup completed signal
            if self._signal_manager:
                await self._signal_manager.emit(Signal(
                    type=SignalType.RAG_CLEANUP_COMPLETED,
                    data={
                        'component': 'rag_pipeline',
                        'components_cleaned': components_to_cleanup
                    }
                ))

            logger.info("RAG pipeline cleanup completed")

        except Exception as e:
            logger.error(f"Error during RAG pipeline cleanup: {str(e)}")
            if self._signal_manager:
                await self._signal_manager.emit(Signal(
                    type=SignalType.RAG_CLEANUP_ERROR,
                    data={
                        'component': 'rag_pipeline',
                        'error': str(e)
                    }
                ))
            raise

    async def _cleanup_professional_pipeline(self) -> None:
        """Clean up professional pipeline components."""
        await self._cleanup_pipeline('professional', ['bet_analyzer'])

    async def _cleanup_servicer_pipeline(self) -> None:
        """Clean up servicer pipeline components."""
        await self._cleanup_pipeline('servicer', ['agent_manager', 'bet_manager'])

    async def _cleanup_actions_pipeline(self) -> None:
        """Clean up actions pipeline components."""
        await self._cleanup_pipeline('actions', ['command_processor', 'betting_controller'])

    async def _cleanup_public_service_pipeline(self) -> None:
        """Clean up public service pipeline components."""
        await self._cleanup_pipeline('public_service', ['component_doctor'])

    async def _check_cache_storage(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check model cache storage health."""
        try:
            model_cache = container.get('model_cache')
            if not model_cache:
                return (
                    HealthStatus.DEGRADED,
                    "Model cache not available",
                    {}
                )

            stats = model_cache.stats.to_dict()
            return (
                HealthStatus.HEALTHY,
                "Model cache storage operational",
                stats
            )
        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking model cache storage: {str(e)}",
                {'error': str(e)}
            )

    async def _check_cached_models(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check cached models health."""
        try:
            model_cache = container.get('model_cache')
            if not model_cache:
                return (
                    HealthStatus.DEGRADED,
                    "Model cache not available",
                    {}
                )

            model_info = await model_cache.get_model_info()
            return (
                HealthStatus.HEALTHY,
                f"Found {model_info['total_models']} cached models",
                model_info
            )
        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking cached models: {str(e)}",
                {'error': str(e)}
            )

    async def _check_bet_cache(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check bet cache health."""
        try:
            bet_cache = container.get('bet_cache')
            if not bet_cache:
                return (
                    HealthStatus.DEGRADED,
                    "Bet cache not available",
                    {}
                )

            stats = bet_cache.stats.to_dict()
            return (
                HealthStatus.HEALTHY,
                "Bet cache operational",
                stats
            )
        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking bet cache: {str(e)}",
                {'error': str(e)}
            )

    async def _check_bet_patterns(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check bet pattern analysis health."""
        try:
            bet_cache = container.get('bet_cache')
            if not bet_cache:
                return (
                    HealthStatus.DEGRADED,
                    "Bet cache not available",
                    {}
                )

            pattern_info = {
                'patterns': bet_cache._bet_patterns,
                'user_stats': bet_cache._user_stats
            }
            return (
                HealthStatus.HEALTHY,
                "Bet pattern analysis operational",
                pattern_info
            )
        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking bet patterns: {str(e)}",
                {'error': str(e)}
            )

    async def _check_embedding_cache(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check embedding cache health."""
        try:
            embedding_cache = container.get('embedding_cache')
            if not embedding_cache:
                return (
                    HealthStatus.DEGRADED,
                    "Embedding cache not available",
                    {}
                )

            stats = embedding_cache.stats.to_dict()
            return (
                HealthStatus.HEALTHY,
                "Embedding cache operational",
                stats
            )
        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking embedding cache: {str(e)}",
                {'error': str(e)}
            )

    async def _check_vector_storage(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check vector storage health."""
        try:
            embedding_cache = container.get('embedding_cache')
            if not embedding_cache:
                return (
                    HealthStatus.DEGRADED,
                    "Embedding cache not available",
                    {}
                )

            # Get sample query to test vector operations
            test_query = np.random.rand(embedding_cache.embedding_dim)
            similar = await embedding_cache.get_similar_embeddings(test_query, k=1)
            
            return (
                HealthStatus.HEALTHY,
                "Vector storage operational",
                {'vector_ops': 'functional', 'similar_count': len(similar)}
            )
        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking vector storage: {str(e)}",
                {'error': str(e)}
            )

    async def _initialize_monitor(self) -> None:
        """Initialize monitoring system with proper integration."""
        try:
            # Start monitor if not already running
            if not monitor.is_monitoring:
                monitor.start()
                
            # Register core components for monitoring
            core_components = {
                'signal_manager', 'container', 'config_manager',
                'pipeline_registry', 'thread_pool', 'orchestrator'
            }
            for component in core_components:
                monitor.register_component(component)
                
            # Register with compactor
            from tenire.organizers.compactor import compactor
            compactor.register_cleanup_task(
                name="monitor_cleanup",
                cleanup_func=self._cleanup_monitor,
                priority=95,
                is_async=True,
                metadata={"tags": ["monitoring", "cleanup"]}
            )
            
            logger.info("Monitoring system initialized")
            
        except Exception as e:
            logger.error(f"Error initializing monitor: {str(e)}")
            raise

    async def _cleanup_monitor(self) -> None:
        """Clean up monitoring system."""
        try:
            if monitor.is_monitoring:
                monitor.stop()
                monitor.clear_metrics()
            logger.info("Monitoring system cleaned up")
        except Exception as e:
            logger.error(f"Error cleaning up monitor: {str(e)}")

    async def _check_docstore_health(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check document store health."""
        try:
            doc_store = container.get('document_store')
            if not doc_store:
                return (
                    HealthStatus.DEGRADED,
                    "Document store not available",
                    {}
                )

            stats = {
                'num_documents': len(doc_store.documents),
                'num_chunks': len(doc_store.chunks),
                'index_type': type(doc_store.index).__name__
            }
            return (
                HealthStatus.HEALTHY,
                "Document store operational",
                stats
            )
        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking document store health: {str(e)}",
                {'error': str(e)}
            )

    async def _check_embedding_health(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check embeddings health."""
        try:
            embeddings = container.get('embeddings_manager')
            if not embeddings:
                return (
                    HealthStatus.DEGRADED,
                    "Embeddings manager not available",
                    {}
                )

            # Test embedding generation
            test_text = "Test embedding generation"
            embedding_result = await embeddings.embed_query(test_text)
            
            stats = {
                'model_type': embeddings.model_type,
                'device': str(embeddings.device),
                'use_tfidf': embeddings.use_tfidf,
                'chunk_fields': embeddings.chunk_fields,
                'vector_dim': embedding_result['vector'].shape[0] if 'vector' in embedding_result else None
            }
            return (
                HealthStatus.HEALTHY,
                "Embeddings manager operational",
                stats
            )
        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking embeddings health: {str(e)}",
                {'error': str(e)}
            )

    async def _check_retriever_health(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check retriever health."""
        try:
            retriever = container.get('gambling_data_retriever')
            if not retriever:
                return (
                    HealthStatus.DEGRADED,
                    "Retriever not available",
                    {}
                )

            stats = {
                'initial_k': retriever.initial_k,
                'final_k': retriever.final_k,
                'has_document_store': retriever.document_store is not None,
                'has_data_manager': retriever.data_manager is not None,
                'has_cross_encoder': retriever.cross_encoder is not None
            }
            return (
                HealthStatus.HEALTHY,
                "Retriever operational",
                stats
            )
        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking retriever health: {str(e)}",
                {'error': str(e)}
            )

    async def _register_rag_signal_handlers(self) -> None:
        """Register signal handlers for RAG components."""
        try:
            # Get signal manager
            signal_manager = container.get('signal_manager')
            if not signal_manager:
                logger.error("Signal manager not available for RAG signal handlers")
                return

            # Document store signals
            signal_manager.register_handler(
                SignalType.RAG_DOCUMENT_ADDED,
                lambda signal: container.get('document_store')._handle_document_added(signal),
                priority=85
            )
            signal_manager.register_handler(
                SignalType.RAG_CLEANUP_STARTED,
                lambda signal: container.get('document_store')._handle_cleanup_started(signal),
                priority=85
            )

            # Embeddings signals
            signal_manager.register_handler(
                SignalType.RAG_EMBEDDING_STARTED,
                lambda signal: container.get('embeddings_manager')._handle_embedding_started(signal),
                priority=80
            )
            signal_manager.register_handler(
                SignalType.RAG_CLEANUP_STARTED,
                lambda signal: container.get('embeddings_manager')._handle_cleanup_started(signal),
                priority=80
            )

            # Retriever signals
            signal_manager.register_handler(
                SignalType.RAG_QUERY_STARTED,
                lambda signal: container.get('gambling_data_retriever')._handle_query_started(signal),
                priority=75
            )
            signal_manager.register_handler(
                SignalType.RAG_DOCUMENT_ADDED,
                lambda signal: container.get('gambling_data_retriever')._handle_document_added(signal),
                priority=75
            )

            logger.info("Registered RAG signal handlers")

        except Exception as e:
            logger.error(f"Error registering RAG signal handlers: {str(e)}")
            raise ComponentRegistryError(f"Failed to register RAG signal handlers: {str(e)}")

    async def _schedule_data_flow_pipeline(self) -> None:
        """Schedule data flow pipeline components."""
        try:
            # Register data flow agent first as it orchestrates data flow
            await self.register_component(
                name="data_flow_agent",
                component_type=ComponentType.DATA,
                provides=["data_flow_orchestration", "data_flow_monitoring"],
                dependencies={"signal_manager", "compactor", "data_sieve", "data_manager"},
                priority=95,  # High priority as it manages data flow
                health_checks={
                    "route_health": self._check_route_health,
                    "stream_health": self._check_stream_health,
                    "flow_metrics": self._check_flow_metrics
                },
                timing_config=TimingConfig(
                    min_interval=0.1,
                    max_interval=1.0,
                    burst_limit=50,
                    cooldown=0.5,
                    priority=95
                ),
                cleanup_priority=95,
                tags={"data_flow", "monitoring", "orchestration"},
                is_critical=True
            )

            # Register data sieve after agent
            await self.register_component(
                name="data_sieve",
                component_type=ComponentType.DATA,
                provides=["data_orchestration", "data_management"],
                dependencies={"signal_manager", "compactor", "data_flow_agent"},
                priority=90,
                health_checks={
                    "cache_storage": self._check_cache_storage,
                    "embedding_cache": self._check_embedding_cache,
                    "vector_storage": self._check_vector_storage
                },
                timing_config=self._get_default_timing_config(ComponentType.DATA),
                cleanup_priority=90,
                tags={"data", "orchestration", "core"},
                is_critical=True
            )

            logger.info("Data flow pipeline components scheduled successfully")

        except Exception as e:
            logger.error(f"Error scheduling data flow pipeline: {str(e)}")
            raise

    async def _check_route_health(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check health of data flow routes."""
        try:
            data_flow_agent = container.get('data_flow_agent')
            if not data_flow_agent:
                return (
                    HealthStatus.DEGRADED,
                    "Data flow agent not available",
                    {}
                )

            route_stats = {
                route_id: {
                    'status': route.metrics['status'],
                    'error_rate': route.metrics['errors'] / max(route.metrics['processed'], 1),
                    'queue_size': route.queue.qsize(),
                    'last_processed': route.metrics['last_processed'].isoformat() if route.metrics['last_processed'] else None
                }
                for route_id, route in data_flow_agent.seive.routes.items()
            }

            # Check for unhealthy routes
            unhealthy_routes = [
                route_id for route_id, stats in route_stats.items()
                if stats['error_rate'] > 0.1 or stats['queue_size'] > 1000
            ]

            if unhealthy_routes:
                return (
                    HealthStatus.DEGRADED,
                    f"Found {len(unhealthy_routes)} unhealthy routes",
                    {'route_stats': route_stats, 'unhealthy_routes': unhealthy_routes}
                )

            return (
                HealthStatus.HEALTHY,
                f"All {len(route_stats)} routes healthy",
                {'route_stats': route_stats}
            )

        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking route health: {str(e)}",
                {'error': str(e)}
            )

    async def _check_stream_health(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check health of data flow streams."""
        try:
            data_flow_agent = container.get('data_flow_agent')
            if not data_flow_agent:
                return (
                    HealthStatus.DEGRADED,
                    "Data flow agent not available",
                    {}
                )

            stream_stats = {
                stream_name: {
                    'status': stream.is_active,
                    'buffer_usage': stream.buffer.qsize() / stream.max_buffer,
                    'cleaning_stage': stream.current_stage.value if stream.cleaning_required else None
                }
                for stream_name, stream in data_flow_agent.seive.streams.items()
            }

            # Check for streams with high buffer usage
            high_buffer_streams = [
                stream_name for stream_name, stats in stream_stats.items()
                if stats['buffer_usage'] > 0.9
            ]

            if high_buffer_streams:
                return (
                    HealthStatus.DEGRADED,
                    f"Found {len(high_buffer_streams)} streams with high buffer usage",
                    {'stream_stats': stream_stats, 'high_buffer_streams': high_buffer_streams}
                )

            return (
                HealthStatus.HEALTHY,
                f"All {len(stream_stats)} streams healthy",
                {'stream_stats': stream_stats}
            )

        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking stream health: {str(e)}",
                {'error': str(e)}
            )

    async def _check_flow_metrics(self) -> Tuple[HealthStatus, str, Dict[str, Any]]:
        """Check data flow metrics."""
        try:
            data_flow_agent = container.get('data_flow_agent')
            if not data_flow_agent:
                return (
                    HealthStatus.DEGRADED,
                    "Data flow agent not available",
                    {}
                )

            metrics = {
                'total_processed': sum(route.metrics['processed'] for route in data_flow_agent.seive.routes.values()),
                'total_errors': sum(route.metrics['errors'] for route in data_flow_agent.seive.routes.values()),
                'avg_latency': sum(
                    route.metrics['latency']
                    for route in data_flow_agent.seive.routes.values()
                    if route.metrics['latency'] is not None
                ) / len(data_flow_agent.seive.routes) if data_flow_agent.seive.routes else 0.0,
                'active_routes': sum(1 for route in data_flow_agent.seive.routes.values() if route.is_active),
                'active_streams': sum(1 for stream in data_flow_agent.seive.streams.values() if stream.is_active)
            }

            # Check for concerning metrics
            if metrics['total_errors'] > metrics['total_processed'] * 0.1:  # More than 10% error rate
                return (
                    HealthStatus.DEGRADED,
                    f"High error rate: {metrics['total_errors']}/{metrics['total_processed']}",
                    {'metrics': metrics}
                )

            if metrics['avg_latency'] > 1.0:  # Average latency > 1 second
                return (
                    HealthStatus.DEGRADED,
                    f"High average latency: {metrics['avg_latency']:.2f}s",
                    {'metrics': metrics}
                )

            return (
                HealthStatus.HEALTHY,
                "Data flow metrics within acceptable ranges",
                {'metrics': metrics}
            )

        except Exception as e:
            return (
                HealthStatus.UNHEALTHY,
                f"Error checking flow metrics: {str(e)}",
                {'error': str(e)}
            )

    async def _cleanup_data_flow_pipeline(self) -> None:
        """Clean up data flow pipeline components."""
        try:
            # Clean up data flow agent first
            data_flow_agent = container.get('data_flow_agent')
            if data_flow_agent:
                await data_flow_agent.stop()
                await data_flow_agent.cleanup()

            # Clean up data sieve
            data_sieve = container.get('data_sieve')
            if data_sieve:
                await data_sieve.cleanup()

            logger.info("Data flow pipeline cleanup completed successfully")

        except Exception as e:
            logger.error(f"Error during data flow pipeline cleanup: {str(e)}")
            raise

    async def _schedule_pipeline_components(self, pipeline_name: str, components: List[Tuple[str, ComponentMetadata]]) -> None:
        """Schedule a group of pipeline components with proper batch handling."""
        try:
            # Let component scheduler handle the batching
            for name, metadata in components:
                await self._schedule_component(name, metadata)
                
            # Wait for all components to be initialized
            if not await component_scheduler.wait_for_initialization(timeout=60.0):
                failed = component_scheduler.get_failed_components()
                if failed:
                    raise ComponentRegistryError(f"Failed to initialize components: {failed}")
                
            logger.info(f"Scheduled {pipeline_name} components successfully")
            
        except Exception as e:
            logger.error(f"Error scheduling {pipeline_name} pipeline: {str(e)}")
            raise ComponentRegistryError(f"Failed to schedule {pipeline_name} pipeline: {str(e)}")

# Create global instance
pipeline_registry = PipelineRegistry()

async def initialize_pipeline_registry() -> None:
    """Initialize the pipeline registry with proper integration."""
    try:
        # Ensure config is initialized first
        if not config_manager._initialized:
            config_manager.initialize()
            logger.debug("Configuration initialized")
            
        # Initialize dependency graph
        await initialize_dependency_graph()
        logger.debug("Dependency graph initialized")
        
        # Create and initialize pipeline registry
        await register_all_pipelines()
        logger.info("Pipeline registry initialized successfully")
        
    except Exception as e:
        logger.error(f"Error initializing pipeline registry: {str(e)}")
        raise ComponentRegistryError(f"Failed to initialize pipeline registry: {str(e)}")

async def register_all_pipelines() -> None:
    """Register all system pipelines with proper integration."""
    try:
        # Schedule individual pipelines first
        await pipeline_registry._schedule_core_pipeline()
        await pipeline_registry._schedule_data_pipeline()
        await pipeline_registry._schedule_gui_pipeline()
        await pipeline_registry._schedule_integration_pipeline()
        await pipeline_registry._schedule_organizer_pipeline()
        await pipeline_registry._schedule_professional_pipeline()
        await pipeline_registry._schedule_public_service_pipeline()
        await pipeline_registry._schedule_rag_pipeline()
        await pipeline_registry._schedule_servicer_pipeline()
        await pipeline_registry._schedule_actions_pipeline()
        await pipeline_registry._schedule_data_flow_pipeline()
        
        # Then register all components
        await pipeline_registry.register_pipelines()
    except Exception as e:
        logger.error(f"Error registering pipelines: {str(e)}")
        raise ComponentRegistryError(f"Failed to register pipelines: {str(e)}")
    
# Export for module access
__all__ = [
    'ComponentType',
    'ComponentMetadata',
    'ComponentLocks',
    'BatchGroup',
    'ComponentRegistryError',
    'ComponentNotFoundError',
    'DependencyError',
    'PipelineRegistry',
    'register_all_pipelines',
    'initialize_pipeline_registry',
    'pipeline_registry'
] 