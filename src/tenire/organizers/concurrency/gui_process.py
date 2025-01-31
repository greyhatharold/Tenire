"""
GUI process management module.

This module provides the GUIProcess class for managing GUI processes
in a separate process.
"""

# Standard library imports
import asyncio
import multiprocessing
import queue
from abc import ABC, abstractmethod
from typing import Any, Optional
from contextlib import suppress

# Local imports
from tenire.utils.logger import get_logger
from tenire.core.event_loop import event_loop_manager

# Configure logging
logger = get_logger(__name__)

class AbstractGUIProcess(ABC, multiprocessing.Process):
    """Abstract base class for GUI process management."""
    
    @abstractmethod
    def run(self) -> None:
        """Run the GUI application."""
        pass
        
    @abstractmethod
    async def _process_commands(self, gui: Any) -> None:
        """Process commands from the command queue."""
        pass

class GUIProcess:
    """Manages the GUI process."""
    
    def __init__(self, width: int, command_queue: multiprocessing.Queue, response_queue: multiprocessing.Queue):
        """Initialize the GUI process manager."""
        self.width = width
        self.command_queue = command_queue
        self.response_queue = response_queue
        self._process = None
        self._startup_event = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._startup_timeout = 30  # 30 second timeout for startup
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        logger.debug("Initialized GUI process manager")
        
    @property
    def is_alive(self) -> bool:
        """Check if the GUI process is alive."""
        return bool(self._process and self._process.is_alive())
        
    async def start(self) -> None:
        """Start the GUI process with improved error handling."""
        if self._process and self._process.is_alive():
            logger.info("GUI process already running")
            return
            
        try:
            # Get or create event loop
            self._loop = event_loop_manager.get_event_loop(for_qt=True)
            if not self._loop:
                raise RuntimeError("Failed to get event loop")

            logger.info("Creating new GUI process")
            # Lazy import to avoid circular dependencies
            from tenire.gui.processes.gui_process import GUIProcess as BaseGUIProcess
            
            # Create and start the process using the base implementation
            self._process = BaseGUIProcess(
                command_queue=self.command_queue,
                response_queue=self.response_queue,
                width=self.width
            )
            
            logger.info("Starting GUI process")
            self._process.start()
            logger.info(f"GUI process started with PID: {self._process.pid}")
            
            # Wait for startup with improved error handling
            try:
                start_time = self._loop.time()
                logger.info("Waiting for GUI process to start")
                
                while True:
                    if not self._process.is_alive():
                        raise RuntimeError("GUI process died during startup")
                        
                    try:
                        response = await self._get_response(timeout=0.5)
                        logger.debug(f"Received response from GUI: {response}")
                        
                        if response and response[0] == "state_update":
                            state = response[1]
                            if state.get("status") == "ready":
                                logger.info("GUI process ready")
                                self._startup_event.set()
                                break
                            elif state.get("status") == "error":
                                error_msg = state.get("error", "Unknown GUI error")
                                logger.error(f"GUI process error: {error_msg}")
                                raise RuntimeError(error_msg)
                    except asyncio.TimeoutError:
                        current_time = self._loop.time()
                        if current_time - start_time > self._startup_timeout:
                            logger.error("Timeout waiting for GUI to start")
                            raise RuntimeError("Timeout waiting for GUI to start")
                        await asyncio.sleep(0.1)  # Short sleep to prevent busy waiting
                        continue
                    
            except Exception as e:
                logger.error(f"GUI startup error: {str(e)}")
                await self.stop()  # Ensure cleanup on startup failure
                raise RuntimeError(f"GUI startup error: {str(e)}")
                
        except Exception as e:
            logger.error(f"Failed to start GUI process: {str(e)}")
            if self._process:
                logger.info("Terminating failed GUI process")
                await self.stop()
            raise RuntimeError(f"Failed to start GUI process: {str(e)}")
            
    async def _get_response(self, timeout: float) -> Optional[tuple]:
        """Get response from queue with proper async handling."""
        try:
            return await asyncio.wait_for(
                self._loop.run_in_executor(
                    None,
                    lambda: self.response_queue.get(timeout=timeout)
                ),
                timeout=timeout
            )
        except (asyncio.TimeoutError, queue.Empty):
            return None
            
    async def stop(self) -> None:
        """Stop the GUI process with improved cleanup."""
        if not self._process:
            return
            
        try:
            logger.info("Stopping GUI process")
            self._shutdown_event.set()
            
            # Send shutdown command first
            with suppress(Exception):
                self.command_queue.put(("shutdown", None))
            
            # Wait for process to end gracefully
            try:
                await asyncio.wait_for(
                    self._loop.run_in_executor(None, self._process.join, 5),
                    timeout=5
                )
            except asyncio.TimeoutError:
                logger.warning("GUI process did not stop gracefully, terminating")
                with suppress(Exception):
                    self._process.terminate()
                    await asyncio.wait_for(
                        self._loop.run_in_executor(None, self._process.join, 1),
                        timeout=1
                    )
                        
            # Clear queues
            await self._clear_queues()
            
        except Exception as e:
            logger.error(f"Error stopping GUI process: {str(e)}")
            if self._process and self._process.is_alive():
                with suppress(Exception):
                    self._process.terminate()
        finally:
            self._process = None
            self._startup_event.clear()
            self._shutdown_event.clear()
            
    async def _clear_queues(self):
        """Clear command and response queues asynchronously."""
        try:
            # Clear command queue
            while True:
                try:
                    await self._loop.run_in_executor(
                        None,
                        lambda: self.command_queue.get_nowait()
                    )
                except queue.Empty:
                    break
                    
            # Clear response queue
            while True:
                try:
                    await self._loop.run_in_executor(
                        None,
                        lambda: self.response_queue.get_nowait()
                    )
                except queue.Empty:
                    break
        except Exception as e:
            logger.error(f"Error clearing queues: {str(e)}")
