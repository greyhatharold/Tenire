graph TB
    %% Core Components
    ComponentRegistry[ComponentRegistry]
    ComponentScheduler[ComponentScheduler]
    DependencyGraph[DependencyGraph]
    SubgraphManager[SubgraphManager]
    
    %% Data Classes
    ComponentMetadata[ComponentMetadata]
    ComponentType[ComponentType]
    InitializationTask[InitializationTask]
    DependencyNode[DependencyNode]
    Subgraph[Subgraph]
    
    %% Enums and States
    DependencyState[DependencyState]
    SubgraphType[SubgraphType]
    InitializationStatus[InitializationStatus]
    
    %% Helper Classes
    ComponentLocks[ComponentLocks]
    DependencyLocks[DependencyLocks]
    BatchGroup[BatchGroup]
    SubgraphMetadata[SubgraphMetadata]

    %% Relationships
    ComponentRegistry --> ComponentMetadata
    ComponentRegistry --> ComponentType
    ComponentRegistry --> InitializationTask
    ComponentRegistry --> ComponentLocks
    ComponentRegistry --> BatchGroup

    ComponentScheduler --> InitializationStatus
    ComponentScheduler --> InitializationTask

    DependencyGraph --> DependencyNode
    DependencyGraph --> DependencyState
    DependencyGraph --> DependencyLocks
    DependencyGraph --> InitializationTask

    SubgraphManager --> Subgraph
    SubgraphManager --> SubgraphType
    SubgraphManager --> SubgraphMetadata
    SubgraphManager --> DependencyGraph

    Subgraph --> DependencyNode
    Subgraph --> DependencyState
    Subgraph --> DependencyLocks

    %% Dependencies
    ComponentRegistry -.-> DependencyGraph
    ComponentRegistry -.-> ComponentScheduler
    SubgraphManager -.-> DependencyGraph
    Subgraph -.-> DependencyGraph

    %% Metadata Classes
    ComponentMetadata --> ComponentType
    SubgraphMetadata --> SubgraphType

    %% Style
    classDef core fill:#f9f,stroke:#333,stroke-width:2px
    classDef dataClass fill:#bbf,stroke:#333
    classDef enum fill:#bfb,stroke:#333
    classDef helper fill:#fbb,stroke:#333

    class ComponentRegistry,ComponentScheduler,DependencyGraph,SubgraphManager core
    class ComponentMetadata,InitializationTask,DependencyNode,Subgraph dataClass
    class ComponentType,DependencyState,SubgraphType,InitializationStatus enum
    class ComponentLocks,DependencyLocks,BatchGroup,SubgraphMetadata helper

    %% Labels
    subgraph Core Components
        ComponentRegistry
        ComponentScheduler
        DependencyGraph
        SubgraphManager
    end

    subgraph Data Classes
        ComponentMetadata
        InitializationTask
        DependencyNode
        Subgraph
    end

    subgraph Enums and States
        ComponentType
        DependencyState
        SubgraphType
        InitializationStatus
    end

    subgraph Helper Classes
        ComponentLocks
        DependencyLocks
        BatchGroup
        SubgraphMetadata
    end