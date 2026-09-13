---
title: "Design Doc: Telegram Interview Coach Bot"
date: 2026-09-13
---

# Telegram Interview Coach Bot — Design Doc

A personal backend-engineering interview coach, delivered as a Telegram bot, running serverless on Modal, backed by a Groq-hosted LLM and a lightweight SQLite state layer.

## 1. Motivation

Preparing for backend engineering interviews (with a deliberate weighting toward Kafka and distributed systems) benefits from active recall and spaced repetition rather than passive reading. This project is a low-friction way to get daily, structured interview practice inside an app I already check constantly — Telegram — instead of a dedicated study tool I'd have to remember to open.

## 2. Goals / Non-Goals

**Goals**
- Conduct realistic, interviewer-style Q&A (not tutor-style over-explaining).
- Track topic-level progress across sessions without manual bookkeeping.
- Nudge daily practice via an automated morning prompt.
- Run cheaply and require near-zero maintenance.

**Non-goals**
- Not a general-purpose chatbot or tutoring platform.
- Not designed for multi-user scale — single-user, personal tool.
- Not attempting long-term analytics/dashboards (yet) — see [Future Work](#8-future-work).

## 3. Architecture Overview

```
Telegram user
     │  message
     ▼
Telegram Bot API  ── webhook POST ──▶  Modal (telegram_webhook, FastAPI endpoint)
                                              │
                                              ├─▶ build_system_prompt()
                                              │      ├─ skills/interview.md   (coaching rules)
                                              │      ├─ skills/telegram.md    (output formatting rules)
                                              │      └─ SQLite study_state    (current topic snapshot)
                                              │
                                              ├─▶ Groq API (openai/gpt-oss-120b)
                                              │
                                              └─▶ reply sent back to Telegram

Modal Cron (00:00 UTC / 08:00 SGT)
     │
     └─▶ daily_reminder_8am()
              ├─ backs up state.db
              └─ sends a warm-up question using the same system prompt
```

Everything runs as a single Modal `App`, deployed with `modal deploy`. There is no always-on server process.

## 4. Component Breakdown

### 4.1 Telegram interface (python-telegram-bot)
Handles `/start`, `/done`, and free-text messages. Each handler builds a fresh system prompt per turn (so SQLite state is always current) and calls the LLM synchronously.

### 4.2 Compute: Modal, serverless webhook (not polling)
**Decision:** use a Telegram *webhook* (`@modal.fastapi_endpoint`) rather than long-polling (`Application.run_polling()`).

Long-polling requires a permanently-running process. Modal's execution model is request-driven and scales to zero; a polling loop fights that model and hits a hard 24-hour function timeout, requiring manual restarts. A webhook fits Modal's model natively: Telegram calls Modal directly, a container spins up on demand, handles the update, and can scale back to zero. No idle cost, no timeout to babysit.

Trade-off accepted: cold-start latency on the first message after idle time, and a small amount of added complexity (managing a shared secret token to authenticate that requests genuinely come from Telegram, since the endpoint is a public URL).

### 4.3 Model: Groq-hosted `openai/gpt-oss-120b`
Originally built against OpenAI, migrated to Groq's API (OpenAI-compatible client, different `base_url`) for its free tier, given this is a personal low-volume project.

### 4.4 Prompting: composable skill files
Two markdown files, loaded at runtime and concatenated into the system prompt:
- `skills/interview.md` — interview behavior, curriculum priority (Kafka-weighted), coaching style.
- `skills/telegram.md` — strict output-formatting constraints (no Markdown tables/headers, HTML-tag whitelist) so replies actually render correctly in Telegram's HTML parse mode.

**Decision:** keep these as separate files rather than inlined Python strings. This makes the bot's "personality" and platform constraints independently editable without touching application code, and keeps the system prompt legible.

Bundled into the Modal image via `Image.add_local_file(...)`, so they ship with the deployed container; changes require a redeploy to take effect (an accepted trade-off for simplicity over live-reloading).

### 4.5 State: two-table SQLite design
| Table | Purpose | Written | Sent to LLM |
|---|---|---|---|
| `study_state` | Current snapshot, one row per topic (`topic` is the primary key) | Upserted only on `/done` | Yes — filtered, capped, and prioritized |
| `study_history` | Append-only log, one row per assessment, never overwritten | Inserted only on `/done` | No |

**Decision rationale:** state is only written on `/done`, not on every message. This keeps regular back-and-forth conversation cheap (no JSON parsing overhead per turn) while giving deliberate, session-level checkpoints of what was actually learned.

Splitting current-state from history solves a specific problem: a flat, ever-growing table sent in full on every prompt would eventually (a) bloat token usage and (b) dilute signal — a handful of genuinely weak topics buried among months of `mastered` entries. `study_state` stays small and prompt-relevant; `study_history` can grow indefinitely as a genuine progress record without touching the LLM's context at all.

`get_db_summary()` further constrains what reaches the prompt:
- Excludes `mastered` topics from the detailed list (shown only as a count).
- Caps the list to the 10 highest-priority, least-recently-reviewed topics.
- Surfaces "days since last reviewed" per topic, so the model can distinguish freshly-verified mastery from a stale self-assessment.

### 4.6 Persistence: Modal Volume
`state.db` lives on a named `modal.Volume` (`interview-coach-state`), independent of the app's code lifecycle. Redeploys, cold starts, and code/schema changes (additive `CREATE TABLE IF NOT EXISTS`) never touch existing data. A daily backup (30-day retention) runs inside the same cron job that sends the morning question.

Known limitation, accepted for now: backups live inside the same volume they're backing up, so they don't protect against the volume itself being deleted. Off-site backup was considered and deliberately deferred as premature optimization for a personal, low-stakes project — see [Future Work](#8-future-work).

### 4.7 Scheduling: Modal Cron
`daily_reminder_8am` runs on `modal.Cron("0 0 * * *")` — expressed in UTC, since Modal cron schedules are always UTC regardless of local timezone. 00:00 UTC = 08:00 SGT (Singapore has no DST, so this offset is fixed year-round).

## 5. Data Flow: a single `/done` call

1. User sends `/done`.
2. Handler appends a special instruction (asking for a summary *and* a trailing `<STUDY_STATE_JSON>` block) to the conversation.
3. LLM responds with a natural-language summary followed by the JSON block.
4. `extract_and_update_state()` regex-extracts the JSON, strips it from the visible text.
5. `save_parsed_state()` upserts each topic into `study_state` and appends a row to `study_history`.
6. The clean summary (no JSON) is sent to the user.
7. The in-memory conversation for that chat is deleted — SQLite, not conversation history, carries context into the next session.

This is the only code path that writes to the database or asks the model to emit structured output; every other interaction is a plain, unstructured chat turn.

## 6. Security Considerations

- The webhook endpoint is a public URL. A shared secret (`TELEGRAM_WEBHOOK_SECRET`), set via Telegram's `set_webhook(secret_token=...)`, is checked against the `X-Telegram-Bot-Api-Secret-Token` header on every request, rejecting anything that doesn't match.
- Secrets (bot token, Groq key, chat ID, webhook secret) are stored in a Modal `Secret`, never hardcoded. Local development uses a git-ignored `.env` file loaded via `python-dotenv`.
- `get_env_var()` strips whitespace to avoid a class of bugs where a copy-pasted token with a trailing newline silently produces an "invalid token" error.

## 7. Key Design Decisions Log

| Decision | Alternative considered | Why this way |
|---|---|---|
| Webhook over polling | Long-polling with `--detach` | Fits Modal's scale-to-zero model; avoids the 24h timeout / manual-restart cycle |
| Two SQLite tables (snapshot + history) | Single flat table | Prevents unbounded prompt growth while preserving a real history |
| State written only on `/done` | State updated every message | Keeps casual chat fast/cheap; makes state changes deliberate, not incidental |
| Skill files as separate `.md` | Inline prompt strings in Python | Editable prompt/behavior without touching app code |
| Groq (`openai/gpt-oss-120b`) | OpenAI | Free tier suitable for a low-volume personal project |
| Same-volume backups only | Off-site backup / export | Deliberately deferred — premature for current scale and stakes |

## 8. Future Work

Explicitly deferred, not forgotten:
- Off-site backup of `state.db` (e.g. periodic export outside the Modal volume).
- Surfacing `study_history` directly (e.g. a `/progress` command, or a periodic trend summary fed back into the coaching prompt).
- Auto-downgrading stale `mastered` topics after N days without review, rather than only displaying staleness.
- Live-reloading skill files from the Volume instead of baking them into the image at deploy time.

## 9. Summary

The core idea underpinning most of the decisions above: keep the hot path (regular chat) cheap and simple, and push complexity (state writes, structured output, backups) into the one deliberate checkpoint (`/done`) where it's actually needed. Combined with a request-driven (webhook) execution model, this keeps the whole system close to zero marginal cost and zero maintenance between study sessions.