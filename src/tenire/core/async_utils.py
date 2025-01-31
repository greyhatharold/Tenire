"""
Async utilities for safe coroutine execution and thread management.
"""

import asyncio
import concurrent.futures
from typing import Any, Callable, Optional
from functools import partial

from tenire.utils.logger import get_logger

logger = get_logger(__name__)

# Global thread pool executor for async operations
_thread_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="async_utils"
)

def run_in_executor(func: Callable[..., Any], *args, **kwargs) -> concurrent.futures.Future:
    """
    Run a function in the thread pool executor.
    
    Args:
        func: The function to run
        *args: Positional arguments for the function
        **kwargs: Keyword arguments for the function
        
    Returns:
        Future: A future representing the execution
    """
    return _thread_pool.submit(func, *args, **kwargs)

async def run_async_with_timeout(
    coro: Callable[..., Any],
    timeout: float = 5.0,
    *args,
    **kwargs
) -> Optional[Any]:
    """
    Run an async function with timeout.
    
    Args:
        coro: The coroutine to run
        timeout: Timeout in seconds
        *args: Positional arguments for the coroutine
        **kwargs: Keyword arguments for the coroutine
        
    Returns:
        Optional[Any]: The result of the coroutine or None if it times out
    """
    try:
        return await asyncio.wait_for(coro(*args, **kwargs), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning(f"Operation timed out after {timeout} seconds")
        return None
    except Exception as e:
        logger.error(f"Error running async operation: {str(e)}")
        return None

def cleanup():
    """Clean up the thread pool executor."""
    try:
        _thread_pool.shutdown(wait=True)
    except Exception as e:
        logger.error(f"Error cleaning up thread pool: {str(e)}")

# Register cleanup with atexit
import atexit
atexit.register(cleanup) 