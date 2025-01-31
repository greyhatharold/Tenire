"""
Browser configuration module for the Tenire framework.

This module contains all browser-related configuration settings,
including timeouts, URLs, and browser launch arguments.
"""

import os
from datetime import timedelta
from urllib.parse import urljoin

# Base URLs
BASE_URL = "https://stake.us"
LOGIN_URL = urljoin(BASE_URL, "/login")

# Timeouts (in milliseconds)
DEFAULT_TIMEOUT = 10000  # 10 seconds
PAGE_LOAD_TIMEOUT = 15000  # 15 seconds
NAVIGATION_TIMEOUT = 20000  # 20 seconds
CONTEXT_REFRESH_INTERVAL = timedelta(minutes=30)  # Refresh context every 30 minutes

# Security settings
SECURITY_CHECK_TIMEOUT = 15  # seconds (Reduced from 30 to 15 seconds)
MAX_SECURITY_RETRIES = 2  # Reduced from 3 to 2 retries
SECURITY_CHECK_INTERVAL = 1.0  # Interval between security check retries in seconds

# Browser window settings
WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 1100

# User data directory for persistent browser data
USER_DATA_DIR = os.path.join(os.path.expanduser("~"), ".tenire", "browser_data")
os.makedirs(USER_DATA_DIR, exist_ok=True)

# File paths
COOKIES_FILE = os.path.join(USER_DATA_DIR, "cookies.json")
OAUTH_TOKENS_FILE = os.path.join(USER_DATA_DIR, "oauth_tokens.json")

# Selectors
SELECTORS = {
    'user_profile': "#stomp-meister",
    'google_button': 'button:has-text("Google")',
    'email_input': 'input[type="gjstrier@gmail.com"], #Email',
    'password_input': 'input[type="GriffinJ10!"], #password',
    'cloudflare_challenge': 'iframe[title*="Cloudflare security challenge"]',
    'recaptcha': 'iframe[title*="reCAPTCHA"]'
}

# Human-like interaction delays (in seconds)
INTERACTION_DELAYS = {
    'pre_login': (3, 1.5),  # Increased base and random delays
    'button_click': (1.5, 1.5),
    'mouse_move': (0.2, 0.3),
    'key_press': (0.1, 0.15),
    'password_key_press': (0.15, 0.2),
    'enter_press': (0.75, 0.3),
    'post_enter': (2.0, 0.75),
    'focus_delay': (0.75, 0.75),
    'final_enter': (1.0, 0.5)
}

# Mouse movement ranges
MOUSE_MOVEMENT = {
    'random_x_range': (100, 500),
    'random_y_range': (100, 500),
    'button_offset_range': (-5, 5)
}

# Browser context configuration
BROWSER_CONTEXT_CONFIG = {
    'browser_window_size': {'width': WINDOW_WIDTH, 'height': WINDOW_HEIGHT},
    'no_viewport': None,
    'locale': 'en-US',
    'wait_for_network_idle_page_load_time': 5.0,  # Increased from 3.0
    'highlight_elements': True,
    'viewport_expansion': 500,
    'minimum_wait_page_load_time': 1.0,  # Increased from 0.5
    'maximum_wait_page_load_time': 10.0,  # Increased from 5.0
    'cookies_file': COOKIES_FILE,
    'user_agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36'
}

# Browser launch arguments
BROWSER_LAUNCH_ARGS = [
    '--no-sandbox',
    '--enable-automation=false',  # Hide automation
    '--password-store=basic',
    f'--window-size={WINDOW_WIDTH},{WINDOW_HEIGHT}',
    '--start-maximized',
    '--enable-cookies',
    '--restore-last-session',
    '--enable-features=NetworkService,NetworkServiceInProcess',
    '--disable-features=IsolateOrigins,site-per-process',  # Disable site isolation for better persistence
    '--disable-gpu',  # Disable GPU acceleration in cold conditions
    '--no-zygote',  # Avoid cold start delays
    '--disable-dev-shm-usage',  # Avoid memory issues in cold conditions
    '--disable-background-networking',  # Reduce background load
    '--disable-default-apps'  # Reduce startup overhead
]

# Browser configuration
BROWSER_CONFIG = {
    'headless': False,
    'disable_security': False,  # Allow security for manual login
    'chrome_instance_path': None,
    'extra_chromium_args': BROWSER_LAUNCH_ARGS
}

# Authentication settings
GOOGLE_AUTH_CONFIG = {
    'client_id': 'com.chrome.browser',
    'response_type': 'token',
    'scope': 'openid profile email',
    'redirect_uri': 'https://accounts.google.com/o/oauth2/approval/chrome',
    'auth_endpoint': 'https://accounts.google.com/o/oauth2/v2/auth',
    'accounts_endpoint': 'https://accounts.google.com/ListAccounts?gpsia=1'
}

# Default cookies for authentication
DEFAULT_AUTH_COOKIES = [
    {
        'name': 'CONSENT',
        'value': 'YES+cb.20231215-11-p0.en+FX+999',
        'domain': '.google.com',
        'path': '/'
    },
    {
        'name': 'SID',
        'value': 'dQj7Y8p7JHDsA_Qj7Y8p7JHDsA_Qj7Y8p7JHDsA_',
        'domain': '.google.com',
        'path': '/'
    },
    {
        'name': '__Secure-1PSID',
        'value': 'dQj7Y8p7JHDsA_Qj7Y8p7JHDsA_Qj7Y8p7JHDsA_',
        'domain': '.google.com',
        'path': '/',
        'secure': True
    }
]

# Browser initialization settings
BROWSER_INIT_CONFIG = {
    'max_retries': 3,
    'retry_delay': 2,  # seconds
    'initialization_timeout': 60000,  # 60 seconds
    'dom_content_timeout': 30000  # 30 seconds
}

# Initial browser state
INITIAL_BROWSER_STATE = {
    'status': 'initialized',
    'is_ready': False,
    'last_error': None,
    'current_url': None,
    'initialization_attempts': 0
}

# Local storage keys
STORAGE_KEYS = {
    'google_auth_cookies': 'google_auth_cookies',
    'google_oauth_token': 'google_oauth_token'
}

# Agent configuration
AGENT_CONFIG = {
    'max_task_retries': 2,
    'task_timeout': 300,  # 5 minutes
    'retry_delay': 1.0,
    'batch_request': {
        'batch_size': 10,
        'request_timeout': 30,
        'max_retries': 3,
        'retry_delay': 1
    },
    'progress_monitoring': {
        'progress_timeout': 30.0,  # 30 seconds max without progress
        'step_delay': 0.1  # Brief pause between steps
    },
    'initial_actions': [
        {'wait_for_load_state': {'state': 'networkidle', 'timeout': 10000}},
        {'handle_recaptcha': {}}
    ],
    'max_failures': 2,
    'use_vision': True,
    'save_conversation_path': "logs/conversation.json"
}

# Agent state
INITIAL_AGENT_STATE = {
    'status': 'initialized',
    'is_ready': False,
    'last_error': None,
    'current_task': None,
    'active_tasks': set(),
    'task_attempts': {},
    'task_results': {}
} 