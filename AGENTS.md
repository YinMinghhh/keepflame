# KeepFlame Project Guide

## Project purpose

KeepFlame is a long-running personal automation system that maintains recurring
states across unrelated Internet services. It is not merely a collection of
check-in scripts.

Typical Flames include:

- daily check-ins on community websites;
- Telegram bot commands, button presses, and reward claims;
- recurring web-game tasks;
- scheduled API calls;
- token, session, or other remote-state refreshes.

Keep the product focused on the loop:

```text
schedule -> inspect -> decide -> act -> record -> notify or wait for a human
```

Do not expand the project into a general-purpose RPA platform, workflow engine,
or cron-management product unless that direction is explicitly approved.

## Ubiquitous language

- **Flame**: one external state or recurring obligation that should remain
  healthy over time.
- **Keeper**: the plugin or executor responsible for inspecting and maintaining
  a Flame.
- **Capability**: reusable infrastructure used by Keepers, such as HTTP,
  browser automation, Telegram, storage, or notifications.
- **Context**: the runtime resources supplied to a Keeper, including account,
  secrets, clients, storage, and logging.
- **Keep result**: the normalized outcome returned by a Keeper. Normalize the
  result, not the platform-specific procedure.

Prefer these terms in code, configuration, documentation, logs, and UI copy.

## Architecture

Use three primary layers:

1. **Core** owns scheduling, lifecycle orchestration, context construction,
   result types, state transitions, and plugin discovery.
2. **Flame plugins** own platform-specific authentication, inspection,
   parsing, actions, and error interpretation.
3. **Capabilities** provide reusable technical integrations such as HTTP,
   browsers, Telegram, storage, secrets, and notifications.

The repository uses a `src` layout so the repository root and the importable
package cannot be confused. Keep the top-level structure aligned with:

```text
.
├── AGENTS.md
├── config/                     # safe configuration examples and schemas
├── src/
│   └── keepflame/              # production package
│       ├── core/               # framework and lifecycle contracts
│       ├── flames/             # one plugin package per maintained service
│       ├── capabilities/       # reusable HTTP/browser/Telegram adapters
│       ├── storage/            # persistence ports and implementations
│       └── notifier/           # notification formatting and delivery
└── tests/                      # automated tests and non-secret fixtures
```

Directory ownership is part of the architecture:

- `core/` must not import a concrete Flame plugin or contain platform-specific
  selectors, endpoints, or SDK logic.
- `flames/` contains platform-specific procedures. Add a subdirectory only
  when implementing that Flame, and keep its authentication, parsing, and
  action logic local to it.
- `capabilities/` exposes reusable technical operations. It must not decide
  which Flame runs or encode a service's business rules.
- `storage/` persists normalized domain state and execution history without
  leaking database details into Keepers.
- `notifier/` renders and delivers results but does not determine lifecycle
  outcomes.
- `config/` contains shareable examples, schemas, and defaults only. Runtime
  credentials and account-specific configuration must remain untracked.
- `tests/` mirrors the production boundaries as the suite grows; real external
  services are never the default test dependency.

Each important directory has a short `README.md`. Update the relevant README
when a directory's responsibility or boundary changes. Do not add placeholder
modules merely to make the tree look complete.

## Design rules

### Normalize outcomes, not procedures

Do not force HTTP requests, Telegram conversations, and browser interactions
behind one generic sequence of steps. Each Keeper may use any appropriate
procedure. The core should consume a small, stable result model.

A result should be able to represent at least:

- status;
- human-readable message;
- observed or claimed reward;
- relevant metadata;
- next eligible or scheduled action time.

### Human intervention is a first-class state

At minimum, preserve distinct states equivalent to:

```text
SUCCESS
FAILED
WAITING_HUMAN
```

QR-code login, MFA, expired credentials, unsupported security challenges, and
interactive approval must not be flattened into ordinary execution failure. Persist enough context
to resume safely after the user intervenes.

### Keep platform complexity local

A platform plugin may split into its own client, parser, configuration, and
handlers as it grows. Do not leak platform-specific response shapes, selectors,
or authentication rules into the core.

### Capabilities are not platforms

Classify browser automation, HTTP, and Telegram as capabilities. A single Flame
may compose several capabilities, and multiple Flames may reuse one capability.

### Configuration drives activation

Do not hard-code enabled Flames or schedules. Configuration should determine
which plugins run, under which account, on what schedule, and with what
plugin-specific settings. Validate configuration early and report actionable
errors.

### Preserve history, not only the latest boolean

Storage should support the current Flame state as well as execution history,
including:

- last successful maintenance time;
- streak or continuity information when meaningful;
- consecutive failure count;
- rewards or noteworthy observations;
- execution logs and failure reasons;
- next action time;
- pending human intervention.

Do not fabricate streaks for services where continuity cannot be observed
reliably. Distinguish observed remote state from local inference.

### Secrets stay out of source and logs

Never commit credentials, cookies, tokens, session files, private keys, or
captured authenticated pages. Redact sensitive values in logs and errors.
Keep account identity explicit so credentials and state cannot cross between
accounts accidentally.

## Product behavior

The primary user-facing question is whether each Flame is still healthy. Make
status, continuity, the latest action, failure reason, and required human action
easy to understand.

Brand language may use the fire metaphor, but correctness comes first. For
example, distinguish a maintained Flame, a failed attempt, and a paused Flame
instead of showing all three as generic success or failure.

Notifications should be useful and restrained. Prefer summaries and actionable
alerts over a message for every routine internal step.

## Implementation guidance

- Keep the core unaware of whether a Keeper uses HTTP, Telegram, or a browser.
- Make Keeper runs idempotent where the external service permits it.
- Set explicit timeouts and bounded retries for network and browser operations.
- Avoid retrying actions that may claim rewards or mutate remote state unless
  their safety is known.
- Record enough structured evidence to diagnose failures without persisting
  secrets or excessive page contents.
- Make schedules and time zones explicit.
- Prefer incremental additions: one vertical Flame flow is more valuable than
  a large set of empty abstractions.

## Verification expectations

For core changes, test lifecycle and state transitions independently of real
services. For a Flame plugin, cover result mapping and failure interpretation
with fixtures or fakes where practical.

Live success against one account does not prove general correctness. Clearly
separate mocked tests, local integration tests, and real external-service
verification in reports and documentation.

Do not perform destructive remote actions or weaken account security for
convenience. The user has explicitly authorized implementing local JoinQuant
slider solving for their own daily sign-in. Keep that flow within its dedicated,
explicitly enrolled account profile; use bounded attempts and verified remote
completion. This does not authorize other reward tasks, third-party challenge
services, unrelated accounts, or live verification without an approved account
handoff. Expired sessions, MFA and unsupported challenges require human action.


The 2026-10-06 continuation authorizes offline implementation and synthetic
verification of separate JoinQuant reading credits, long-lived browser contexts
and disabled deployment preparation. This does not authorize live reading,
login, reward claims, copying account profiles, server connections or service
installation. Mail work is paused. Keep every default non-executing, preserve
unconfirmed action intents across restarts and never require a human login for
development tests.
