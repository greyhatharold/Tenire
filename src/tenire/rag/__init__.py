"""
RAG (Retrieval Augmented Generation) module for querying historical gambling data.
"""

from dataclasses import dataclass
from typing import Dict, Any, Optional, Literal
import asyncio

from tenire.core.codex import Signal, SignalType
from tenire.servicers import SignalManager
from tenire.utils.logger import get_logger

# Base configuration for RAG components
@dataclass
class RAGConfig:
    """Shared configuration for RAG components."""
    model_name: str
    model_type: Literal['sentence_transformers', 'ollama'] = 'sentence_transformers'
    embedding_dim: int = 384
    use_tfidf: bool = True
    ngram_range: tuple = (1, 2)
    chunk_fields: bool = True
    field_weights: Optional[Dict[str, float]] = None
    ollama_kwargs: Optional[Dict[str, Any]] = None

# Base class for RAG components with shared functionality
class RAGComponent:
    """Base class for RAG components with shared signal handling."""
    
    def __init__(self, name: str):
        self.logger = get_logger(f"rag.{name}")
        self.signal_manager = SignalManager()
        
    async def emit_signal(self, signal_type: SignalType, data: Dict[str, Any]) -> None:
        """Emit a signal with standard format."""
        await self.signal_manager.emit(Signal(
            type=signal_type,
            data=data,
            source=self.__class__.__name__.lower()
        ))
        
    async def emit_error(self, error: Exception, context: Dict[str, Any]) -> None:
        """Emit an error signal with standard format."""
        await self.emit_signal(
            SignalType.RAG_COMPONENT_ERROR,
            {
                'error': str(error),
                'context': context,
                'component': self.__class__.__name__.lower()
            }
        )

# Import components after base classes
from .docustore import DocumentStore
from .embeddings import LocalEmbeddings
from .retriever import GamblingDataRetriever, RetrieverConfig

__all__ = [
    'RAGConfig',
    'RAGComponent',
    'DocumentStore',
    'LocalEmbeddings',
    'GamblingDataRetriever',
    'RetrieverConfig'
] 