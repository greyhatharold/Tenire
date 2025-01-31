"""
Retriever module implementing the RAG pipeline for gambling data queries.
"""

# Standard library imports
import json
from typing import Dict, List, Optional, Tuple, TYPE_CHECKING
import asyncio
from dataclasses import dataclass

# Third-party imports
from langchain.prompts import PromptTemplate
from langchain_ollama import OllamaLLM
from sentence_transformers import CrossEncoder

# Local imports
from .docustore import DocumentStore
from tenire.utils.logger import get_logger
from tenire.servicers import SignalManager
from tenire.core.codex import Signal, SignalType
from . import RAGComponent, RAGConfig

logger = get_logger(__name__)

# Type checking imports
if TYPE_CHECKING:
    from tenire.data.seive import DataManager

# Default prompt template for the RAG pipeline
DEFAULT_PROMPT_TEMPLATE = """
You are a talented researcher and game theorist helping to analyze historical gambling data.
Use the following pieces of relevant gambling data to answer the question.
If you don't know the answer, just say that you don't know.

Relevant data (ordered by relevance):
{context}

Question: {question}

Answer: Let me analyze this data for you.
"""

@dataclass
class RetrieverConfig:
    """Configuration for the retriever."""
    rag_config: RAGConfig
    initial_k: int = 20
    final_k: int = 5
    cross_encoder_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    prompt_template: Optional[str] = None

class GamblingDataRetriever(RAGComponent):
    """Implements RAG pipeline for querying gambling data."""
    
    def __init__(
        self,
        config: RetrieverConfig,
        document_store: Optional[DocumentStore] = None,
        data_manager: Optional['DataManager'] = None
    ):
        """Initialize the RAG pipeline with configuration."""
        super().__init__("retriever")
        
        self.config = config
        self.initial_k = config.initial_k
        self.final_k = config.final_k
        self.data_manager = data_manager
        
        # Initialize document store if not provided
        self.document_store = document_store or DocumentStore(config.rag_config)
        
        # Initialize local LLM
        self.llm = OllamaLLM(model=config.rag_config.model_name)
        
        # Initialize cross-encoder for reranking
        self.logger.info(f"Loading cross-encoder model: {config.cross_encoder_model}")
        self.cross_encoder = CrossEncoder(config.cross_encoder_model)
        
        # Set up prompt template
        self.prompt = PromptTemplate(
            template=config.prompt_template or DEFAULT_PROMPT_TEMPLATE,
            input_variables=["context", "question"]
        )
        
        # Register signal handlers
        SignalManager().register_handler(
            SignalType.RAG_QUERY_STARTED,
            self._handle_query_started,
            priority=80
        )
        SignalManager().register_handler(
            SignalType.RAG_DOCUMENT_ADDED,
            self._handle_document_added,
            priority=80
        )
        
        # Emit initialization signal
        self.emit_signal(SignalType.RAG_COMPONENT_INITIALIZED, {
            'config': {
                'rag_config': config.rag_config.__dict__,
                'initial_k': config.initial_k,
                'final_k': config.final_k,
                'cross_encoder_model': config.cross_encoder_model
            }
        })
        
    async def _handle_query_started(self, signal: Signal) -> None:
        """Handle query started signal."""
        query = signal.data.get('query')
        if query:
            try:
                result = await self.query(query)
                await SignalManager().emit(Signal(
                    type=SignalType.RAG_QUERY_COMPLETED,
                    data={
                        'query': query,
                        'result': result,
                        'num_results': len(result) if result else 0
                    },
                    source='retriever'
                ))
            except Exception as e:
                await SignalManager().emit(Signal(
                    type=SignalType.RAG_QUERY_ERROR,
                    data={'query': query, 'error': str(e)},
                    source='retriever'
                ))

    async def _handle_document_added(self, signal: Signal) -> None:
        """Handle document added signal."""
        document = signal.data.get('document')
        if document:
            try:
                await self.add_documents([document])
                await SignalManager().emit(Signal(
                    type=SignalType.RAG_DOCUMENT_ADDED,
                    data={'document_id': document.get('id')},
                    source='retriever'
                ))
            except Exception as e:
                    await SignalManager().emit(Signal(
                    type=SignalType.RAG_COMPONENT_ERROR,
                    data={'error': str(e)},
                    source='retriever'
                ))

    async def _rerank_documents(
        self,
        query: str,
        docs_with_scores: List[Tuple[Dict, float]]
    ) -> List[Tuple[Dict, float]]:
        """
        Rerank documents using cross-encoder.
        
        Args:
            query: Original query string
            docs_with_scores: List of (document, score) tuples from initial retrieval
            
        Returns:
            Reranked list of (document, score) tuples
        """
        if not docs_with_scores:
            return []
            
        try:
            # Get concurrency manager
            from tenire.organizers.concurrency.concurrency_manager import concurrency_manager
            
            # Prepare document texts for reranking
            doc_texts = [json.dumps(doc, sort_keys=True) for doc, _ in docs_with_scores]
            
            # Create query-document pairs for cross-encoder
            pairs = [[query, doc_text] for doc_text in doc_texts]
            
            # Get cross-encoder scores using thread pool
            logger.debug("Computing cross-encoder scores")
            cross_scores = await concurrency_manager.run_in_thread(
                self.cross_encoder.predict,
                pairs
            )
            
            # Combine documents with new scores and sort
            reranked_docs = [
                (doc, float(score))
                for (doc, _), score in zip(docs_with_scores, cross_scores)
            ]
            reranked_docs.sort(key=lambda x: x[1], reverse=True)
            
            # Return top-k reranked documents
            return reranked_docs[:self.final_k]
            
        except Exception as e:
            logger.error(f"Error in document reranking: {str(e)}")
            raise
        
    def _format_documents(self, docs_with_scores: List[Tuple[Dict, float]]) -> str:
        """
        Format retrieved documents for the prompt.
        
        Args:
            docs_with_scores: List of (document, score) tuples
            
        Returns:
            Formatted string of documents
        """
        formatted_docs = []
        for doc, score in docs_with_scores:
            # Format each document as a readable string
            doc_str = json.dumps(doc, indent=2)
            formatted_docs.append(f"Document (relevance score: {score:.4f}):\n{doc_str}")
        return "\n\n".join(formatted_docs)
        
    async def query(self, question: str) -> str:
        """
        Process a question using the RAG pipeline with reranking.
        
        Args:
            question: User's question about the gambling data
            
        Returns:
            Generated answer using retrieved context
        """
        await SignalManager().emit(Signal(
            type=SignalType.RAG_QUERY_STARTED,
            data={'query': question},
            source='retriever'
        ))
        
        try:
            # Get concurrency manager
            from tenire.organizers.concurrency.concurrency_manager import concurrency_manager
            
            logger.info(f"Processing question: {question}")
            
            # Use data manager for search if available
            if self.data_manager:
                initial_docs = await self.data_manager.search_documents(
                    question,
                    k=self.initial_k,
                    use_cache=True
                )
            else:
                # Fallback to direct document store search
                initial_docs = await self.document_store.search(
                    question,
                    k=self.initial_k
                )
            
            if not initial_docs:
                logger.warning("No relevant documents found")
                return "I couldn't find any relevant gambling data to answer your question."
            
            # Rerank documents
            logger.debug("Reranking documents")
            reranked_docs = await self._rerank_documents(question, initial_docs)
            
            # Format documents for prompt
            context = self._format_documents(reranked_docs)
            
            # Generate prompt
            prompt = self.prompt.format(
                context=context,
                question=question
            )
            
            # Get response from LLM using thread pool
            logger.debug("Generating response from LLM")
            response = await concurrency_manager.run_in_thread(
                self.llm,
                prompt
            )
            
            await SignalManager().emit(Signal(
                type=SignalType.RAG_QUERY_COMPLETED,
                data={'query': question, 'result': response},
                source='retriever'
            ))
            return response
            
        except Exception as e:
            await SignalManager().emit(Signal(
                type=SignalType.RAG_QUERY_ERROR,
                data={'query': question, 'error': str(e)},
                source='retriever'
            ))
            raise
        
    async def add_documents(self, documents: List[Dict]) -> None:
        """
        Add new documents to the retriever's document store.
        
        Args:
            documents: List of gambling data documents to add
        """
        try:
            if self.data_manager:
                # Use data manager for document processing
                await self.data_manager.process_documents(
                    documents,
                    self.document_store.embeddings,
                    self.document_store.index
                )
            else:
                # Fallback to direct document store addition
                await self.document_store.add_documents(documents)
            
            await SignalManager().emit(Signal(
                type=SignalType.RAG_DOCUMENT_ADDED,
                data={'num_documents': len(documents)},
                source='retriever'
            ))
        except Exception as e:
            await SignalManager().emit(Signal(
                type=SignalType.RAG_COMPONENT_ERROR,
                data={'error': str(e)},
                source='retriever'
            ))
            raise
        
    def save(self, directory: str) -> None:
        """
        Save the retriever's document store.
        
        Args:
            directory: Directory to save to
        """
        if self.data_manager:
            self.data_manager.save_state(directory)
        else:
            self.document_store.save(directory)
        
    @classmethod
    def load(
        cls,
        directory: str,
        model_name: str = "llama2",
        prompt_template: Optional[str] = None,
        initial_k: int = 20,
        final_k: int = 5,
        cross_encoder_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2",
        data_manager: Optional['DataManager'] = None
    ) -> 'GamblingDataRetriever':
        """
        Load a retriever with a saved document store.
        
        Args:
            directory: Directory to load from
            model_name: Name of local Ollama model to use
            prompt_template: Custom prompt template (optional)
            initial_k: Number of documents to retrieve initially
            final_k: Number of documents to keep after reranking
            cross_encoder_model: Name of cross-encoder model to use
            data_manager: Optional DataManager instance
            
        Returns:
            Loaded GamblingDataRetriever instance
        """
        if data_manager:
            document_store = data_manager._document_store
        else:
            document_store = DocumentStore.load(directory)
            
        return cls(
            config=RetrieverConfig(
                RAGConfig(model_name=model_name),
                initial_k=initial_k,
                final_k=final_k,
                cross_encoder_model=cross_encoder_model,
                prompt_template=prompt_template
            ),
            document_store=document_store,
            data_manager=data_manager
        ) 