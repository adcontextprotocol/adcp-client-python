#!/bin/bash
#
# Conductor Environment Setup Script
#
# This script copies .env configuration from the parent repository to the current
# Conductor worktree. Run this when starting a new worktree in Conductor.
#
# Usage:
#   ./scripts/setup_conductor_env.sh
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$WORKTREE_ROOT/../.." && pwd)"

echo "================================================"
echo "Conductor Environment Setup"
echo "================================================"
echo ""
echo "Worktree: $WORKTREE_ROOT"
echo "Repository: $REPO_ROOT"
echo ""

# Check if we're in a Conductor worktree
if [[ ! "$WORKTREE_ROOT" =~ \.conductor ]]; then
    echo "⚠️  Warning: This doesn't appear to be a Conductor worktree"
    echo "   Current path: $WORKTREE_ROOT"
    echo ""
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Check if .env exists in repository root
if [ ! -f "$REPO_ROOT/.env" ]; then
    echo "❌ No .env file found in repository root: $REPO_ROOT/.env"
    echo ""
    echo "Please create one based on .env.example:"
    echo "  cd $REPO_ROOT"
    echo "  cp .env.example .env"
    echo "  # Edit .env with your configuration"
    echo ""
    exit 1
fi

# Copy .env to worktree
echo "📋 Copying .env to worktree..."
cp "$REPO_ROOT/.env" "$WORKTREE_ROOT/.env"

if [ $? -eq 0 ]; then
    echo "✅ Environment configuration copied successfully!"
    echo ""
    echo "Configuration file: $WORKTREE_ROOT/.env"
    echo ""

    # Show loaded agents
    echo "📊 Checking agent configuration..."
    if command -v python3 &> /dev/null; then
        python3 -c "
import json
import os
import sys

env_path = '$WORKTREE_ROOT/.env'
agents_json = None

# Parse .env file to extract ADCP_AGENTS
with open(env_path, 'r') as f:
    for line in f:
        if line.startswith('ADCP_AGENTS='):
            agents_json = line.split('=', 1)[1].strip().strip(\"'\")
            break

if agents_json:
    try:
        agents = json.loads(agents_json)
        print(f'   Found {len(agents)} agent(s):')
        for agent in agents:
            auth = '🔐' if agent.get('auth_token') else '🔓'
            print(f'   {auth} {agent[\"name\"]} ({agent[\"protocol\"].upper()})')
    except json.JSONDecodeError:
        print('   ⚠️  Could not parse ADCP_AGENTS JSON')
else:
    print('   ⚠️  ADCP_AGENTS not found in .env')
" 2>/dev/null
    fi

    echo ""
    echo "🎉 Setup complete! You can now run integration tests."
    echo ""
    echo "Next steps:"
    echo "  1. Install dependencies: pip install -e ."
    echo "  2. Run tests: python3 tests/integration/test_creative_agent.py"
    echo ""
else
    echo "❌ Failed to copy .env file"
    exit 1
fi
