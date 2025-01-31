"""
Bet management module for the Tenire framework.

This module handles bet placement operations and tracking, working in conjunction
with the browser integration layer to execute bets on the Stake.us platform.
"""

# Standard library imports
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

# Local imports
from tenire.actions.risky.betting_actions import BetParameters, betting_controller
from tenire.core.config import Config
from tenire.integrations.browser import BrowserIntegration
from tenire.utils.logger import get_logger
from tenire.servicers import SignalManager
from tenire.core.codex import Signal, SignalType

# Configure logging
logger = get_logger(__name__)

@dataclass
class BetResult:
    """Data structure for storing bet execution results."""
    success: bool
    amount: float
    game: str
    timestamp: datetime
    error_message: Optional[str] = None
    bet_id: Optional[str] = None


class BetValidationError(Exception):
    """Raised when bet parameters fail validation."""
    pass


class BetManager:
    """
    Manages bet placement and tracking for the Tenire framework.
    
    This class is responsible for:
    1. Validating bet parameters
    2. Executing bets through browser integration
    3. Tracking bet history
    4. Providing bet status information
    
    The class maintains a clean separation between bet logic and browser
    interaction, delegating the actual browser operations to BrowserIntegration.
    """

    # Supported games and their actions
    GAME_ACTIONS = {
        "mines": "Place bet on Mines game",
        "dice": "Place bet on Dice game",
        "keno": "Place bet on Keno game",
    }

    def __init__(self, browser_integration: BrowserIntegration):
        """
        Initialize the bet manager.
        
        Args:
            browser_integration: Instance of BrowserIntegration for browser operations
        """
        self._browser = browser_integration
        self._bet_history: List[BetResult] = []
        
        # Register signal handlers
        SignalManager().register_handler(
            SignalType.BET_PLACED,
            self._handle_bet_placed,
            priority=10
        )
        SignalManager().register_handler(
            SignalType.BET_ERROR,
            self._handle_bet_error,
            priority=10
        )
        SignalManager().register_handler(
            SignalType.BALANCE_UPDATED,
            self._handle_balance_updated,
            priority=5
        )

    async def _handle_bet_placed(self, signal: Signal) -> None:
        """Handle bet placed signal."""
        bet_data = signal.data
        bet_result = BetResult(
            success=True,
            amount=bet_data.get("amount", 0.0),
            game=bet_data.get("game", "unknown"),
            timestamp=datetime.now(),
            bet_id=bet_data.get("bet_id")
        )
        self._bet_history.append(bet_result)
        logger.info(f"Bet placed: {bet_data.get('amount')} on {bet_data.get('game')}")

    async def _handle_bet_error(self, signal: Signal) -> None:
        """Handle bet error signal."""
        error = signal.data.get("error")
        bet_data = signal.data.get("bet_data", {})
        bet_result = BetResult(
            success=False,
            amount=bet_data.get("amount", 0.0),
            game=bet_data.get("game", "unknown"),
            timestamp=datetime.now(),
            error_message=error
        )
        self._bet_history.append(bet_result)
        logger.error(f"Bet error: {error}")

    async def _handle_balance_updated(self, signal: Signal) -> None:
        """Handle balance updated signal."""
        balance = signal.data.get("balance")
        if balance is not None:
            logger.info(f"Balance updated: {balance}")

    async def _execute_bet(self, params: Dict[str, Any]) -> BetResult:
        """Execute a single bet with the given parameters."""
        try:
            # Create bet parameters
            bet_params = BetParameters(
                amount=params["amount"],
                game=params["game"],
                mines_count=params.get("mines_count", 3),
                target_multiplier=params.get("target_multiplier"),
                roll_over=params.get("roll_over"),
                target_number=params.get("target_number")
            )
            
            # Get the appropriate action for the game
            action_name = self.GAME_ACTIONS.get(params["game"].lower())
            if not action_name:
                raise BetValidationError(f"No action defined for game: {params['game']}")
            
            # Execute the bet action
            result = await betting_controller.execute(
                action_name,
                params=bet_params,
                browser=self._browser._browser
            )
            
            # Create bet result
            bet_result = BetResult(
                success=result.success,
                amount=params["amount"],
                game=params["game"],
                timestamp=datetime.now(),
                error_message=result.error if not result.success else None
            )
            
            return bet_result
            
        except Exception as e:
            return BetResult(
                success=False,
                amount=params["amount"],
                game=params["game"],
                timestamp=datetime.now(),
                error_message=str(e)
            )

    async def place_bet(self, amount: float, game: str, **kwargs) -> BetResult:
        """
        Place a bet on the specified game.
        
        Args:
            amount: Bet amount in USD
            game: Game identifier (e.g., 'mines', 'dice')
            **kwargs: Additional game-specific parameters (e.g., mines_count for Mines)
        
        Returns:
            BetResult containing the outcome and details
            
        Raises:
            BetValidationError: If bet parameters are invalid
        """
        try:
            # Validate parameters
            self._validate_bet_params(amount, game)
            
            # Prepare bet parameters
            bet_params = {
                "amount": amount,
                "game": game,
                **kwargs
            }
            
            # Lazy import to avoid circular dependency
            from tenire.organizers.concurrency import concurrency_manager
            
            # Execute bet with semaphore protection
            async with concurrency_manager.semaphores.acquire("betting", 5):
                task_id = f"bet_{game}_{datetime.now().timestamp()}"
                task = concurrency_manager.async_tasks.create_task(
                    self._execute_bet(bet_params),
                    task_id
                )
                
                # Wait for bet completion
                results = await concurrency_manager.async_tasks.wait_for_tasks([task_id])
                result = results[task_id]
                
                if not result.success:
                    bet_result = BetResult(
                        success=False,
                        amount=amount,
                        game=game,
                        timestamp=datetime.now(),
                        error_message=result.error
                    )
                else:
                    bet_result = result.result
                
                # Track bet history
                self._bet_history.append(bet_result)
                return bet_result
            
        except Exception as e:
            error_result = BetResult(
                success=False,
                amount=amount,
                game=game,
                timestamp=datetime.now(),
                error_message=str(e)
            )
            self._bet_history.append(error_result)
            logger.error(f"Failed to place bet: {str(e)}")
            return error_result

    async def place_multiple_bets(self, bets: List[Dict[str, Any]]) -> List[BetResult]:
        """Place multiple bets concurrently."""
        try:
            # Lazy import to avoid circular dependency
            from tenire.organizers.concurrency import concurrency_manager
            
            tasks = []
            for i, bet in enumerate(bets):
                task_id = f"multi_bet_{i}_{datetime.now().timestamp()}"
                task = concurrency_manager.async_tasks.create_task(
                    self.place_bet(**bet),
                    task_id
                )
                tasks.append(task_id)
            
            # Wait for all bets to complete
            results = await concurrency_manager.async_tasks.wait_for_tasks(tasks)
            return [results[task_id].result for task_id in tasks if results[task_id].success]
            
        except Exception as e:
            logger.error(f"Failed to place multiple bets: {str(e)}")
            return []

    async def check_balance(self) -> Optional[str]:
        """
        Check the current account balance.
        
        Returns:
            Current balance as string if successful, None otherwise
        """
        try:
            result = await betting_controller.execute(
                'Check current balance',
                browser=self._browser._browser
            )
            if result.success:
                return result.extracted_content
            return None
        except Exception as e:
            logger.error(f"Failed to check balance: {str(e)}")
            return None

    def get_bet_history(self) -> List[BetResult]:
        """
        Retrieve the history of placed bets.
        
        Returns:
            List of BetResult objects representing past bets
        """
        return self._bet_history.copy()

    def get_total_wagered(self) -> float:
        """
        Calculate total amount wagered across all bets.
        
        Returns:
            Total amount wagered in USD
        """
        return sum(bet.amount for bet in self._bet_history if bet.success)

    def _validate_bet_params(self, amount: float, game: str) -> None:
        """
        Validate bet parameters before execution.
        
        Args:
            amount: Bet amount to validate
            game: Game identifier to validate
            
        Raises:
            BetValidationError: If parameters are invalid
        """
        if amount <= 0:
            raise BetValidationError("Bet amount must be positive")
            
        if amount < Config.DEFAULT_BET_AMOUNT:
            raise BetValidationError(
                f"Bet amount must be at least {Config.DEFAULT_BET_AMOUNT}"
            )
            
        if game.lower() not in self.GAME_ACTIONS:
            raise BetValidationError(
                f"Unsupported game: {game}. Supported games: "
                f"{', '.join(self.GAME_ACTIONS.keys())}"
            ) 