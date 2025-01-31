graph TB
    %% Core Components
    ComponentRegistry[Component Registry]
    ComponentScheduler[Component Scheduler]
    DependencyGraph[Dependency Graph]
    SubgraphManager[Subgraph Manager]
    
    %% Data Classes
    ComponentMetadata[Component Metadata]
    ComponentType[Component Type]
    InitializationTask[Initialization Task]
    DependencyNode[Dependency Node]
    Subgraph[Subgraph]
    
    %% Enums and States
    DependencyState[Dependency State]
    SubgraphType[Subgraph Type]
    InitializationStatus[Initialization Status]
    
    %% Helper Classes
    ComponentLocks[Component Locks]
    DependencyLocks[Dependency Locks]
    BatchGroup[Batch Group]
    SubgraphMetadata[Subgraph Metadata]

    %% Core Component Relationships
    ComponentRegistry --> |manages| ComponentMetadata
    ComponentRegistry --> |defines| ComponentType
    ComponentRegistry --> |creates| InitializationTask
    ComponentRegistry --> |uses| ComponentLocks
    ComponentRegistry --> |organizes| BatchGroup

    ComponentScheduler --> |tracks| InitializationStatus
    ComponentScheduler --> |executes| InitializationTask

    DependencyGraph --> |contains| DependencyNode
    DependencyGraph --> |manages| DependencyState
    DependencyGraph --> |uses| DependencyLocks
    DependencyGraph --> |schedules| InitializationTask

    SubgraphManager --> |manages| Subgraph
    SubgraphManager --> |defines| SubgraphType
    SubgraphManager --> |uses| SubgraphMetadata
    SubgraphManager --> |depends on| DependencyGraph

    %% Subgraph Relationships
    Subgraph --> |contains| DependencyNode
    Subgraph --> |tracks| DependencyState
    Subgraph --> |uses| DependencyLocks

    %% System Dependencies
    ComponentRegistry -.->|depends on| DependencyGraph
    ComponentRegistry -.->|uses| ComponentScheduler
    SubgraphManager -.->|integrates with| DependencyGraph
    Subgraph -.->|extends| DependencyGraph

    %% Metadata Relationships
    ComponentMetadata -->|typed by| ComponentType
    SubgraphMetadata -->|typed by| SubgraphType

    %% Styling
    classDef core fill:#f9f,stroke:#333,stroke-width:2px,rx:5px
    classDef dataClass fill:#bbf,stroke:#333,rx:5px
    classDef enum fill:#bfb,stroke:#333,rx:5px
    classDef helper fill:#fbb,stroke:#333,rx:5px

    class ComponentRegistry,ComponentScheduler,DependencyGraph,SubgraphManager core
    class ComponentMetadata,InitializationTask,DependencyNode,Subgraph dataClass
    class ComponentType,DependencyState,SubgraphType,InitializationStatus enum
    class ComponentLocks,DependencyLocks,BatchGroup,SubgraphMetadata helper

    %% Subgraphs for Visual Organization
    subgraph Core ["Core Components"]
        style Core fill:#f9f9f9,stroke:#999
        ComponentRegistry
        ComponentScheduler
        DependencyGraph
        SubgraphManager
    end

    subgraph Data ["Data Classes"]
        style Data fill:#f0f0ff,stroke:#999
        ComponentMetadata
        InitializationTask
        DependencyNode
        Subgraph
    end

    subgraph Enums ["Enums and States"]
        style Enums fill:#f0fff0,stroke:#999
        ComponentType
        DependencyState
        SubgraphType
        InitializationStatus
    end

    subgraph Helpers ["Helper Classes"]
        style Helpers fill:#fff0f0,stroke:#999
        ComponentLocks
        DependencyLocks
        BatchGroup
        SubgraphMetadata
    end