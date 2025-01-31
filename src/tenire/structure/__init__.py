"""
Structure package for the Tenire framework.

This package provides core structural components including:
- Dependency graph management
- Subgraph organization
- Component lifecycle management
- Component scheduling and initialization
"""

from tenire.structure.dependency_graph import (
    DependencyGraph,
    DependencyNode,
    DependencyState,
    dependency_graph
)

from tenire.structure.subgraph import (
    Subgraph,
    SubgraphType,
    SubgraphMetadata,
    SubgraphManager,
    subgraph_manager
)

from tenire.structure.component_scheduler import (
    InitializationStatus,
    InitializationTask,
    ComponentScheduler,
    component_scheduler
)

__all__ = [
    'DependencyGraph',
    'DependencyNode', 
    'DependencyState',
    'dependency_graph',
    'Subgraph',
    'SubgraphType',
    'SubgraphMetadata', 
    'SubgraphManager',
    'subgraph_manager',
    'InitializationStatus',
    'InitializationTask',
    'ComponentScheduler',
    'component_scheduler'
]
