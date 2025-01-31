"""
Custom betting actions for the Tenire framework.

This module defines custom browser actions for betting operations,
using the browser-use Controller pattern for clean integration.
"""

# Standard library imports
import asyncio
from typing import Any, Callable, Dict, Optional

# Third-party imports
from browser_use import ActionResult, Browser, Controller as BaseController
from playwright.async_api import Browser
from pydantic import BaseModel, Field

# Local imports
from tenire.core.config import config_manager
from tenire.core.codex import Signal, SignalType
from tenire.servicers import get_signal_manager
from tenire.utils.logger import get_logger
from tenire.public_services.federales.safe_guards import SafeguardType, safeguard_manager

logger = get_logger(__name__)

# Optimized timeouts
ACTION_TIMEOUT = 10000  # 10 seconds
SELECTOR_TIMEOUT = 5000  # 5 seconds
RECAPTCHA_TIMEOUT = 15000  # 15 seconds


# Define action descriptions that will be used consistently across the codebase
ACTION_DESCRIPTIONS = {
    'goto': 'Navigate to URL',
    'login_with_google': 'Handle Google login process for Stake.us',
    'wait_for_load_state': 'Wait for network to be idle',
    'place_mines_bet': 'Place a bet on the Mines game',
    'place_keno_bet': 'Place a bet on the Keno game',
    'place_dice_bet': 'Place a bet on the Dice game',
    'check_balance': 'Check the current account balance',
    'handle_recaptcha': 'Handle reCAPTCHA verification when it appears',
    'manual_google_login': 'Handle Google login process with manual element interaction to prevent autofill',
    'wait_for_popup': 'Wait for and handle popup window events'
}


class Controller(BaseController):
    """Extended Controller that supports named actions while maintaining browser-use compatibility."""
    
    def __init__(self):
        """Initialize the controller."""
        super().__init__()
        self._current_context = None

    def action(self, name: str, **kwargs) -> Callable:
        """
        Decorator for registering actions.
        
        Args:
            name: The name of the action
            **kwargs: Additional arguments for the action
        """
        description = ACTION_DESCRIPTIONS.get(name)
        if not description:
            raise ValueError(f"No description found for action: {name}")

        # Pass description as first argument as per browser-use docs
        return super().action(description, **kwargs)

    async def execute_action(self, action_name: str, browser_context=None, **kwargs):
        """Execute an action with context reuse."""
        if browser_context:
            self._current_context = browser_context
        elif not self._current_context:
            raise ValueError("No browser context available")
            
        return await super().execute_action(action_name, self._current_context, **kwargs)


# Define structured parameter models for betting actions
class BetParameters(BaseModel):
    amount: float = Field(..., description="Bet amount in Stake cash")
    game: str = Field(..., description="Game to bet on (e.g., 'mines', 'dice', 'keno')")
    mines_count: Optional[int] = Field(3, description="Number of mines for Mines game")
    target_multiplier: Optional[float] = Field(None, description="Target multiplier for Keno")
    roll_over: Optional[bool] = Field(None, description="Roll over/under for Dice")
    target_number: Optional[float] = Field(None, description="Target number for Dice")


# Initialize controller for betting actions
betting_controller = Controller()


@betting_controller.action(
    'goto',
    requires_browser=True
)
async def navigate_to_url(browser: Browser, url: str) -> ActionResult:
    """Navigate to a specified URL."""
    try:
        page = await browser.get_current_page()
        await page.goto(url)
        return ActionResult(success=True)
    except Exception as e:
        return ActionResult(success=False, error=str(e))

@betting_controller.action(
    'wait_for_load_state',
    requires_browser=True
)
async def wait_for_load_state(browser: Browser, state: str = 'networkidle') -> ActionResult:
    """Wait for a specific load state with optimized timeout."""
    try:
        page = await browser.get_current_page()
        await page.wait_for_load_state(state, timeout=ACTION_TIMEOUT)
        return ActionResult(success=True)
    except Exception as e:
        return ActionResult(success=False, error=str(e))

@betting_controller.action(
    name='login_with_google',
    requires_browser=True
)
async def login_with_google(browser: Browser, username: str = None, password: str = None) -> ActionResult:
    """Handle Google login process for Stake.us."""
    try:
        # Check password entry safeguard
        if not await safeguard_manager.check_safeguard(SafeguardType.PASSWORD_ENTRY):
            return ActionResult(
                success=False,
                error="Password entry blocked by safeguards"
            )
            
        page = await browser.get_current_page()
        
        username = username or config_manager.get_betting_config().username
        password = password or config_manager.get_betting_config().password
        
        if not username or not password:
            return ActionResult(success=False, error="Missing login credentials")
        
        # Click Google login with optimized timeout
        await page.wait_for_selector("#google-login-button", timeout=SELECTOR_TIMEOUT, delay=100)
        await page.click("#google-login-button")
        
        # Wait for and handle popup with optimized timeout and retries
        popup_page = None
        retry_count = 0
        max_retries = 3
        
        while retry_count < max_retries and not popup_page:
            try:
                # Listen for popup before clicking
                popup_promise = page.wait_for_event('popup', timeout=ACTION_TIMEOUT)
                
                # Wait for the popup
                popup_page = await popup_promise
                
                # Ensure popup is loaded
                await popup_page.wait_for_load_state("domcontentloaded", timeout=ACTION_TIMEOUT)
                await popup_page.wait_for_load_state("networkidle", timeout=ACTION_TIMEOUT)
                
                # Optimized login sequence with explicit waits
                # Wait for and fill email using direct element interaction
                email_input = await popup_page.wait_for_selector('input[type="email"]', timeout=SELECTOR_TIMEOUT)
                await email_input.evaluate('(el) => { el.value = ""; }')  # Clear any autofill
                await email_input.type(username, delay=100)  # Type slowly to prevent detection
                
                # Click next and wait for password field
                next_button = await popup_page.wait_for_selector('#identifierNext', timeout=SELECTOR_TIMEOUT)
                await next_button.click()
                
                # Wait specifically for password field to be visible and interactable
                password_input = await popup_page.wait_for_selector(
                    'input[type="password"]',
                    state="visible",
                    timeout=SELECTOR_TIMEOUT
                )
                await password_input.evaluate('(el) => { el.value = ""; }')  # Clear any autofill
                await password_input.type(password, delay=100)  # Type slowly to prevent detection
                
                # Click password next button
                password_next = await popup_page.wait_for_selector('#passwordNext', timeout=SELECTOR_TIMEOUT)
                await password_next.click()
                
                # Wait for login completion on main page
                await page.wait_for_selector("#user-profile", timeout=ACTION_TIMEOUT)
                
                # Close popup if still open
                try:
                    if not popup_page.is_closed():
                        await popup_page.close()
                except Exception:
                    pass  # Ignore errors closing popup
                
                # Emit login completed signal
                await get_signal_manager().emit(Signal(
                    type=SignalType.LOGIN_COMPLETED,
                    data={"username": username},
                    source="login_with_google"
                ))
                
                # Reset password entry attempts on success
                safeguard_manager.reset_attempt_count(SafeguardType.PASSWORD_ENTRY)
                
                return ActionResult(
                    success=True,
                    extracted_content=f"Successfully logged in with Google account {username}"
                )
                
            except Exception as e:
                logger.warning(f"Popup handling attempt {retry_count + 1} failed: {str(e)}")
                retry_count += 1
                await asyncio.sleep(1)  # Brief delay before retry
                
                if retry_count == max_retries:
                    return ActionResult(success=False, error="Failed to handle Google login popup")
                    
    except Exception as e:
        # Increment password entry attempts on failure
        if "password" in str(e).lower():
            safeguard_manager._attempt_counts[SafeguardType.PASSWORD_ENTRY] = \
                safeguard_manager._attempt_counts.get(SafeguardType.PASSWORD_ENTRY, 0) + 1
        
        # Emit login failed signal
        await get_signal_manager().emit(Signal(
            type=SignalType.LOGIN_FAILED,
            data={"username": username, "error": str(e)},
            source="login_with_google"
        ))
        return ActionResult(
            success=False,
            error=f"Failed to login with Google: {str(e)}"
        )


@betting_controller.action(
    name='manual_google_login',
    requires_browser=True
)
async def manual_google_login(browser: Browser, username: str = None, password: str = None) -> ActionResult:
    """Handle Google login process with manual element interaction to prevent autofill."""
    try:
        page = await browser.get_current_page()
        
        username = username or config_manager.get_betting_config().username
        password = password or config_manager.get_betting_config().password
        
        if not username or not password:
            return ActionResult(success=False, error="Missing login credentials")

        # First check if we're already logged in
        try:
            user_profile = await page.wait_for_selector('[data-testid="user-profile"]', timeout=2000)
            if user_profile:
                logger.info("Already logged in, skipping login process")
                return ActionResult(success=True, extracted_content="Already logged in")
        except:
            pass

        # Check if we're on a login-related page or have a login button
        try:
            current_url = page.url
            login_exists = await page.evaluate('''() => {
                const buttons = Array.from(document.querySelectorAll('button'));
                return buttons.some(b => b.textContent.toLowerCase().includes('sign in'));
            }''')
            
            if not login_exists and not any(x in current_url.lower() for x in ['/login', 'sign-in', 'auth']):
                logger.info("Not on login page and no login button found")
                return ActionResult(success=True, extracted_content="Not on login page")
        except Exception as e:
            logger.warning(f"Error checking login state: {str(e)}")
            return ActionResult(success=True, extracted_content="Unable to determine login state")

        # Proceed with login process
        logger.info("Starting login process")
        
        # Click login button
        await page.evaluate('''() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const loginButton = buttons.find(b => b.textContent.toLowerCase().includes('sign in'));
            if (loginButton) loginButton.click();
        }''')
        await page.wait_for_load_state("networkidle", timeout=5000)

        # Click Google login (index 1 is typically the Google login button)
        await page.evaluate('''() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            const googleButton = buttons.find(b => b.textContent.toLowerCase().includes('google'));
            if (googleButton) googleButton.click();
        }''')

        # Handle Google popup
        popup_page = await page.wait_for_event('popup', timeout=10000)
        await popup_page.wait_for_load_state("domcontentloaded", timeout=5000)

        # Handle email input (index 2 is typically the email input)
        await popup_page.evaluate('''(username) => {
            const inputs = document.querySelectorAll('input');
            const emailInput = inputs[0];
            if (emailInput) {
                emailInput.value = '';
                emailInput.setAttribute('autocomplete', 'off');
                emailInput.value = username;
            }
        }''', username)
        
        # Click next (index 6 is typically the next button)
        await popup_page.evaluate('''() => {
            const buttons = document.querySelectorAll('button');
            const nextButton = Array.from(buttons).find(b => b.textContent.toLowerCase().includes('next'));
            if (nextButton) nextButton.click();
        }''')
        await popup_page.wait_for_load_state("networkidle", timeout=5000)

        # Handle password input (index 4 is typically the password input)
        await popup_page.evaluate('''(password) => {
            const inputs = document.querySelectorAll('input[type="password"]');
            const passwordInput = inputs[0];
            if (passwordInput) {
                passwordInput.value = '';
                passwordInput.setAttribute('autocomplete', 'off');
                passwordInput.value = password;
            }
        }''', password)

        # Click next again
        await popup_page.evaluate('''() => {
            const buttons = document.querySelectorAll('button');
            const nextButton = Array.from(buttons).find(b => b.textContent.toLowerCase().includes('next'));
            if (nextButton) nextButton.click();
        }''')

        # Wait for login completion
        await page.wait_for_function('''() => {
            const userProfile = document.querySelector('[data-testid="user-profile"]');
            return !!userProfile;
        }''', timeout=15000)

        return ActionResult(success=True, extracted_content="Successfully logged in with manual Google login")

    except Exception as e:
        return ActionResult(success=False, error=f"Manual Google login failed: {str(e)}")


@betting_controller.action(
    name='place_mines_bet',
    requires_browser=True,
    param_model=BetParameters
)
async def place_mines_bet(browser: Browser, params: BetParameters) -> ActionResult:
    """Place a bet on the Mines game."""
    try:
        # Check safeguards first
        if not await safeguard_manager.check_safeguard(
            SafeguardType.FIRST_BET if not safeguard_manager._has_placed_first_bet else SafeguardType.BET_AMOUNT,
            value=params.amount
        ):
            return ActionResult(
                success=False,
                error="Action blocked by safeguards"
            )
            
        # Check bet frequency
        if not await safeguard_manager.check_safeguard(SafeguardType.BET_FREQUENCY):
            return ActionResult(
                success=False,
                error="Please wait before placing another bet"
            )
            
        # Check session duration
        session_duration = safeguard_manager.get_session_duration()
        if not await safeguard_manager.check_safeguard(
            SafeguardType.SESSION_DURATION,
            value=session_duration
        ):
            return ActionResult(
                success=False,
                error=f"Session duration limit reached ({session_duration} minutes)"
            )
            
        # Emit bet placed signal
        await get_signal_manager().emit(Signal(
            type=SignalType.BET_PLACED,
            data={"game": "mines", "amount": params.amount, "mines_count": params.mines_count},
            source="place_mines_bet"
        ))
        
        page = await browser.get_current_page()
        
        # Navigate to Mines game if not already there
        if not await page.get_by_text("Mines Game").is_visible():
            await page.goto("https://stake.us/casino/games/mines")
            await page.wait_for_load_state("networkidle")
            await get_signal_manager().emit(Signal(
                type=SignalType.PAGE_NAVIGATED,
                data={"url": "https://stake.us/casino/games/mines"},
                source="place_mines_bet"
            ))
        
        # Set bet amount
        amount_input = await page.wait_for_selector("#bet-amount")
        await amount_input.fill(str(params.amount))
        
        # Set mines count
        mines_input = await page.wait_for_selector("#mines-count")
        await mines_input.fill(str(params.mines_count))
        
        # Place bet
        bet_button = await page.wait_for_selector("#place-bet-button")
        await bet_button.click()
        
        # Update loss tracking on bet placement
        safeguard_manager.update_loss_tracking(params.amount)
        
        # Emit bet completed signal
        await get_signal_manager().emit(Signal(
            type=SignalType.BET_COMPLETED,
            data={"game": "mines", "amount": params.amount, "mines_count": params.mines_count},
            source="place_mines_bet"
        ))
        
        return ActionResult(
            success=True,
            extracted_content=f"Placed {params.amount} USD bet on Mines with {params.mines_count} mines"
        )
    except Exception as e:
        # Emit bet error signal
        await get_signal_manager().emit(Signal(
            type=SignalType.BET_ERROR,
            data={"game": "mines", "amount": params.amount, "error": str(e)},
            source="place_mines_bet"
        ))
        return ActionResult(
            success=False,
            error=f"Failed to place Mines bet: {str(e)}"
        )


@betting_controller.action(
    name='place_keno_bet',
    requires_browser=True,
    param_model=BetParameters
)
async def place_keno_bet(browser: Browser, params: BetParameters) -> ActionResult:
    """Place a bet on the Keno game."""
    try:
        # Check safeguards first
        if not await safeguard_manager.check_safeguard(
            SafeguardType.FIRST_BET if not safeguard_manager._has_placed_first_bet else SafeguardType.BET_AMOUNT,
            value=params.amount
        ):
            return ActionResult(
                success=False,
                error="Action blocked by safeguards"
            )
            
        # Check bet frequency
        if not await safeguard_manager.check_safeguard(SafeguardType.BET_FREQUENCY):
            return ActionResult(
                success=False,
                error="Please wait before placing another bet"
            )
            
        # Check session duration
        session_duration = safeguard_manager.get_session_duration()
        if not await safeguard_manager.check_safeguard(
            SafeguardType.SESSION_DURATION,
            value=session_duration
        ):
            return ActionResult(
                success=False,
                error=f"Session duration limit reached ({session_duration} minutes)"
            )
            
        page = await browser.get_current_page()
        
        # Navigate to Keno game if not already there
        if not await page.get_by_text("Keno Game").is_visible():
            await page.goto("https://stake.us/casino/games/keno")
            await page.wait_for_load_state("networkidle")
        
        # Ensure we're in Manual mode
        manual_button = await page.wait_for_selector("button:has-text('Manual')")
        await manual_button.click()
        
        # Set bet amount with dynamic adjustment
        amount_input = await page.wait_for_selector("input[type='number']")
        await amount_input.fill(str(params.amount))
        
        # Clear any existing selections
        clear_button = await page.wait_for_selector("button:has-text('Clear Table')")
        await clear_button.click()
        
        # Select the specific squares in our strategy
        # These squares form a specific pattern shown in the screenshots
        target_squares = [11, 12, 19, 20, 27, 28, 34, 35, 36, 37]
        
        for square in target_squares:
            # Wait for and click each square
            square_selector = f"div[role='button']:has-text('{square}')"
            square_element = await page.wait_for_selector(square_selector)
            await square_element.click()
            # Brief delay to ensure click registers
            await asyncio.sleep(0.1)
        
        # Set risk level to Medium if available
        try:
            risk_button = await page.wait_for_selector("button:has-text('Medium')")
            await risk_button.click()
        except Exception:
            logger.debug("Risk level selection not available or already set")
        
        # Click the Play button
        play_button = await page.wait_for_selector("button:has-text('Play')")
        await play_button.click()
        
        # Wait for result to ensure bet was placed
        try:
            await page.wait_for_selector(".result-multiplier", timeout=5000)
        except Exception:
            logger.debug("No immediate result multiplier visible")
        
        # Update loss tracking on bet placement
        safeguard_manager.update_loss_tracking(params.amount)
        
        return ActionResult(
            success=True,
            extracted_content=f"Placed {params.amount} USD bet on Keno with optimal 10-square strategy"
        )
    except Exception as e:
        return ActionResult(
            success=False,
            error=f"Failed to place Keno bet: {str(e)}"
        )


@betting_controller.action(
    name='place_dice_bet',
    requires_browser=True,
    param_model=BetParameters
)
async def place_dice_bet(browser: Browser, params: BetParameters) -> ActionResult:
    """Place a bet on the Dice game."""
    try:
        page = await browser.get_current_page()
        
        # Navigate to Dice game if not already there
        if not await page.get_by_text("Dice Game").is_visible():
            await page.goto("https://stake.us/casino/games/dice")
            await page.wait_for_load_state("networkidle")
        
        # Set bet amount
        amount_input = await page.wait_for_selector("#bet-amount")
        await amount_input.fill(str(params.amount))
        
        # Set roll direction
        if params.roll_over is not None:
            roll_button = await page.wait_for_selector(
                "#roll-over" if params.roll_over else "#roll-under"
            )
            await roll_button.click()
        
        # Set target number
        if params.target_number is not None:
            target_input = await page.wait_for_selector("#target-number")
            await target_input.fill(str(params.target_number))
        
        # Place bet
        bet_button = await page.wait_for_selector("#place-bet-button")
        await bet_button.click()
        
        return ActionResult(
            success=True,
            extracted_content=(
                f"Placed {params.amount} USD bet on Dice "
                f"{'over' if params.roll_over else 'under'} {params.target_number}"
            )
        )
    except Exception as e:
        return ActionResult(
            success=False,
            error=f"Failed to place Dice bet: {str(e)}"
        )


@betting_controller.action(
    name='check_balance',
    requires_browser=True
)
async def check_balance(browser: Browser) -> ActionResult:
    """Check the current account balance."""
    try:
        # Check loss limit safeguard
        if not await safeguard_manager.check_safeguard(
            SafeguardType.LOSS_LIMIT,
            value=safeguard_manager._total_losses
        ):
            return ActionResult(
                success=False,
                error="Loss limit reached"
            )
            
        page = await browser.get_current_page()
        balance_element = await page.wait_for_selector("#balance-amount")
        balance = await balance_element.text_content()
        
        # Emit balance updated signal
        await get_signal_manager().emit(Signal(
            type=SignalType.BALANCE_UPDATED,
            data={"balance": balance},
            source="check_balance"
        ))
        
        return ActionResult(
            success=True,
            extracted_content=f"Current balance: {balance} USD"
        )
    except Exception as e:
        return ActionResult(
            success=False,
            error=f"Failed to check balance: {str(e)}"
        )


@betting_controller.action(
    name='handle_recaptcha',
    requires_browser=True
)
async def handle_recaptcha(browser: Browser) -> ActionResult:
    """Handle reCAPTCHA verification with optimized timeouts."""
    try:
        page = await browser.get_current_page()
        
        # Emit security challenge detected signal
        await get_signal_manager().emit(Signal(
            type=SignalType.SECURITY_CHALLENGE_DETECTED,
            data={"type": "recaptcha"},
            source="handle_recaptcha"
        ))
        
        # First check for Cloudflare security challenge
        cloudflare_frame = await page.wait_for_selector(
            'iframe[title*="Cloudflare security challenge"]',
            timeout=SELECTOR_TIMEOUT
        )
        
        if cloudflare_frame:
            # Switch to the Cloudflare iframe
            frames = await page.frames()
            challenge_frame = next((f for f in frames if "challenge" in f.url.lower()), None)
            
            if challenge_frame:
                try:
                    # Wait for and click the checkbox
                    checkbox = await challenge_frame.wait_for_selector(
                        '[type="checkbox"]',
                        timeout=SELECTOR_TIMEOUT
                    )
                    if checkbox:
                        await checkbox.click()
                        
                        # Wait for verification to complete
                        await page.wait_for_function(
                            """() => {
                                return !document.querySelector('iframe[title*="Cloudflare security challenge"]');
                            }""",
                            timeout=RECAPTCHA_TIMEOUT
                        )
                        # Emit security challenge completed signal
                        await get_signal_manager().emit(Signal(
                            type=SignalType.SECURITY_CHALLENGE_COMPLETED,
                            data={"type": "recaptcha", "success": True},
                            source="handle_recaptcha"
                        ))
                        return ActionResult(success=True, extracted_content="Cloudflare challenge completed")
                except Exception as e:
                    logger.error(f"Failed to handle Cloudflare challenge: {str(e)}")
                    # Emit security challenge completed signal with error
                    await get_signal_manager().emit(Signal(
                        type=SignalType.SECURITY_CHALLENGE_COMPLETED,
                        data={"type": "recaptcha", "success": False, "error": str(e)},
                        source="handle_recaptcha"
                    ))
                    return ActionResult(success=False, error="Failed to complete Cloudflare challenge")
        
        # If no Cloudflare challenge, check for regular reCAPTCHA
        recaptcha_frame = await page.wait_for_selector(
            'iframe[title*="reCAPTCHA"]',
            timeout=SELECTOR_TIMEOUT
        )
        
        if not recaptcha_frame:
            return ActionResult(success=True, extracted_content="No security verification required")
        
        # Handle regular reCAPTCHA
        frames = await page.frames()
        recaptcha_iframe = next((f for f in frames if "recaptcha" in f.url.lower()), None)
        
        if not recaptcha_iframe:
            return ActionResult(success=False, error="Could not locate reCAPTCHA iframe")
        
        # Click checkbox with optimized timeout
        checkbox = await recaptcha_iframe.wait_for_selector(
            '.recaptcha-checkbox-border',
            timeout=SELECTOR_TIMEOUT
        )
        await checkbox.click()
        
        # Optimized verification wait
        try:
            await page.wait_for_function(
                """() => {
                    const frames = document.querySelectorAll('iframe');
                    for (const frame of frames) {
                        if (frame.title.includes('reCAPTCHA')) {
                            const checked = frame.contentDocument.querySelector('.recaptcha-checkbox-checked');
                            return !!checked;
                        }
                    }
                    return false;
                }""",
                timeout=RECAPTCHA_TIMEOUT
            )
            # Emit security challenge completed signal
            await get_signal_manager().emit(Signal(
                type=SignalType.SECURITY_CHALLENGE_COMPLETED,
                data={"type": "recaptcha", "success": True},
                source="handle_recaptcha"
            ))
            return ActionResult(success=True, extracted_content="reCAPTCHA verified successfully")
        except Exception:
            return await solve_recaptcha_challenge(browser)
            
    except Exception as e:
        if "timeout" in str(e).lower():
            return ActionResult(success=True, extracted_content="No security verification required")
        # Emit security challenge completed signal with error
        await get_signal_manager().emit(Signal(
            type=SignalType.SECURITY_CHALLENGE_COMPLETED,
            data={"type": "recaptcha", "success": False, "error": str(e)},
            source="handle_recaptcha"
        ))
        return ActionResult(success=False, error=f"Failed to handle security verification: {str(e)}")

async def solve_recaptcha_challenge(browser: Browser) -> ActionResult:
    """Handle advanced reCAPTCHA challenge if checkbox is insufficient."""
    try:
        page = await browser.get_current_page()
        
        # Switch to the challenge iframe
        frames = await page.frames()
        challenge_iframe = next((f for f in frames if "bframe" in f.url.lower()), None)
        if not challenge_iframe:
            return ActionResult(success=False, error="Could not locate challenge iframe")
            
        # Wait for challenge to load
        await challenge_iframe.wait_for_load_state("networkidle")
        
        # Check for audio challenge option (often easier to solve)
        audio_button = await challenge_iframe.wait_for_selector('#recaptcha-audio-button')
        if audio_button:
            await audio_button.click()
            await challenge_iframe.wait_for_load_state("networkidle")
            
            # Get audio challenge URL
            audio_source = await challenge_iframe.wait_for_selector('.rc-audiochallenge-tdownload-link')
            if audio_source:
                audio_url = await audio_source.get_attribute('href')
                if audio_url:
                    # TODO: Implement audio challenge solving if needed
                    pass
        
        # For now, we'll need human intervention for challenges
        logger.warning("Advanced reCAPTCHA challenge detected - requires human intervention")
        return ActionResult(
            success=False,
            error="Advanced reCAPTCHA challenge requires human intervention"
        )
        
    except Exception as e:
        return ActionResult(
            success=False,
            error=f"Failed to solve reCAPTCHA challenge: {str(e)}"
        )

@betting_controller.action(
    name='wait_for_popup',
    requires_browser=True
)
async def wait_for_popup(browser: Browser, timeout: int = 30000) -> ActionResult:
    """
    Wait for and handle a popup window.
    
    Args:
        browser: Browser instance
        timeout: Maximum time to wait for popup in milliseconds
        
    Returns:
        ActionResult indicating success/failure of popup handling
    """
    try:
        # Get current context
        context = await browser.get_context()
        
        # Wait for popup
        popup_page = await context.wait_for_event('page', timeout=timeout)
        
        if popup_page:
            return ActionResult(
                success=True,
                extracted_content="Popup detected and handled"
            )
        return ActionResult(
            success=False,
            error="No popup detected within timeout"
        )
            
    except Exception as e:
        return ActionResult(
            success=False, 
            error=f"Failed to handle popup: {str(e)}"
        ) 