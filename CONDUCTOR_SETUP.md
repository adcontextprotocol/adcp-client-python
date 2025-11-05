# Conductor Setup Guide

This guide explains how to set up the AdCP Python client when working with Conductor worktrees.

## Why This Matters

When using Conductor, you work in isolated worktrees (e.g., `.conductor/my-feature/`). Each worktree needs its own environment configuration, but you don't want to manually copy credentials to every new worktree.

**Solution**: Store credentials in the repository root (`.env`) and automatically copy them to new worktrees with a setup script.

## Setup Process

### Step 1: Create Repository-Level Configuration

In the **repository root** (not in a worktree), create your `.env` file:

```bash
# Navigate to repository root
cd /path/to/adcp-client-python

# Copy example file
cp .env.example .env

# Edit with your configurations
nano .env  # or your preferred editor
```

The `.env.example` includes configurations for all test agents:
- Creative Agent (MCP, no auth)
- Optable Signals (MCP, with token)
- Wonderstruck Sales (MCP, with token)
- Test Agent (A2A, with token)

### Step 2: Run Setup Script in Worktree

Every time you start work in a Conductor worktree, run the setup script:

```bash
# In your Conductor worktree
cd .conductor/my-worktree/

# Run setup script (Python version)
python3 scripts/setup_conductor_env.py

# Or bash version
./scripts/setup_conductor_env.sh
```

The script will:
1. ✅ Check if you're in a Conductor worktree
2. ✅ Find the `.env` file in repository root
3. ✅ Copy it to your current worktree
4. ✅ Display configured agents

**Output example**:
```
============================================================
Conductor Environment Setup
============================================================

Worktree: /Users/you/conductor/adcp-client-python/.conductor/my-worktree
Repository: /Users/you/conductor/adcp-client-python

📋 Copying .env to worktree...
✅ Environment configuration copied successfully!

Configuration file: /Users/you/conductor/adcp-client-python/.conductor/my-worktree/.env

📊 Checking agent configuration...
   Found 4 agent(s):
   🔓 Creative Agent (MCP)
   🔐 Optable Signals (MCP)
   🔐 Wonderstruck (MCP)
   🔐 Test Agent (A2A)

🎉 Setup complete! You can now run integration tests.
```

### Step 3: Install and Test

```bash
# Install dependencies
pip install -e .

# Or with dev dependencies
pip install -e ".[dev]"

# Run integration tests
python3 tests/integration/test_creative_agent.py
python3 tests/integration/test_optable_signals.py
python3 tests/integration/test_wonderstruck_sales.py
```

## Environment Variables

### ADCP_AGENTS (JSON Format)

The main configuration variable used by `ADCPMultiAgentClient.from_env()`:

```bash
ADCP_AGENTS='[
  {
    "id": "creative_agent",
    "name": "Creative Agent",
    "agent_uri": "https://creative.adcontextprotocol.org",
    "protocol": "mcp"
  },
  {
    "id": "sales_agent",
    "name": "Sales Agent",
    "agent_uri": "https://sales.example.com/mcp",
    "protocol": "mcp",
    "auth_token": "your-token-here"
  }
]'
```

### Individual Agent Variables

For single-agent usage:

```bash
# Creative Agent
CREATIVE_AGENT_URI=https://creative.adcontextprotocol.org
CREATIVE_AGENT_PROTOCOL=mcp

# Optable Signals
OPTABLE_AGENT_URI=https://sandbox.optable.co/admin/adcp/signals/mcp
OPTABLE_AGENT_PROTOCOL=mcp
OPTABLE_AGENT_TOKEN=your-token-here
```

### Webhook Configuration (Optional)

```bash
WEBHOOK_URL_TEMPLATE=https://myapp.com/webhooks/{agent_id}/{task_type}/{operation_id}
WEBHOOK_SECRET=your-webhook-secret-here
```

## Usage in Code

### Load All Agents from Environment

```python
from adcp import ADCPMultiAgentClient

# Load from ADCP_AGENTS environment variable
client = ADCPMultiAgentClient.from_env()

# Use specific agent
sales_agent = client.agent("sales_agent")
result = await sales_agent.get_products(brief="tech products")
```

### Manual Configuration

```python
from adcp import ADCPClient
from adcp.types import AgentConfig, Protocol
import os

config = AgentConfig(
    id="my_agent",
    agent_uri=os.getenv("MY_AGENT_URI"),
    protocol=Protocol.MCP,
    auth_token=os.getenv("MY_AGENT_TOKEN"),
)

client = ADCPClient(config)
```

## Integration Tests

Each test agent has a dedicated integration test file:

| Test File | Agent | Protocol | Auth Required |
|-----------|-------|----------|---------------|
| `test_creative_agent.py` | Creative Agent | MCP | No |
| `test_optable_signals.py` | Optable Signals | MCP | Yes |
| `test_wonderstruck_sales.py` | Wonderstruck Sales | MCP | Yes |
| `test_a2a_agent.py` | Test Agent | A2A | Yes |

Run all tests:

```bash
# Run individual test
python3 tests/integration/test_creative_agent.py

# Run all tests (if using pytest)
pytest tests/integration/ -v
```

## Security Best Practices

### ✅ DO
- Keep `.env` in repository root (outside worktrees)
- Add `.env` to `.gitignore`
- Use `.env.example` for documentation
- Rotate tokens regularly
- Use different tokens for dev/staging/production

### ❌ DON'T
- Commit `.env` to git
- Share tokens in public channels
- Use production tokens in development
- Hard-code credentials in test files

## Troubleshooting

### "No .env file found in repository root"

**Problem**: Setup script can't find `.env` in repository root.

**Solution**:
```bash
cd /path/to/adcp-client-python  # Repository root
cp .env.example .env
# Edit .env with your tokens
```

### "Could not parse ADCP_AGENTS JSON"

**Problem**: Invalid JSON in `ADCP_AGENTS` variable.

**Solution**:
1. Check JSON syntax (proper quotes, commas, brackets)
2. Validate at https://jsonlint.com/
3. Ensure proper escaping in shell

### "MCP SDK not installed"

**Problem**: Python version is too old or MCP not installed.

**Solution**:
```bash
# Check Python version
python3 --version  # Need 3.10+

# Install MCP SDK
pip install mcp

# Or use Docker with Python 3.10+
docker run -it --rm -v $(pwd):/app python:3.10 bash
```

### Tests return 404 errors

**Problem**: Wrong endpoint or agent not responding.

**Possible causes**:
1. Agent URI is incorrect
2. Authentication token is invalid/expired
3. Agent doesn't support the protocol
4. Network/firewall issues

**Solution**:
1. Verify agent URI in `.env`
2. Check token validity with agent provider
3. Try without auth first (creative agent)
4. Check agent documentation

## Adding New Agents

To add a new agent to your configuration:

### 1. Update `.env` in Repository Root

```bash
# Individual variables
NEW_AGENT_URI=https://new-agent.example.com
NEW_AGENT_PROTOCOL=mcp
NEW_AGENT_TOKEN=token-here

# Add to ADCP_AGENTS JSON
ADCP_AGENTS='[
  ...,
  {
    "id": "new_agent",
    "name": "New Agent",
    "agent_uri": "https://new-agent.example.com",
    "protocol": "mcp",
    "auth_token": "token-here"
  }
]'
```

### 2. Create Integration Test

```python
# tests/integration/test_new_agent.py
import asyncio
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from adcp import ADCPClient
from adcp.types import AgentConfig, Protocol

async def test_new_agent():
    config = AgentConfig(
        id="new_agent",
        agent_uri="https://new-agent.example.com",
        protocol=Protocol.MCP,
        auth_token="token-here",
    )

    client = ADCPClient(config)

    result = await client.get_products(brief="test")
    print(f"Success: {result.success}")
    print(f"Data: {result.data}")

if __name__ == "__main__":
    asyncio.run(test_new_agent())
```

### 3. Run Setup Script Again

```bash
python3 scripts/setup_conductor_env.py
```

The new agent will be available in your worktree!

## Automation Ideas

### Auto-run on Worktree Creation

Add to your shell profile (`.bashrc`, `.zshrc`):

```bash
# Auto-setup AdCP environment in Conductor worktrees
conductor_adcp_setup() {
    if [[ "$PWD" =~ \.conductor.*adcp-client-python ]]; then
        if [ -f "scripts/setup_conductor_env.py" ]; then
            python3 scripts/setup_conductor_env.py
        fi
    fi
}

# Run on cd
cd() {
    builtin cd "$@" && conductor_adcp_setup
}
```

### Pre-commit Hook

Create `.git/hooks/pre-commit`:

```bash
#!/bin/bash
# Prevent committing .env files

if git diff --cached --name-only | grep -E '\.env$'; then
    echo "❌ Error: Attempted to commit .env file!"
    echo "Remove it from staging: git reset HEAD .env"
    exit 1
fi
```

## Additional Resources

- **Implementation Summary**: See `IMPLEMENTATION_SUMMARY.md`
- **Testing Status**: See `TESTING_STATUS.md`
- **API Documentation**: See main `README.md`
- **Examples**: See `examples/` directory

## Support

For issues or questions:
1. Check `TESTING_STATUS.md` for known issues
2. Review integration test files for examples
3. Open an issue in the repository
