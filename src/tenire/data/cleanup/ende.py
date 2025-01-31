"""
Encoder/Decoder module for bet data serialization and deserialization.
"""

# Standard library imports
import base64
import json
import uuid
import zlib
from datetime import datetime
from typing import Any, Dict, List, Union
from pydantic import ValidationError

from tenire.utils.logger import get_logger
from tenire.organizers.compactor import compactor
from tenire.data.validation.schemas import BetRecord, CleanedBetRecord, BetData

logger = get_logger(__name__)

class BetDataCodec:
    """Handles encoding and decoding of bet data."""
    
    def __init__(self):
        """Initialize the codec."""
        # Register cleanup tasks with compactor
        compactor.register_cleanup_task(
            name=f"codec_cleanup_{id(self)}",
            cleanup_func=self.cleanup,
            priority=75,
            is_async=True,
            metadata={"tags": ["data", "encoding"]}
        )
        
    async def cleanup(self) -> None:
        """Cleanup codec resources."""
        try:
            logger.info("Cleaning up codec resources")
            # Force garbage collection
            compactor.force_garbage_collection()
        except Exception as e:
            logger.error(f"Error cleaning up codec: {str(e)}")
            raise
            
    @staticmethod
    def encode_bet_data(data: Union[BetRecord, CleanedBetRecord, Dict[str, Any]], compress: bool = True) -> str:
        """
        Encode bet data to a compressed base64 string.
        
        Args:
            data: Bet record to encode
            compress: Whether to compress the encoded data
            
        Returns:
            Base64 encoded string
        """
        # Convert Pydantic models to dict if needed
        if isinstance(data, (BetRecord, CleanedBetRecord)):
            processed_data = data.model_dump()
        else:
            processed_data = data
            
        # Convert UUIDs to strings
        processed_data = BetDataCodec._process_uuids(processed_data)
        
        # Convert to JSON string
        json_str = json.dumps(processed_data)
        
        if compress:
            # Compress using zlib
            compressed = zlib.compress(json_str.encode('utf-8'))
            # Encode to base64
            return base64.b64encode(compressed).decode('utf-8')
        else:
            # Just encode to base64 without compression
            return base64.b64encode(json_str.encode('utf-8')).decode('utf-8')
    
    @staticmethod
    def decode_bet_data(
        encoded_data: str,
        decompress: bool = True,
        validate: bool = True
    ) -> Union[BetRecord, Dict[str, Any]]:
        """
        Decode bet data from a compressed base64 string.
        
        Args:
            encoded_data: Base64 encoded string
            decompress: Whether to decompress the decoded data
            validate: Whether to validate and return a BetRecord
            
        Returns:
            BetRecord if validate=True, otherwise dictionary containing bet data
        """
        try:
            # Decode base64
            decoded = base64.b64decode(encoded_data)
            
            if decompress:
                # Decompress using zlib
                decompressed = zlib.decompress(decoded)
                # Parse JSON
                data = json.loads(decompressed.decode('utf-8'))
            else:
                # Parse JSON directly
                data = json.loads(decoded.decode('utf-8'))
            
            # Convert string UUIDs back to UUID objects
            data = BetDataCodec._restore_uuids(data)
            
            if validate:
                # Validate and return BetRecord
                return BetRecord(**data)
            return data
            
        except ValidationError as e:
            logger.error(f"Validation error decoding bet data: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"Error decoding bet data: {str(e)}")
            raise
    
    @staticmethod
    def _process_uuids(data: Union[Dict, List, Any]) -> Union[Dict, List, Any]:
        """Convert UUID objects to strings recursively."""
        if isinstance(data, dict):
            return {k: BetDataCodec._process_uuids(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [BetDataCodec._process_uuids(item) for item in data]
        elif isinstance(data, uuid.UUID):
            return str(data)
        elif isinstance(data, datetime):
            return data.isoformat()
        return data
    
    @staticmethod
    def _restore_uuids(data: Union[Dict, List, Any]) -> Union[Dict, List, Any]:
        """Convert UUID strings back to UUID objects recursively."""
        if isinstance(data, dict):
            return {k: BetDataCodec._restore_uuids(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [BetDataCodec._restore_uuids(item) for item in data]
        elif isinstance(data, str):
            try:
                return uuid.UUID(data)
            except ValueError:
                # Try parsing as datetime
                try:
                    return datetime.fromisoformat(data)
                except ValueError:
                    return data
        return data 