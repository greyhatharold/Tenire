"""
Global container module for the Tenire framework.

This module provides centralized access to global instances and manages their
initialization order to prevent circular dependencies through lazy loading
and dependency tracking.
"""

import asyncio
import threading
import weakref
from typing import Dict, Any, Optional, Set, Callable, List
from dataclasses import dataclass, field

from tenire.utils.logger import get_logger

logger = get_logger(__name__)

async def _get_dependency_graph():
    """Lazy import and get dependency graph to avoid circular dependencies."""
    from tenire.structure.dependency_graph import dependency_graph
    return dependency_graph

@dataclass
class ComponentMetadata:
    """Metadata for a container component."""
    name: str
    dependencies: Set[str] = field(default_factory=set)
    is_initialized: bool = False
    is_initializing: bool = False
    lazy_init_fn: Optional[Callable[[], Any]] = None
    instance: Optional[Any] = None
    error: Optional[Exception] = None
    is_registered: bool = False

class GlobalContainer:
    """
    Enhanced container for managing global framework instances.
    
    Features:
    - Thread-safe singleton pattern
    - Lazy loading of components
    - Circular dependency detection and resolution
    - Weak references to allow garbage collection
    - Dependency tracking and validation
    - Error recovery and retry mechanisms
    """
    
    _instance = None
    _lock = threading.RLock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(GlobalContainer, cls).__new__(cls)
            return cls._instance
    
    def __init__(self):
        with self._lock:
            if not hasattr(self, '_initialized'):
                self._initialized = True
                self._components: Dict[str, ComponentMetadata] = {}
                self._weak_refs: Dict[str, weakref.ref] = {}
                self._init_error_counts: Dict[str, int] = {}
                self._max_retries = 3
                self._event_loop = None
                logger.debug("Initialized GlobalContainer")

    def _ensure_event_loop(self) -> asyncio.AbstractEventLoop:
        """Ensure an event loop exists and is running."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            return loop
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            return loop

    async def register(self, name: str, instance: Any, dependencies: Optional[Set[str]] = None) -> None:
        """
        Register a global instance with optional dependencies.
        
        Args:
            name: Component name
            instance: Component instance
            dependencies: Optional set of dependency component names
        """
        with self._lock:
            # Register with dependency graph
            dependency_graph = await _get_dependency_graph()
            dependency_graph.register_node(
                name=name,
                dependencies=dependencies or set(),
                metadata={'container_registered': True, 'instance_available': True}
            )
            
            # Update container metadata
            if name in self._components:
                metadata = self._components[name]
                metadata.instance = instance
                metadata.is_initialized = True
                metadata.is_registered = True
                metadata.dependencies.update(dependencies or set())
            else:
                metadata = ComponentMetadata(
                    name=name,
                    dependencies=dependencies or set(),
                    is_initialized=True,
                    instance=instance,
                    is_registered=True
                )
                self._components[name] = metadata
            
            # Store weak reference
            self._weak_refs[name] = weakref.ref(instance)
            
            logger.debug(f"Registered global instance: {name}")

    async def update_dependencies(self, name: str, dependencies: Set[str]) -> None:
        """
        Update dependencies for a registered component.
        
        Args:
            name: Component name
            dependencies: Set of dependency component names
        """
        with self._lock:
            if name not in self._components:
                raise ValueError(f"Component {name} not registered")
                
            # Update dependency graph
            dependency_graph = await _get_dependency_graph()
            node = dependency_graph.get_node(name)
            if node:
                node.dependencies.update(dependencies)
                
            # Update container metadata
            metadata = self._components[name]
            metadata.dependencies.update(dependencies)
            
            logger.debug(f"Updated dependencies for {name}: {dependencies}")

    async def register_lazy(
        self,
        name: str,
        init_fn: Callable[[], Any],
        dependencies: Optional[Set[str]] = None
    ) -> None:
        """
        Register a component for lazy initialization.
        
        Args:
            name: Component name
            init_fn: Initialization function
            dependencies: Optional set of dependency component names
        """
        with self._lock:
            # Register with dependency graph
            dependency_graph = await _get_dependency_graph()
            dependency_graph.register_node(
                name=name,
                dependencies=dependencies or set(),
                metadata={'container_registered': True, 'lazy': True}
            )
            
            # Update container metadata
            metadata = ComponentMetadata(
                name=name,
                dependencies=dependencies or set(),
                lazy_init_fn=init_fn,
                is_registered=True
            )
            self._components[name] = metadata
            logger.debug(f"Registered lazy component: {name}")

    def register_sync(self, name: str, instance: Any, dependencies: Optional[Set[str]] = None) -> None:
        """
        Synchronous version of register for non-async contexts.
        
        Args:
            name: Component name
            instance: Component instance
            dependencies: Optional set of dependency component names
        """
        try:
            loop = self._ensure_event_loop()
            if loop.is_running():
                loop.create_task(self.register(name, instance, dependencies))
            else:
                # Store registration for later if dependency graph isn't available
                with self._lock:
                    self._components[name] = ComponentMetadata(
                        name=name,
                        dependencies=dependencies or set(),
                        instance=instance,
                        is_initialized=True,
                        is_registered=True
                    )
                logger.debug(f"Stored registration for {name} until dependency graph is available")
        except Exception as e:
            logger.error(f"Error in register_sync for {name}: {str(e)}")
            # Store registration anyway
            with self._lock:
                self._components[name] = ComponentMetadata(
                    name=name,
                    dependencies=dependencies or set(),
                    instance=instance,
                    is_initialized=True,
                    is_registered=True
                )

    def register_lazy_sync(
        self,
        name: str,
        init_fn: Callable[[], Any],
        dependencies: Optional[Set[str]] = None
    ) -> None:
        """
        Synchronous version of register_lazy for non-async contexts.
        
        Args:
            name: Component name
            init_fn: Initialization function
            dependencies: Optional set of dependency component names
        """
        try:
            loop = self._ensure_event_loop()
            if loop.is_running():
                loop.create_task(self.register_lazy(name, init_fn, dependencies))
            else:
                # Store lazy registration for later
                with self._lock:
                    self._components[name] = ComponentMetadata(
                        name=name,
                        dependencies=dependencies or set(),
                        lazy_init_fn=init_fn,
                        is_registered=True
                    )
                logger.debug(f"Stored lazy registration for {name}")
        except Exception as e:
            logger.error(f"Error in register_lazy_sync for {name}: {str(e)}")
            # Store registration anyway
            with self._lock:
                self._components[name] = ComponentMetadata(
                    name=name,
                    dependencies=dependencies or set(),
                    lazy_init_fn=init_fn,
                    is_registered=True
                )

    def get(self, name: str, initialize: bool = True) -> Optional[Any]:
        """
        Get a global instance by name, optionally initializing it if needed.
        
        Args:
            name: Component name
            initialize: Whether to initialize the component if not already initialized
            
        Returns:
            Component instance or None if not found/initialization failed
        """
        with self._lock:
            if name not in self._components:
                return None
                
            metadata = self._components[name]
            
            # Return existing instance if initialized
            if metadata.is_initialized and metadata.instance is not None:
                return metadata.instance
                
            # Initialize if requested and not already initializing
            if initialize and not metadata.is_initializing:
                try:
                    loop = asyncio.get_event_loop()
                    loop.run_until_complete(self.ensure_initialized(name))
                    return metadata.instance
                except Exception as e:
                    logger.error(f"Error getting component {name}: {str(e)}")
                    return None
            
            return None

    def has(self, name: str) -> bool:
        """Check if a global instance exists and is registered."""
        with self._lock:
            return (name in self._components and 
                   self._components[name].is_registered)

    async def ensure_initialized(self, name: str) -> bool:
        """Ensure a component is initialized, handling dependencies."""
        with self._lock:
            if name not in self._components:
                logger.error(f"Component {name} not registered")
                return False
                
            metadata = self._components[name]
            if metadata.is_initialized:
                return True
                
            if metadata.is_initializing:
                logger.warning(f"Circular dependency detected for {name}")
                return False
                
            try:
                metadata.is_initializing = True
                
                # Initialize using dependency graph
                dependency_graph = await _get_dependency_graph()
                success = await dependency_graph.initialize_node(name)
                
                if success:
                    # Update container metadata
                    node = dependency_graph.get_node(name)
                    if node and node.instance:
                        metadata.instance = node.instance
                        metadata.is_initialized = True
                        self._weak_refs[name] = weakref.ref(node.instance)
                        
                return success
                
            except Exception as e:
                logger.error(f"Error initializing {name}: {str(e)}")
                return False
            finally:
                metadata.is_initializing = False

    async def cleanup(self) -> None:
        """Clean up the container and all components."""
        with self._lock:
            try:
                # Clean up dependency graph first
                dependency_graph = await _get_dependency_graph()
                await dependency_graph.cleanup()
                
                # Clear container state
                self._components.clear()
                self._weak_refs.clear()
                self._init_error_counts.clear()
                
                logger.debug("Cleaned up container")
                
            except Exception as e:
                logger.error(f"Error during container cleanup: {str(e)}")
                raise

# Create global instance
container = GlobalContainer() 