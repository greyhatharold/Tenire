"""
Betting analytics module combining local models with Claude-powered pattern analysis.
"""

# Standard library imports
import json
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass
import asyncio
import hashlib
from functools import lru_cache
import os

# Third-party imports
import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.preprocessing import StandardScaler
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.decomposition import TruncatedSVD
import anthropic

# Local imports
from tenire.utils.logger import get_logger
from tenire.core.codex import (
    Signal,
    SignalType,
)
from tenire.servicers import get_signal_manager
from tenire.core.config import config_manager
from tenire.data.cacher import MemoryCache, CachePolicy, CacheKey

logger = get_logger(__name__)

@dataclass
class BetStreak:
    """Represents a streak of wins or losses."""
    start_time: datetime
    end_time: datetime
    streak_type: str  # 'win' or 'loss'
    length: int
    total_payout: float
    games: List[Dict[str, Any]]

@dataclass
class GamePattern:
    """Represents a pattern in game outcomes."""
    pattern_type: str
    frequency: int
    avg_payout: float
    confidence: float
    supporting_data: List[Dict[str, Any]]
    logits: Optional[np.ndarray] = None

@dataclass
class GameAnalysis:
    """Represents detailed analysis of a game type."""
    game_type: str
    win_rate: float
    avg_payout: float
    importance_score: float
    feature_importance: Dict[str, float]
    embedding: np.ndarray
    supporting_data: List[Dict[str, Any]]
    patterns: List[GamePattern]
    model_logits: Optional[np.ndarray] = None

class BetAnalyzer:
    """Analyzes betting patterns using hybrid local/Claude approach."""
    
    def __init__(
        self,
        min_streak_length: int = 3,
        confidence_threshold: float = 0.7,
        analysis_window_days: int = 30,
        embedding_dim: int = 64,
        batch_size: int = 100,
        use_claude: bool = True,
        claude_model: str = "claude-3-opus-20240229"
    ):
        """
        Initialize the bet analyzer.
        
        Args:
            min_streak_length: Minimum length for a streak to be considered
            confidence_threshold: Minimum confidence for pattern detection
            analysis_window_days: Number of days to consider for analysis
            embedding_dim: Dimension of game analysis embeddings
            batch_size: Size of batches for API calls
            use_claude: Whether to use Claude for high-level pattern analysis
            claude_model: Claude model version to use
        """
        self.min_streak_length = min_streak_length
        self.confidence_threshold = confidence_threshold
        self.analysis_window_days = analysis_window_days
        self.embedding_dim = embedding_dim
        self.batch_size = batch_size
        self.use_claude = use_claude
        self.claude_model = claude_model
        
        # Initialize models
        self.outcome_model = RandomForestClassifier(
            n_estimators=100,
            max_depth=10,
            n_jobs=-1,
            class_weight='balanced'
        )
        
        self.payout_model = RandomForestRegressor(
            n_estimators=100,
            max_depth=10,
            n_jobs=-1
        )
        
        self.tfidf = TfidfVectorizer(max_features=256)
        self.svd = TruncatedSVD(n_components=embedding_dim)
        self.scaler = StandardScaler()
        
        # Store analysis results
        self.streaks: List[BetStreak] = []
        self.patterns: List[GamePattern] = []
        self.game_analyses: Dict[str, GameAnalysis] = {}
        self.df: Optional[pd.DataFrame] = None
        
        # Initialize cache as None - will be set up in setup()
        self.pattern_cache: Optional[MemoryCache] = None
        self._cleanup_task: Optional[asyncio.Task] = None

    async def setup(self) -> None:
        """Set up the analyzer, including cache initialization."""
        try:
            # Initialize pattern cache
            self.pattern_cache = MemoryCache[str, Dict[str, Any]](
                max_size=1000,
                policy=CachePolicy.TTL,
                ttl=3600  # 1 hour TTL
            )
            
            # Initialize Claude client if needed
            if self.use_claude:
                anthropic_api_key = os.getenv('ANTHROPIC_API_KEY')
                if not anthropic_api_key:
                    logger.warning("ANTHROPIC_API_KEY not found in environment variables")
                    self.use_claude = False
                else:
                    self.claude = anthropic.Client(api_key=anthropic_api_key)
            
            logger.info("BetAnalyzer setup completed successfully")
            
        except Exception as e:
            logger.error(f"Error during BetAnalyzer setup: {str(e)}")
            raise

    async def cleanup(self) -> None:
        """Clean up resources."""
        try:
            if self.pattern_cache:
                await self.pattern_cache.clear()
                if hasattr(self.pattern_cache, '_cleanup_task'):
                    self.pattern_cache._cleanup_task.cancel()
                    try:
                        await self.pattern_cache._cleanup_task
                    except asyncio.CancelledError:
                        pass
            
            logger.info("BetAnalyzer cleanup completed successfully")
            
        except Exception as e:
            logger.error(f"Error during BetAnalyzer cleanup: {str(e)}")
            raise

    async def _get_cached_pattern(self, features: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Get cached pattern analysis result."""
        if not self.pattern_cache:
            return None
            
        key = CacheKey.generate(features, prefix="pattern")
        return await self.pattern_cache.get(key)

    async def _cache_pattern(self, features: Dict[str, Any], result: Dict[str, Any]) -> None:
        """Cache pattern analysis result."""
        if not self.pattern_cache:
            return
            
        key = CacheKey.generate(features, prefix="pattern")
        await self.pattern_cache.set(key, result)

    async def analyze_bets(self, documents: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Analyze betting data using hybrid approach.
        
        Args:
            documents: List of bet documents
            
        Returns:
            Dictionary containing analysis results
        """
        try:
            # Ensure setup is complete
            if not self.pattern_cache:
                await self.setup()
                
            logger.info(f"Analyzing {len(documents)} betting records")
            
            # Prepare data
            self.df = self._prepare_data(documents)
            
            # Train local models efficiently
            self._train_local_models(self.df)
            
            # Detect streaks
            self.streaks = self._detect_streaks(self.df)
            
            # Analyze games with high-level patterns
            self.game_analyses = await self._analyze_game_importance(self.df)
            
            # Calculate summary statistics
            summary = {
                "total_bets": len(documents),
                "win_rate": float(self.df["is_win"].mean()),
                "avg_payout": float(self.df["payout_multiplier"].mean()),
                "longest_win_streak": max((s.length for s in self.streaks if s.streak_type == "win"), default=0),
                "longest_loss_streak": max((s.length for s in self.streaks if s.streak_type == "loss"), default=0),
                "total_streaks": len(self.streaks),
                "games_analyzed": len(self.game_analyses),
                "analysis_window_days": self.analysis_window_days
            }
            
            # Add game importance rankings
            game_rankings = sorted(
                [(game, analysis.importance_score) for game, analysis in self.game_analyses.items()],
                key=lambda x: x[1],
                reverse=True
            )
            summary["top_games"] = [
                {"game": game, "importance": score}
                for game, score in game_rankings[:5]
            ]
            
            # Emit analysis completed signal
            await get_signal_manager().emit(Signal(
                type=SignalType.RAG_COMPONENT_INITIALIZED,
                data={
                    "operation": "bet_analysis",
                    "summary": summary
                },
                source="bet_analyzer"
            ))
            
            return {
                "summary": summary,
                "streaks": [vars(streak) for streak in self.streaks],
                "game_analyses": {
                    game: {
                        "win_rate": analysis.win_rate,
                        "avg_payout": analysis.avg_payout,
                        "importance_score": analysis.importance_score,
                        "feature_importance": analysis.feature_importance,
                        "embedding": analysis.embedding.tolist(),
                        "patterns": [vars(p) for p in analysis.patterns],
                        "model_logits": analysis.model_logits.tolist() if analysis.model_logits is not None else None
                    }
                    for game, analysis in self.game_analyses.items()
                }
            }
            
        except Exception as e:
            logger.error(f"Error analyzing bets: {str(e)}")
            await get_signal_manager().emit(Signal(
                type=SignalType.RAG_COMPONENT_ERROR,
                data={"error": str(e)},
                source="bet_analyzer"
            ))
            raise

    @lru_cache(maxsize=1000)
    def _get_cached_pattern_key(self, features: Dict[str, Any]) -> str:
        """Generate cache key for pattern analysis."""
        features_str = json.dumps(features, sort_keys=True)
        return hashlib.md5(features_str.encode()).hexdigest()

    async def _get_high_level_patterns(
        self,
        game_data: pd.DataFrame,
        local_patterns: List[GamePattern]
    ) -> List[GamePattern]:
        """Get high-level patterns using Claude."""
        if not self.use_claude:
            return local_patterns

        try:
            # Prepare data summary for Claude
            data_summary = {
                "game_stats": {
                    "win_rate": float(game_data["is_win"].mean()),
                    "avg_payout": float(game_data["payout_multiplier"].mean()),
                    "total_games": len(game_data),
                    "hourly_patterns": game_data.groupby("hour")["is_win"].mean().to_dict(),
                    "daily_patterns": game_data.groupby("day_of_week")["is_win"].mean().to_dict(),
                    "streak_patterns": [
                        {
                            "type": pattern.pattern_type,
                            "frequency": pattern.frequency,
                            "confidence": pattern.confidence
                        }
                        for pattern in local_patterns
                    ]
                }
            }

            # Check cache first
            cache_key = self._get_cached_pattern_key(data_summary)
            cached_result = await self._get_cached_pattern(data_summary)
            if cached_result:
                return cached_result

            # Prepare prompt for Claude
            prompt = f"""Analyze this betting pattern data and identify significant patterns, trends, and insights. Focus on:
1. Win rate patterns across different time periods
2. Streak significance and potential predictive value
3. Payout optimization opportunities
4. Risk patterns and anomalies

Data:
{json.dumps(data_summary, indent=2)}

Provide analysis in this JSON format:
{{
    "patterns": [
        {{
            "pattern_type": "string",
            "frequency": number,
            "confidence": number,
            "importance": number,
            "explanation": "string"
        }}
    ],
    "logits": [probabilities for each pattern]
}}"""

            # Get Claude's analysis
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model=self.claude_model,
                max_tokens=1000,
                temperature=0.2,  # Low temperature for more focused analysis
                messages=[{"role": "user", "content": prompt}]
            )

            # Parse Claude's response
            analysis = json.loads(response.content)

            # Convert to GamePattern objects
            enhanced_patterns = []
            for pattern, local_pattern in zip(analysis["patterns"], local_patterns):
                enhanced_pattern = GamePattern(
                    pattern_type=pattern["pattern_type"],
                    frequency=pattern["frequency"],
                    avg_payout=local_pattern.avg_payout,
                    confidence=max(pattern["confidence"], local_pattern.confidence),
                    supporting_data=local_pattern.supporting_data,
                    logits=np.array(analysis["logits"]) if "logits" in analysis else None
                )
                enhanced_patterns.append(enhanced_pattern)

            # Cache the result
            await self._cache_pattern(data_summary, enhanced_patterns)
            
            return enhanced_patterns

        except Exception as e:
            logger.warning(f"Error getting Claude patterns: {str(e)}")
            return local_patterns

    async def _analyze_game_importance(self, df: pd.DataFrame) -> Dict[str, GameAnalysis]:
        """
        Analyze importance and create embeddings for each game type.
        
        Args:
            df: DataFrame with betting data
            
        Returns:
            Dictionary mapping game types to their analysis
        """
        game_analyses = {}
        
        # Get feature importance from the outcome model
        feature_importance = dict(zip(
            ['hour', 'day_of_week', 'month', 'streak_length'],
            self.outcome_model.feature_importances_
        ))
        
        # Process games in batches
        unique_games = df["game_type"].unique()
        for i in range(0, len(unique_games), self.batch_size):
            batch_games = unique_games[i:i + self.batch_size]
            
            for game_type in batch_games:
                game_df = df[df["game_type"] == game_type]
                
                if len(game_df) < 10:  # Skip games with too few samples
                    continue
                    
                # Calculate basic metrics
                win_rate = float(game_df["is_win"].mean())
                avg_payout = float(game_df["payout_multiplier"].mean())
                
                # Calculate importance score
                volume_score = len(game_df) / len(df)
                impact_score = win_rate * avg_payout
                importance_score = volume_score * impact_score
                
                # Get local patterns
                local_patterns = self._detect_patterns(game_df)
                
                # Get high-level patterns
                enhanced_patterns = await self._get_high_level_patterns(game_df, local_patterns)
                
                # Create game description for embedding
                game_desc = f"""
                Game Type: {game_type}
                Win Rate: {win_rate:.2%}
                Average Payout: {avg_payout:.2f}
                Total Bets: {len(game_df)}
                Importance Score: {importance_score:.3f}
                Best Hour: {game_df.groupby('hour')['is_win'].mean().idxmax()}
                Best Day: {game_df.groupby('day_of_week')['is_win'].mean().idxmax()}
                Patterns: {len(enhanced_patterns)}
                """
                
                # Generate embedding
                if not hasattr(self, "game_embeddings_fit"):
                    self.tfidf.fit([game_desc])
                    self.game_embeddings_fit = True
                    
                game_vector = self.tfidf.transform([game_desc])
                if not hasattr(self, "svd_fit"):
                    self.svd.fit(game_vector)
                    self.svd_fit = True
                    
                embedding = self.svd.transform(game_vector)[0]
                
                # Get model logits if available
                model_logits = None
                if hasattr(self.outcome_model, "predict_proba"):
                    X = self._prepare_features(game_df)
                    model_logits = self.outcome_model.predict_proba(X)
                
                # Create game analysis
                analysis = GameAnalysis(
                    game_type=game_type,
                    win_rate=win_rate,
                    avg_payout=avg_payout,
                    importance_score=importance_score,
                    feature_importance=feature_importance,
                    embedding=embedding,
                    supporting_data=game_df.to_dict("records"),
                    patterns=enhanced_patterns,
                    model_logits=model_logits
                )
                
                game_analyses[game_type] = analysis
            
            # Sleep briefly between batches to avoid API rate limits
            if self.use_claude and i + self.batch_size < len(unique_games):
                await asyncio.sleep(0.1)
        
        return game_analyses

    def _prepare_features(self, df: pd.DataFrame) -> np.ndarray:
        """Prepare features for model training/prediction."""
        feature_cols = ['hour', 'day_of_week', 'month', 'streak_length']
        if 'prev_outcome' in df.columns:
            feature_cols.append('prev_outcome')
            
        X = df[feature_cols].fillna(0)
        return self.scaler.transform(X)

    def _prepare_data(self, documents: List[Dict[str, Any]]) -> pd.DataFrame:
        """
        Prepare betting data for analysis.
        
        Args:
            documents: List of bet documents
            
        Returns:
            DataFrame with prepared data
        """
        # Extract relevant features
        records = []
        for doc in documents:
            data = doc.get('data', {})
            record = {
                'id': doc.get('id'),
                'user_id': doc.get('user_id'),
                'store_id': doc.get('store_id'),
                'game_id': data.get('iid', ''),
                'game_type': data.get('game_type', ''),
                'payout_multiplier': float(data.get('payoutMultiplier', 0)),
                'timestamp': pd.to_datetime(data.get('timestamp', datetime.now(timezone.utc).isoformat())),
                'is_win': float(data.get('payoutMultiplier', 0)) > 0
            }
            records.append(record)
            
        # Create DataFrame
        df = pd.DataFrame(records)
        
        # Add derived features
        df['hour'] = df['timestamp'].dt.hour
        df['day_of_week'] = df['timestamp'].dt.dayofweek
        df['month'] = df['timestamp'].dt.month
        
        # Add streak features
        df['prev_outcome'] = df.groupby('user_id')['is_win'].shift(1)
        df['streak_length'] = df.groupby('user_id')['is_win'].transform(
            lambda x: x.groupby((x != x.shift(1)).cumsum()).cumcount() + 1
        )
        
        return df
        
    def _detect_streaks(self, df: pd.DataFrame) -> List[BetStreak]:
        """
        Detect winning and losing streaks.
        
        Args:
            df: DataFrame with betting data
            
        Returns:
            List of detected streaks
        """
        streaks = []
        
        # Group by user and consecutive outcomes
        for user_id in df['user_id'].unique():
            user_df = df[df['user_id'] == user_id].sort_values('timestamp')
            
            # Find streaks
            streak_groups = (user_df['is_win'] != user_df['is_win'].shift()).cumsum()
            for group_id in streak_groups.unique():
                streak_data = user_df[streak_groups == group_id]
                if len(streak_data) >= self.min_streak_length:
                    streak = BetStreak(
                        start_time=streak_data['timestamp'].min(),
                        end_time=streak_data['timestamp'].max(),
                        streak_type='win' if streak_data['is_win'].iloc[0] else 'loss',
                        length=len(streak_data),
                        total_payout=streak_data['payout_multiplier'].sum(),
                        games=streak_data.to_dict('records')
                    )
                    streaks.append(streak)
                    
        return streaks
        
    def _detect_patterns(self, df: pd.DataFrame) -> List[GamePattern]:
        """
        Detect patterns in betting outcomes.
        
        Args:
            df: DataFrame with betting data
            
        Returns:
            List of detected patterns
        """
        patterns = []
        
        # Analyze time-based patterns
        for feature in ['hour', 'day_of_week', 'month']:
            for outcome in [True, False]:
                subset = df[df['is_win'] == outcome]
                if len(subset) > 0:
                    # Calculate win rate for each value
                    rates = df.groupby(feature)['is_win'].mean()
                    
                    # Find significant patterns
                    for value, rate in rates.items():
                        if rate > self.confidence_threshold:
                            pattern = GamePattern(
                                pattern_type=f"High win rate ({rate:.2%}) on {feature}={value}",
                                frequency=len(subset[subset[feature] == value]),
                                avg_payout=float(subset[subset[feature] == value]['payout_multiplier'].mean()),
                                confidence=float(rate),
                                supporting_data=subset[subset[feature] == value].to_dict('records')
                            )
                            patterns.append(pattern)
                            
        # Analyze streak-based patterns
        streak_win_rates = df.groupby('streak_length')['is_win'].mean()
        for length, rate in streak_win_rates.items():
            if rate > self.confidence_threshold and length >= self.min_streak_length:
                pattern = GamePattern(
                    pattern_type=f"High win rate ({rate:.2%}) after {length} consecutive {'wins' if df['is_win'].iloc[-1] else 'losses'}",
                    frequency=len(df[df['streak_length'] == length]),
                    avg_payout=float(df[df['streak_length'] == length]['payout_multiplier'].mean()),
                    confidence=float(rate),
                    supporting_data=df[df['streak_length'] == length].to_dict('records')
                )
                patterns.append(pattern)
                
        return patterns
        
    async def predict_outcome(
        self,
        game_type: str,
        current_streak_length: int,
        hour: int,
        day_of_week: int,
        prev_outcome: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Predict the outcome and payout for a potential bet.
        
        Args:
            game_type: Type of game
            current_streak_length: Length of current streak
            hour: Hour of the day (0-23)
            day_of_week: Day of the week (0-6)
            prev_outcome: Previous bet outcome
            
        Returns:
            Dictionary containing predictions
        """
        try:
            if self.df is None:
                raise ValueError("No data available for predictions. Run analyze_bets first.")
                
            # Prepare features
            features = {
                'hour': hour,
                'day_of_week': day_of_week,
                'month': datetime.now(timezone.utc).month,
                'streak_length': current_streak_length
            }
            if prev_outcome is not None:
                features['prev_outcome'] = float(prev_outcome)
                
            X = pd.DataFrame([features])
            X_scaled = self.scaler.transform(X)
            
            # Make predictions
            win_prob = self.outcome_model.predict_proba(X_scaled)[0][1]
            expected_payout = self.payout_model.predict(X_scaled)[0]
            
            # Get historical stats for context
            game_stats = None
            if game_type:
                game_data = self.df[self.df['game_type'] == game_type]
                if len(game_data) > 0:
                    game_stats = {
                        'historical_win_rate': float(game_data['is_win'].mean()),
                        'avg_payout': float(game_data['payout_multiplier'].mean()),
                        'total_games': len(game_data)
                    }
            
            prediction = {
                'win_probability': float(win_prob),
                'expected_payout': float(expected_payout),
                'confidence': float(win_prob if win_prob > 0.5 else 1 - win_prob),
                'game_stats': game_stats,
                'features_used': features
            }
            
            # Emit prediction signal
            await get_signal_manager().emit(Signal(
                type=SignalType.RAG_QUERY_COMPLETED,
                data={
                    'prediction': prediction,
                    'game_type': game_type
                },
                source='bet_analyzer'
            ))
            
            return prediction
            
        except Exception as e:
            logger.error(f"Error making prediction: {str(e)}")
            await get_signal_manager().emit(Signal(
                type=SignalType.RAG_QUERY_ERROR,
                data={'error': str(e)},
                source='bet_analyzer'
            ))
            raise 