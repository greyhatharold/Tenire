"""
Agent data pipeline module.

This module handles the data flow between agents and the data pipeline components,
providing a clean interface for data operations while maintaining separation of concerns.
"""

import asyncio
from typing import Any, Dict, List, Optional, Set, Union
from datetime import datetime
from dataclasses import dataclass

from tenire.utils.logger import get_logger
from tenire.core.codex import (
    Signal, SignalType, DataFlowStatus, DataFlowPriority,
    DataCleaningStage, DataFlowMetrics
)
from tenire.data.seive import DataSeive
from tenire.data.manager import DataManager
from tenire.servicers.data_flow_agent import DataFlowAgent
from tenire.professionals.bet_professional import BetAnalyzer, BetStreak, GamePattern, GameAnalysis

logger = get_logger(__name__)

@dataclass
class AgentDataConfig:
    """Configuration for agent data pipeline."""
    batch_size: int = 100
    max_queue_size: int = 1000
    cleaning_required: bool = True
    priority: DataFlowPriority = DataFlowPriority.HIGH
    cleaning_stage: DataCleaningStage = DataCleaningStage.VALIDATION
    cache_size: int = 1000
    prefetch_size: int = 5
    analysis_window_days: int = 30
    min_streak_length: int = 3
    confidence_threshold: float = 0.7

class AgentDataPipeline:
    """
    Manages data flow between agents and the data pipeline.
    
    This class:
    1. Coordinates data flow between agents and the data pipeline
    2. Handles data routing and transformation
    3. Manages data cleaning and validation
    4. Provides monitoring and metrics
    5. Handles error recovery and cleanup
    6. Coordinates with BetAnalyzer for betting analysis
    """
    
    def __init__(
        self,
        data_seive: DataSeive,
        data_manager: DataManager,
        data_flow_agent: DataFlowAgent,
        config: Optional[AgentDataConfig] = None
    ):
        """Initialize the agent data pipeline."""
        self.seive = data_seive
        self.manager = data_manager
        self.flow_agent = data_flow_agent
        self.config = config or AgentDataConfig()
        
        # Initialize state
        self._routes: Dict[str, str] = {}  # task_id -> route_id
        self._streams: Dict[str, str] = {}  # task_id -> stream_id
        self._metrics: Dict[str, DataFlowMetrics] = {}
        self._active_tasks: Set[str] = set()
        self._error_counts: Dict[str, int] = {}
        self._cleanup_tasks: Set[asyncio.Task] = set()
        self._shutdown_event = asyncio.Event()
        
        # Initialize bet analyzer
        self._bet_analyzer = BetAnalyzer(
            min_streak_length=self.config.min_streak_length,
            confidence_threshold=self.config.confidence_threshold,
            analysis_window_days=self.config.analysis_window_days,
            batch_size=self.config.batch_size
        )
        
        # Start monitoring
        self._monitor_task = asyncio.create_task(self._monitor_pipeline())
        
    async def setup(self) -> None:
        """Set up the pipeline and its components."""
        try:
            # Initialize bet analyzer
            await self._bet_analyzer.setup()
            logger.info("Initialized bet analyzer")
            
        except Exception as e:
            logger.error(f"Error during pipeline setup: {str(e)}")
            raise
            
    async def create_task_pipeline(self, task_id: str, data_type: type) -> None:
        """
        Create a data pipeline for a specific agent task.
        
        Args:
            task_id: The unique task identifier
            data_type: The type of data to be processed
        """
        try:
            # Create data stream
            stream = await self.flow_agent.create_agent_stream(
                task_id=task_id,
                data_type=data_type,
                max_buffer=self.config.max_queue_size,
                cleaning_required=self.config.cleaning_required
            )
            self._streams[task_id] = stream.id
            
            # Create data route with bet analysis integration
            route = await self.seive.create_route(
                source=f"agent_task_{task_id}",
                destination="data_processor",
                priority=self.config.priority,
                cleaning_stage=self.config.cleaning_stage,
                processors=[self._process_betting_data]  # Add betting data processor
            )
            self._routes[task_id] = route.id
            
            # Initialize metrics
            self._metrics[task_id] = DataFlowMetrics()
            self._active_tasks.add(task_id)
            
            logger.info(f"Created data pipeline for task {task_id}")
            
            # Emit signal
            await self._emit_signal(
                SignalType.DATA_FLOW_ROUTE_CREATED,
                {
                    'task_id': task_id,
                    'route_id': route.id,
                    'stream_id': stream.id
                }
            )
            
        except Exception as e:
            logger.error(f"Error creating pipeline for task {task_id}: {str(e)}")
            await self._emit_signal(
                SignalType.DATA_FLOW_ERROR,
                {
                    'task_id': task_id,
                    'error': str(e),
                    'stage': 'creation'
                }
            )
            raise
            
    async def _process_betting_data(self, data: Any, metadata: Dict[str, Any]) -> Any:
        """Process betting data through the bet analyzer."""
        try:
            if not isinstance(data, dict):
                return data
                
            # Check if this is betting data
            if 'game_type' not in data and 'payoutMultiplier' not in data:
                return data
                
            # Get task context
            task_id = metadata.get('task_id')
            if not task_id:
                return data
                
            # Analyze betting data
            analysis_result = await self._bet_analyzer.analyze_bets([data])
            
            # Store analysis in metadata
            metadata['betting_analysis'] = analysis_result
            
            # Update metrics
            if task_id in self._metrics:
                metrics = self._metrics[task_id]
                metrics.processed_count += 1
                if analysis_result.get('summary', {}).get('win_rate'):
                    metrics.success_rate = analysis_result['summary']['win_rate']
                    
            # Emit analysis signal
            await self._emit_signal(
                SignalType.DATA_FLOW_TRANSFORMATION,
                {
                    'task_id': task_id,
                    'analysis': analysis_result,
                    'stage': 'betting_analysis'
                }
            )
            
            return data
            
        except Exception as e:
            logger.error(f"Error processing betting data: {str(e)}")
            return data
            
    async def process_agent_data(
        self,
        task_id: str,
        data: Any,
        metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Process data from an agent task.
        
        Args:
            task_id: The task identifier
            data: The data to process
            metadata: Optional metadata about the data
        """
        try:
            if task_id not in self._active_tasks:
                raise ValueError(f"No active pipeline for task {task_id}")
                
            route_id = self._routes[task_id]
            stream_id = self._streams[task_id]
            
            # Update metrics
            metrics = self._metrics[task_id]
            metrics.processed_count += 1
            metrics.last_processed = datetime.now()
            
            # Add task context to metadata
            full_metadata = (metadata or {}).copy()
            full_metadata['task_id'] = task_id
            
            # Process through seive
            await self.seive.process_data(
                route_id=route_id,
                data=data,
                metadata=full_metadata
            )
            
            # Update stream status
            await self.flow_agent.update_stream_status(
                stream_id=stream_id,
                status=DataFlowStatus.ACTIVE,
                metrics=metrics
            )
            
        except Exception as e:
            logger.error(f"Error processing data for task {task_id}: {str(e)}")
            self._error_counts[task_id] = self._error_counts.get(task_id, 0) + 1
            
            await self._emit_signal(
                SignalType.DATA_FLOW_ERROR,
                {
                    'task_id': task_id,
                    'error': str(e),
                    'stage': 'processing'
                }
            )
            
            # Check for error threshold
            if self._error_counts[task_id] >= 3:
                await self._handle_error_threshold(task_id)
                
    async def cleanup_task_pipeline(self, task_id: str) -> None:
        """
        Clean up the data pipeline for a task.
        
        Args:
            task_id: The task identifier
        """
        try:
            # Remove from active tasks
            self._active_tasks.discard(task_id)
            
            # Clean up route
            if task_id in self._routes:
                route_id = self._routes.pop(task_id)
                await self.seive.stop_route(route_id)
                
            # Clean up stream
            if task_id in self._streams:
                stream_id = self._streams.pop(task_id)
                await self.flow_agent.cleanup_agent_resources(task_id)
                
            # Clean up metrics
            self._metrics.pop(task_id, None)
            self._error_counts.pop(task_id, None)
            
            logger.info(f"Cleaned up pipeline for task {task_id}")
            
            await self._emit_signal(
                SignalType.DATA_FLOW_CLEANUP,
                {'task_id': task_id}
            )
            
        except Exception as e:
            logger.error(f"Error cleaning up pipeline for task {task_id}: {str(e)}")
            await self._emit_signal(
                SignalType.DATA_FLOW_ERROR,
                {
                    'task_id': task_id,
                    'error': str(e),
                    'stage': 'cleanup'
                }
            )
            
    async def _handle_error_threshold(self, task_id: str) -> None:
        """Handle when a task reaches the error threshold."""
        try:
            logger.warning(f"Task {task_id} reached error threshold")
            
            # Pause the pipeline
            route_id = self._routes[task_id]
            stream_id = self._streams[task_id]
            
            await self.seive.update_route_status(
                route_id=route_id,
                status=DataFlowStatus.ERROR
            )
            
            await self.flow_agent.update_stream_status(
                stream_id=stream_id,
                status=DataFlowStatus.ERROR,
                metrics=self._metrics[task_id]
            )
            
            # Emit signal
            await self._emit_signal(
                SignalType.DATA_FLOW_THRESHOLD_EXCEEDED,
                {
                    'task_id': task_id,
                    'error_count': self._error_counts[task_id]
                }
            )
            
        except Exception as e:
            logger.error(f"Error handling threshold for task {task_id}: {str(e)}")
            
    async def _monitor_pipeline(self) -> None:
        """Monitor the data pipeline and collect metrics."""
        while not self._shutdown_event.is_set():
            try:
                for task_id in list(self._active_tasks):
                    metrics = self._metrics[task_id]
                    
                    # Check for bottlenecks
                    if metrics.queue_size > self.config.max_queue_size * 0.9:
                        await self._emit_signal(
                            SignalType.DATA_FLOW_BOTTLENECK,
                            {
                                'task_id': task_id,
                                'queue_size': metrics.queue_size
                            }
                        )
                        
                    # Check for stalled tasks
                    if metrics.last_processed:
                        idle_time = (datetime.now() - metrics.last_processed).seconds
                        if idle_time > 300:  # 5 minutes
                            await self._emit_signal(
                                SignalType.DATA_FLOW_WARNING,
                                {
                                    'task_id': task_id,
                                    'idle_time': idle_time
                                }
                            )
                            
                    # Emit metrics
                    await self._emit_signal(
                        SignalType.DATA_FLOW_METRICS,
                        {
                            'task_id': task_id,
                            'metrics': metrics.__dict__
                        }
                    )
                    
            except Exception as e:
                logger.error(f"Error monitoring pipeline: {str(e)}")
                
            await asyncio.sleep(60)  # Check every minute
            
    async def _emit_signal(self, signal_type: SignalType, data: Dict[str, Any]) -> None:
        """Emit a signal with the given type and data."""
        try:
            from tenire.servicers import get_signal_manager
            signal_manager = await get_signal_manager()
            if signal_manager:
                await signal_manager.emit(Signal(
                    type=signal_type,
                    data=data,
                    source='agent_data_pipeline'
                ))
        except Exception as e:
            logger.error(f"Error emitting signal: {str(e)}")
            
    async def cleanup(self) -> None:
        """Clean up all pipeline resources."""
        try:
            self._shutdown_event.set()
            
            # Cancel monitor task
            if self._monitor_task:
                self._monitor_task.cancel()
                try:
                    await self._monitor_task
                except asyncio.CancelledError:
                    pass
                
            # Clean up all active tasks
            for task_id in list(self._active_tasks):
                await self.cleanup_task_pipeline(task_id)
                
            # Clear all state
            self._routes.clear()
            self._streams.clear()
            self._metrics.clear()
            self._active_tasks.clear()
            self._error_counts.clear()
            
            logger.info("Cleaned up agent data pipeline")
            
        except Exception as e:
            logger.error(f"Error during pipeline cleanup: {str(e)}")
            raise 

    async def get_betting_analysis(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Get betting analysis for a task."""
        try:
            if task_id not in self._active_tasks:
                return None
                
            route_id = self._routes[task_id]
            route_data = await self.seive.get_route_data(route_id)
            
            if not route_data:
                return None
                
            # Analyze all betting data for the task
            betting_data = [
                doc for doc in route_data
                if isinstance(doc, dict) and 'game_type' in doc
            ]
            
            if not betting_data:
                return None
                
            return await self._bet_analyzer.analyze_bets(betting_data)
            
        except Exception as e:
            logger.error(f"Error getting betting analysis for task {task_id}: {str(e)}")
            return None

    async def predict_bet_outcome(
        self,
        task_id: str,
        game_type: str,
        current_streak: int,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        """Predict outcome for a potential bet."""
        try:
            if task_id not in self._active_tasks:
                return None
                
            # Get current time features
            now = datetime.now()
            prediction = await self._bet_analyzer.predict_outcome(
                game_type=game_type,
                current_streak_length=current_streak,
                hour=now.hour,
                day_of_week=now.weekday(),
                prev_outcome=metadata.get('prev_outcome') if metadata else None
            )
            
            return prediction
            
        except Exception as e:
            logger.error(f"Error predicting bet outcome for task {task_id}: {str(e)}")
            return None 