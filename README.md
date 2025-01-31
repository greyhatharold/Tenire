# Tenire Framework

A lightweight Python framework for orchestrating automated browser interactions with stake.us, powered by ChatGPT and browser automation.

## Overview

Tenire is a proof-of-concept framework that demonstrates the integration of:
- Browser automation using the `browser-use` library for reliable web interaction
- ChatGPT-powered command generation and decision making through LangChain
- Structured betting workflows with intelligent risk management
- Modular architecture for extensible automation pipelines

The framework implements a sophisticated pipeline:
1. **Browser Management**: Headless browser control with optimized performance settings
2. **Agent Orchestration**: LLM-powered agents that make intelligent betting decisions
3. **Command Processing**: Natural language command parsing and structured execution
4. **Betting Logic**: Risk-aware bet placement with comprehensive tracking
5. **Logging & Monitoring**: Detailed activity logging with millisecond precision

**⚠️ Disclaimer**: This framework is intended for educational purposes only. Gambling can be addictive and carries inherent risks. Always gamble responsibly and be aware of your local laws and regulations regarding online gambling.

## Directory Structure

```
src/
├── main.py                 # Main entry point and workflow orchestration
└── tenire/
    ├── __init__.py        # Package initialization and version info
    ├── agent_manager.py   # LLM agent configuration with caching and parallel execution
    ├── bet_manager.py     # Structured bet placement with validation and tracking
    ├── betting_actions.py # Browser actions with optimized timeouts and error handling
    ├── browser_integration.py # Browser session with performance-tuned configuration
    ├── command_processor.py   # Command parsing with JSON/text format support
    ├── config.py          # Environment-based configuration with secret management
    └── logging_utils.py   # ISO-formatted logging with millisecond precision
```

## Configuration

Set the following environment variables:

```bash
export TENIRE_CHATGPT_API_KEY="your-openai-api-key"
export TENIRE_STAKE_USERNAME="your-stake-username"
export TENIRE_STAKE_PASSWORD="your-stake-password"
```

Or create a `.env` file:

```env
TENIRE_CHATGPT_API_KEY=your-openai-api-key
TENIRE_STAKE_USERNAME=your-stake-username
TENIRE_STAKE_PASSWORD=your-stake-password
```

## Usage

Run the main workflow:

```bash
python src/main.py
```

This will:
1. Initialize a browser session
2. Log in to stake.us using provided credentials
3. Execute a sample betting workflow using ChatGPT for decision-making
4. Analyze betting history and optimize strategy
5. Clean up resources on completion

## Architecture

The framework follows a modular architecture designed for reliability and extensibility:

- `BrowserIntegration`: Manages browser sessions with optimized performance settings
  - Configurable timeouts and retry mechanisms
  - Clean session cleanup and resource management
  - DNS pre-warming and connection optimization

- `AgentManager`: Orchestrates ChatGPT-powered decision making
  - LLM response caching for improved performance
  - Parallel operation support via ThreadPoolExecutor
  - Structured conversation history tracking
  - Vision-enabled browser interaction

- `BetManager`: Implements betting logic and risk management
  - Parameter validation and safety checks
  - Historical bet tracking and analysis
  - Comprehensive error handling
  - Real-time balance monitoring

- `CommandProcessor`: Handles natural language command interpretation
  - Support for both JSON and text-based commands
  - Extensible command handler registration
  - Structured parameter parsing
  - Error recovery mechanisms

- `Config`: Provides secure configuration management
  - Environment variable integration
  - Secret value handling via Pydantic
  - Runtime configuration validation
  - Flexible default values

- `LoggingUtils`: Implements comprehensive logging
  - ISO-formatted timestamps with millisecond precision
  - Contextual log formatting
  - File and console output support
  - Log level management

The framework's components work together to provide:
- Reliable browser automation with optimized performance
- Intelligent decision making through LLM integration
- Structured command processing and execution
- Comprehensive logging and monitoring
- Secure configuration management

## Dependencies

- [browser-use](https://github.com/browser-use/browser-use): Browser automation library
- [langchain-openai](https://python.langchain.com/docs/integrations/llms/openai): OpenAI/ChatGPT integration
- [pydantic](https://docs.pydantic.dev/): Data validation and settings management

## Responsible Gaming

This framework is provided as a technical demonstration only. If you choose to use it:

- Set strict limits on time and money spent
- Never gamble with money you can't afford to lose
- Be aware that no betting strategy guarantees profits
- Seek help if gambling becomes problematic

## Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Submit a pull request

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Disclaimer

This is a proof-of-concept project intended to demonstrate technical integration capabilities. The authors are not responsible for any losses incurred through gambling activities. Always gamble responsibly and in accordance with your local laws and regulations. 