# Conductor Setup

## Automatic Setup

When you create a new Conductor worktree, the environment is automatically configured via `.conductor.json`.

The setup script copies `.env` from the repository root to your worktree, giving you instant access to configured agents.

## First-Time Setup

### 1. Configure Repository Root

In the **repository root** (not in a worktree):

```bash
cd /path/to/adcp-client-python
cp .env.example .env
# Edit .env with your agent configurations
```

### 2. Start Working

That's it! When you create or enter a Conductor worktree, the setup happens automatically.

## Environment Variables

### ADCP_AGENTS (Recommended)

Use this JSON format for multi-agent configuration:

```bash
ADCP_AGENTS='[
  {
    "id": "creative_agent",
    "name": "Creative Agent",
    "agent_uri": "https://creative.adcontextprotocol.org",
    "protocol": "mcp"
  },
  {
    "id": "my_agent",
    "name": "My Agent",
    "agent_uri": "https://my-agent.example.com",
    "protocol": "mcp",
    "auth_token": "your-token-here"
  }
]'
```

Then in code:

```python
from adcp import ADCPMultiAgentClient

client = ADCPMultiAgentClient.from_env()
agent = client.agent("my_agent")
```

### Individual Variables

For single-agent usage:

```bash
AGENT_URI=https://agent.example.com
AGENT_PROTOCOL=mcp
AGENT_TOKEN=your-token
```

## Manual Setup

If automatic setup doesn't work, run manually:

```bash
python3 scripts/setup_conductor_env.py
```

## Security

- ✅ `.env` is gitignored
- ✅ Store tokens in `.env` at repository root
- ✅ Use `.env.example` for documentation
- ❌ Never commit actual tokens to git

## Testing

```bash
# Creative agent (public, no auth)
python3 tests/integration/test_creative_agent.py

# Your own agents (configured in .env)
python3 tests/integration/test_your_agent.py
```

## Troubleshooting

**"No .env file found"**:
```bash
cd /path/to/adcp-client-python  # Repository root
cp .env.example .env
# Edit .env
```

**Setup didn't run automatically**:
```bash
# Run manually in your worktree
python3 scripts/setup_conductor_env.py
```

See full documentation in README.md for complete usage examples.
