"""
Data flow orchestration and routing module with integrated data cleaning pipeline.

This module provides the DataSeive class for managing data flow between components,
including routing, filtering, transformation, and data cleaning capabilities.
"""
# Standard library imports
import asyncio
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Any, Callable, TypeVar, Generic, Union
from pathlib import Path

# Local imports
from tenire.core.codex import Signal, SignalType
from tenire.core.container import container
from tenire.organizers.compactor import compactor
from tenire.servicers import SignalManager
from tenire.utils.logger import get_logger

# Import data cleaning components
from tenire.data.head_janitor import HeadJanitor
from tenire.data.cleanup.cleaner import BetDataCleaner
from tenire.data.cleanup.compressor import BetDataCompressor
from tenire.data.cleanup.ende import BetDataCodec
from tenire.data.validation.schemas import BetRecord, CleanedBetRecord

logger = get_logger(__name__)

T = TypeVar('T')
U = TypeVar('U')

class DataPriority(Enum):
    """Priority levels for data routing."""
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    CRITICAL = 3

class DataCleaningStage(Enum):
    """Stages in the data cleaning pipeline."""
    RAW = 0
    VALIDATED = 1
    CLEANED = 2
    COMPRESSED = 3
    ENCODED = 4

class DataRoute:
    """Defines a route for data flow between components."""
    
    def __init__(
        self,
        source: str,
        destination: str,
        priority: DataPriority = DataPriority.MEDIUM,
        filter_fn: Optional[Callable[[Any], bool]] = None,
        transform_fn: Optional[Callable[[Any], Any]] = None,
        buffer_size: int = 100,
        cleaning_stage: Optional[DataCleaningStage] = None
    ):
        self.source = source
        self.destination = destination
        self.priority = priority
        self.filter_fn = filter_fn
        self.transform_fn = transform_fn
        self.buffer_size = buffer_size
        self.cleaning_stage = cleaning_stage
        self.queue = asyncio.Queue(maxsize=buffer_size)
        self.is_active = True
        self.metrics = {
            'processed': 0,
            'filtered': 0,
            'cleaned': 0,
            'errors': 0,
            'last_processed': None
        }

class DataStream(Generic[T]):
    """Represents a typed data stream with filtering and transformation capabilities."""
    
    def __init__(
        self,
        name: str,
        data_type: type[T],
        max_buffer: int = 1000,
        filter_fn: Optional[Callable[[T], bool]] = None,
        cleaning_required: bool = False
    ):
        self.name = name
        self.data_type = data_type
        self.max_buffer = max_buffer
        self.filter_fn = filter_fn
        self.cleaning_required = cleaning_required
        self.subscribers: List[DataRoute] = []
        self.buffer = asyncio.Queue(maxsize=max_buffer)
        self.is_active = True
        self.current_stage = DataCleaningStage.RAW

class DataSeive:
    """
    Manages data flow between components with integrated data cleaning.
    
    Features:
    1. Dynamic route configuration
    2. Priority-based data routing
    3. Data transformation pipelines
    4. Integrated data cleaning
    5. Compression and encoding
    6. Stream monitoring
    """
    
    def __init__(self, data_dir: str = 'data'):
        self.routes: Dict[str, DataRoute] = {}
        self.streams: Dict[str, DataStream] = {}
        self._processing_tasks: Dict[str, asyncio.Task] = {}
        self._monitor_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        
        # Initialize data cleaning components
        self.head_janitor = HeadJanitor(
            data_dir=data_dir,
            archive_dir=f"{data_dir}/archive",
            analysis_dir=f"{data_dir}/analysis"
        )
        self.cleaner = BetDataCleaner()
        self.compressor = BetDataCompressor()
        self.codec = BetDataCodec()
        
        # Register cleanup
        compactor.register_cleanup_task(
            name="seive_cleanup",
            cleanup_func=self.shutdown,
            priority=95,
            is_async=True,
            metadata={"tags": ["data", "flow"]}
        )
        
        # Register signal handlers
        SignalManager().register_handler(
            SignalType.DATA_FLOW_CONFIG_UPDATED,
            self._handle_config_update,
            priority=100
        )
        SignalManager().register_handler(
            SignalType.RAG_DOCUMENT_ADDED,
            self._handle_document_added,
            priority=90
        )
        
        logger.info("Initialized DataSeive with cleaning pipeline")
        
    async def create_route(
        self,
        source: str,
        destination: str,
        priority: DataPriority = DataPriority.MEDIUM,
        filter_fn: Optional[Callable[[Any], bool]] = None,
        transform_fn: Optional[Callable[[Any], Any]] = None,
        buffer_size: int = 100,
        cleaning_stage: Optional[DataCleaningStage] = None
    ) -> DataRoute:
        """Create a new data route with optional cleaning stage."""
        route_id = f"{source}->{destination}"
        route = DataRoute(
            source=source,
            destination=destination,
            priority=priority,
            filter_fn=filter_fn,
            transform_fn=transform_fn,
            buffer_size=buffer_size,
            cleaning_stage=cleaning_stage
        )
        self.routes[route_id] = route
        
        # Start processing task for route
        self._processing_tasks[route_id] = asyncio.create_task(
            self._process_route(route)
        )
        
        await SignalManager().emit(Signal(
            type=SignalType.DATA_FLOW_ROUTE_CREATED,
            data={'route': route_id},
            source='seive'
        ))
        
        return route
        
    async def create_stream[T](
        self,
        name: str,
        data_type: type[T],
        max_buffer: int = 1000,
        filter_fn: Optional[Callable[[T], bool]] = None,
        cleaning_required: bool = False
    ) -> DataStream[T]:
        """Create a new typed data stream with optional cleaning."""
        stream = DataStream(
            name=name,
            data_type=data_type,
            max_buffer=max_buffer,
            filter_fn=filter_fn,
            cleaning_required=cleaning_required
        )
        self.streams[name] = stream
        
        await SignalManager().emit(Signal(
            type=SignalType.DATA_FLOW_STREAM_CREATED,
            data={'stream': name},
            source='seive'
        ))
        
        return stream
        
    async def push_to_stream(
        self,
        stream_name: str,
        data: Any,
        clean_data: bool = False
    ) -> bool:
        """Push data to a named stream with optional cleaning."""
        if stream_name not in self.streams:
            logger.error(f"Stream not found: {stream_name}")
            return False
            
        stream = self.streams[stream_name]
        
        if clean_data or stream.cleaning_required:
            try:
                # Clean data through pipeline
                if isinstance(data, (dict, BetRecord)):
                    cleaned_data = await self._clean_data(data)
                    return await stream.push(cleaned_data)
                elif isinstance(data, list):
                    cleaned_batch = await self._clean_batch(data)
                    return all(await stream.push(item) for item in cleaned_batch)
            except Exception as e:
                logger.error(f"Error cleaning data for stream {stream_name}: {str(e)}")
                return False
                
        return await stream.push(data)
        
    async def _clean_data(
        self,
        data: Union[Dict[str, Any], BetRecord]
    ) -> Optional[CleanedBetRecord]:
        """Clean a single data record through the pipeline."""
        try:
            # Validate and clean
            if isinstance(data, dict):
                validated = await self.cleaner.validation_processor.process(data)
            else:
                validated = data
                
            if validated:
                # Transform to cleaned record
                cleaned = await self.cleaner.transformation_processor.process(validated)
                if cleaned:
                    # Encode if needed
                    encoded = self.codec.encode_bet_data(cleaned, compress=False)
                    return cleaned
                    
            return None
            
        except Exception as e:
            logger.error(f"Error cleaning data: {str(e)}")
            return None
            
    async def _clean_batch(
        self,
        batch: List[Union[Dict[str, Any], BetRecord]]
    ) -> List[CleanedBetRecord]:
        """Clean a batch of records through the pipeline."""
        try:
            # Process through head janitor
            cleaned_records, _ = await self.cleaner.clean_bet_records(batch)
            return cleaned_records
            
        except Exception as e:
            logger.error(f"Error cleaning batch: {str(e)}")
            return []
            
    async def _process_route(self, route: DataRoute) -> None:
        """Process data flowing through a route with cleaning stages."""
        while not self._shutdown_event.is_set() and route.is_active:
            try:
                data = await route.queue.get()
                
                # Apply filter if exists
                if route.filter_fn and not route.filter_fn(data):
                    route.metrics['filtered'] += 1
                    continue
                    
                # Apply cleaning if needed
                if route.cleaning_stage:
                    if route.cleaning_stage == DataCleaningStage.CLEANED:
                        data = await self._clean_data(data)
                    elif route.cleaning_stage == DataCleaningStage.COMPRESSED:
                        data = await self.compressor.compress_records([data], "temp")[0]
                    elif route.cleaning_stage == DataCleaningStage.ENCODED:
                        data = self.codec.encode_bet_data(data)
                        
                    if data is None:
                        route.metrics['errors'] += 1
                        continue
                        
                    route.metrics['cleaned'] += 1
                    
                # Apply transformation if exists
                if route.transform_fn:
                    data = route.transform_fn(data)
                    
                # Get destination component
                destination = container.get(route.destination)
                if not destination:
                    logger.error(f"Destination not found: {route.destination}")
                    continue
                    
                # Send data to destination
                if hasattr(destination, 'process'):
                    await destination.process(data)
                elif hasattr(destination, 'handle'):
                    await destination.handle(data)
                else:
                    logger.error(f"Invalid destination interface: {route.destination}")
                    continue
                    
                # Update metrics
                route.metrics['processed'] += 1
                route.metrics['last_processed'] = datetime.now()
                
            except Exception as e:
                logger.error(f"Error processing route {route.source}->{route.destination}: {str(e)}")
                route.metrics['errors'] += 1
                
    async def _monitor_routes(self) -> None:
        """Monitor routes and emit metrics."""
        while not self._shutdown_event.is_set():
            try:
                metrics = {
                    route_id: route.metrics
                    for route_id, route in self.routes.items()
                }
                
                await SignalManager().emit(Signal(
                    type=SignalType.DATA_FLOW_METRICS,
                    data={'metrics': metrics},
                    source='seive'
                ))
                
                await asyncio.sleep(60)  # Monitor every minute
                
            except Exception as e:
                logger.error(f"Error in route monitor: {str(e)}")
                await asyncio.sleep(5)  # Back off on error
                
    async def start(self) -> None:
        """Start the seive and monitoring."""
        if not self._monitor_task:
            self._monitor_task = asyncio.create_task(self._monitor_routes())
            logger.info("Started DataSeive monitoring")
            
    async def stop_route(self, route_id: str) -> None:
        """Stop a data route."""
        if route_id in self.routes:
            self.routes[route_id].is_active = False
            if route_id in self._processing_tasks:
                self._processing_tasks[route_id].cancel()
                
    async def stop_stream(self, stream_name: str) -> None:
        """Stop a data stream."""
        if stream_name in self.streams:
            self.streams[stream_name].is_active = False
            
    async def _handle_config_update(self, signal: Signal) -> None:
        """Handle configuration update signals."""
        config = signal.data.get('config', {})
        
        # Update route configurations
        for route_id, route_config in config.get('routes', {}).items():
            if route_id in self.routes:
                route = self.routes[route_id]
                route.priority = route_config.get('priority', route.priority)
                route.buffer_size = route_config.get('buffer_size', route.buffer_size)
                route.cleaning_stage = route_config.get('cleaning_stage', route.cleaning_stage)
                
        # Update stream configurations
        for stream_name, stream_config in config.get('streams', {}).items():
            if stream_name in self.streams:
                stream = self.streams[stream_name]
                stream.max_buffer = stream_config.get('max_buffer', stream.max_buffer)
                stream.cleaning_required = stream_config.get('cleaning_required', stream.cleaning_required)
                
    async def _handle_document_added(self, signal: Signal) -> None:
        """Handle document added signals."""
        if 'records' in signal.data:
            # Process through cleaning pipeline if needed
            records = signal.data['records']
            if any(stream.cleaning_required for stream in self.streams.values()):
                cleaned_records = await self._clean_batch(records)
                # Route cleaned records to appropriate streams
                for stream in self.streams.values():
                    if stream.cleaning_required:
                        for record in cleaned_records:
                            await stream.push(record)
                            
    async def shutdown(self) -> None:
        """Shutdown the seive and cleanup resources."""
        logger.info("Shutting down DataSeive")
        
        # Set shutdown event
        self._shutdown_event.set()
        
        # Stop all routes
        for route_id in list(self.routes.keys()):
            await self.stop_route(route_id)
            
        # Stop all streams
        for stream_name in list(self.streams.keys()):
            await self.stop_stream(stream_name)
            
        # Cancel monitor task
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
                
        # Clear all queues
        for route in self.routes.values():
            while not route.queue.empty():
                try:
                    route.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                    
        for stream in self.streams.values():
            while not stream.buffer.empty():
                try:
                    stream.buffer.get_nowait()
                except asyncio.QueueEmpty:
                    break
                    
        # Cleanup data pipeline components
        await self.head_janitor.cleanup()
        await self.cleaner.cleanup()
        await self.compressor.cleanup()
        await self.codec.cleanup()
        
        logger.info("DataSeive shutdown complete")

# Create singleton instance
data_seive = DataSeive()

# Register with container
container.register_sync('data_seive', data_seive) 