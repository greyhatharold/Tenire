"""
Browser integration module for the Tenire framework.

This module provides a clean interface for browser automation tasks,
handling browser session management, navigation, and authentication
while maintaining separation of concerns from betting logic.
"""
# Standard library imports
import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import os
import random
from pathlib import Path

# Third-party imports
from browser_use import Browser, BrowserConfig
from browser_use.browser.context import BrowserContextConfig
from playwright.async_api import async_playwright

# Local imports
from tenire.core.config import Config
from tenire.servicers import SignalManager
from tenire.core.codex import Signal, SignalType
from tenire.utils.logger import get_logger
from tenire.core.immutables import (
    BASE_URL, LOGIN_URL, DEFAULT_TIMEOUT, PAGE_LOAD_TIMEOUT,
    NAVIGATION_TIMEOUT, CONTEXT_REFRESH_INTERVAL, USER_DATA_DIR,
    BROWSER_CONTEXT_CONFIG, BROWSER_CONFIG, GOOGLE_AUTH_CONFIG,
    DEFAULT_AUTH_COOKIES, BROWSER_INIT_CONFIG, SECURITY_CHECK_TIMEOUT,
    MAX_SECURITY_RETRIES, SECURITY_CHECK_INTERVAL, BROWSER_LAUNCH_ARGS,
    SELECTORS, INTERACTION_DELAYS, MOUSE_MOVEMENT, INITIAL_BROWSER_STATE,
    STORAGE_KEYS, OAUTH_TOKENS_FILE
)

logger = get_logger(__name__)

class BrowserIntegrationError(Exception):
    """Custom exception for browser integration related errors."""
    pass

class BrowserIntegration:
    _instance = None
    _lock = asyncio.Lock()
    _security_check_timeout = SECURITY_CHECK_TIMEOUT
    _max_security_retries = MAX_SECURITY_RETRIES
    _security_check_interval = SECURITY_CHECK_INTERVAL

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(BrowserIntegration, cls).__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, '_initialized'):
            self._initialized = True
            self._browser = None
            self._context_config = None
            self._browser_config = None
            self._is_logged_in = False
            self._last_context_refresh = None
            self._security_check_in_progress = False
            self._popup_handlers = {}  # Store popup handlers by URL pattern
            self._playwright = None
            self._browser_context = None  # Store browser context instance
            self._main_page = None
            self._state = INITIAL_BROWSER_STATE.copy()
            self._event_loop = None
            
            # Get required managers from container
            from tenire.core.container import container
            self.compactor = container.get('compactor')
            self.concurrency_manager = container.get('concurrency_manager')
            
            if not self.compactor or not self.concurrency_manager:
                raise RuntimeError("Required managers not available")
            
            # Register with compactor for cleanup
            self.compactor.register_cleanup_task(
                name="browser_cleanup",
                cleanup_func=self.close_browser_session,
                priority=90,
                is_async=True,
                metadata={"tags": ["browser"]}
            )
            
            # Register with concurrency manager
            self.concurrency_manager.register_browser_session(self)
            
            # Get and register event loop
            self._event_loop = asyncio.get_event_loop()
            self.compactor.register_event_loop(self._event_loop)
            
            # Register enhanced signal handlers with priorities and filters
            SignalManager().register_handler(
                SignalType.BROWSER_STARTED,
                self._handle_browser_started,
                priority=10,
                filter_func=lambda s: s.source == "browser_integration"
            )
            SignalManager().register_handler(
                SignalType.BROWSER_CLOSED,
                self._handle_browser_closed,
                priority=10,
                filter_func=lambda s: s.source == "browser_integration"
            )
            SignalManager().register_handler(
                SignalType.LOGIN_COMPLETED,
                self._handle_login_completed,
                priority=10,
                filter_func=lambda s: s.source == "browser_integration"
            )
            SignalManager().register_handler(
                SignalType.LOGIN_FAILED,
                self._handle_login_failed,
                priority=10,
                filter_func=lambda s: s.source == "browser_integration"
            )
            SignalManager().register_handler(
                SignalType.SECURITY_CHALLENGE_COMPLETED,
                self._handle_security_challenge_completed,
                priority=5,
                filter_func=lambda s: s.source == "browser_integration"
            )
            
            # Register error handler
            SignalManager().register_error_handler(self._handle_signal_error)
            
            logger.debug("BrowserIntegration initialized")

    @property
    def browser(self) -> Optional[Browser]:
        """Get the underlying browser instance."""
        return self._browser

    @property
    def is_logged_in(self) -> bool:
        """Return the current login status."""
        logger.debug(f"Checking login status: {self._is_logged_in}")
        return self._is_logged_in

    def _create_context_config(self) -> BrowserContextConfig:
        """Create a new context configuration."""
        logger.debug("Creating browser context configuration")
        config = BrowserContextConfig(**BROWSER_CONTEXT_CONFIG)
        logger.debug("Browser context configuration created")
        return config

    def _create_browser_config(self, context_config: BrowserContextConfig) -> BrowserConfig:
        """Create a new browser configuration."""
        logger.debug("Creating browser configuration")
        config_dict = BROWSER_CONFIG.copy()
        config_dict['new_context_config'] = context_config
        config = BrowserConfig(**config_dict)
        logger.debug(f"Browser configuration created with user data directory: {USER_DATA_DIR}")
        return config

    async def _refresh_context_if_needed(self) -> None:
        """
        Refresh the browser context if it's stale.
        
        This helps prevent issues with stale contexts and improves session reliability.
        """
        if not self._browser or not self._last_context_refresh:
            logger.debug("No browser or context refresh timestamp - skipping refresh")
            return

        now = datetime.now(timezone.utc)
        if now - self._last_context_refresh > CONTEXT_REFRESH_INTERVAL:
            try:
                logger.info("Refreshing browser context")
                # Store current URL to restore after refresh
                current_url = await self._main_page.url() if self._main_page else None
                
                if current_url and current_url != "about:blank":
                    logger.debug(f"Restoring URL: {current_url}")
                    await self._main_page.goto(current_url)
                
                self._last_context_refresh = now
                logger.info("Browser context refreshed successfully")
            except Exception as e:
                logger.error(f"Failed to refresh context: {str(e)}")
                # Continue with existing context if refresh fails

    async def _handle_security_challenge(self, page) -> bool:
        """Handle security challenges more efficiently."""
        if self._security_check_in_progress:
            logger.debug("Security check already in progress")
            return False

        try:
            self._security_check_in_progress = True
            
            # First check for Cloudflare challenge with shorter timeout
            try:
                cloudflare_frame = await page.wait_for_selector(
                    SELECTORS['cloudflare_challenge'],
                    timeout=DEFAULT_TIMEOUT
                )
                if cloudflare_frame:
                    logger.info("Detected Cloudflare challenge")
                    # Wait for automatic challenge completion with reduced timeout
                    await page.wait_for_function(
                        f"""() => !document.querySelector('{SELECTORS['cloudflare_challenge']}')""",
                        timeout=self._security_check_timeout * 1000
                    )
                    await asyncio.sleep(1)  # Brief pause after completion
                    return True
            except Exception:
                pass

            # Check for reCAPTCHA with shorter timeout
            try:
                recaptcha_frame = await page.wait_for_selector(
                    SELECTORS['recaptcha'],
                    timeout=DEFAULT_TIMEOUT
                )
                if recaptcha_frame:
                    logger.info("Detected reCAPTCHA")
                    # Wait for automatic challenge completion with reduced timeout
                    await page.wait_for_function(
                        f"""() => {{
                            const frames = document.querySelectorAll('iframe');
                            return !Array.from(frames).some(f => f.title.includes('reCAPTCHA'));
                        }}""",
                        timeout=self._security_check_timeout * 1000
                    )
                    await asyncio.sleep(1)  # Brief pause after completion
                    return True
            except Exception:
                pass

            return False

        finally:
            self._security_check_in_progress = False

    async def _try_cookie_auth(self) -> bool:
        """Attempt to authenticate using stored cookies."""
        try:
            # Check if we have stored cookies
            cookie_file = os.path.join(USER_DATA_DIR, "cookies.json")
            if not os.path.exists(cookie_file):
                return False
                
            # Load and set cookies
            await self._main_page.evaluate("""async () => {
                try {
                    const storedCookies = localStorage.getItem('google_auth_cookies');
                    if (storedCookies) {
                        const cookies = JSON.parse(storedCookies);
                        cookies.forEach(cookie => {
                            document.cookie = `${cookie.name}=${cookie.value};domain=${cookie.domain};path=${cookie.path}`;
                        });
                        return true;
                    }
                } catch (e) {
                    console.error('Error setting cookies:', e);
                }
                return false;
            }""")
            
            # Try to access a Google authenticated endpoint
            response = await self._main_page.evaluate("""async () => {
                try {
                    const response = await fetch('https://accounts.google.com/ListAccounts?gpsia=1', {
                        credentials: 'include'
                    });
                    return await response.text();
                } catch (e) {
                    return null;
                }
            }""")
            
            if response and 'session_state' in response:
                logger.info("Cookie authentication successful")
                self._is_logged_in = True
                return True
                
            return False
        except Exception as e:
            logger.debug(f"Cookie authentication failed: {str(e)}")
            return False

    async def _try_oauth_token_auth(self) -> bool:
        """Attempt to authenticate using OAuth token exchange."""
        try:
            # Try to get stored OAuth tokens
            if not os.path.exists(OAUTH_TOKENS_FILE):
                return False

            # Make the OAuth token exchange request
            response = await self._main_page.evaluate("""async (oauthData) => {
                try {
                    const response = await fetch(oauthData.auth_endpoint, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/x-www-form-urlencoded'
                        },
                        body: new URLSearchParams(oauthData),
                        credentials: 'include'
                    });
                    return await response.text();
                } catch (e) {
                    return null;
                }
            }""", GOOGLE_AUTH_CONFIG)

            if response and 'access_token' in response:
                # Set the OAuth token in browser storage
                await self._main_page.evaluate("""async (token, key) => {
                    localStorage.setItem(key, token);
                }""", response, STORAGE_KEYS['google_oauth_token'])
                
                logger.info("OAuth token authentication successful")
                self._is_logged_in = True
                return True

            return False
        except Exception as e:
            logger.debug(f"OAuth token authentication failed: {str(e)}")
            return False

    async def handle_login(self) -> bool:
        """Handle the login process with proper element waiting."""
        try:
            if not self._browser or not self._main_page:
                logger.error("No active browser session")
                return False
            
            # Try cookie authentication first
            if await self._try_cookie_auth():
                return True
                
            # Try OAuth token authentication next
            if await self._try_oauth_token_auth():
                return True

            # Check if already logged in by looking for user profile
            try:
                user_profile = await self._main_page.wait_for_selector(
                    SELECTORS['user_profile'],
                    timeout=DEFAULT_TIMEOUT
                )
                if user_profile:
                    logger.info("User already logged in")
                    self._is_logged_in = True
                    return True
            except Exception:
                logger.debug("User not logged in, proceeding with login flow")
            
            # Simulate natural browsing behavior before login
            await self._main_page.goto(BASE_URL, timeout=NAVIGATION_TIMEOUT)
            await self._main_page.wait_for_load_state("networkidle")
            
            base_delay, random_delay = INTERACTION_DELAYS['pre_login']
            await asyncio.sleep(base_delay + random.random() * random_delay)
            
            # Simulate mouse movements
            x_min, x_max = MOUSE_MOVEMENT['random_x_range']
            y_min, y_max = MOUSE_MOVEMENT['random_y_range']
            await self._main_page.mouse.move(
                random.randint(x_min, x_max),
                random.randint(y_min, y_max)
            )
            
            await self._main_page.goto(LOGIN_URL, timeout=NAVIGATION_TIMEOUT)
            await self._main_page.wait_for_load_state("networkidle")
            
            # Set sophisticated cookies
            await self._main_page.context.add_cookies(DEFAULT_AUTH_COOKIES)
            
            # Wait for and click Google login with human-like delay
            button = await self._main_page.wait_for_selector(SELECTORS['google_button'])
            base_delay, random_delay = INTERACTION_DELAYS['button_click']
            await asyncio.sleep(base_delay + random.random() * random_delay)
            
            # Get button position and add slight randomness
            box = await button.bounding_box()
            offset_min, offset_max = MOUSE_MOVEMENT['button_offset_range']
            x = box['x'] + box['width'] / 2 + random.randint(offset_min, offset_max)
            y = box['y'] + box['height'] / 2 + random.randint(offset_min, offset_max)
            
            # Move mouse naturally and click
            await self._main_page.mouse.move(x, y)
            base_delay, random_delay = INTERACTION_DELAYS['mouse_move']
            await asyncio.sleep(base_delay + random.random() * random_delay)
            
            # Create popup listener before clicking
            popup_promise = self._main_page.wait_for_event('popup')
            await button.click()
            popup_page = await popup_promise
            
            # Inject sophisticated anti-detection before proceeding
            await self._inject_anti_detection_scripts(popup_page)
            await popup_page.wait_for_load_state("domcontentloaded")
            
            # Handle both old and new Google sign-in forms with retry logic
            max_retries = BROWSER_INIT_CONFIG['max_retries']
            for attempt in range(max_retries):
                try:
                    # Email input with human-like behavior
                    email_input = await popup_page.wait_for_selector(SELECTORS['email_input'])
                    await email_input.focus()
                    base_delay, random_delay = INTERACTION_DELAYS['focus_delay']
                    await asyncio.sleep(base_delay + random.random() * random_delay)
                    
                    # Type email with random delays between characters
                    for char in Config.STAKE_US_USERNAME:
                        await popup_page.keyboard.type(char)
                        base_delay, random_delay = INTERACTION_DELAYS['key_press']
                        await asyncio.sleep(base_delay + random.random() * random_delay)
                    
                    base_delay, random_delay = INTERACTION_DELAYS['enter_press']
                    await asyncio.sleep(base_delay + random.random() * random_delay)
                    await popup_page.keyboard.press('Enter')
                    
                    base_delay, random_delay = INTERACTION_DELAYS['post_enter']
                    await asyncio.sleep(base_delay + random.random() * random_delay)
                    
                    # Password input with human-like behavior
                    password_input = await popup_page.wait_for_selector(SELECTORS['password_input'])
                    await password_input.focus()
                    base_delay, random_delay = INTERACTION_DELAYS['focus_delay']
                    await asyncio.sleep(base_delay + random.random() * random_delay)
                    
                    # Type password with random delays
                    for char in Config.STAKE_US_PASSWORD:
                        await popup_page.keyboard.type(char)
                        base_delay, random_delay = INTERACTION_DELAYS['password_key_press']
                        await asyncio.sleep(base_delay + random.random() * random_delay)
                    
                    base_delay, random_delay = INTERACTION_DELAYS['final_enter']
                    await asyncio.sleep(base_delay + random.random() * random_delay)
                    await popup_page.keyboard.press('Enter')
                    
                    # Handle potential security check
                    await self._handle_security_challenge(popup_page)
                    break
                    
                except Exception as e:
                    logger.warning(f"Login attempt {attempt + 1} failed: {str(e)}")
                    if attempt == max_retries - 1:
                        raise
                    await asyncio.sleep(BROWSER_INIT_CONFIG['retry_delay'])
            
            # Wait for successful login
            try:
                await self._main_page.wait_for_selector(SELECTORS['user_profile'], timeout=PAGE_LOAD_TIMEOUT * 2)
                self._is_logged_in = True
                logger.info("Login completed successfully")
                
                # Store authentication state
                cookies = await popup_page.context.cookies()
                await self._main_page.evaluate("""async (cookies, key) => {
                    localStorage.setItem(key, JSON.stringify(cookies));
                }""", cookies, STORAGE_KEYS['google_auth_cookies'])
                
                return True
            except Exception as e:
                logger.error(f"Failed to detect successful login after completion: {str(e)}")
                return False
            
        except Exception as e:
            logger.error(f"Login failed: {str(e)}")
            return False

    async def _get_playwright_context(self):
        """Get the underlying Playwright context from the browser-use context."""
        if not self._browser_context:
            raise BrowserIntegrationError("No browser context available")
        session = await self._browser_context.get_session()
        return session.context

    async def _emit_state_update(self, update_data: dict) -> None:
        """Emit browser state update signal."""
        self._state.update(update_data)
        await SignalManager().emit(Signal(
            type=SignalType.GUI_STATE,
            data=self._state.copy(),
            source="browser_integration",
            gui_context={"component": "browser"}
        ))

    async def _handle_signal_error(self, error: Exception, signal: Signal) -> None:
        """Handle signal processing errors."""
        await self._emit_state_update({
            'status': 'error',
            'last_error': str(error)
        })
        logger.error(f"Signal error in {signal.type}: {str(error)}")

    async def start_browser_session(self) -> None:
        """Start a new browser session with persistent context."""
        try:
            if self._browser:
                logger.info("Browser session already active")
                return
            
            await self._emit_state_update({
                'status': 'starting',
                'is_ready': False
            })
            
            # Initialize playwright if not already initialized
            if not self._playwright:
                self._playwright = await async_playwright().start()
            
            # Create task for browser initialization
            browser_task = await self.concurrency_manager.async_tasks.create_task(
                self._initialize_browser_context(),
                "browser_initialization"
            )
            
            try:
                await browser_task
            except Exception as e:
                logger.error(f"Browser initialization task failed: {str(e)}")
                raise
                
        except Exception as e:
            await self._emit_state_update({
                'status': 'error',
                'is_ready': False,
                'last_error': str(e)
            })
            
            logger.error(f"Failed to start browser session: {str(e)}")
            await SignalManager().emit(Signal(
                type=SignalType.BROWSER_STARTED,
                data={
                    "status": "error",
                    "error": str(e),
                    "state": self._state.copy()
                },
                source="browser_integration"
            ))
            
            # Ensure cleanup of any partially initialized state
            await self._cleanup_browser_resources()
            raise BrowserIntegrationError(f"Failed to start browser session: {str(e)}")

    async def _initialize_browser_context(self) -> None:
        """Initialize browser context with proper lifecycle management."""
        try:
            # Pre-warm the system
            await self._pre_warm_system()
            
            # Launch persistent context with increased timeout and optimized args
            persistent_context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=USER_DATA_DIR,
                headless=BROWSER_CONFIG['headless'],
                viewport=BROWSER_CONTEXT_CONFIG['browser_window_size'],
                args=BROWSER_LAUNCH_ARGS,
                timeout=BROWSER_INIT_CONFIG['initialization_timeout'] * 2  # Double timeout for cold conditions
            )
            
            # Store the context and ensure we have a page
            self._browser_context = persistent_context
            if not persistent_context.pages:
                self._main_page = await persistent_context.new_page()
            else:
                self._main_page = persistent_context.pages[0]
            
            # Enhanced initialization sequence with retries and state tracking
            max_retries = BROWSER_INIT_CONFIG['max_retries'] * 2  # Double retries for cold conditions
            retry_delay = BROWSER_INIT_CONFIG['retry_delay']
            
            for attempt in range(max_retries):
                try:
                    self._state['initialization_attempts'] = attempt + 1
                    await self._emit_state_update({
                        'status': 'initializing',
                        'initialization_attempt': attempt + 1
                    })
                    
                    # Wait for the page with increased timeouts
                    await self._main_page.wait_for_load_state("domcontentloaded", timeout=60000)  # Increased from 30000
                    
                    # Get and track current URL
                    current_url = await self._main_page.evaluate('() => window.location.href')
                    await self._emit_state_update({'current_url': current_url})
                    
                    if current_url == "about:blank":
                        await self._main_page.goto(BASE_URL, timeout=NAVIGATION_TIMEOUT * 2)  # Double navigation timeout
                        await self._main_page.wait_for_load_state("domcontentloaded", timeout=NAVIGATION_TIMEOUT * 2)
                        await self._main_page.wait_for_load_state("networkidle", timeout=NAVIGATION_TIMEOUT * 2)
                        
                        current_url = await self._main_page.evaluate('() => window.location.href')
                        await self._emit_state_update({'current_url': current_url})
                    
                    # Verify browser is ready with increased checks
                    is_ready = await self._check_browser_ready_enhanced()
                    logger.debug(f"Browser ready check result: {is_ready}")
                    if is_ready:
                        self._initialized = True
                        self._last_context_refresh = datetime.now()
                        logger.info("Browser initialization successful")
                        
                        # Register with compactor
                        self.compactor.register_resource(self._browser_context)
                        
                        # Update and emit final state
                        await self._emit_state_update({
                            'status': 'ready',
                            'is_ready': True,
                            'initialization_attempts': attempt + 1
                        })
                        
                        # Register with concurrency manager for lifecycle management
                        self.concurrency_manager.register_browser_context(self._browser_context)
                        
                        # Emit success signal with detailed state
                        await SignalManager().emit(Signal(
                            type=SignalType.BROWSER_STARTED,
                            data={
                                "status": "success",
                                "state": self._state.copy(),
                                "context": {
                                    "user_data_dir": USER_DATA_DIR,
                                    "current_url": current_url,
                                    "initialization_attempts": attempt + 1
                                }
                            },
                            source="browser_integration"
                        ))
                        return
                    
                    if attempt < max_retries - 1:
                        await self._emit_state_update({
                            'status': 'retry',
                            'is_ready': False,
                            'retry_attempt': attempt + 1,
                            'next_retry_delay': retry_delay * (1.5 ** attempt)  # Gentler exponential backoff
                        })
                        logger.debug(f"Browser not ready on attempt {attempt + 1}, retrying in {retry_delay * (1.5 ** attempt)}s...")
                        await asyncio.sleep(retry_delay * (1.5 ** attempt))
                    else:
                        error_msg = "Browser failed to initialize after maximum retries"
                        logger.error(error_msg)
                        await self._emit_state_update({
                            'status': 'error',
                            'is_ready': False,
                            'last_error': error_msg,
                            'total_attempts': attempt + 1
                        })
                        raise BrowserIntegrationError(error_msg)
                        
                except Exception as init_error:
                    if attempt < max_retries - 1:
                        await self._emit_state_update({
                            'status': 'retry_error',
                            'last_error': str(init_error),
                            'retry_attempt': attempt + 1,
                            'next_retry_delay': retry_delay * (1.5 ** attempt)
                        })
                        logger.warning(f"Initialization attempt {attempt + 1} failed: {str(init_error)}")
                        await asyncio.sleep(retry_delay * (1.5 ** attempt))
                    else:
                        error_msg = f"Browser initialization failed after {max_retries} attempts: {str(init_error)}"
                        logger.error(error_msg)
                        await self._emit_state_update({
                            'status': 'error',
                            'is_ready': False,
                            'last_error': error_msg,
                            'total_attempts': attempt + 1
                        })
                        raise init_error
                        
        except Exception as e:
            logger.error(f"Browser context initialization failed: {str(e)}")
            raise BrowserIntegrationError(f"Browser context initialization failed: {str(e)}")

    async def _pre_warm_system(self):
        """Pre-warm the system before browser initialization."""
        try:
            # Pre-initialize key components
            await asyncio.gather(
                self._warm_up_network(),
                self._warm_up_disk_cache(),
                self._warm_up_memory()
            )
        except Exception as e:
            logger.warning(f"Pre-warm failed (non-critical): {str(e)}")

    async def _warm_up_network(self):
        """Warm up network connections."""
        try:
            # Ping key domains
            domains = ['google.com', 'stake.us']
            for domain in domains:
                await asyncio.create_subprocess_shell(f'ping -c 1 {domain}')
        except Exception as e:
            logger.debug(f"Network warm-up failed: {str(e)}")

    async def _warm_up_disk_cache(self):
        """Warm up disk cache."""
        try:
            cache_dir = Path(USER_DATA_DIR) / 'Cache'
            if cache_dir.exists():
                # Read some recently used cache files
                for file in cache_dir.glob('*')[:10]:
                    try:
                        with open(file, 'rb') as f:
                            f.read(1024)  # Read first KB
                    except Exception:
                        pass
        except Exception as e:
            logger.debug(f"Disk cache warm-up failed: {str(e)}")

    async def _warm_up_memory(self):
        """Warm up memory subsystem."""
        try:
            # Allocate and free some memory to warm up memory management
            temp_data = [bytearray(1024*1024) for _ in range(5)]  # 5MB total
            del temp_data
        except Exception as e:
            logger.debug(f"Memory warm-up failed: {str(e)}")

    async def _check_browser_ready_enhanced(self) -> bool:
        """Enhanced browser readiness check with better cold condition handling."""
        try:
            if not self._browser_context or not self._main_page:
                logger.debug("Browser or main page not initialized")
                return False
                
            # Basic readiness check with increased timeouts
            try:
                # Wait for critical states with longer timeouts
                await asyncio.gather(
                    self._main_page.wait_for_load_state("domcontentloaded", timeout=20000),  # Doubled from 10000
                    self._main_page.wait_for_load_state("networkidle", timeout=20000)  # Doubled from 10000
                )
                
                # Enhanced DOM check with cold boot considerations
                is_ready = await self._main_page.evaluate('''() => {
                    const readyState = document.readyState === "complete";
                    const loadEvent = window.performance.timing.loadEventEnd > 0;
                    const noLoadingElements = !document.querySelector('[aria-busy="true"]') &&
                                            !document.querySelector('.loading') &&
                                            !document.querySelector('#loading');
                                            
                // Additional cold boot checks
                const renderingComplete = document.visibilityState === "visible";
                const resourcesLoaded = performance.getEntriesByType('resource')
                    .every(r => r.responseEnd > 0);
                    
                return readyState && loadEvent && noLoadingElements && 
                       renderingComplete && resourcesLoaded;
            }''')
                
                if is_ready:
                    # Additional check for any pending XHR requests with longer timeout
                    no_pending_requests = await self._main_page.evaluate('''() => {
                        return new Promise((resolve) => {
                            const checkXHR = () => {
                                const entries = performance.getEntriesByType('resource');
                                const pendingXHR = entries.filter(e => 
                                    e.initiatorType === 'xmlhttprequest' && 
                                    !e.responseEnd
                                );
                                if (pendingXHR.length === 0) {
                                    resolve(true);
                                } else {
                                    setTimeout(checkXHR, 100);
                                }
                            };
                            checkXHR();
                        });
                    }''', timeout=10000)  # 10s timeout for XHR check
                    
                    if no_pending_requests:
                        logger.debug("Browser is fully ready")
                        return True
                    
                    logger.debug("Browser ready but has pending XHR requests")
                    return False
                    
                logger.debug("Browser DOM not fully ready yet")
                return False
                
            except Exception as e:
                logger.debug(f"Page not responsive during ready check: {str(e)}")
                return False
                
        except Exception as e:
            logger.error(f"Error checking browser ready state: {str(e)}")
            return False

    async def _cleanup_browser_resources(self) -> None:
        """Clean up browser resources in a controlled manner."""
        try:
            await self._emit_state_update({
                'status': 'cleaning_up',
                'is_ready': False
            })
            
            if self._browser_context:
                # Unregister from concurrency manager first
                self.concurrency_manager.unregister_browser_context(self._browser_context)
                await self._browser_context.close()
                
            if self._playwright:
                await self._playwright.stop()
                
            await self._emit_state_update({
                'status': 'cleaned_up',
                'is_ready': False,
                'current_url': None
            })
            
        except Exception as e:
            await self._emit_state_update({
                'status': 'cleanup_error',
                'last_error': str(e)
            })
            logger.error(f"Error during browser resource cleanup: {str(e)}")
        finally:
            self._browser_context = None
            self._main_page = None
            self._playwright = None
            self._initialized = False
            self._is_logged_in = False

    async def close_browser_session(self) -> None:
        """Safely close the current browser session."""
        if self._browser_context:
            try:
                logger.info("Closing browser session")
                await self._cleanup_browser_resources()
                
                # Emit browser closed signal with final state
                await SignalManager().emit(Signal(
                    type=SignalType.BROWSER_CLOSED,
                    data={
                        "timestamp": datetime.now(),
                        "state": self._state.copy()
                    },
                    source="browser_integration"
                ))
                
                logger.info("Browser session closed successfully")
            except Exception as e:
                logger.error(f"Error closing browser session: {str(e)}")
        else:
            logger.warning("No active browser session to close")

    async def __aenter__(self):
        """Async context manager entry point."""
        logger.debug("Entering async context")
        await self.start_browser_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit point."""
        logger.debug("Exiting async context")
        await self.close_browser_session()

    async def _handle_browser_started(self, signal: Signal) -> None:
        """Handle browser started signal."""
        config = signal.data.get("config")
        if config:
            logger.info("Browser started with config")
            self._browser_config = config

    async def _handle_browser_closed(self, signal: Signal) -> None:
        """Handle browser closed signal."""
        timestamp = signal.data.get("timestamp")
        logger.info(f"Browser closed at {timestamp}")
        self._browser = None
        self._main_page = None
        self._is_logged_in = False

    async def _handle_login_completed(self, signal: Signal) -> None:
        """Handle login completed signal."""
        username = signal.data.get("username")
        if username:
            self._is_logged_in = True
            logger.info(f"Login completed for user: {username}")

    async def _handle_login_failed(self, signal: Signal) -> None:
        """Handle login failed signal."""
        error = signal.data.get("error")
        logger.error(f"Login failed: {error}")
        self._is_logged_in = False

    async def _handle_security_challenge_completed(self, signal: Signal) -> None:
        """Handle security challenge completed signal."""
        success = signal.data.get("success", False)
        if success:
            logger.info("Security challenge completed successfully")
        else:
            error = signal.data.get("error")
            logger.error(f"Security challenge failed: {error}")