# Kyiv Monitor Air Alert (English)

Kyiv Monitor is a continuously running Python worker that reads Ukrainian Telegram
sources and publishes English-language information to two separate destinations:

- an alert-only channel for time-sensitive Kyiv air-alert updates;
- a news group for hourly categorized summaries during normal operation.

The service is informational. It must not be treated as a replacement for official
Ukrainian civil-defence instructions or local emergency alerts.

## Operating modes

### NORMAL

Messages from `@kievinfo_kyiv`, `@shv_ukr`, `@AMK_Mapping`, and `@insiderukr` are persisted in
SQLite and summarized hourly. Between 01:00 and 07:00 Europe/Kyiv, hourly publishing
is paused and the accumulated material is used for the morning recap.

### ALERT

The explicit Kyiv state from `@kyiv_airraid_alert` controls the mode. While active,
actionable new messages and edits from `@kyivnebomonitoring` enter the low-latency translation
pipeline. Alert transitions are serialized, public delivery must be confirmed before
the state is committed, and stale translations are discarded after the alert ends.

### TEST

`TEST_MODE=true` disables real-source handlers and confines output to `TEST_CHAT_ID`.
By default it does not open `TELEGRAM_SESSION`, preventing staging from invalidating the
production authorization key. Interactive test commands require a separate
`TEST_TELEGRAM_SESSION`; never reuse the production session. Available commands are
`/test_start`, `/test_message`, `/test_burst N`, `/test_end`, and `/test_summary`.

## Architecture

```text
Telegram sources ── Telethon ── SQLite queue ── Anthropic ── Telegram Bot API
                              │
@kyiv_airraid_alert ── pure classifier ── serialized alert state
```

- `monitor.py`: orchestration, pipelines and integrations.
- `alert_rules.py`: pure trigger classification with no network or global state.
- `tests/`: routing, persistence, restart and transition regression tests.
- `docs/`: detailed architecture, operations and historical test log.

The project is being separated incrementally rather than rewritten, keeping external
behaviour covered by tests at each step.

## Configuration

Copy `.env.example` and provide real values through the deployment environment.
Never commit credentials or a Telethon session string.

Required in production:

- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_SESSION`;
- `ANTHROPIC_API_KEY`, `BOT_TOKEN`;
- `TARGET_CHAT_ID`, `SUMMARY_CHAT_ID`, `SUMMARY_CHAT_LINK`;
- `OWNER_CHAT_ID`; optionally a distinct `OPS_CHAT_ID`;
- `ADMIN_USER_IDS`, a comma-separated allowlist for manual overrides.

`TARGET_CHAT_ID` and `SUMMARY_CHAT_ID` must be different. `SUMMARY_CHAT_LINK` must
point to the news group, not the alert channel.

## Local verification

The tests stub external services and do not send Telegram or Anthropic requests.

```bash
python3 -m pip install -r requirements.txt
python3 -m py_compile monitor.py alert_rules.py predeploy_check.py
python3 -m unittest discover -s tests -v
```

Lint locally with [ruff](https://docs.astral.sh/ruff/) (`pip install ruff`, config in
`pyproject.toml`):

```bash
ruff check .
```

## Deployment safety

GitHub Actions runs syntax checks and the complete test suite for pull requests and
pushes to `main`. Configure GitHub branch protection so `main` requires the `test`
check and at least one review. Railway should deploy production only from protected
`main`; use a separate Railway service/environment with `TEST_MODE=true` for staging.

`railway.json` runs `predeploy_check.py` before promotion. It rejects missing or
malformed production configuration—including an invalid Telethon StringSession—without
printing secret values, so a bad configuration cannot replace a running deployment.
Failed workers are limited to three restart attempts instead of looping indefinitely.

Production acquires an exclusive lock on the persistent `/data` volume before opening
Telethon. A replacement container waits for the previous worker to exit, preventing
rolling deploys from invalidating `TELEGRAM_SESSION` through simultaneous connections.

Do not test manual alert commands in the production channel. Verify the candidate
commit in staging, merge only after green checks, and retain the preceding Railway
deployment for rollback.

## Manual overrides

Only Telegram user IDs listed in `ADMIN_USER_IDS` may execute `/alert` or `/normal`.
Rejected attempts are logged and reported to Ops. The canonical automatic state
continues to come from `@kyiv_airraid_alert`.

## Further documentation

- [`docs/PROJECT_ARCHITECTURE.md`](docs/PROJECT_ARCHITECTURE.md)
- [`docs/SELF_HEALING_LOOPS.md`](docs/SELF_HEALING_LOOPS.md)
- [`NEXT_STEPS_AND_IMPLEMENTATIONS.md`](NEXT_STEPS_AND_IMPLEMENTATIONS.md)
