"""
Logging utilities for the Tenire framework.

This module provides a centralized logging configuration and utilities.
"""

import logging
import sys
from typing import Optional, Dict, Any
from logging import LogRecord
import threading

# Default logging configuration
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_FORMAT = "%(levelname)-8s | %(name)s | %(message)s"
DEFAULT_LOG_FILE = "tenire.log"
DEFAULT_MAX_BYTES = 10_485_760  # 10MB
DEFAULT_BACKUP_COUNT = 5

# Global lock for logger configuration
_logger_lock = threading.Lock()
_loggers: Dict[str, logging.Logger] = {}

class LogFormatter(logging.Formatter):
    """Custom log formatter with color support and structured output."""
    
    COLORS = {
        'DEBUG': '\033[36m',     # Cyan
        'INFO': '\033[32m',      # Green
        'WARNING': '\033[33m',   # Yellow
        'ERROR': '\033[31m',     # Red
        'CRITICAL': '\033[35m',  # Magenta
        'RESET': '\033[0m'       # Reset
    }
    
    def __init__(self, fmt: Optional[str] = None, use_color: bool = True):
        """Initialize the formatter with optional color support."""
        super().__init__(fmt or DEFAULT_LOG_FORMAT)
        self.use_color = use_color
    
    def format(self, record: LogRecord) -> str:
        """Format the log record with optional color."""
        # Save original values
        orig_msg = record.msg
        orig_levelname = record.levelname
        
        try:
            if self.use_color and record.levelname in self.COLORS:
                record.levelname = f"{self.COLORS[record.levelname]}{record.levelname}{self.COLORS['RESET']}"
            
            return super().format(record)
        finally:
            # Restore original values
            record.msg = orig_msg
            record.levelname = orig_levelname

def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance with the specified name.
    
    Args:
        name: The name of the logger
        
    Returns:
        A configured logger instance
    """
    global _loggers
    
    with _logger_lock:
        if name in _loggers:
            return _loggers[name]
        
        logger = logging.getLogger(name)
        
        # Only configure if not already configured
        if not logger.handlers:
            try:
                # Try to get config from config_manager, but don't fail if not available
                try:
                    from tenire.core.config import config_manager
                    log_config = config_manager.get_logging_config()
                except (ImportError, AttributeError):
                    log_config = None
                
                if log_config:
                    level = getattr(logging, log_config.level, DEFAULT_LOG_LEVEL)
                    format_str = log_config.format
                    file_path = log_config.file_path
                    max_bytes = log_config.max_size
                    backup_count = log_config.backup_count
                else:
                    level = getattr(logging, DEFAULT_LOG_LEVEL, logging.DEBUG)
                    format_str = DEFAULT_LOG_FORMAT
                    file_path = DEFAULT_LOG_FILE
                    max_bytes = DEFAULT_MAX_BYTES
                    backup_count = DEFAULT_BACKUP_COUNT
                
                # Set level
                logger.setLevel(level)
                
                # Create formatters
                console_formatter = LogFormatter(format_str, use_color=True)
                file_formatter = LogFormatter(format_str, use_color=False)
                
                # Console handler
                console_handler = logging.StreamHandler(sys.stdout)
                console_handler.setFormatter(console_formatter)
                logger.addHandler(console_handler)
                
                # File handler
                if file_path:
                    from logging.handlers import RotatingFileHandler
                    file_handler = RotatingFileHandler(
                        file_path,
                        maxBytes=max_bytes,
                        backupCount=backup_count
                    )
                    file_handler.setFormatter(file_formatter)
                    logger.addHandler(file_handler)
                
            except Exception as e:
                # Fallback to basic configuration if anything fails
                logger.setLevel(logging.DEBUG)
                handler = logging.StreamHandler(sys.stdout)
                handler.setFormatter(LogFormatter())
                logger.addHandler(handler)
                logger.warning(f"Using fallback logging configuration: {str(e)}")
        
        _loggers[name] = logger
        return logger


def set_framework_log_level(level: str) -> None:
    """
    Set the log level for all framework loggers.
    
    Args:
        level: The logging level to set (e.g. 'DEBUG', 'INFO', etc.)
    """
    root_logger = logging.getLogger('tenire')
    root_logger.setLevel(getattr(logging, level.upper()))


def add_file_handler(logger: logging.Logger, file_path: str) -> None:
    """
    Add a file handler to the given logger.
    
    Args:
        logger: The logger to add the handler to
        file_path: Path to the log file
    """
    try:
        file_handler = logging.FileHandler(file_path)
        file_handler.setFormatter(logging.Formatter(DEFAULT_LOG_FORMAT))
        logger.addHandler(file_handler)
    except Exception as e:
        logger.warning(f"Could not create file handler for {file_path}: {str(e)}")


# Example usage in other modules:
"""
from tenire.logging_utils import get_logger

logger = get_logger(__name__)

def some_function():
    try:
        logger.info("Starting operation")
        # ... do something ...
        logger.debug("Operation details: %s", some_details)
    except Exception as e:
        logger.error("Operation failed: %s", str(e))
        raise
""" 