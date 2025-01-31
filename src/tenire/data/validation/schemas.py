"""
Data validation schemas for bet data.

This module defines Pydantic models and validators for ensuring data integrity
throughout the betting data pipeline.
"""

from typing import Dict, Any, Optional, List, Type, Tuple, Union
from datetime import datetime, timezone
from uuid import UUID
from pydantic import (
    BaseModel,
    Field,
    field_validator,
    IPvAnyNetwork,
    ValidationError,
    model_validator
)
from enum import Enum
import asyncio
import re

# Local imports
from tenire.organizers.concurrency import concurrency_manager
from tenire.utils.logger import get_logger

logger = get_logger(__name__)

class ValidationError(Exception):
    """Base exception for validation errors."""
    pass

class SchemaError(ValidationError):
    """Raised when schema validation fails."""
    pass

class GameType(str, Enum):
    """Available game types."""
    KENO = "keno"
    MINES = "mines"
    ROULETTE = "roulette"
    WHEEL = "wheel"
    BACCARAT = "baccarat"
    SPORTS = "sports"
    POKER = "poker"

class Currency(str, Enum):
    """Available currencies."""
    GOLD = "gold"
    USD = "usd"

class Risk(str, Enum):
    """Risk levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class BaseGameState(BaseModel):
    """Base class for game state data."""
    risk: Risk
    
    @model_validator(mode='before')
    def validate_state(cls, values):
        """Validate game state data."""
        return values

class WheelState(BaseGameState):
    """State data for wheel games."""
    result: int = Field(..., ge=0, lt=360)
    segments: int = Field(..., gt=0)
    
    @model_validator(mode='before')
    def validate_wheel_state(cls, values):
        """Validate wheel-specific state."""
        if values.get('segments') > 0:
            if values.get('result') >= (360 / values.get('segments')):
                raise ValueError("Result must be within segment bounds")
        return values

class MinesState(BaseGameState):
    """State data for mines game."""
    grid_size: int = Field(..., gt=0)
    mines_count: int = Field(..., gt=0)
    revealed_cells: List[int] = Field(default_factory=list)
    
    @model_validator(mode='before')
    def validate_mines_state(cls, values):
        """Validate mines-specific state."""
        if values.get('mines_count') >= values.get('grid_size'):
            raise ValueError("Mines count must be less than grid size")
        return values

class BetData(BaseModel):
    """Core bet data model."""
    id: UUID
    ip: IPvAnyNetwork
    iid: str = Field(..., pattern=r"^house:\d+$")
    type: GameType
    nonce: int = Field(..., ge=0)
    value: float = Field(..., gt=0)
    active: bool = False
    amount: float = Field(..., gt=0)
    gameId: UUID
    mobile: bool = False
    payout: float = Field(..., ge=0)
    userId: UUID
    currency: Currency
    gameName: str
    createdAt: datetime
    updatedAt: datetime
    clientSeed: str
    stateWheel: Optional[WheelState]
    stateMines: Optional[MinesState]
    clientSeedId: UUID
    serverSeedId: UUID
    expectedAmount: float = Field(..., ge=0)
    serverSeedHash: str
    payoutMultiplier: float = Field(..., ge=0)

    @field_validator('serverSeedHash')
    def validate_hash(cls, v: str) -> str:
        """Validate server seed hash format."""
        if not len(v) == 64 or not all(c in '0123456789abcdef' for c in v):
            raise ValueError('Invalid server seed hash format')
        return v
        
    @field_validator('gameName')
    def validate_game_name(cls, v: str) -> str:
        """Validate game name format."""
        if not re.match(r'^[A-Z][a-zA-Z0-9]+$', v):
            raise ValueError('Invalid game name format')
        return v
        
    @model_validator(mode='before')
    def validate_amounts(cls, values):
        """Validate bet amounts and payouts."""
        if values.get('amount') > values.get('expectedAmount'):
            raise ValueError('Bet amount cannot exceed expected amount')
        if values.get('payout') > 0 and values.get('payoutMultiplier') <= 0:
            raise ValueError('Invalid payout multiplier for winning bet')
        return values

class BetRecord(BaseModel):
    """Complete bet record including metadata."""
    id: UUID
    store_id: UUID
    user_id: UUID
    data: BetData
    created_at: datetime
    
    @model_validator(mode='before')
    def validate_timestamps(cls, values):
        """Validate record timestamps."""
        if values.get('created_at') < values.get('data', {}).get('createdAt'):
            raise ValueError('Record creation time cannot be before bet creation')
        return values

    class Config:
        json_encoders = {
            datetime: lambda v: v.isoformat(),
            UUID: lambda v: str(v)
        }

class CleanedBetRecord(BetRecord):
    """Bet record with additional cleaning metadata."""
    metadata: Dict[str, Any] = Field(default_factory=dict)
    
    @model_validator(mode='before')
    def validate_metadata(cls, values):
        """Validate cleaning metadata."""
        metadata = values.get('metadata', {})
        required_fields = {'cleaned_at', 'original_size', 'cleaning_version'}
        if not all(field in metadata for field in required_fields):
            raise ValueError('Missing required metadata fields')
        return values

class CompressedBetRecord(BaseModel):
    """Compressed bet record format."""
    id: UUID
    compressed_data: str
    compression_ratio: float = Field(..., gt=0)
    original_size: int = Field(..., gt=0)
    compressed_size: int = Field(..., gt=0)
    created_at: datetime
    compression_method: str
    
    @field_validator('compression_method')
    def validate_compression_method(cls, v: str) -> str:
        """Validate compression method."""
        valid_methods = {'zstd', 'lz4'}
        if v not in valid_methods:
            raise ValueError(f'Invalid compression method. Must be one of: {valid_methods}')
        return v
        
    @model_validator(mode='before')
    def validate_compression(cls, values):
        """Validate compression metrics."""
        if values.get('compressed_size') >= values.get('original_size'):
            raise ValueError('Compressed size must be less than original size')
        if values.get('compression_ratio') >= 1:
            raise ValueError('Compression ratio must be less than 1')
        return values

class BetAnalysis(BaseModel):
    """Bet pattern analysis results."""
    user_id: UUID
    game_type: GameType
    total_bets: int = Field(..., ge=0)
    win_rate: float = Field(..., ge=0, le=1)
    avg_bet_amount: float = Field(..., ge=0)
    avg_payout: float = Field(..., ge=0)
    streak_data: Dict[str, Any]
    patterns: List[Dict[str, Any]]
    confidence_score: float = Field(..., ge=0, le=1)
    analysis_timestamp: datetime
    
    @model_validator(mode='before')
    def validate_analysis(cls, values):
        """Validate analysis metrics."""
        if values.get('total_bets') > 0:
            if values.get('avg_bet_amount') <= 0:
                raise ValueError('Average bet amount must be positive for active users')
        return values

class SchemaValidator:
    """Validates bet data against defined schemas."""
    
    def __init__(self):
        """Initialize the schema validator."""
        self.schemas = {
            'bet_data': BetData,
            'bet_record': BetRecord,
            'cleaned_record': CleanedBetRecord,
            'compressed_record': CompressedBetRecord,
            'bet_analysis': BetAnalysis,
            'wheel_state': WheelState,
            'mines_state': MinesState
        }
        
        # Cache for validation results
        self._validation_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = asyncio.Lock()
        
        # Register with concurrency manager's data manager
        concurrency_manager.data_manager._document_cache['validator'] = self
        
    async def validate(
        self,
        data: Dict[str, Any],
        schema_type: str,
        use_cache: bool = True
    ) -> Optional[BaseModel]:
        """
        Validate data against a specified schema.
        
        Args:
            data: Data to validate
            schema_type: Type of schema to validate against
            use_cache: Whether to use validation cache
            
        Returns:
            Validated model instance or None if validation fails
        """
        if use_cache:
            # Try cache first
            cache_key = f"{schema_type}:{hash(str(data))}"
            async with self._cache_lock:
                cached = self._validation_cache.get(cache_key)
                if cached:
                    return cached['result']
        
        try:
            schema_class = self.schemas.get(schema_type)
            if not schema_class:
                raise SchemaError(f"Unknown schema type: {schema_type}")
            
            # Run validation in thread pool to avoid blocking
            result = await concurrency_manager.run_in_thread(
                schema_class.model_validate,
                data
            )
            
            if use_cache and result:
                # Cache successful validation
                async with self._cache_lock:
                    self._validation_cache[cache_key] = {
                        'result': result,
                        'timestamp': datetime.now(timezone.utc)
                    }
            
            return result
            
        except ValidationError as e:
            logger.warning(f"Validation error for schema {schema_type}: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"Error validating against schema {schema_type}: {str(e)}")
            raise SchemaError(f"Validation failed: {str(e)}")
            
    async def validate_batch(
        self,
        items: List[Dict[str, Any]],
        schema_type: str,
        use_cache: bool = True
    ) -> Tuple[List[BaseModel], List[Dict[str, Any]]]:
        """
        Validate a batch of items against a schema.
        
        Args:
            items: List of items to validate
            schema_type: Type of schema to validate against
            use_cache: Whether to use validation cache
            
        Returns:
            Tuple of (valid_items, invalid_items)
        """
        # Process items in parallel using concurrency manager
        validation_tasks = [
            self.validate(item, schema_type, use_cache)
            for item in items
        ]
        
        results = await asyncio.gather(*validation_tasks)
        
        valid_items = []
        invalid_items = []
        
        for item, result in zip(items, results):
            if result:
                valid_items.append(result)
            else:
                invalid_items.append(item)
                
        return valid_items, invalid_items
        
    async def cleanup_cache(self, max_age_seconds: int = 3600) -> None:
        """
        Clean up old validation cache entries.
        
        Args:
            max_age_seconds: Maximum age of cache entries in seconds
        """
        async with self._cache_lock:
            current_time = datetime.now(timezone.utc)
            keys_to_remove = [
                key for key, value in self._validation_cache.items()
                if (current_time - value['timestamp']).total_seconds() > max_age_seconds
            ]
            
            for key in keys_to_remove:
                del self._validation_cache[key]
                
        if keys_to_remove:
            logger.debug(f"Cleaned up {len(keys_to_remove)} validation cache entries")
        
    def get_schema(self, schema_type: str) -> Optional[Type[BaseModel]]:
        """Get a schema class by type."""
        return self.schemas.get(schema_type)
        
    def register_schema(self, schema_type: str, schema_class: Type[BaseModel]) -> None:
        """Register a new schema type."""
        if not issubclass(schema_class, BaseModel):
            raise SchemaError("Schema class must inherit from BaseModel")
        self.schemas[schema_type] = schema_class
        
    async def __aenter__(self):
        """Async context manager entry."""
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.cleanup_cache() 