"""
Betting toolkit for the Tenire framework.

This module implements the betting toolkit interface, providing access to
betting-related commands and functionality.
"""

from typing import Any, Dict, Optional, Tuple
from datetime import datetime

from tenire.actions.risky.betting_actions import betting_controller, BetParameters
from tenire.professionals.bet_professional import BetAnalyzer
from tenire.toolkit.base import ToolkitInterface
from tenire.utils.logger import get_logger
from tenire.core.codex import (
    Signal, 
    SignalType, 
    DataFlowStatus, 
    DataFlowMetrics,
    DataFlowPriority
)
from tenire.actions.command_processor import CommandType
from tenire.servicers import get_signal_manager, SignalManager

logger = get_logger(__name__)

class BettingToolkit(ToolkitInterface):
    """Toolkit for betting-related commands and functionality."""
    
    def __init__(self, bet_analyzer: Optional[BetAnalyzer] = None, 
                 signal_manager: Optional[SignalManager] = None):
        """Initialize the betting toolkit.
        
        Args:
            bet_analyzer: Optional custom bet analyzer instance
            signal_manager: Optional custom signal manager instance
        """
        self._bet_analyzer = bet_analyzer or BetAnalyzer()
        self._signal_manager = signal_manager or get_signal_manager()
        self._controller = betting_controller
        self._metrics = {
            'total_bets': 0,
            'successful_bets': 0,
            'failed_bets': 0,
            'total_amount': 0.0,
            'last_bet_time': None,
            'command_counts': {},
            'errors': [],
            'analysis_metrics': {
                'total_analyses': 0,
                'successful_analyses': 0,
                'failed_analyses': 0,
                'avg_confidence': 0.0
            }
        }
        
        # Register signal handlers
        self._register_signal_handlers()
        
    def _register_signal_handlers(self) -> None:
        """Register handlers for relevant signals."""
        signal_manager = self._signal_manager
        
        # Register for command-related signals
        signal_manager.register_handler(
            SignalType.COMMAND_RECEIVED,
            self._handle_command_received,
            priority=DataFlowPriority.HIGH.value
        )
        
        # Register for bet-related signals
        signal_manager.register_handler(
            SignalType.BET_PLACED,
            self._handle_bet_placed,
            priority=DataFlowPriority.HIGH.value
        )
        signal_manager.register_handler(
            SignalType.BET_ERROR,
            self._handle_bet_error,
            priority=DataFlowPriority.HIGH.value
        )
        
        # Register for analysis-related signals
        signal_manager.register_handler(
            SignalType.RAG_QUERY_COMPLETED,
            self._handle_analysis_completed,
            priority=DataFlowPriority.NORMAL.value
        )
        signal_manager.register_handler(
            SignalType.RAG_QUERY_ERROR,
            self._handle_analysis_error,
            priority=DataFlowPriority.NORMAL.value
        )
        
    async def _handle_command_received(self, signal: Signal) -> None:
        """Handle received command signals."""
        try:
            command_data = signal.data
            command_type = command_data.get('command_type')
            
            if command_type in self.supported_commands:
                # Emit command processing signal
                await self._signal_manager.emit(Signal(
                    type=SignalType.COMMAND_PROCESSED,
                    data={
                        'command_type': command_type,
                        'toolkit': self.name,
                        'timestamp': datetime.now().isoformat()
                    },
                    source=self.name,
                    priority=DataFlowPriority.HIGH.value
                ))
                
        except Exception as e:
            logger.error(f"Error handling command received signal: {str(e)}")
            
    async def _handle_bet_placed(self, signal: Signal) -> None:
        """Handle bet placed signals."""
        try:
            bet_data = signal.data
            
            # Update metrics
            self._metrics['total_bets'] += 1
            self._metrics['successful_bets'] += 1
            self._metrics['total_amount'] += bet_data.get('amount', 0)
            self._metrics['last_bet_time'] = datetime.now()
            
            # Emit data flow metrics signal
            await self._signal_manager.emit(Signal(
                type=SignalType.DATA_FLOW_METRICS,
                data={
                    'component': self.name,
                    'metrics': await self.get_metrics(),
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.NORMAL.value
            ))
            
        except Exception as e:
            logger.error(f"Error handling bet placed signal: {str(e)}")
            
    async def _handle_bet_error(self, signal: Signal) -> None:
        """Handle bet error signals."""
        try:
            error_data = signal.data
            
            # Update metrics
            self._metrics['total_bets'] += 1
            self._metrics['failed_bets'] += 1
            self._metrics['errors'].append(error_data.get('error'))
            
            # Emit error signal
            await self._signal_manager.emit(Signal(
                type=SignalType.ERROR,
                data={
                    'component': self.name,
                    'error': error_data.get('error'),
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.HIGH.value
            ))
            
        except Exception as e:
            logger.error(f"Error handling bet error signal: {str(e)}")
            
    async def _handle_analysis_completed(self, signal: Signal) -> None:
        """Handle analysis completed signals."""
        try:
            analysis_data = signal.data
            
            # Update analysis metrics
            self._metrics['analysis_metrics']['successful_analyses'] += 1
            if 'confidence' in analysis_data:
                self._metrics['analysis_metrics']['avg_confidence'] = (
                    (self._metrics['analysis_metrics']['avg_confidence'] * 
                     (self._metrics['analysis_metrics']['successful_analyses'] - 1) +
                     analysis_data['confidence']) / 
                    self._metrics['analysis_metrics']['successful_analyses']
                )
                
            # Emit metrics update signal
            await self._signal_manager.emit(Signal(
                type=SignalType.DATA_FLOW_METRICS,
                data={
                    'component': self.name,
                    'metrics': await self.get_metrics(),
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.NORMAL.value
            ))
            
        except Exception as e:
            logger.error(f"Error handling analysis completed signal: {str(e)}")
            
    async def _handle_analysis_error(self, signal: Signal) -> None:
        """Handle analysis error signals."""
        try:
            error_data = signal.data
            
            # Update analysis metrics
            self._metrics['analysis_metrics']['failed_analyses'] += 1
            self._metrics['errors'].append(error_data.get('error'))
            
            # Emit error signal
            await self._signal_manager.emit(Signal(
                type=SignalType.ERROR,
                data={
                    'component': self.name,
                    'error': error_data.get('error'),
                    'context': 'bet_analysis',
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.HIGH.value
            ))
            
        except Exception as e:
            logger.error(f"Error handling analysis error signal: {str(e)}")
            
    @property
    def name(self) -> str:
        return "betting"
        
    @property
    def description(self) -> str:
        return "Provides betting-related commands and functionality"
        
    @property
    def supported_commands(self) -> Dict[str, str]:
        return {
            CommandType.PLACE_BET.name: "Place a bet on a game",
            CommandType.CHECK_BALANCE.name: "Check current balance",
            CommandType.LOGIN.name: "Login to betting platform",
            CommandType.LOGOUT.name: "Logout from betting platform"
        }
        
    async def execute_command(self, command: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a betting command."""
        try:
            # Emit command received signal
            await self._signal_manager.emit(Signal(
                type=SignalType.COMMAND_RECEIVED,
                data={
                    'command_type': command,
                    'params': params,
                    'toolkit': self.name,
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.HIGH.value
            ))
            
            # Update command metrics
            self._metrics['command_counts'][command] = self._metrics['command_counts'].get(command, 0) + 1
            
            # Validate command
            if not await self.validate_command(command, params):
                error = f"Invalid command or parameters: {command}"
                self._metrics['errors'].append(error)
                
                # Emit command error signal
                await self._signal_manager.emit(Signal(
                    type=SignalType.COMMAND_ERROR,
                    data={
                        'command_type': command,
                        'error': error,
                        'toolkit': self.name,
                        'timestamp': datetime.now().isoformat()
                    },
                    source=self.name,
                    priority=DataFlowPriority.HIGH.value
                ))
                
                return {
                    "success": False,
                    "error": error
                }
                
            # Get analysis if needed for betting commands
            if command == CommandType.PLACE_BET.name:
                try:
                    self._metrics['analysis_metrics']['total_analyses'] += 1
                    
                    # Emit analysis started signal
                    await self._signal_manager.emit(Signal(
                        type=SignalType.RAG_QUERY_STARTED,
                        data={
                            'game_type': params.get('game'),
                            'amount': params.get('amount'),
                            'toolkit': self.name,
                            'timestamp': datetime.now().isoformat()
                        },
                        source=self.name,
                        priority=DataFlowPriority.NORMAL.value
                    ))
                    
                    analysis = await self._analyze_bet(params)
                    if analysis:
                        params["analysis"] = analysis
                        self._metrics['analysis_metrics']['successful_analyses'] += 1
                        self._metrics['analysis_metrics']['avg_confidence'] = (
                            (self._metrics['analysis_metrics']['avg_confidence'] * 
                             (self._metrics['analysis_metrics']['successful_analyses'] - 1) +
                             analysis.get('confidence', 0)) / 
                            self._metrics['analysis_metrics']['successful_analyses']
                        )
                except Exception as e:
                    logger.error(f"Error during bet analysis: {str(e)}")
                    self._metrics['analysis_metrics']['failed_analyses'] += 1
                    
                    # Emit analysis error signal
                    await self._signal_manager.emit(Signal(
                        type=SignalType.RAG_QUERY_ERROR,
                        data={
                            'error': str(e),
                            'game_type': params.get('game'),
                            'toolkit': self.name,
                            'timestamp': datetime.now().isoformat()
                        },
                        source=self.name,
                        priority=DataFlowPriority.HIGH.value
                    ))
                
            # Execute command through controller
            if command == CommandType.PLACE_BET.name:
                bet_params = BetParameters(**params)
                result = await self._controller.execute_action("place_bet", bet_params)
                
                # Update bet metrics and emit appropriate signals
                self._metrics['total_bets'] += 1
                self._metrics['last_bet_time'] = datetime.now()
                
                if result.success:
                    self._metrics['successful_bets'] += 1
                    self._metrics['total_amount'] += params.get('amount', 0)
                    
                    # Emit bet placed signal
                    await self._signal_manager.emit(Signal(
                        type=SignalType.BET_PLACED,
                        data={
                            'game': params.get('game'),
                            'amount': params.get('amount'),
                            'result': result.extracted_content,
                            'toolkit': self.name,
                            'timestamp': datetime.now().isoformat()
                        },
                        source=self.name,
                        priority=DataFlowPriority.HIGH.value
                    ))
                else:
                    self._metrics['failed_bets'] += 1
                    
                    # Emit bet error signal
                    await self._signal_manager.emit(Signal(
                        type=SignalType.BET_ERROR,
                        data={
                            'error': result.error,
                            'game': params.get('game'),
                            'amount': params.get('amount'),
                            'toolkit': self.name,
                            'timestamp': datetime.now().isoformat()
                        },
                        source=self.name,
                        priority=DataFlowPriority.HIGH.value
                    ))
                    
            elif command == CommandType.CHECK_BALANCE.name:
                result = await self._controller.execute_action("check_balance")
                
                if result.success:
                    # Emit balance updated signal
                    await self._signal_manager.emit(Signal(
                        type=SignalType.BALANCE_UPDATED,
                        data={
                            'balance': result.extracted_content,
                            'toolkit': self.name,
                            'timestamp': datetime.now().isoformat()
                        },
                        source=self.name,
                        priority=DataFlowPriority.NORMAL.value
                    ))
                    
            elif command == CommandType.LOGIN.name:
                result = await self._controller.execute_action("login_with_google", **params)
                
                if result.success:
                    # Emit login completed signal
                    await self._signal_manager.emit(Signal(
                        type=SignalType.LOGIN_COMPLETED,
                        data={
                            'username': params.get('username'),
                            'toolkit': self.name,
                            'timestamp': datetime.now().isoformat()
                        },
                        source=self.name,
                        priority=DataFlowPriority.HIGH.value
                    ))
                else:
                    # Emit login failed signal
                    await self._signal_manager.emit(Signal(
                        type=SignalType.LOGIN_FAILED,
                        data={
                            'error': result.error,
                            'username': params.get('username'),
                            'toolkit': self.name,
                            'timestamp': datetime.now().isoformat()
                        },
                        source=self.name,
                        priority=DataFlowPriority.HIGH.value
                    ))
                    
            elif command == CommandType.LOGOUT.name:
                result = await self._controller.execute_action("logout")
            else:
                error = f"Unsupported command: {command}"
                self._metrics['errors'].append(error)
                
                # Emit command error signal
                await self._signal_manager.emit(Signal(
                    type=SignalType.COMMAND_ERROR,
                    data={
                        'command_type': command,
                        'error': error,
                        'toolkit': self.name,
                        'timestamp': datetime.now().isoformat()
                    },
                    source=self.name,
                    priority=DataFlowPriority.HIGH.value
                ))
                
                return {
                    "success": False,
                    "error": error
                }
                
            # Emit command completed signal
            await self._signal_manager.emit(Signal(
                type=SignalType.COMMAND_COMPLETED,
                data={
                    'command_type': command,
                    'success': result.success,
                    'result': result.extracted_content if result.success else None,
                    'error': result.error if not result.success else None,
                    'toolkit': self.name,
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.HIGH.value
            ))
                
            return {
                "success": result.success,
                "result": result.extracted_content if result.success else None,
                "error": result.error if not result.success else None
            }
            
        except Exception as e:
            error = f"Error executing betting command {command}: {str(e)}"
            self._metrics['errors'].append(error)
            logger.error(error)
            
            # Emit error signal
            await self._signal_manager.emit(Signal(
                type=SignalType.ERROR,
                data={
                    'component': self.name,
                    'error': error,
                    'command_type': command,
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.HIGH.value
            ))
            
            return {
                "success": False,
                "error": error
            }
            
    async def validate_command(self, command: str, params: Dict[str, Any]) -> bool:
        """Validate a betting command and its parameters."""
        try:
            if command not in self.supported_commands:
                logger.error(f"Unsupported command: {command}")
                return False
                
            if command == CommandType.PLACE_BET.name:
                # Validate required parameters
                required = {"game", "amount"}
                missing = required - set(params.keys())
                if missing:
                    logger.error(f"Missing required parameters for {command}: {missing}")
                    return False
                    
                # Validate amount
                amount = params.get("amount")
                if not isinstance(amount, (int, float)) or amount <= 0:
                    logger.error(f"Invalid amount for {command}: {amount}")
                    return False
                    
                # Validate game-specific parameters
                game = params["game"].lower()
                if game == "mines":
                    if "mines_count" not in params:
                        logger.error("Missing mines_count parameter for mines game")
                        return False
                    mines_count = params["mines_count"]
                    if not isinstance(mines_count, int) or mines_count < 1 or mines_count > 24:
                        logger.error(f"Invalid mines_count: {mines_count}. Must be between 1 and 24")
                        return False
                elif game == "dice":
                    if "roll_over" not in params:
                        logger.error("Missing roll_over parameter for dice game")
                        return False
                    if "target_number" not in params:
                        logger.error("Missing target_number parameter for dice game")
                        return False
                    roll_over = params["roll_over"]
                    target = params["target_number"]
                    if not isinstance(roll_over, bool):
                        logger.error(f"Invalid roll_over parameter: {roll_over}. Must be boolean")
                        return False
                    if not isinstance(target, int) or target < 1 or target > 100:
                        logger.error(f"Invalid target_number: {target}. Must be between 1 and 100")
                        return False
                elif game not in ["crash", "mines", "dice", "limbo"]:
                    logger.error(f"Unsupported game type: {game}")
                    return False
                    
            elif command == CommandType.LOGIN.name:
                # Validate login parameters
                if not any(key in params for key in ["username", "password", "token"]):
                    logger.error("Login requires either username/password or token")
                    return False
                if "username" in params and "password" not in params:
                    logger.error("Password required when username is provided")
                    return False
                if "password" in params and "username" not in params:
                    logger.error("Username required when password is provided")
                    return False
                    
            elif command == CommandType.CHECK_BALANCE.name:
                # No parameters required for balance check
                pass
                
            elif command == CommandType.LOGOUT.name:
                # No parameters required for logout
                pass
                
            return True
            
        except Exception as e:
            logger.error(f"Error validating betting command {command}: {str(e)}")
            return False
            
    async def _analyze_bet(self, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Analyze a potential bet using the bet professional."""
        try:
            # Get current time info
            now = datetime.now()
            
            # Get streak info from metrics
            current_streak = 0
            prev_outcome = None
            if self._metrics['total_bets'] > 0:
                if self._metrics['successful_bets'] > self._metrics['failed_bets']:
                    current_streak = self._metrics['successful_bets'] - self._metrics['failed_bets']
                    prev_outcome = True
                else:
                    current_streak = self._metrics['failed_bets'] - self._metrics['successful_bets']
                    prev_outcome = False
            
            # Get prediction from analyzer
            prediction = await self._bet_analyzer.predict_outcome(
                game_type=params['game'],
                current_streak_length=current_streak,
                hour=now.hour,
                day_of_week=now.weekday(),
                prev_outcome=prev_outcome
            )
            
            return prediction
            
        except Exception as e:
            logger.error(f"Error analyzing bet: {str(e)}")
            return None
            
    async def cleanup(self) -> None:
        """Clean up betting toolkit resources."""
        try:
            # Emit cleanup started signal
            await self._signal_manager.emit(Signal(
                type=SignalType.DATA_FLOW_CLEANING_STAGE,
                data={
                    'component': self.name,
                    'stage': 'cleanup_started',
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.HIGH.value
            ))
            
            if self._bet_analyzer:
                await self._bet_analyzer.cleanup()
                
            # Emit cleanup completed signal
            await self._signal_manager.emit(Signal(
                type=SignalType.DATA_FLOW_CLEANING_STAGE,
                data={
                    'component': self.name,
                    'stage': 'cleanup_completed',
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.HIGH.value
            ))
            
        except Exception as e:
            error = f"Error cleaning up betting toolkit: {str(e)}"
            logger.error(error)
            
            # Emit cleanup error signal
            await self._signal_manager.emit(Signal(
                type=SignalType.DATA_FLOW_ERROR,
                data={
                    'component': self.name,
                    'error': error,
                    'stage': 'cleanup',
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.HIGH.value
            ))
            
    async def check_health(self) -> bool:
        """Check health of betting toolkit."""
        try:
            # Check analyzer health
            if not self._bet_analyzer or not await self._bet_analyzer.check_health():
                await self._signal_manager.emit(Signal(
                    type=SignalType.COMPONENT_UNHEALTHY,
                    data={
                        'component': self.name,
                        'subcomponent': 'bet_analyzer',
                        'timestamp': datetime.now().isoformat()
                    },
                    source=self.name,
                    priority=DataFlowPriority.HIGH.value
                ))
                return False
                
            # Check controller health
            if not self._controller or not await self._controller.check_health():
                await self._signal_manager.emit(Signal(
                    type=SignalType.COMPONENT_UNHEALTHY,
                    data={
                        'component': self.name,
                        'subcomponent': 'betting_controller',
                        'timestamp': datetime.now().isoformat()
                    },
                    source=self.name,
                    priority=DataFlowPriority.HIGH.value
                ))
                return False
                
            # Check error rate
            if self._metrics['total_bets'] > 0:
                error_rate = self._metrics['failed_bets'] / self._metrics['total_bets']
                if error_rate > 0.3:  # More than 30% failure rate
                    await self._signal_manager.emit(Signal(
                        type=SignalType.COMPONENT_DEGRADED,
                        data={
                            'component': self.name,
                            'reason': 'high_error_rate',
                            'error_rate': error_rate,
                            'timestamp': datetime.now().isoformat()
                        },
                        source=self.name,
                        priority=DataFlowPriority.HIGH.value
                    ))
                    return False
                    
            # Check analysis health
            if self._metrics['analysis_metrics']['total_analyses'] > 0:
                analysis_error_rate = (
                    self._metrics['analysis_metrics']['failed_analyses'] / 
                    self._metrics['analysis_metrics']['total_analyses']
                )
                if analysis_error_rate > 0.3:  # More than 30% analysis failure rate
                    await self._signal_manager.emit(Signal(
                        type=SignalType.COMPONENT_DEGRADED,
                        data={
                            'component': self.name,
                            'reason': 'high_analysis_error_rate',
                            'error_rate': analysis_error_rate,
                            'timestamp': datetime.now().isoformat()
                        },
                        source=self.name,
                        priority=DataFlowPriority.HIGH.value
                    ))
                    return False
                    
            # Emit healthy signal
            await self._signal_manager.emit(Signal(
                type=SignalType.COMPONENT_RECOVERED,
                data={
                    'component': self.name,
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.NORMAL.value
            ))
            
            return True
            
        except Exception as e:
            # Emit error signal
            await self._signal_manager.emit(Signal(
                type=SignalType.ERROR,
                data={
                    'component': self.name,
                    'error': str(e),
                    'context': 'health_check',
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.HIGH.value
            ))
            return False
            
    async def get_metrics(self) -> Optional[DataFlowMetrics]:
        """Get betting toolkit metrics."""
        try:
            metrics = DataFlowMetrics(
                processed_count=self._metrics['total_bets'],
                success_count=self._metrics['successful_bets'],
                error_count=self._metrics['failed_bets'],
                processing_time=0.0,  # Not tracked currently
                queue_size=0,  # Not applicable
                batch_size=1,  # Not applicable
                last_processed=self._metrics['last_bet_time'],
                custom_metrics={
                    'total_amount': self._metrics['total_amount'],
                    'command_counts': self._metrics['command_counts'],
                    'analysis_metrics': self._metrics['analysis_metrics']
                }
            )
            
            # Emit metrics signal
            await self._signal_manager.emit(Signal(
                type=SignalType.DATA_FLOW_METRICS,
                data={
                    'component': self.name,
                    'metrics': metrics,
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.NORMAL.value
            ))
            
            return metrics
            
        except Exception as e:
            # Emit error signal
            await self._signal_manager.emit(Signal(
                type=SignalType.ERROR,
                data={
                    'component': self.name,
                    'error': str(e),
                    'context': 'metrics_collection',
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.HIGH.value
            ))
            return None
            
    async def get_status(self) -> Tuple[DataFlowStatus, str]:
        """Get betting toolkit status."""
        try:
            is_healthy = await self.check_health()
            metrics = await self.get_metrics()
            
            if not is_healthy:
                recent_errors = self._metrics['errors'][-3:]  # Last 3 errors
                status = DataFlowStatus.UNHEALTHY
                message = f"Toolkit unhealthy. Recent errors: {recent_errors}"
            elif metrics:
                analysis_success_rate = 0
                if self._metrics['analysis_metrics']['total_analyses'] > 0:
                    analysis_success_rate = (
                        self._metrics['analysis_metrics']['successful_analyses'] /
                        self._metrics['analysis_metrics']['total_analyses'] * 100
                    )
                
                status = DataFlowStatus.HEALTHY
                message = (
                    f"Healthy. Total bets: {metrics.processed_count}, "
                    f"Success rate: {(metrics.success_count/metrics.processed_count)*100:.1f}% "
                    f"if metrics.processed_count > 0 else 'No bets placed'. "
                    f"Analysis success rate: {analysis_success_rate:.1f}%"
                )
            else:
                status = DataFlowStatus.HEALTHY
                message = "Toolkit operating normally"
                
            # Emit status signal
            await self._signal_manager.emit(Signal(
                type=SignalType.DATA_FLOW_STATUS,
                data={
                    'component': self.name,
                    'status': status.value,
                    'message': message,
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.NORMAL.value
            ))
            
            return status, message
            
        except Exception as e:
            error = f"Error checking toolkit status: {str(e)}"
            
            # Emit error signal
            await self._signal_manager.emit(Signal(
                type=SignalType.ERROR,
                data={
                    'component': self.name,
                    'error': error,
                    'context': 'status_check',
                    'timestamp': datetime.now().isoformat()
                },
                source=self.name,
                priority=DataFlowPriority.HIGH.value
            ))
            
            return DataFlowStatus.ERROR, error 