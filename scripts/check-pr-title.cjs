#!/usr/bin/env node

const title = (process.argv.slice(2).join(' ') || process.env.PR_TITLE || '').trim();

if (!title) {
  console.error('PR title is empty.');
  process.exit(1);
}

const prefixMatch = title.match(/^\[([^\]]+)\](?:\s|:|-|$)/);
const agentTokens = new Set([
  'agent',
  'agents',
  'ai',
  'aider',
  'chatgpt',
  'claude',
  'codex',
  'copilot',
  'cursor',
  'devin',
  'openai',
]);

const hasAgentPrefix =
  prefixMatch &&
  prefixMatch[1]
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .some((token) => agentTokens.has(token));

if (hasAgentPrefix) {
  console.error(`Invalid PR title: ${title}`);
  console.error('Remove the leading agent/tool prefix. Use a concrete conventional-commits title instead, for example:');
  console.error('  fix(ci): block agent PR title prefixes');
  process.exit(1);
}
