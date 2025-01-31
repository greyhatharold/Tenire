"""
Configuration management system for the Tenire framework.

This module provides a flexible configuration system that supports:
1. Multiple configuration sources (env vars, files, defaults)
2. Dynamic configuration updates
3. Type validation and conversion
4. Environment-specific settings
5. Secure credential management
"""

# Standard library imports
import os
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional, Union
import json
import threading

# Third-party imports
import yaml
from pydantic import BaseModel, Field
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Constants
DEFAULT_CONFIG_PATH = Path("config/default.yaml")
ENV_CONFIG_PREFIX = "TENIRE_"

class Environment(Enum):
    """Available environment types."""
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"
    TESTING = "testing"

class CacheConfig(BaseModel):
    """Cache-specific configuration."""
    memory_size: int = Field(default=10000, description="Maximum number of items in memory cache")
    disk_size: int = Field(default=1_000_000, description="Maximum size of disk cache in bytes")
    disk_path: Optional[str] = Field(default=None, description="Path to disk cache directory")
    ttl: int = Field(default=3600, description="Default TTL in seconds")
    policy: str = Field(default="lru", description="Cache eviction policy")
    
    class Config:
        use_enum_values = True

class LoggingConfig(BaseModel):
    """Logging configuration."""
    level: str = Field(default="DEBUG", description="Default log level")
    format: str = Field(
        default="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        description="Log format string"
    )
    file_path: Optional[str] = Field(default=None, description="Log file path")
    max_size: int = Field(default=10_485_760, description="Max log file size in bytes")
    backup_count: int = Field(default=5, description="Number of backup log files")

class DatabaseConfig(BaseModel):
    """Database configuration."""
    url: str = Field(..., description="Database connection URL")
    pool_size: int = Field(default=5, description="Connection pool size")
    max_overflow: int = Field(default=10, description="Maximum number of connections")
    pool_timeout: int = Field(default=30, description="Connection pool timeout")
    echo: bool = Field(default=False, description="SQL echo mode")

class SecurityConfig(BaseModel):
    """Security configuration."""
    secret_key: str = Field(..., description="Secret key for cryptographic operations")
    token_expiry: int = Field(default=3600, description="Token expiry in seconds")
    allowed_origins: List[str] = Field(default_factory=list, description="CORS allowed origins")
    ssl_verify: bool = Field(default=True, description="Verify SSL certificates")

class BettingConfig(BaseModel):
    """Betting-specific configuration."""
    default_amount: float = Field(default=1.0, description="Default bet amount")
    min_amount: float = Field(default=0.1, description="Minimum bet amount")
    max_amount: float = Field(default=100.0, description="Maximum bet amount")
    cooldown_minutes: int = Field(default=1, description="Cooldown period between bets")
    daily_limit: float = Field(default=1000.0, description="Daily betting limit")
    session_limit: float = Field(default=500.0, description="Session betting limit")
    
    class Config:
        use_enum_values = True

class ChatGPTConfig(BaseModel):
    """ChatGPT-specific configuration."""
    api_key: str = Field(..., description="ChatGPT API key")
    model: str = Field(default="gpt-o1", description="Model to use")
    temperature: float = Field(default=0.025, description="Sampling temperature")
    max_tokens: int = Field(default=1000, description="Maximum tokens per request")
    top_p: float = Field(default=1.0, description="Top p sampling parameter")
    frequency_penalty: float = Field(default=0.0, description="Frequency penalty")
    presence_penalty: float = Field(default=0.0, description="Presence penalty")
    stop: Optional[str] = Field(default=None, description="Stop sequence")
    max_context_length: int = Field(default=10000, description="Maximum context length")
    system_prompt: str = Field(default="", description="System prompt")
    user_prompt: str = Field(default="", description="User prompt template")
    response_format: str = Field(default="json", description="Response format")
    response_format_json_schema: Optional[str] = Field(default=None, description="JSON schema for response")
    max_concurrent_requests: int = Field(default=5, description="Maximum number of concurrent API requests")

class StakeConfig(BaseModel):
    """Stake-specific configuration."""
    username: str = Field(..., description="Stake username")
    password: str = Field(..., description="Stake password")

class GUIConfig(BaseModel):
    """GUI-specific configuration."""
    sidebar_width: int = Field(default=400, description="Width of the GUI sidebar in pixels")
    update_interval: int = Field(default=100, description="GUI update interval in milliseconds")
    max_messages: int = Field(default=1000, description="Maximum number of messages to keep in history")
    theme: str = Field(default="dark", description="GUI theme (light/dark)")
    
    class Config:
        use_enum_values = True

class Config(BaseModel):
    """Main configuration class."""
    environment: Environment = Field(
        default=Environment.DEVELOPMENT,
        description="Current environment"
    )
    debug: bool = Field(default=False, description="Debug mode flag")
    testing: bool = Field(default=False, description="Testing mode flag")
    
    # Component configurations
    cache: CacheConfig = Field(default_factory=CacheConfig, description="Cache settings")
    logging: LoggingConfig = Field(default_factory=LoggingConfig, description="Logging settings")
    database: Optional[DatabaseConfig] = Field(default=None, description="Database settings")
    security: Optional[SecurityConfig] = Field(default=None, description="Security settings")
    betting: BettingConfig = Field(default_factory=BettingConfig, description="Betting settings")
    chatgpt: ChatGPTConfig = Field(..., description="ChatGPT settings")
    stake: StakeConfig = Field(..., description="Stake settings")
    gui: GUIConfig = Field(default_factory=GUIConfig, description="GUI settings")
    
    # Custom settings
    max_workers: int = Field(default=4, description="Maximum number of worker threads")
    batch_size: int = Field(default=1000, description="Default batch size for operations")
    request_timeout: int = Field(default=30, description="Default request timeout in seconds")
    
    class Config:
        use_enum_values = True
        
    @classmethod
    def load_from_file(cls, path: Union[str, Path]) -> "Config":
        """Load configuration from a YAML file."""
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"Config file not found: {path}")
            
        with open(path) as f:
            config_data = yaml.safe_load(f)
            return cls(**config_data)
            
    @classmethod
    def load_from_env(cls) -> "Config":
        """
        Load configuration from environment variables.
        
        Environment variables should be prefixed with TENIRE_ and use underscore
        as separator for nested keys. For example:
        TENIRE_CHATGPT_API_KEY=your-api-key
        TENIRE_STAKE_USERNAME=your-username
        """
        # Initialize with default configurations
        config_data = {
            'chatgpt': {},
            'stake': {},
            'betting': {},
            'cache': {},  # Deprecated: Use CacheConfig().dict() if needed
            'logging': {},  # Deprecated: Use LoggingConfig().dict() if needed
            'security': None,  # Make security optional
            'database': None,  # Make database optional
            'environment': Environment.DEVELOPMENT.value,
            'debug': False,
            'testing': False,
            'max_workers': 4,
            'batch_size': 1000,
            'request_timeout': 30
        }
        
        def convert_value(value: str) -> Any:
            """Convert string value to appropriate type."""
            # Handle None/null values
            if value.lower() in ('none', 'null'):
                return None
            # Handle boolean values
            if value.lower() in ('true', 'false'):
                return value.lower() == 'true'
            # Handle numeric values
            try:
                if value.isdigit():
                    return int(value)
                if value.replace(".", "", 1).isdigit():
                    return float(value)
            except (ValueError, AttributeError):
                pass
            # Return as string by default
            return value
        
        for key, value in os.environ.items():
            if key.startswith(ENV_CONFIG_PREFIX):
                # Remove prefix and convert to lowercase
                config_key = key[len(ENV_CONFIG_PREFIX):].lower()
                parts = config_key.split('_')
                
                # Handle root-level configs
                if len(parts) == 1:
                    config_data[parts[0]] = convert_value(value)
                    continue
                
                # Handle nested configs
                section = parts[0]  # e.g., 'chatgpt', 'stake'
                if section in config_data and isinstance(config_data[section], dict):
                    # Convert remaining parts to key
                    nested_key = '_'.join(parts[1:])
                    config_data[section][nested_key] = convert_value(value)
        
        # Create instance with converted data
        try:
            return cls(**config_data)
        except Exception as e:
            # Log the error and configuration data for debugging
            print(f"Error creating config: {str(e)}")
            print(f"Config data: {json.dumps(config_data, indent=2, default=str)}")
            raise
        
    def save_to_file(self, path: Union[str, Path]) -> None:
        """Save configuration to a YAML file."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(path, "w") as f:
            yaml.dump(self.dict(), f, default_flow_style=False)
            
    def update(self, **kwargs) -> None:
        """Update configuration values."""
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)

    @classmethod
    def validate_config(cls) -> bool:
        """
        Validate the current configuration state.
        
        This method checks:
        1. Required environment variables are set
        2. Critical configuration values are within valid ranges
        3. Component configurations are valid
        
        Returns:
            bool: True if configuration is valid, False otherwise
        """
        try:
            # Get current config from manager
            config_manager = ConfigManager()
            current_config = config_manager.config
            
            # Validate environment
            if not isinstance(current_config.environment, Environment):
                return False
                
            # Validate critical settings
            if current_config.max_workers < 1:
                return False
            if current_config.batch_size < 1:
                return False
            if current_config.request_timeout < 1:
                return False
                
            # Validate component configs
            if current_config.betting:
                if (current_config.betting.min_amount < 0 or
                    current_config.betting.max_amount <= current_config.betting.min_amount or
                    current_config.betting.daily_limit <= 0 or
                    current_config.betting.session_limit <= 0):
                    return False
                    
            # All validations passed
            return True
            
        except Exception:
            # If any error occurs during validation, consider config invalid
            return False

class ConfigManager:
    """Configuration manager for the Tenire framework."""
    
    def __init__(self):
        """Initialize configuration manager."""
        self._initialized = False
        self._config = None
        self._config_file_path = None
        self._env_file_path = None
        self._logger = None
        self._initialization_lock = threading.Lock()  # Non-reentrant lock
        self._is_initializing = False
        
    def initialize(
        self,
        config_file: Optional[Union[str, Path]] = None,
        env_file: Optional[Union[str, Path]] = None,
        environment: Optional[Environment] = None
    ) -> None:
        """Initialize configuration from files and environment."""
        # Fast path if already initialized
        if self._initialized:
            return
            
        # Prevent reentrant initialization
        if self._is_initializing:
            return
            
        try:
            # Acquire initialization lock
            if not self._initialization_lock.acquire(blocking=False):
                # If we can't acquire the lock, someone else is initializing
                return
                
            self._is_initializing = True
            
            try:
                # Load .env file if provided
                if env_file:
                    self._env_file_path = Path(env_file)
                    if self._env_file_path.exists():
                        self._load_env_file()
                        
                # Set environment
                if environment:
                    os.environ["TENIRE_ENVIRONMENT"] = environment.value
                    
                # Load or create configuration
                if config_file:
                    self._config_file_path = Path(config_file)
                    if not self._config_file_path.exists():
                        # Create default config
                        default_config = Config(
                            environment=Environment.DEVELOPMENT,
                            debug=True,
                            testing=False,
                            max_workers=8,
                            batch_size=1000,
                            request_timeout=30,
                            max_retries=3,
                            retry_delay=1.0,
                            queue_size=100,
                            cache=CacheConfig(),
                            logging=LoggingConfig(),
                            betting=BettingConfig(),
                            chatgpt=ChatGPTConfig(
                                api_key=os.getenv("TENIRE_CHATGPT_API_KEY", ""),
                                model="gpt-o1",
                                temperature=0.025,
                                max_tokens=1000,
                                top_p=1.0,
                                frequency_penalty=0.0,
                                presence_penalty=0.0,
                                max_context_length=10000,
                                max_concurrent_requests=5
                            ),
                            stake=StakeConfig(
                                username=os.getenv("TENIRE_STAKE_USERNAME", ""),
                                password=os.getenv("TENIRE_STAKE_PASSWORD", "")
                            ),
                            gui=GUIConfig()
                        )
                        # Save default config
                        default_config.save_to_file(self._config_file_path)
                        self.logger.info(f"Created default configuration file at {self._config_file_path}")
                        self._config = default_config
                    else:
                        self._config = Config.load_from_file(self._config_file_path)
                else:
                    # Try default config file
                    if DEFAULT_CONFIG_PATH.exists():
                        self._config = Config.load_from_file(DEFAULT_CONFIG_PATH)
                    else:
                        # Load from environment with defaults
                        self._config = Config(
                            environment=Environment.DEVELOPMENT,
                            debug=True,
                            testing=False,
                            max_workers=8,
                            batch_size=1000,
                            request_timeout=30,
                            max_retries=3,
                            retry_delay=1.0,
                            queue_size=100,
                            cache=CacheConfig(),
                            logging=LoggingConfig(),
                            betting=BettingConfig(),
                            chatgpt=ChatGPTConfig(),
                            stake=StakeConfig(),
                            gui=GUIConfig()
                        )
                        self._config.update_from_env()
                        
                self._initialized = True
                self.logger.info(f"Configuration initialized from {self._config_file_path or 'environment'}")
                
            except Exception as e:
                self.logger.error(f"Error initializing configuration: {str(e)}")
                raise
                
        finally:
            self._is_initializing = False
            if self._initialization_lock.locked():
                self._initialization_lock.release()

    @property
    def config(self) -> Optional[Config]:
        """Get the current configuration with lazy initialization."""
        if not self._initialized and not self._is_initializing:
            self.initialize()
        return self._config

    def get_config(self) -> Optional[Config]:
        """Get the current configuration."""
        try:
            return self.config
        except Exception as e:
            self.logger.warning(f"Error getting config, returning None: {str(e)}")
            return None

    @property
    def logger(self):
        """Get a logger instance for the config manager."""
        if self._logger is None:
            import logging
            self._logger = logging.getLogger("tenire.core.config")
            if not self._logger.handlers:
                handler = logging.StreamHandler()
                handler.setFormatter(
                    logging.Formatter('%(asctime)s | %(levelname)-8s | %(name)s | %(message)s')
                )
                self._logger.addHandler(handler)
                self._logger.setLevel(logging.INFO)
        return self._logger
        
    def _load_env_file(self) -> None:
        """Load environment variables from .env file."""
        if not self._env_file_path.exists():
            return
            
        with open(self._env_file_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                    
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip()
                
    def update_config(self, **kwargs) -> None:
        """Update configuration values."""
        self.config.update(**kwargs)
        
        # Save to file if available
        if self._config_file_path:
            self.config.save_to_file(self._config_file_path)
            
    def get_cache_config(self) -> CacheConfig:
        """Get cache-specific configuration."""
        return self.config.cache
        
    def get_logging_config(self) -> LoggingConfig:
        """Get logging-specific configuration."""
        try:
            if self.config and self.config.logging:
                return self.config.logging
        except Exception as e:
            self.logger.warning(f"Error getting logging config, using defaults: {str(e)}")
        return LoggingConfig()  # Return default config if not initialized or error
        
    def get_database_config(self) -> Optional[DatabaseConfig]:
        """Get database-specific configuration."""
        return self.config.database
        
    def get_security_config(self) -> Optional[SecurityConfig]:
        """Get security-specific configuration."""
        return self.config.security
        
    def get_betting_config(self) -> BettingConfig:
        """Get betting-specific configuration."""
        return self.config.betting
        
    def get_chatgpt_config(self) -> ChatGPTConfig:
        """Get ChatGPT-specific configuration."""
        return self.config.chatgpt
        
    def get_stake_config(self) -> StakeConfig:
        """Get stake-specific configuration."""
        return self.config.stake
        
    def get_gui_config(self) -> GUIConfig:
        """Get GUI-specific configuration."""
        return self.config.gui
        
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.config.environment == Environment.DEVELOPMENT
        
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.config.environment == Environment.PRODUCTION
        
    def is_testing(self) -> bool:
        """Check if running in testing environment."""
        return self.config.environment == Environment.TESTING

# Global instance
config_manager = ConfigManager() 