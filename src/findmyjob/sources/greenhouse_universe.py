from __future__ import annotations

# Best-effort public Greenhouse seed universe for broad US-focused autopilot runs.
# This list is intentionally curated rather than exhaustive. Some boards may be
# retired over time; sync logic tolerates partial failures and continues.
BUILTIN_GREENHOUSE_US_BOARD_TOKENS: tuple[str, ...] = (
    'affirm',
    'airbyte',
    'asana',
    'benchling',
    'brex',
    'clickhouse',
    'cloudflare',
    'coinbase',
    'coursera',
    'datadog',
    'discord',
    'doordash',
    'dropbox',
    'figma',
    'flexport',
    'gusto',
    'hashicorp',
    'instacart',
    'mongodb',
    'netlify',
    'notion',
    'openai',
    'plaid',
    'ramp',
    'reddit',
    'replit',
    'rippling',
    'robinhood',
    'scaleai',
    'segment',
    'sentry',
    'snowflake',
    'sourcegraph',
    'spotify',
    'stripe',
    'turo',
    'twilio',
    'vercel',
    'webflow',
    'zapier',
)


def builtin_greenhouse_board_tokens() -> list[str]:
    return list(BUILTIN_GREENHOUSE_US_BOARD_TOKENS)
