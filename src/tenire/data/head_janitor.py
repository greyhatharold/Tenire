"""
Head janitor module for coordinating data cleaning and analysis.
"""
# Standard library imports
import json
from typing import Dict, List, Any, Optional
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Local imports - Services
from tenire.servicers import SignalManager
from tenire.core.codex import Signal, SignalType
from tenire.organizers.compactor import compactor

# Local imports - Data schemas
from tenire.data.validation.schemas import (
    CleanedBetRecord,
    BetAnalysis,
    GameType,
)

# Local imports - RAG components
from tenire.rag.docustore import DocumentStore
from tenire.rag.retriever import GamblingDataRetriever

# Local imports - Data processing
from tenire.data.cleanup.ende import BetDataCodec
from tenire.data.cleanup.cleaner import BetDataCleaner
from tenire.data.cleanup.compressor import BetDataCompressor

# Local imports - Analytics
from tenire.professionals.bet_professional import BetAnalyzer

# Local imports - Utilities
from tenire.utils.logger import get_logger
logger = get_logger(__name__)

class HeadJanitor:
    """
    Coordinates data cleaning, compression, and analysis operations.
    
    This class acts as the main orchestrator for:
    1. Data cleaning and validation
    2. Compression and archival
    3. Pattern analysis and insights
    """
    
    def __init__(
        self,
        data_dir: str = 'data',
        archive_dir: str = 'archive',
        analysis_dir: str = 'analysis',
        max_workers: Optional[int] = None,
        compression_level: int = 3,
        compression_method: str = 'zstd',
        chunk_size: int = 1000,
        min_streak_length: int = 3,
        confidence_threshold: float = 0.7,
        analysis_window_days: int = 30,
        embedding_dim: int = 64
    ):
        """
        Initialize the head janitor.
        
        Args:
            data_dir: Directory for processed data
            archive_dir: Directory for compressed archives
            analysis_dir: Directory for analysis results
            max_workers: Maximum number of worker threads
            compression_level: Compression level for archives
            compression_method: Compression method ('zstd' or 'lz4')
            chunk_size: Number of records per chunk
            min_streak_length: Minimum length for a streak to be considered
            confidence_threshold: Minimum confidence for pattern detection
            analysis_window_days: Number of days to consider for analysis
            embedding_dim: Dimension for game analysis embeddings
        """
        # Create directories
        self.data_dir = Path(data_dir)
        self.archive_dir = Path(archive_dir)
        self.analysis_dir = Path(analysis_dir)
        self.data_dir.mkdir(exist_ok=True)
        self.archive_dir.mkdir(exist_ok=True)
        self.analysis_dir.mkdir(exist_ok=True)
        
        # Initialize pipeline components
        self.cleaner = BetDataCleaner(max_workers=max_workers)
        self.compressor = BetDataCompressor(
            compression_level=compression_level,
            compression_method=compression_method,
            chunk_size=chunk_size
        )
        self.codec = BetDataCodec()  # Initialize codec for serialization
        
        # Initialize analytics component
        self.analyzer = BetAnalyzer(
            min_streak_length=min_streak_length,
            confidence_threshold=confidence_threshold,
            analysis_window_days=analysis_window_days,
            embedding_dim=embedding_dim
        )
        
        # Initialize RAG components
        self.document_store = None
        self.retriever = None
        
        # Register cleanup tasks with compactor
        compactor.register_cleanup_task(
            name=f"head_janitor_cleanup_{id(self)}",
            cleanup_func=self.cleanup,
            priority=95,  # High priority since this coordinates other components
            is_async=True,
            metadata={"tags": ["data", "orchestration"]}
        )
        
        # Register resources with compactor
        compactor.register_resource(self.cleaner)
        compactor.register_resource(self.compressor)
        compactor.register_resource(self.codec)
        compactor.register_resource(self.analyzer)
        
        # Register signal handlers
        SignalManager().register_handler(
            SignalType.RAG_DOCUMENT_ADDED,
            self._handle_document_added,
            priority=90
        )
        SignalManager().register_handler(
            SignalType.RAG_CLEANUP_STARTED,
            self._handle_cleanup_started,
            priority=90
        )
        
    async def process_bet_archive(
        self,
        archive_path: str,
        batch_size: int = 1000,
        run_analysis: bool = True
    ) -> Dict[str, Any]:
        """
        Process a bet archive file through the pipeline.
        
        Args:
            archive_path: Path to the archive file
            batch_size: Number of records to process in each batch
            run_analysis: Whether to run analytics after processing
            
        Returns:
            Dictionary containing processing and analysis statistics
        """
        try:
            logger.info(f"Processing bet archive: {archive_path}")
            stats = {
                'start_time': datetime.now(timezone.utc).isoformat(),
                'archive_path': archive_path,
                'records_processed': 0,
                'records_cleaned': 0,
                'chunks_created': 0
            }
            
            # Read and parse archive file
            raw_records = await self._read_archive_file(archive_path, batch_size)
            stats['total_records'] = len(raw_records)
            
            # Clean and validate records
            logger.info("Cleaning and validating records...")
            cleaned_records = await self.cleaner.clean_bet_records(raw_records)
            stats['records_cleaned'] = len(cleaned_records)
            
            # Encode and save cleaned records
            await self._save_cleaned_records(cleaned_records)
            
            # Compress records
            logger.info("Compressing records...")
            compressed_paths = await self.compressor.compress_records(
                cleaned_records,
                str(self.archive_dir)
            )
            stats['chunks_created'] = len(compressed_paths)
            
            # Run analytics if requested
            if run_analysis:
                logger.info("Running betting analytics...")
                analysis_results = await self._run_analysis(cleaned_records)
                stats['analysis'] = analysis_results
                
                # Save analysis results
                await self._save_analysis_results(analysis_results)
                
                # Add game analysis embeddings to RAG if initialized
                if self.document_store and self.retriever:
                    await self._add_analysis_to_rag(analysis_results)
                    
            # Add cleaned records to RAG if initialized
            if self.document_store and self.retriever:
                await self._add_records_to_rag(cleaned_records)
                stats['added_to_rag'] = True
                
            # Update final stats
            stats['end_time'] = datetime.now(timezone.utc).isoformat()
            stats['success'] = True
            
            # Emit processing completed signal
            await SignalManager().emit(Signal(
                type=SignalType.RAG_DOCUMENT_ADDED,
                data={'archive_path': archive_path, 'stats': stats},
                source='head_janitor'
            ))
            
            return stats
            
        except Exception as e:
            logger.error(f"Error processing bet archive: {str(e)}")
            stats['success'] = False
            stats['error'] = str(e)
            stats['end_time'] = datetime.now(timezone.utc).isoformat()
            
            await SignalManager().emit(Signal(
                type=SignalType.RAG_COMPONENT_ERROR,
                data={'archive_path': archive_path, 'error': str(e)},
                source='head_janitor'
            ))
            
            return stats
            
    async def _read_archive_file(
        self,
        archive_path: str,
        batch_size: int
    ) -> List[Dict[str, Any]]:
        """Read and parse archive file in batches."""
        raw_records = []
        
        with open(archive_path) as f:
            batch = []
            for line in f:
                try:
                    # Try to decode if encoded
                    try:
                        record = BetDataCodec.decode_bet_data(line.strip(), validate=False)
                    except:
                        # If not encoded, try parsing as JSON
                        record = json.loads(line.strip())
                    batch.append(record)
                    
                    if len(batch) >= batch_size:
                        raw_records.extend(batch)
                        batch = []
                        
                except (json.JSONDecodeError, ValueError) as e:
                    logger.warning(f"Invalid record in line: {str(e)}")
                    continue
                    
            # Process remaining records
            if batch:
                raw_records.extend(batch)
                
        return raw_records
        
    async def _save_cleaned_records(self, records: List[CleanedBetRecord]) -> None:
        """Save cleaned records to data directory."""
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        file_path = self.data_dir / f"cleaned_records_{timestamp}.json"
        
        # Encode records before saving
        encoded_records = [
            BetDataCodec.encode_bet_data(record, compress=True)
            for record in records
        ]
        
        with open(file_path, 'w') as f:
            json.dump(encoded_records, f, indent=2)
            
    async def _run_analysis(
        self,
        records: List[CleanedBetRecord]
    ) -> Dict[str, Any]:
        """Run betting pattern analysis."""
        # Group records by game type
        game_analyses = {}
        for game_type in GameType:
            game_records = [r for r in records if r.data.type == game_type]
            if game_records:
                analysis = await self.analyzer.analyze_patterns(game_records)
                game_analyses[game_type] = analysis
                
        return {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'total_records': len(records),
            'game_analyses': game_analyses
        }
        
    async def _save_analysis_results(self, results: Dict[str, Any]) -> None:
        """Save analysis results to file."""
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        file_path = self.analysis_dir / f"analysis_{timestamp}.json"
        
        # Encode results before saving
        encoded_results = BetDataCodec.encode_bet_data(results, compress=True)
        with open(file_path, 'w') as f:
            json.dump(encoded_results, f)
            
    async def _add_analysis_to_rag(self, analysis_results: Dict[str, Any]) -> None:
        """Add game analysis embeddings to RAG system."""
        for game_type, analysis in analysis_results.get('game_analyses', {}).items():
            game_doc = {
                'id': f"game_analysis_{game_type}",
                'content': f"Game Analysis: {game_type}",
                'metadata': {
                    'type': 'game_analysis',
                    'game_type': game_type,
                    'win_rate': analysis['win_rate'],
                    'avg_payout': analysis['avg_payout'],
                    'importance_score': analysis['importance_score']
                },
                'embedding': analysis['embedding']
            }
            await self.document_store.add_documents([game_doc])
            
    async def _add_records_to_rag(self, records: List[CleanedBetRecord]) -> None:
        """Add cleaned records to RAG system."""
        if self.retriever:
            await self.retriever.add_documents(records)
            
    async def analyze_patterns(
        self,
        game_type: GameType,
        user_id: Optional[str] = None,
        time_window_days: Optional[int] = None
    ) -> BetAnalysis:
        """
        Analyze betting patterns for insights.
        
        Args:
            game_type: Type of game to analyze
            user_id: Optional user ID to filter by
            time_window_days: Optional time window in days
            
        Returns:
            Analysis results
        """
        try:
            # Get relevant records
            records = await self._get_relevant_records(
                game_type=game_type,
                user_id=user_id,
                days=time_window_days
            )
            
            # Perform analysis
            analysis = await self.analyzer.analyze_patterns(records)
            
            # Create analysis record
            analysis_record = BetAnalysis(
                user_id=user_id if user_id else "all_users",
                game_type=game_type,
                total_bets=len(records),
                win_rate=analysis['win_rate'],
                avg_bet_amount=analysis['avg_bet_amount'],
                avg_payout=analysis['avg_payout'],
                streak_data=analysis['streaks'],
                patterns=analysis['patterns'],
                confidence_score=analysis['confidence'],
                analysis_timestamp=datetime.now(timezone.utc)
            )
            
            # Save analysis results
            await self._save_analysis(analysis_record)
            
            return analysis_record
            
        except Exception as e:
            logger.error(f"Error analyzing patterns: {str(e)}")
            raise
            
    async def _get_relevant_records(
        self,
        game_type: GameType,
        user_id: Optional[str] = None,
        days: Optional[int] = None
    ) -> List[CleanedBetRecord]:
        """Get relevant records for analysis."""
        # Read all cleaned record files
        all_records = []
        for file_path in self.data_dir.glob('cleaned_records_*.json'):
            with open(file_path) as f:
                encoded_records = json.load(f)
                # Decode records
                records = [
                    CleanedBetRecord(**BetDataCodec.decode_bet_data(encoded, validate=True))
                    for encoded in encoded_records
                ]
                all_records.extend(records)
                
        # Filter records
        filtered_records = []
        cutoff_time = None
        if days:
            cutoff_time = datetime.now(timezone.utc) - timedelta(days=days)
            
        for record in all_records:
            if record.data.type != game_type:
                continue
            if user_id and record.user_id != user_id:
                continue
            if cutoff_time and record.created_at < cutoff_time:
                continue
            filtered_records.append(record)
            
        return filtered_records
        
    async def predict_bet_outcome(
        self,
        game_type: str,
        current_streak_length: int,
        prev_outcome: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Predict the outcome of a potential bet.
        
        Args:
            game_type: Type of game
            current_streak_length: Length of current streak
            prev_outcome: Previous bet outcome
            
        Returns:
            Dictionary containing prediction results
        """
        try:
            # Get current time features
            now = datetime.now(timezone.utc)
            prediction = await self.analyzer.predict_outcome(
                game_type=game_type,
                current_streak_length=current_streak_length,
                hour=now.hour,
                day_of_week=now.weekday(),
                prev_outcome=prev_outcome
            )
            
            return prediction
            
        except Exception as e:
            logger.error(f"Error predicting bet outcome: {str(e)}")
            raise
            
    async def get_analysis_summary(self) -> Dict[str, Any]:
        """
        Get a summary of all betting analysis results.
        
        Returns:
            Dictionary containing analysis summary
        """
        try:
            summary = {
                'total_records_analyzed': len(self.analyzer.df) if self.analyzer.df is not None else 0,
                'total_patterns_found': len(self.analyzer.patterns),
                'total_streaks_detected': len(self.analyzer.streaks),
                'analysis_files': []
            }
            
            # List and decode analysis files
            for file_path in self.analysis_dir.glob('analysis_*.json'):
                with open(file_path) as f:
                    encoded_data = json.load(f)
                    analysis_data = BetDataCodec.decode_bet_data(encoded_data, validate=False)
                    summary['analysis_files'].append({
                        'file': str(file_path),
                        'timestamp': file_path.stem.split('_')[1],
                        'summary': analysis_data.get('summary', {})
                    })
                    
            return summary
            
        except Exception as e:
            logger.error(f"Error getting analysis summary: {str(e)}")
            raise
            
    async def get_processing_stats(self) -> Dict[str, Any]:
        """Get overall processing statistics."""
        stats = {
            'data_dir_size': sum(f.stat().st_size for f in self.data_dir.glob('**/*') if f.is_file()),
            'archive_dir_size': sum(f.stat().st_size for f in self.archive_dir.glob('**/*') if f.is_file()),
            'analysis_dir_size': sum(f.stat().st_size for f in self.analysis_dir.glob('**/*') if f.is_file()),
            'num_chunks': len(list(self.archive_dir.glob(f"*.{self.compressor.compression_method}"))),
            'num_analysis_files': len(list(self.analysis_dir.glob('analysis_*.json'))),
            'compression_method': self.compressor.compression_method,
            'compression_level': self.compressor.compression_level
        }
        
        if self.document_store:
            stats['rag_initialized'] = True
            stats['num_documents'] = len(self.document_store.documents)
        else:
            stats['rag_initialized'] = False
            
        if self.analyzer.df is not None:
            stats['analysis_stats'] = {
                'total_records': len(self.analyzer.df),
                'total_patterns': len(self.analyzer.patterns),
                'total_streaks': len(self.analyzer.streaks)
            }
            
        return stats

    async def initialize_rag(
        self,
        embedding_dim: int = 384,
        use_tfidf: bool = True,
        model_name: str = "llama2"
    ) -> None:
        """
        Initialize RAG components.
        
        Args:
            embedding_dim: Dimension of embeddings
            use_tfidf: Whether to use TF-IDF for text search
            model_name: Name of the LLM model to use
        """
        try:
            # Initialize document store
            self.document_store = DocumentStore(
                embedding_dim=embedding_dim,
                use_tfidf=use_tfidf
            )
            
            # Initialize retriever
            self.retriever = GamblingDataRetriever(
                document_store=self.document_store,
                model_name=model_name
            )
            
            logger.info("RAG components initialized successfully")
            
        except Exception as e:
            logger.error(f"Error initializing RAG components: {str(e)}")
            raise
            
    async def _handle_document_added(self, signal: Signal) -> None:
        """Handle document added signal."""
        if 'records' in signal.data:
            await self.process_bet_archive(signal.data['archive_path'])
            
    async def _handle_cleanup_started(self, signal: Signal) -> None:
        """Handle cleanup started signal."""
        # Implementation for cleanup
        pass
        
    async def shutdown(self):
        """Cleanup resources."""
        # Implementation for cleanup
        pass

    async def _save_analysis(self, analysis: BetAnalysis) -> None:
        """Save analysis results to file."""
        try:
            file_path = self.analysis_dir / f"analysis_{analysis.analysis_timestamp.strftime('%Y%m%d_%H%M%S')}.json"
            # Encode analysis before saving
            encoded_analysis = BetDataCodec.encode_bet_data(analysis.dict(), compress=True)
            with open(file_path, 'w') as f:
                json.dump(encoded_analysis, f)
        except Exception as e:
            logger.error(f"Error saving analysis: {str(e)}")
            raise

    async def cleanup(self) -> None:
        """Cleanup all resources."""
        try:
            logger.info("Starting head janitor cleanup")
            
            # Cleanup pipeline components
            await self.cleaner.cleanup()
            await self.compressor.cleanup()
            await self.codec.cleanup()
            
            # Cleanup analytics
            if hasattr(self.analyzer, 'cleanup'):
                await self.analyzer.cleanup()
                
            # Cleanup RAG components
            if self.document_store:
                await self.document_store.cleanup()
            if self.retriever:
                await self.retriever.cleanup()
                
            # Force garbage collection
            compactor.force_garbage_collection()
            
            logger.info("Head janitor cleanup completed")
            
        except Exception as e:
            logger.error(f"Error during head janitor cleanup: {str(e)}")
            raise 