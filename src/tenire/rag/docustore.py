"""
Document store module using FAISS for vector storage and TF-IDF for text search.
Optimized for bet data with field-specific chunking and multiple embedding models.
"""

# Standard library imports
import json
import os
import asyncio
from typing import Dict, List, Tuple, Literal, Any, Optional

# Third-party imports
import faiss
import numpy as np

# Local imports
from tenire.utils.logger import get_logger
from tenire.utils.optimizer import optimizer
from tenire.core.codex import SignalType, Signal
from . import RAGComponent, RAGConfig
from .embeddings import LocalEmbeddings, BetChunk
from tenire.servicers import SignalManager

logger = get_logger(__name__)

class DocumentStore(RAGComponent):
    """Stores and retrieves documents using multiple search methods."""
    
    def __init__(self, config: RAGConfig):
        """Initialize the document store with configuration."""
        super().__init__("document_store")
        
        # Initialize FAISS index with optimizations
        self.index = optimizer.create_optimized_index(config.embedding_dim)
        
        # Store documents and chunks
        self.documents: List[Dict] = []
        self.chunks: List[BetChunk] = []
        self.chunk_to_doc: Dict[str, str] = {}
        
        # Initialize embeddings
        self.embeddings = LocalEmbeddings(config)
        
        # Emit initialization signal
        self.emit_signal(SignalType.RAG_COMPONENT_INITIALIZED, {
            'config': config.__dict__,
            'index_type': type(self.index).__name__
        })
        
        # Register signal handlers
        SignalManager().register_handler(
            SignalType.RAG_DOCUMENT_ADDED,
            self._handle_document_added,
            priority=70
        )
        SignalManager().register_handler(
            SignalType.RAG_CLEANUP_STARTED,
            self._handle_cleanup_started,
            priority=70
        )
            
    async def _handle_document_added(self, signal: Signal) -> None:
        """Handle document added signal."""
        document = signal.data.get('document')
        if document and document not in self.documents:
            try:
                await self.add_documents([document])
            except Exception as e:
                await SignalManager().emit(Signal(
                    type=SignalType.RAG_COMPONENT_ERROR,
                    data={'error': str(e)},
                    source='document_store'
                ))

    async def _handle_cleanup_started(self, signal: Signal) -> None:
        """Handle cleanup started signal."""
        try:
            # Perform any necessary cleanup
            self.index.reset()  # Reset FAISS index
            self.documents.clear()  # Clear documents
            self.chunks.clear()  # Clear chunks
            self.chunk_to_doc.clear()  # Clear mapping
            
            # Reinitialize embeddings with same config
            self.embeddings = LocalEmbeddings(self.embeddings.config)
            
            await SignalManager().emit(Signal(
                type=SignalType.RAG_CLEANUP_COMPLETED,
                data={'component': 'document_store'},
                source='document_store'
            ))
        except Exception as e:
            await SignalManager().emit(Signal(
                type=SignalType.RAG_COMPONENT_ERROR,
                data={'error': str(e)},
                source='document_store'
            ))

    async def add_documents(self, documents: List[Dict]) -> None:
        """
        Add documents to the store.
        
        Args:
            documents: List of JSON-like documents to add
        """
        if not documents:
            logger.warning("No documents provided to add")
            return
            
        logger.info(f"Adding {len(documents)} documents to store")
        
        try:
            # Generate embeddings with chunks
            embeddings_result = self.embeddings.embed_documents(documents)
            
            # Store chunks and update mapping
            self.chunks.extend(embeddings_result['chunks'])
            for chunk in embeddings_result['chunks']:
                self.chunk_to_doc[id(chunk)] = chunk.source_doc_id
            
            # Add to index
            self.index.add(embeddings_result['vector'])
            
            # Store original documents
            self.documents.extend(documents)
            
            logger.debug(f"Store now contains {len(self.documents)} documents and {len(self.chunks)} chunks")
            
            # Emit document added signal
            await SignalManager().emit(Signal(
                type=SignalType.RAG_DOCUMENT_ADDED,
                data={
                    'num_documents': len(documents),
                    'total_documents': len(self.documents),
                    'total_chunks': len(self.chunks),
                    'embedding_shape': embeddings_result['vector'].shape
                },
                source='document_store'
            ))
            
        except Exception as e:
            logger.error(f"Error adding documents: {str(e)}")
            await SignalManager().emit(Signal(
                type=SignalType.RAG_COMPONENT_ERROR,
                data={'error': str(e)},
                source='document_store'
            ))
            raise
            
    async def search(
        self,
        query: str,
        k: int = 5,
        method: Literal['vector', 'text', 'hybrid'] = 'hybrid',
        alpha: float = 0.5,  # Weight for hybrid search
        chunk_type: Optional[str] = None  # Focus on specific field type
    ) -> List[Tuple[Dict, float]]:
        """
        Search for similar documents using specified method.
        
        Args:
            query: Search query string
            k: Number of results to return
            method: Search method to use ('vector', 'text', or 'hybrid')
            alpha: Weight between vector and text scores for hybrid search
            chunk_type: Optional field type to focus search on
            
        Returns:
            List of (document, score) tuples
        """
        try:
            # Emit search started signal
            await SignalManager().emit(Signal(
                type=SignalType.RAG_QUERY_STARTED,
                data={
                    'query': query,
                    'k': k,
                    'method': method,
                    'alpha': alpha,
                    'chunk_type': chunk_type
                },
                source='document_store'
            ))
            
            if not self.chunks:
                logger.warning("No chunks in store")
                return []
            
            # Get query embeddings
            query_embeddings = self.embeddings.embed_query(query, chunk_type=chunk_type)
            
            if method == 'vector' or (method == 'hybrid' and alpha == 1.0):
                results = await self._vector_search(query_embeddings['vector'], k, chunk_type)
            elif method == 'text' or (method == 'hybrid' and alpha == 0.0):
                if not self.embeddings.use_tfidf:
                    logger.warning("Text search requested but TF-IDF is disabled")
                    results = await self._vector_search(query_embeddings['vector'], k, chunk_type)
                else:
                    results = await self._text_search(
                        query_embeddings['tfidf_vector'],
                        k,
                        chunk_type
                    )
            else:  # hybrid search
                results = await self._hybrid_search(query_embeddings, k, alpha, chunk_type)
            
            # Emit search completed signal
            await SignalManager().emit(Signal(
                type=SignalType.RAG_QUERY_COMPLETED,
                data={
                    'query': query,
                    'num_results': len(results),
                    'method': method,
                    'chunk_type': chunk_type
                },
                source='document_store'
            ))
            
            return results
            
        except Exception as e:
            logger.error(f"Error during search: {str(e)}")
            await SignalManager().emit(Signal(
                type=SignalType.RAG_QUERY_ERROR,
                data={
                    'query': query,
                    'error': str(e)
                },
                source='document_store'
            ))
            raise
            
    async def _vector_search(
        self,
        query_vector: np.ndarray,
        k: int,
        chunk_type: Optional[str] = None
    ) -> List[Tuple[Dict, float]]:
        """Perform vector similarity search using FAISS."""
        try:
            # Get concurrency manager
            from tenire.organizers.concurrency.concurrency_manager import concurrency_manager
            
            # Search FAISS index with nprobe optimization for IVF
            if isinstance(self.index, faiss.IndexIVFFlat):
                self.index.nprobe = min(20, k * 2)
                
            # Run FAISS search in thread pool
            distances, indices = await concurrency_manager.run_in_thread(
                self.index.search,
                query_vector.reshape(1, -1),
                min(k * 2, len(self.chunks))  # Get more results for filtering
            )
            
            # Filter and combine results
            results = []
            seen_docs = set()
            
            for idx, dist in zip(indices[0], distances[0]):
                chunk = self.chunks[idx]
                doc_id = chunk.source_doc_id
                
                # Skip if we already have this document
                if doc_id in seen_docs:
                    continue
                    
                # Skip if chunk type doesn't match (if specified)
                if chunk_type and chunk.chunk_type != chunk_type:
                    continue
                    
                # Find original document
                doc = next((d for d in self.documents if d['id'] == doc_id), None)
                if doc:
                    results.append((doc, float(dist)))
                    seen_docs.add(doc_id)
                    
                if len(results) >= k:
                    break
                    
            return results[:k]
            
        except Exception as e:
            logger.error(f"Error in vector search: {str(e)}")
            raise
        
    async def _text_search(
        self,
        query_vector: Any,
        k: int,
        chunk_type: Optional[str] = None
    ) -> List[Tuple[Dict, float]]:
        """Perform text similarity search using TF-IDF."""
        # Get document matrix
        doc_matrix = self.embeddings.get_tfidf_matrix()
        if doc_matrix is None:
            logger.error("TF-IDF matrix not available")
            return []
            
        # Compute similarities with chunk type weighting
        similarities = self.embeddings.compute_text_similarity(
            query_vector,
            doc_matrix,
            chunk_types=[chunk.chunk_type for chunk in self.chunks],
            chunk_type=chunk_type
        )
        
        # Get top-k unique documents
        results = []
        seen_docs = set()
        sorted_indices = np.argsort(similarities)[::-1]
        
        for idx in sorted_indices:
            chunk = self.chunks[idx]
            doc_id = chunk.source_doc_id
            
            if doc_id in seen_docs:
                continue
                
            if chunk_type and chunk.chunk_type != chunk_type:
                continue
                
            doc = next((d for d in self.documents if d['id'] == doc_id), None)
            if doc:
                results.append((doc, float(similarities[idx])))
                seen_docs.add(doc_id)
                
            if len(results) >= k:
                break
                
        return results[:k]
        
    async def _hybrid_search(
        self,
        query_embeddings: Dict[str, Any],
        k: int,
        alpha: float,
        chunk_type: Optional[str] = None
    ) -> List[Tuple[Dict, float]]:
        """Combine vector and text similarity scores."""
        # Get both types of results
        vector_results = await self._vector_search(
            query_embeddings['vector'],
            k * 2,  # Get more results for better combination
            chunk_type
        )
        
        text_results = (
            await self._text_search(query_embeddings['tfidf_vector'], k * 2, chunk_type)
            if self.embeddings.use_tfidf else None
        )
        
        if text_results is None:
            return vector_results[:k]
            
        # Combine scores efficiently
        all_docs = list(set(
            [doc['id'] for doc, _ in vector_results] +
            [doc['id'] for doc, _ in text_results]
        ))
        
        # Create score arrays
        vector_scores = np.zeros(len(all_docs))
        text_scores = np.zeros(len(all_docs))
        
        # Map document IDs to indices
        doc_to_idx = {doc_id: i for i, doc_id in enumerate(all_docs)}
        
        # Fill score arrays
        for doc, score in vector_results:
            vector_scores[doc_to_idx[doc['id']]] = score
            
        for doc, score in text_results:
            text_scores[doc_to_idx[doc['id']]] = score
            
        # Normalize scores
        vector_scores = (vector_scores - vector_scores.min()) / (vector_scores.max() - vector_scores.min() + 1e-6)
        text_scores = (text_scores - text_scores.min()) / (text_scores.max() - text_scores.min() + 1e-6)
        
        # Combine scores with field weighting
        weight = query_embeddings.get('weight', 1.0)
        combined_scores = (alpha * vector_scores + (1 - alpha) * text_scores) * weight
        
        # Get top-k results
        top_k_indices = np.argsort(combined_scores)[-k:][::-1]
        
        # Create final results
        results = [
            (next(doc for doc in self.documents if doc['id'] == all_docs[idx]),
             float(combined_scores[idx]))
            for idx in top_k_indices
        ]
        
        return results
        
    async def save(self, directory: str) -> None:
        """
        Save the document store to disk.
        
        Args:
            directory: Directory to save to
        """
        try:
            os.makedirs(directory, exist_ok=True)
            
            # Save FAISS index
            index_path = os.path.join(directory, "index.faiss")
            logger.info(f"Saving FAISS index to {index_path}")
            faiss.write_index(self.index, index_path)
            
            # Save documents
            docs_path = os.path.join(directory, "documents.json")
            logger.info(f"Saving documents to {docs_path}")
            with open(docs_path, 'w') as f:
                json.dump(self.documents, f)
                
            # Emit save completed signal
            self.emit_signal(SignalType.RAG_COMPONENT_INITIALIZED, {
                'operation': 'save',
                'directory': directory,
                'num_documents': len(self.documents)
            })
            
        except Exception as e:
            logger.error(f"Error saving document store: {str(e)}")
            self.emit_signal(SignalType.RAG_COMPONENT_ERROR, {'error': str(e)})
            raise
        
    @classmethod
    async def load(
        cls,
        directory: str,
        use_tfidf: bool = True,
        ngram_range: tuple = (1, 2),
        model_name: str = 'all-MiniLM-L6-v2',
        model_type: Literal['sentence_transformers', 'ollama'] = 'sentence_transformers',
        ollama_kwargs: Optional[Dict[str, Any]] = None
    ) -> 'DocumentStore':
        """
        Load a document store from disk.
        
        Args:
            directory: Directory to load from
            use_tfidf: Whether to enable text similarity search
            ngram_range: Range of n-grams for TF-IDF
            model_name: Name of the embedding model to use
            model_type: Type of embedding model
            ollama_kwargs: Additional kwargs for Ollama model
            
        Returns:
            Loaded DocumentStore instance
        """
        logger.info(f"Loading document store from {directory}")
        
        # Create store instance
        store = cls(RAGConfig(
            embedding_dim=384,  # Will be updated after loading index
            use_tfidf=use_tfidf,
            ngram_range=ngram_range,
            model_name=model_name,
            model_type=model_type,
            ollama_kwargs=ollama_kwargs
        ))
        
        # Load FAISS index
        index_path = os.path.join(directory, "index.faiss")
        store.index = faiss.read_index(index_path)
        
        # Load documents
        docs_path = os.path.join(directory, "documents.json")
        with open(docs_path) as f:
            store.documents = json.load(f)
        
        # Optimize FAISS for M4 Max
        optimizer.optimize_faiss(store.index)
        
        # Rebuild embeddings if needed
        if use_tfidf or store.embeddings.chunk_fields:
            await store.embeddings.embed_documents(store.documents)
        
        logger.info(f"Loaded store with {len(store.documents)} documents")
        return store 