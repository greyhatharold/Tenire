"""
Component registry module for managing framework components.

This module provides a registry for framework components with proper initialization
and dependency management.
"""

import asyncio
import enum
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from collections import defaultdict

from tenire.core.config import config_manager
from tenire.core.container import container
from tenire.core.codex import TimingConfig
from tenire.structure.component_types import ComponentType
from tenire.structure.dependency_graph import initialize_dependency_graph, dependency_graph
from tenire.servicers.signal import SignalType, Signal
from tenire.organizers.concurrency import concurrency_manager
from tenire.structure.component_scheduler import component_scheduler, InitializationTask
from tenire.utils.logger import get_logger
from tenire.core.event_loop import event_loop_manager

logger = get_logger(__name__)

@dataclass
class InitializationState:
    """Tracks the initialization state of the registry."""
    started: bool = False
    dependencies_initialized: bool = False
    orchestrator_initialized: bool = False
    components_registered: bool = False
    error: Optional[str] = None

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
    initialized: bool = field(default=False)  # Track if component is initialized

class ComponentRegistryError(Exception):
    """Base exception for component registry errors."""

class ComponentNotFoundError(ComponentRegistryError):
    """Raised when a component is not found."""

class DependencyError(ComponentRegistryError):
    """Raised when there are dependency issues."""

class ComponentRegistry:
    """Registry for framework components with improved initialization."""
    
    def __init__(self) -> None:
        """Initialize the registry with optimized caching and batching."""
        self._components = {}
        self._batch_size = 10
        self._pending_registrations = []
        self._registration_lock = asyncio.Lock()
        self._cache = {}  # Simple in-memory cache
        self._initialized = False
        self._logger = get_logger(__name__)
        self._init_lock = asyncio.Lock()
        self._initialization_state = InitializationState()
        self._batch_groups = defaultdict(list)
        self._health_coordinator = None
        self._orchestrator = None
        self._signal_manager = None
        self._compactor = None
        
    async def initialize(self) -> bool:
        """Initialize the component registry with timeout and error handling."""
        if self._initialized:
            return True
            
        try:
            async with self._init_lock:
                if self._initialized:  # Double-check pattern
                    return True
                    
                self._logger.info("Initializing component registry")
                
                try:
                    async with asyncio.timeout(30):  # 30 second timeout
                        # Ensure event loop is initialized first
                        if not await event_loop_manager.ensure_initialized():
                            raise ComponentRegistryError("Event loop not initialized")
                            
                        # Ensure concurrency manager is initialized
                        await concurrency_manager.ensure_initialized()
                        
                        # Initialize dependency graph
                        await initialize_dependency_graph()
                        
                        # Register with container
                        await container.register(
                            'component_registry',
                            self,
                            dependencies={'dependency_graph', 'concurrency_manager'}
                        )
                        
                        # Initialize health coordinator if available
                        await self._ensure_health_coordinator()
                        
                        # Initialize orchestrator if available
                        await self._ensure_orchestrator()
                        
                        # Process any pending registrations
                        await self._process_pending_registrations()
                        
                        self._initialized = True
                        self._logger.info("Component registry initialized successfully")
                        return True
                        
                except asyncio.TimeoutError:
                    self._logger.error("Timeout initializing component registry")
                    return False
                    
        except Exception as e:
            self._logger.error(f"Error initializing component registry: {str(e)}")
            return False
            
    async def _ensure_health_coordinator(self) -> None:
        """Ensure health coordinator is initialized."""
        if self._health_coordinator is None:
            try:
                from tenire.public_services.hospital import health_coordinator
                self._health_coordinator = health_coordinator
            except ImportError:
                self._logger.debug("Health coordinator not available")
                
    async def _ensure_orchestrator(self) -> None:
        """Ensure orchestrator is initialized."""
        if self._orchestrator is None:
            try:
                from tenire.organizers.scheduler import orchestrator
                self._orchestrator = orchestrator
            except ImportError:
                self._logger.debug("Orchestrator not available")
                
    async def _process_pending_registrations(self) -> None:
        """Process pending registrations in batches using concurrency manager."""
        if not self._pending_registrations:
            return
            
        try:
            # Group registrations by component type
            for registration in self._pending_registrations:
                component_type = registration.get('component_type')
                if component_type:
                    self._batch_groups[component_type].append(registration)
                    
            # Process batches concurrently
            tasks = []
            for component_type, batch in self._batch_groups.items():
                for i in range(0, len(batch), self._batch_size):
                    batch_slice = batch[i:i + self._batch_size]
                    task = concurrency_manager.async_tasks.create_task(
                        self._process_registration_batch(batch_slice),
                        task_id=f"register_batch_{component_type}_{i}"
                    )
                    tasks.append(task)
                    
            # Wait for all batches to complete
            if tasks:
                await asyncio.gather(*tasks)
                
        except Exception as e:
            self._logger.error(f"Error processing pending registrations: {str(e)}")
        finally:
            # Clear processed registrations
            self._pending_registrations.clear()
            self._batch_groups.clear()
            
    async def _process_registration_batch(self, batch: List[dict]) -> None:
        """Process a batch of registrations concurrently."""
        try:
            # Register components in parallel
            tasks = []
            for registration in batch:
                task = concurrency_manager.async_tasks.create_task(
                    self._register_single_component(registration),
                    task_id=f"register_{registration['name']}"
                )
                tasks.append(task)
                
            # Wait for all registrations in batch
            if tasks:
                await asyncio.gather(*tasks)
                
        except Exception as e:
            self._logger.error(f"Error processing registration batch: {str(e)}")
            
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
                async with self._init_lock:
                    await self._initialize_dependencies()
            except Exception as e:
                self._logger.error(f"Failed to initialize dependencies for {name}: {str(e)}")
                raise ComponentRegistryError(f"Initialization failed for {name}: {str(e)}")
            
        # Special handling for async_task_manager
        if name == "async_task_manager":
            # Ensure only event_loop_manager dependency
            dependencies = {"event_loop_manager"}
            priority = 900  # High priority after event_loop_manager
            is_critical = True
            if not timing_config:
                timing_config = TimingConfig(
                    min_interval=0.01,
                    max_interval=0.1,
                    burst_limit=100,
                    cooldown=0.2,
                    priority=priority
                )
            
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
            
            self._logger.debug(f"Registered component: {name}")
            
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
                self._logger.error(f"Failed to register {name} with dependency graph: {str(e)}")
                raise ComponentRegistryError(f"Dependency graph registration failed for {name}: {str(e)}")
            
            # Register with health coordinator if health checks provided
            if health_checks and self._health_coordinator:
                try:
                    await concurrency_manager.async_tasks.create_task(
                        self._health_coordinator.register_component(
                            name,
                            health_checks,
                            is_critical=is_critical
                        ),
                        task_id=f"register_health_{name}"
                    )
                except Exception as e:
                    self._logger.warning(f"Failed to register health checks for {name}: {str(e)}")
                    # Continue as health monitoring is not critical
            
            # Register timing config with orchestrator
            if timing_config is None:
                # Use default timing config for component type
                timing_config = self._get_default_timing_config(component_type)
                
            if timing_config and self._orchestrator:
                try:
                    await concurrency_manager.async_tasks.create_task(
                        self._orchestrator.register_handler(
                            name,
                            timing_config
                        ),
                        task_id=f"register_timing_{name}"
                    )
                except Exception as e:
                    self._logger.warning(f"Failed to register timing config for {name}: {str(e)}")
                    # Continue as timing is not critical
                
            # Register with monitor using thread pool
            try:
                await concurrency_manager.thread_pool.run_in_thread(
                    lambda: container.get('monitor').register_component(name)
                )
            except Exception as e:
                self._logger.warning(f"Failed to register {name} with monitor: {str(e)}")
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
                
                await concurrency_manager.async_tasks.create_task(
                    component_scheduler.schedule_component(init_task),
                    task_id=f"schedule_init_{name}"
                )
            except Exception as e:
                self._logger.error(f"Failed to schedule initialization for {name}: {str(e)}")
                raise ComponentRegistryError(f"Initialization scheduling failed for {name}: {str(e)}")
                
            # Emit registration signal using concurrency manager
            if self._signal_manager:
                try:
                    await concurrency_manager.async_tasks.create_task(
                        self._signal_manager.emit(Signal(
                            type=SignalType.COMPONENT_REGISTERED,
                            data={
                                'component': name,
                                'type': component_type.value,
                                'priority': priority,
                                'is_critical': is_critical,
                                'timing_config': timing_config._asdict() if timing_config else None
                            }
                        )),
                        task_id=f"emit_signal_{name}"
                    )
                except Exception as e:
                    self._logger.warning(f"Failed to emit registration signal for {name}: {str(e)}")
                    # Continue as signal emission is not critical
                    
        except Exception as e:
            self._logger.error(f"Component registration failed for {name}: {str(e)}")
            # Clean up any partial registration
            await self._cleanup_failed_registration(name)
            raise ComponentRegistryError(f"Registration failed for {name}: {str(e)}")
            
    async def _cleanup_failed_registration(self, name: str) -> None:
        """Clean up after failed component registration."""
        try:
            # Remove from components dict
            self._components.pop(name, None)
            
            # Remove from cache
            await self._cache_operation('delete', f"metadata:{name}")
            
            # Remove from dependency graph
            try:
                dependency_graph.remove_node(name)
            except Exception as e:
                self._logger.warning(f"Failed to remove {name} from dependency graph: {str(e)}")
                
            # Remove from health coordinator
            if self._health_coordinator:
                try:
                    await self._health_coordinator.unregister_component(name)
                except Exception as e:
                    self._logger.warning(f"Failed to unregister health checks for {name}: {str(e)}")
                    
            # Remove from orchestrator
            if self._orchestrator:
                try:
                    await self._orchestrator.unregister_handler(name)
                except Exception as e:
                    self._logger.warning(f"Failed to unregister timing config for {name}: {str(e)}")
                    
            # Remove from monitor
            try:
                await concurrency_manager.thread_pool.run_in_thread(
                    lambda: container.get('monitor').unregister_component(name)
                )
            except Exception as e:
                self._logger.warning(f"Failed to unregister {name} from monitor: {str(e)}")
                
        except Exception as e:
            self._logger.error(f"Error cleaning up failed registration for {name}: {str(e)}")
            
    def _get_default_timing_config(self, component_type: ComponentType) -> Optional[TimingConfig]:
        """Get default timing configuration for a component type."""
        defaults = {
            ComponentType.CORE: TimingConfig(
                min_interval=0.01,
                max_interval=0.1,
                burst_limit=100,
                cooldown=0.5,
                priority=90
            ),
            ComponentType.SERVICE: TimingConfig(
                min_interval=0.05,
                max_interval=0.5,
                burst_limit=50,
                cooldown=1.0,
                priority=70
            ),
            ComponentType.INTEGRATION: TimingConfig(
                min_interval=0.1,
                max_interval=1.0,
                burst_limit=20,
                cooldown=2.0,
                priority=50
            )
        }
        return defaults.get(component_type)

# Create global instances
component_registry = ComponentRegistry()

# Define PipelineRegistry as a proper class inheriting from ComponentRegistry
class PipelineRegistry(ComponentRegistry):
    """Pipeline-specific component registry with backward compatibility.
    
    This class extends ComponentRegistry to maintain backward compatibility
    with existing pipeline-based code while providing all the functionality
    of the base registry.
    """
    def __init__(self) -> None:
        """Initialize pipeline registry using the global component registry instance."""
        # Share the same instance as component_registry for singleton behavior
        self.__dict__ = component_registry.__dict__

# Create singleton instance
pipeline_registry = PipelineRegistry()

async def initialize_registry() -> None:
    """Initialize the component registry with proper integration."""
    try:
        # Ensure config is initialized first
        if not config_manager._initialized:
            config_manager.initialize()
            logger.debug("Configuration initialized")
            
        # Initialize dependency graph
        await initialize_dependency_graph()
        logger.debug("Dependency graph initialized")
        
        # Initialize component registry with concurrency
        if not await component_registry.initialize():
            raise ComponentRegistryError("Failed to initialize component registry")
            
        logger.info("Component registry initialized successfully")
        
    except Exception as e:
        logger.error(f"Error initializing component registry: {str(e)}")
        raise ComponentRegistryError(f"Failed to initialize component registry: {str(e)}")

# Alias for backward compatibility
initialize_pipeline_registry = initialize_registry

__all__ = [
    'ComponentRegistry',
    'ComponentMetadata',
    'ComponentType',
    'InitializationState',
    'ComponentRegistryError',
    'ComponentNotFoundError',
    'DependencyError',
    'component_registry',
    'PipelineRegistry',  # Add PipelineRegistry class
    'pipeline_registry',
    'initialize_registry',
    'initialize_pipeline_registry'
] 