"""
Local embeddings module supporting both vector and text similarity with bet-specific optimizations.
Supports both SentenceTransformers and Ollama models.
"""

# Standard library imports
import json
from typing import List, Dict, Any, Optional, Literal
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

# Third-party imports
import numpy as np
import torch
from sentence_transformers import SentenceTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from langchain_ollama import OllamaLLM
from langchain.callbacks.base import BaseCallbackHandler

# Local imports
from tenire.utils.logger import get_logger
from tenire.utils.optimizer import optimizer
from tenire.servicers import SignalManager
from tenire.core.codex import Signal, SignalType
from . import RAGComponent, RAGConfig

logger = get_logger(__name__)

class OllamaCallbackHandler(BaseCallbackHandler):
    """Callback handler for Ollama embeddings."""
    
    def __init__(self):
        """Initialize the callback handler."""
        self.current_embeddings = []
        
    def on_llm_new_token(self, token: str, **kwargs) -> None:
        """Handle new token from Ollama."""
        try:
            # Parse embedding from token
            if token.strip():
                values = json.loads(token)
                if isinstance(values, list):
                    self.current_embeddings.extend(values)
        except json.JSONDecodeError:
            pass
            
    def get_embeddings(self) -> List[float]:
        """Get collected embeddings."""
        embeddings = self.current_embeddings
        self.current_embeddings = []
        return embeddings

@dataclass
class BetChunk:
    """Represents a chunk of bet data for embedding."""
    content: str
    metadata: Dict[str, Any]
    source_doc_id: str
    chunk_type: str
    embedding: Optional[np.ndarray] = None

class LocalEmbeddings(RAGComponent):
    """Handles document and query embeddings using multiple methods."""
    
    def __init__(self, config: RAGConfig):
        """Initialize embeddings with configuration."""
        super().__init__("embeddings")
        
        # Configure device
        self.device = optimizer.get_optimal_device()
        
        # Initialize model based on type
        if config.model_type == 'sentence_transformers':
            self.model = SentenceTransformer(config.model_name, device=self.device)
            optimizer.optimize_sentence_transformers(self.model)
        else:
            self.model = OllamaLLM(model=config.model_name, **(config.ollama_kwargs or {}))
            self.callback_handler = OllamaCallbackHandler()
            
        self.model_type = config.model_type
        self.use_tfidf = config.use_tfidf
        self.ngram_range = config.ngram_range
        self.chunk_fields = config.chunk_fields
        self.field_weights = config.field_weights or {
            'game': 1.0,
            'user': 0.8,
            'payout': 0.6,
            'full': 1.0
        }
        
        if config.use_tfidf:
            self.tfidf = TfidfVectorizer(
                ngram_range=config.ngram_range,
                analyzer='word',
                stop_words='english',
                max_features=10000
            )
            self._tfidf_matrix = None
            self._document_texts = []
            
        # Emit initialization signal
        self.emit_signal(SignalType.RAG_COMPONENT_INITIALIZED, {
            'config': config.__dict__,
            'device': self.device
        })
            
    async def _get_ollama_embeddings(self, texts: List[str]) -> np.ndarray:
        """
        Get embeddings from Ollama model.
        
        Args:
            texts: List of texts to embed
            
        Returns:
            Numpy array of embeddings
        """
        embeddings = []
        for text in texts:
            # Reset callback handler
            self.callback_handler.current_embeddings = []
            
            # Get embedding from Ollama
            await self.model.agenerate(
                [text],
                callbacks=[self.callback_handler]
            )
            
            # Get embeddings from callback handler
            embedding = self.callback_handler.get_embeddings()
            embeddings.append(embedding)
            
        return np.array(embeddings)
        
    def _create_bet_chunks(self, document: Dict[str, Any]) -> List[BetChunk]:
        """
        Create chunks from a bet document based on fields.
        
        Args:
            document: Bet document to chunk
            
        Returns:
            List of BetChunk objects
        """
        chunks = []
        doc_id = document.get('id', 'unknown')
        
        if not self.chunk_fields:
            # Create single chunk with full document
            full_text = json.dumps(document, sort_keys=True)
            chunks.append(BetChunk(
                content=full_text,
                metadata=document,
                source_doc_id=doc_id,
                chunk_type='full'
            ))
            return chunks
            
        # Extract game-related information
        if 'data' in document:
            game_data = {
                'iid': document['data'].get('iid', ''),
                'game_type': document['data'].get('game_type', ''),
                'payoutMultiplier': document['data'].get('payoutMultiplier', 0)
            }
            game_text = f"Game {game_data['iid']} of type {game_data['game_type']} with payout multiplier {game_data['payoutMultiplier']}"
            chunks.append(BetChunk(
                content=game_text,
                metadata=game_data,
                source_doc_id=doc_id,
                chunk_type='game'
            ))
            
        # Extract user-related information
        user_data = {
            'user_id': document.get('user_id', ''),
            'store_id': document.get('store_id', ''),
            'ip': document['data'].get('ip', '') if 'data' in document else ''
        }
        user_text = f"User {user_data['user_id']} from store {user_data['store_id']} with IP {user_data['ip']}"
        chunks.append(BetChunk(
            content=user_text,
            metadata=user_data,
            source_doc_id=doc_id,
            chunk_type='user'
        ))
        
        # Extract payout-related information
        if 'data' in document:
            payout_data = {
                'payoutMultiplier': document['data'].get('payoutMultiplier', 0),
                'timestamp': document['data'].get('timestamp', datetime.now(timezone.utc).isoformat())
            }
            payout_text = f"Payout multiplier {payout_data['payoutMultiplier']} at {payout_data['timestamp']}"
            chunks.append(BetChunk(
                content=payout_text,
                metadata=payout_data,
                source_doc_id=doc_id,
                chunk_type='payout'
            ))
            
        # Always include full document as a chunk
        full_text = json.dumps(document, sort_keys=True)
        chunks.append(BetChunk(
            content=full_text,
            metadata=document,
            source_doc_id=doc_id,
            chunk_type='full'
        ))
        
        return chunks
        
    async def embed_documents(
        self,
        documents: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Generate embeddings for a list of bet documents.
        
        Args:
            documents: List of bet documents to embed
            
        Returns:
            Dictionary containing different types of embeddings
        """
        try:
            # Get concurrency manager
            from tenire.organizers.concurrency.concurrency_manager import concurrency_manager
            
            # Create chunks for all documents
            all_chunks: List[BetChunk] = []
            for doc in documents:
                chunks = self._create_bet_chunks(doc)
                all_chunks.extend(chunks)
                
            # Generate embeddings for chunks
            chunk_texts = [chunk.content for chunk in all_chunks]
            
            if self.model_type == 'sentence_transformers':
                # Process in batches using thread pool
                batch_size = concurrency_manager.data_manager.batch_size
                vectors = []
                
                for i in range(0, len(chunk_texts), batch_size):
                    batch = chunk_texts[i:i + batch_size]
                    batch_vectors = await concurrency_manager.run_in_thread(
                        self.model.encode,
                        batch,
                        convert_to_numpy=True,
                        device=self.device,
                        show_progress_bar=len(batch) > 1000
                    )
                    vectors.append(batch_vectors)
                    
                vectors = np.vstack(vectors)
            else:  # ollama
                vectors = await self._get_ollama_embeddings(chunk_texts)
                
            # Assign embeddings to chunks
            for chunk, vector in zip(all_chunks, vectors):
                chunk.embedding = vector
                
            # Organize results by chunk type
            result = {
                'chunks': all_chunks,
                'vector': np.vstack([chunk.embedding for chunk in all_chunks]),
                'chunk_types': [chunk.chunk_type for chunk in all_chunks],
                'source_doc_ids': [chunk.source_doc_id for chunk in all_chunks]
            }
            
            if self.use_tfidf:
                # Update TF-IDF matrix using thread pool
                if self._tfidf_matrix is None:
                    self._tfidf_matrix = await concurrency_manager.run_in_thread(
                        self.tfidf.fit_transform,
                        chunk_texts
                    )
                    self._document_texts = chunk_texts
                else:
                    all_texts = self._document_texts + chunk_texts
                    self._tfidf_matrix = await concurrency_manager.run_in_thread(
                        self.tfidf.fit_transform,
                        all_texts
                    )
                    self._document_texts = all_texts
                    
                result['tfidf_matrix'] = self._tfidf_matrix
                
            # Emit embedding completed signal
            asyncio.create_task(SignalManager().emit(Signal(
                type=SignalType.RAG_EMBEDDING_COMPLETED,
                data={
                    'num_documents': len(documents),
                    'num_chunks': len(all_chunks),
                    'vector_shape': vectors.shape,
                    'has_tfidf': self.use_tfidf,
                    'model_type': self.model_type
                },
                source='embeddings'
            )))
            
            return result
            
        except Exception as e:
            logger.error(f"Error generating embeddings: {str(e)}")
            asyncio.create_task(SignalManager().emit(Signal(
                type=SignalType.RAG_EMBEDDING_ERROR,
                data={'error': str(e)},
                source='embeddings'
            )))
            raise
            
    async def embed_query(
        self,
        query: str,
        chunk_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Generate embeddings for a query string.
        
        Args:
            query: Query string to embed
            chunk_type: Optional chunk type to focus on
            
        Returns:
            Dictionary containing embeddings
        """
        try:
            logger.debug(f"Generating embeddings for query: {query}")
            
            # Generate vector embedding
            if self.model_type == 'sentence_transformers':
                vector = self.model.encode(
                    [query],
                    convert_to_numpy=True,
                    device=self.device
                )[0]
            else:  # ollama
                vector = (await self._get_ollama_embeddings([query]))[0]
            
            result = {
                'vector': vector,
                'chunk_type': chunk_type,
                'weight': self.field_weights.get(chunk_type, 1.0) if chunk_type else 1.0
            }
            
            if self.use_tfidf and self._tfidf_matrix is not None:
                result['tfidf_vector'] = self.tfidf.transform([query])
                
            return result
            
        except Exception as e:
            logger.error(f"Error generating query embeddings: {str(e)}")
            raise
            
    def compute_text_similarity(
        self,
        query_vector: Any,
        document_matrix: Any,
        chunk_types: Optional[List[str]] = None,
        chunk_type: Optional[str] = None
    ) -> np.ndarray:
        """
        Compute text similarity scores.
        
        Args:
            query_vector: Query TF-IDF vector
            document_matrix: Document TF-IDF matrix
            chunk_types: List of chunk types for each document
            chunk_type: Optional chunk type to focus on
            
        Returns:
            Array of similarity scores
        """
        similarities = cosine_similarity(query_vector, document_matrix).flatten()
        
        if chunk_types and chunk_type:
            # Apply weights based on chunk type matching
            weights = np.array([
                self.field_weights.get(ct, 1.0) if ct == chunk_type else 0.5
                for ct in chunk_types
            ])
            similarities *= weights
            
        return similarities
        
    def get_tfidf_matrix(self) -> Any:
        """Get the current TF-IDF matrix."""
        return self._tfidf_matrix if self.use_tfidf else None 