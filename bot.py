import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from difflib import get_close_matches
from pathlib import Path
import modal
from fastapi import Request
from fastapi.responses import JSONResponse
from openai import OpenAI
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# LOGGING & ENVIRONMENT RESOLUTION
# ============================================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Populate os.environ from a local .env file, if one exists. This is
# intentionally NOT gated behind modal.is_local() -- that check does not
# reliably return True at this point in the import lifecycle when the
# module is loaded via `modal run`/`modal deploy`. Calling load_dotenv()
# unconditionally is safe: by default it never overrides a variable that's
# already set, so in the cloud (where Modal's secret already populated
# os.environ before this code runs) it's just a harmless no-op.
try:
    from dotenv import load_dotenv

    _dotenv_loaded = load_dotenv()
    logger.info(f"load_dotenv() ran; found a .env file: {_dotenv_loaded}")
except ImportError:
    logger.warning(
        "python-dotenv not installed; relying on already-exported "
        "environment variables."
    )


def get_modal_secrets():
    """Always resolve to the named Modal secret.

    Secret.from_name(...) is a LAZY reference: Modal's backend looks up the
    secret's current value each time a container actually starts, wherever
    that happens. This is correct for both `modal run` and `modal deploy` --
    unlike branching on modal.is_local() here, which would only reflect
    whatever machine happened to import this module (almost always your
    laptop, since that's where the CLI parses the file), not where the
    function actually executes.
    """
    return [modal.Secret.from_name("interview-coach-secrets")]


state_volume = modal.Volume.from_name("interview-coach-state", create_if_missing=True)

image = (
    modal.Image.debian_slim()
    .pip_install_from_requirements("requirements.txt")
    .pip_install("fastapi[standard]")
    .add_local_file("skills/interview.md", "/root/skills/interview.md")
    .add_local_file("skills/telegram.md", "/root/skills/telegram.md")
)

app = modal.App(name="telegram-interview-coach", image=image)

MODEL = "openai/gpt-oss-120b"

# Dynamic Storage Path: Local folder on Mac vs Modal Volume in Cloud
if modal.is_local():
    VOLUME_DIR = Path("./local_dev_state")
    INTERVIEW_SKILL_PATH = Path("./skills/interview.md")
    TELEGRAM_SKILL_PATH = Path("./skills/telegram.md")
else:
    VOLUME_DIR = Path("/root/skills/state_vol")
    INTERVIEW_SKILL_PATH = Path("/root/skills/interview.md")
    TELEGRAM_SKILL_PATH = Path("/root/skills/telegram.md")

DB_PATH = VOLUME_DIR / "state.db"
BACKUP_DIR = VOLUME_DIR / "backups"

# Canonical topic names the model is instructed to use in skills/interview.md
# (see the "TOPIC TAXONOMY (STRICT)" section). Duplicated here as plain
# Python data -- not parsed out of the .md file -- because normalize_topic()
# needs to run difflib matching against it. The two lists change rarely
# enough that manual sync between them is an acceptable trade-off for a
# personal project; if you edit one, edit the other.
TOPIC_TAXONOMY = [
    # Kafka & Messaging
    "Kafka Producers & Acknowledgements",
    "Kafka Consumers & Consumer Groups",
    "Kafka Partitioning & Ordering",
    "Kafka Offsets & Delivery Semantics",
    "Kafka Rebalancing",
    "Kafka Replication & Leader/Follower",
    "Kafka Failure Scenarios & Scaling",
    # Distributed Systems
    "CAP Theorem & Consistency Models",
    "Consensus (Raft/Paxos)",
    "Replication & Partitioning",
    "Distributed Transactions",
    "Failure Detection & Fault Tolerance",
    "Load Balancing & Service Discovery",
    # Concurrency
    "Threads & Processes",
    "Locks & Synchronization",
    "Deadlocks & Race Conditions",
    "Concurrent Data Structures",
    "Async / Non-blocking IO",
    # Databases
    "Indexing",
    "Transactions & ACID",
    "Isolation Levels",
    "Query Optimization",
    "Sharding & Replication",
    "SQL vs NoSQL Tradeoffs",
    # Java / JVM
    "Garbage Collection",
    "Memory Model",
    "JVM Internals",
    # Networking & OS
    "TCP/IP Fundamentals",
    "HTTP / REST",
    "OS Scheduling & Processes",
    "OS Memory Management",
    # Low-Level Design
    "Design Patterns",
    "API Design",
    "Class / Object Design",
    # Real-world Backend Engineering
    "Caching Strategies",
    "Rate Limiting",
    "Observability & Monitoring",
    "Idempotency & Retries",
    # DSA
    "Arrays & Strings",
    "Trees & Graphs",
    "Dynamic Programming",
    # System Design
    "Scalability Fundamentals",
    "System Design Case Studies",
]


def normalize_topic(proposed_topic: str, threshold: float = 0.7) -> str:
    """Safety net behind the interview.md TOPIC TAXONOMY rule. skills/interview.md
    instructs the model to only ever emit topic names from that fixed list, but
    LLM output isn't guaranteed -- this catches drift in code so a slight
    rephrasing (e.g. "Kafka Partition Rebalancing" instead of the canonical
    "Kafka Rebalancing") gets snapped back to the existing row instead of
    silently fragmenting study_state into near-duplicate topics.

    If nothing in the taxonomy is close enough, the model's proposed name is
    kept as-is -- this covers the legitimate "nothing on the list fits"
    case interview.md tells the model to flag explicitly in its summary.
    """
    if not proposed_topic:
        return proposed_topic
    matches = get_close_matches(proposed_topic, TOPIC_TAXONOMY, n=1, cutoff=threshold)
    if matches and matches[0] != proposed_topic:
        logger.info(f"Normalized topic '{proposed_topic}' -> '{matches[0]}'")
        return matches[0]
    return proposed_topic


def _read_skill_file(path: Path, label: str) -> str:
    """Reads a skill markdown file for inclusion in the system prompt.
    Returns an empty string (and logs a warning) if missing, so a missing
    skill file never crashes the bot -- it just runs without that context.
    """
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        logger.warning(f"{label} not found at {path}; continuing without it.")
        return ""
    except Exception as e:
        logger.error(f"Failed to read {label} at {path}: {e}")
        return ""


def load_interview_skill() -> str:
    """Reads skills/interview.md, the interview-coaching rules."""
    return _read_skill_file(INTERVIEW_SKILL_PATH, "interview.md")


def load_telegram_skill() -> str:
    """Reads skills/telegram.md, the Telegram output-formatting rules."""
    return _read_skill_file(TELEGRAM_SKILL_PATH, "telegram.md")


def get_env_var(var_name: str) -> str:
    """Reads an environment variable from the process environment.

    Locally this comes from your shell or the .env file loaded above.
    In the cloud this comes from whatever Secret Modal injected when the
    container started. Values are stripped to avoid trailing-newline /
    whitespace issues from copy-pasted tokens causing silent auth failures.
    """
    val = os.environ.get(var_name, "").strip()
    if not val:
        logger.error(f"Missing required environment variable: {var_name}")
    return val


def require_env_var(var_name: str) -> str:
    """Like get_env_var, but raises loudly instead of returning "" ."""
    val = get_env_var(var_name)
    if not val:
        raise RuntimeError(
            f"{var_name} is empty. Locally: check your .env file. "
            f"In the cloud: check `modal secret list` / the "
            f"'interview-coach-secrets' secret in your Modal dashboard."
        )
    return val

# ============================================================
# SQLITE DATABASE & BACKUP MANAGEMENT
# ============================================================
def commit_volume_if_cloud():
    """Safely commits changes to Modal Volume when running in the cloud."""
    if not modal.is_local():
        try:
            state_volume.commit()
            logger.info("Committed changes to Modal Volume.")
        except Exception as e:
            logger.error(f"Failed to commit to Modal Volume: {e}")


def init_db():
    """Initializes the SQLite database.

    Two tables, two different jobs:
    - study_state: current snapshot only. One row per topic, overwritten on
      every /done. This is the compact table sent to the AI as prompt
      context -- it should stay small.
    - study_history: append-only log. A new row every time /done records an
      assessment for a topic, timestamped, never overwritten. This is your
      actual progress-over-time record (e.g. "how has my Kafka understanding
      evolved over the last month") -- it is NOT sent to the AI on every
      message, so it can grow indefinitely without bloating prompts.
    - conversation_state: persists each chat's in-progress message history
      to SQLite instead of an in-memory dict. The webhook is stateless
      (each request may land on a fresh container), so an in-memory dict
      would silently lose an in-progress conversation whenever Modal
      recycles the container between messages. Keyed by chat_id.
    """
    VOLUME_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS study_state (
            topic TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            notes TEXT,
            priority INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS study_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            status TEXT NOT NULL,
            notes TEXT,
            priority INTEGER DEFAULT 1,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_state (
            chat_id INTEGER PRIMARY KEY,
            messages TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()
    commit_volume_if_cloud()


def backup_sqlite_db(retention_days: int = 30):
    """Creates a timestamped backup copy of state.db and removes backups older than retention_days."""
    if not DB_PATH.exists():
        logger.warning("No database found to backup.")
        return

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    today = datetime.now().strftime("%Y-%m-%d")
    backup_file = BACKUP_DIR / f"state_backup_{today}.db"

    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(backup_file)

        with dst:
            src.backup(dst)

        src.close()
        dst.close()
        logger.info(f"Successfully created database backup at {backup_file}")
    except Exception as e:
        logger.error(f"Failed to create database backup: {e}")
        return

    cutoff_date = datetime.now() - timedelta(days=retention_days)
    for old_backup in BACKUP_DIR.glob("state_backup_*.db"):
        try:
            date_str = old_backup.stem.replace("state_backup_", "")
            file_date = datetime.strptime(date_str, "%Y-%m-%d")
            if file_date < cutoff_date:
                old_backup.unlink()
                logger.info(f"Pruned old backup file: {old_backup.name}")
        except ValueError:
            continue

    commit_volume_if_cloud()


# How many non-mastered topics to include in the AI's prompt context. Keeps
# the prompt small and focused even after months of accumulated topics.
DB_SUMMARY_LIMIT = 10


def get_db_summary() -> str:
    """Fetches a compact, prioritized summary of study_state for prompt
    context. Deliberately excludes 'mastered' topics from the detailed list
    (they'd just be dead weight in every future prompt) and caps the count,
    so this stays small no matter how many topics accumulate over weeks.
    Also surfaces how many days it's been since each topic was last
    reviewed, so the AI can tell "mastered last week" from "mastered two
    months ago and possibly rusty by now" instead of treating both as
    equally current.
    """
    if not DB_PATH.exists():
        init_db()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT topic, status, notes, priority,
               CAST(julianday('now') - julianday(updated_at) AS INTEGER) AS days_ago
        FROM study_state
        WHERE status != 'mastered'
        ORDER BY priority DESC, updated_at ASC
        LIMIT ?
        """,
        (DB_SUMMARY_LIMIT,),
    )
    active_rows = cursor.fetchall()

    cursor.execute("SELECT COUNT(*) FROM study_state WHERE status = 'mastered'")
    mastered_count = cursor.fetchone()[0]
    conn.close()

    if not active_rows and mastered_count == 0:
        return "No study state topics recorded yet in the database."

    summary = ["Current Study State (SQLite):"]
    for topic, status, notes, priority, days_ago in active_rows:
        when = "today" if days_ago == 0 else f"{days_ago}d ago"
        summary.append(f"- **{topic}** | Status: {status} | Priority: {priority} | Last reviewed: {when} | Notes: {notes}")

    if mastered_count:
        summary.append(f"({mastered_count} other topic(s) already marked mastered, omitted here for brevity.)")

    return "\n".join(summary)


def get_progress_summary() -> str:
    """Builds a /progress report for the user directly (not for the AI
    prompt) -- a plain grouped list of each topic and subtopic's current
    learning status. No tables, no counts, no trend clutter -- just what's
    being learned and where it stands. Formatted per telegram.md's HTML
    rules since this is sent to Telegram as-is, not passed through the model.

    Topics are grouped by their first word as a category (e.g. "Kafka
    Rebalancing" -> category "Kafka", subtopic "Rebalancing"), since that's
    how the AI has been naming topics. A single-word topic (e.g.
    "Concurrency") is shown under itself with no separate subtopic label.
    """
    if not DB_PATH.exists():
        init_db()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT topic, status FROM study_state ORDER BY topic ASC")
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return "No study progress recorded yet -- finish a session with /done to start tracking."

    groups: dict[str, list[tuple[str, str]]] = {}
    for topic, status in rows:
        parts = topic.split(" ", 1)
        if len(parts) == 2:
            category, subtopic = parts
        else:
            category, subtopic = topic, None
        groups.setdefault(category, []).append((subtopic, status))

    lines = ["<b>Study Progress</b>", ""]
    for category in sorted(groups.keys()):
        lines.append(f"<b>{category}</b>")
        for subtopic, status in groups[category]:
            if subtopic:
                lines.append(f"• {subtopic}: {status}")
            else:
                lines.append(f"• {status}")
        lines.append("")

    return "\n".join(lines).strip()


def save_parsed_state(state_json_str: str):
    """Parses JSON output from the AI, upserts the CURRENT snapshot into
    study_state, and additionally appends a permanent record to
    study_history so the assessment for this date is never lost even after
    study_state gets overwritten by a later session.

    Each topic name is passed through normalize_topic() first -- a safety
    net behind the TOPIC TAXONOMY rule in skills/interview.md -- so a model
    rephrasing of a canonical topic gets snapped back to the existing row
    instead of creating a near-duplicate.
    """
    try:
        items = json.loads(state_json_str)
        if not isinstance(items, list):
            items = [items]

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        for item in items:
            topic = normalize_topic(item.get("topic", ""))
            status = item.get("status", "in_progress")
            notes = item.get("notes", "")
            priority = item.get("priority", 1)

            if not topic:
                logger.warning(f"Skipping study-state item with no topic: {item}")
                continue

            cursor.execute(
                """
                INSERT INTO study_state (topic, status, notes, priority, updated_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(topic) DO UPDATE SET
                    status=excluded.status,
                    notes=excluded.notes,
                    priority=excluded.priority,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (topic, status, notes, priority),
            )
            cursor.execute(
                """
                INSERT INTO study_history (topic, status, notes, priority, recorded_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (topic, status, notes, priority),
            )

        conn.commit()
        conn.close()
        commit_volume_if_cloud()
        logger.info("Successfully updated SQLite state (snapshot + history).")
    except Exception as e:
        logger.error(f"Failed to save state to SQLite: {e}")


def load_conversation(chat_id: int):
    """Loads a chat's in-progress message history from SQLite, or None if
    there isn't one. Replaces the old in-memory `conversations` dict, which
    would silently lose an in-progress interview whenever Modal recycled
    the webhook's container between messages.
    """
    if not DB_PATH.exists():
        init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT messages FROM conversation_state WHERE chat_id = ?", (chat_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception as e:
        logger.error(f"Failed to parse stored conversation for chat {chat_id}: {e}")
        return None


def save_conversation(chat_id: int, messages: list):
    """Persists a chat's message history to SQLite after every turn."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO conversation_state (chat_id, messages, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(chat_id) DO UPDATE SET
                messages=excluded.messages,
                updated_at=CURRENT_TIMESTAMP
            """,
            (chat_id, json.dumps(messages)),
        )
        conn.commit()
        conn.close()
        commit_volume_if_cloud()
    except Exception as e:
        logger.error(f"Failed to save conversation for chat {chat_id}: {e}")


def delete_conversation(chat_id: int):
    """Clears a chat's message history, e.g. after /done completes."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM conversation_state WHERE chat_id = ?", (chat_id,))
        conn.commit()
        conn.close()
        commit_volume_if_cloud()
    except Exception as e:
        logger.error(f"Failed to delete conversation for chat {chat_id}: {e}")


def safe_llm_call(client: OpenAI, messages: list, temperature: float = 0.5):
    """Calls the LLM and returns (content, error_message). On any failure
    (timeout, rate limit, API error, etc.) content is None and error_message
    describes what went wrong -- callers use this to send the user a clear
    "something went wrong" reply instead of the request silently failing
    with no response at all.
    """
    try:
        response = client.chat.completions.create(model=MODEL, messages=messages, temperature=temperature)
        return response.choices[0].message.content, None
    except Exception as e:
        logger.error(f"LLM call failed: {e}")
        return None, str(e)


def extract_and_update_state(text: str) -> str:
    """Extracts a <STUDY_STATE_JSON> block from the AI's reply, writes it to
    SQLite (so the NEXT prompt to the AI has fresh context), and strips the
    tag out of the text before it's sent to Telegram.
    """
    match = re.search(r"<STUDY_STATE_JSON>(.*?)</STUDY_STATE_JSON>", text, re.DOTALL)
    if match:
        json_data = match.group(1).strip()
        save_parsed_state(json_data)
        text = re.sub(r"<STUDY_STATE_JSON>.*?</STUDY_STATE_JSON>", "", text, flags=re.DOTALL).strip()
    return text


def build_system_prompt() -> str:
    """Base system prompt used for /start, regular messages, and the daily
    8 AM reminder. Includes the interview coaching rules, Telegram
    formatting rules, and the current SQLite study-state summary as
    read-only context -- but does NOT ask the AI to emit JSON. State is
    only written on /done (see build_done_prompt_addition).
    """
    interview_context = load_interview_skill()
    telegram_context = load_telegram_skill()
    db_context = get_db_summary()

    interview_section = interview_context or "You are an elite technical interview coach."
    telegram_section = f"\n{telegram_context}\n" if telegram_context else ""

    return f"""{interview_section}
{telegram_section}
{db_context}
"""


def build_done_prompt_addition() -> str:
    """Extra instruction appended only when the user issues /done. Asks for
    a session summary AND a <STUDY_STATE_JSON> block, which is parsed and
    saved to SQLite before the summary is sent back.

    Topic naming is primarily constrained by the TOPIC TAXONOMY section in
    skills/interview.md (already present in the system prompt) -- this
    addition just reinforces that instruction right before the model emits
    JSON. normalize_topic() in save_parsed_state() is the code-level backup
    in case the model drifts anyway.
    """
    return """The user issued /done. Provide a concise summary of this session's
progress, following the interview style and Telegram formatting rules above.
Then, at the very end of your response, output JSON enclosed strictly in
<STUDY_STATE_JSON> tags summarizing the status of each topic covered this
session, so it can be saved to SQLite for next time.

Use ONLY topic names from the TOPIC TAXONOMY list defined earlier in your
instructions -- do not invent new phrasing for a topic that's already on
that list. Only use a name outside the list if nothing on it is even
approximately related to what was covered, and say so explicitly in your
summary text.

Example Tag Format:
<STUDY_STATE_JSON>
[
  {"topic": "Kafka Rebalancing", "status": "needs_review", "notes": "Confused rebalancing with partition assignment", "priority": 3}
]
</STUDY_STATE_JSON>
"""


def format_for_telegram(text: str) -> str:
    """Passes the AI's text through unchanged for Telegram's HTML parse mode.

    We intentionally do NOT escape <, >, & here. telegram.md instructs the
    AI to use a small whitelist of real HTML tags (<b>, <i>, <code>, <pre>)
    for formatting, and those need to reach Telegram unescaped so they
    actually render as bold/italic/etc. instead of showing up as literal
    text. The skill file is the guardrail on which tags get used, not this
    function.
    """
    return text


async def safe_reply(message, text: str, prefix: str = ""):
    """Sends a reply via Telegram's HTML parse mode; if the model emitted
    malformed or non-whitelisted HTML (unbalanced tags, a tag outside
    telegram.md's whitelist, etc.), Telegram rejects the parse and
    reply_text() raises. Without this wrapper that exception propagates
    out of the handler and the user gets silence -- no reply at all, and
    no visible error. Falling back to a plain-text send guarantees the
    user always gets *something* back.

    `prefix` is plain text prepended before formatting (e.g. an emoji
    header) -- kept separate from `text` so it's never affected by
    whatever went wrong with the model's HTML.
    """
    full_text = f"{prefix}{format_for_telegram(text)}"
    try:
        await message.reply_text(full_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        logger.warning(f"HTML parse failed, falling back to plain text: {e}")
        await message.reply_text(full_text, parse_mode=None)

# ============================================================
# TELEGRAM HANDLERS
# ============================================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    chat_id = update.effective_chat.id
    logger.info(f"Session started for chat_id: {chat_id}")

    client = OpenAI(api_key=require_env_var("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1", timeout=30.0)
    messages = [{"role": "system", "content": build_system_prompt()}]
    messages.append(
        {"role": "user", "content": "Start the interview now with highest priority question from SQLite state."}
    )

    raw_content, error = safe_llm_call(client, messages)
    if error:
        await message.reply_text("Sorry, I hit an error reaching the model. Please try again in a moment.")
        return

    messages.append({"role": "assistant", "content": raw_content})
    save_conversation(chat_id, messages)

    await safe_reply(message, raw_content)


async def done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    chat_id = update.effective_chat.id
    messages = load_conversation(chat_id)
    if messages is None:
        await message.reply_text("No active session.")
        return

    client = OpenAI(api_key=require_env_var("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1", timeout=30.0)
    messages.append({"role": "user", "content": build_done_prompt_addition()})

    raw_content, error = safe_llm_call(client, messages)
    if error:
        await message.reply_text("Sorry, I hit an error reaching the model while wrapping up. Your progress up to now is still saved -- please try /done again in a moment.")
        return

    # Only /done parses and writes the study-state JSON to SQLite.
    clean_message = extract_and_update_state(raw_content)

    await safe_reply(message, clean_message, prefix="🏁 Session Complete!\n\n")
    # Drop the whole conversation so tomorrow's 8 AM reminder (and the next
    # /start) begins fresh instead of an ever-growing message history --
    # the SQLite state we just saved is what carries context forward.
    delete_conversation(chat_id)


async def progress(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /progress -- shows a snapshot of study_state (all topics,
    including mastered ones, unlike the AI's prompt context) plus a simple
    trend per topic derived from study_history. This never touches the LLM
    or costs any tokens -- it's a pure SQLite read.
    """
    message = update.effective_message
    if not message:
        return

    summary = get_progress_summary()
    await safe_reply(message, summary)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.text:
        return

    chat_id = update.effective_chat.id
    client = OpenAI(api_key=require_env_var("GROQ_API_KEY"), base_url="https://api.groq.com/openai/v1", timeout=30.0)

    messages = load_conversation(chat_id)
    if messages is None:
        messages = [{"role": "system", "content": build_system_prompt()}]
    else:
        # Refresh the system prompt in place, so it always reflects the
        # latest SQLite study-state even mid-conversation.
        messages[0] = {"role": "system", "content": build_system_prompt()}

    messages.append({"role": "user", "content": message.text})

    raw_content, error = safe_llm_call(client, messages)
    if error:
        await message.reply_text("Sorry, I hit an error reaching the model. Please try again in a moment.")
        return

    messages.append({"role": "assistant", "content": raw_content})
    save_conversation(chat_id, messages)

    await safe_reply(message, raw_content)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception handling update:", exc_info=context.error)


def build_telegram_application(telegram_token: str) -> Application:
    """Builds a python-telegram-bot Application with all handlers attached.
    Shared by both the local-dev polling path (main()) and the production
    webhook path (telegram_webhook), so handler wiring only lives in one place.
    """
    application = Application.builder().token(telegram_token).connect_timeout(30.0).read_timeout(30.0).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("done", done))
    application.add_handler(CommandHandler("progress", progress))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_error_handler(error_handler)
    return application


def main():
    """Local-only dev/testing entrypoint using long-polling. NOT used in
    production -- the deployed bot uses the telegram_webhook endpoint below,
    which doesn't need a permanently-running process.
    """
    init_db()
    telegram_token = require_env_var("TELEGRAM_BOT_TOKEN")
    logger.info("Starting Telegram bot worker (local polling mode)...")

    # Guarantee a usable event loop for this thread. If something earlier
    # in this process (e.g. daily_reminder_8am's asyncio.run() call during
    # test_local()) already ran and cleared the thread's event loop,
    # ApplicationBuilder's internal asyncio.Queue() construction would
    # otherwise fail with "There is no current event loop".
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    application = build_telegram_application(telegram_token)
    application.run_polling()

# ============================================================
# PRODUCTION WEBHOOK ENDPOINT
# ============================================================
# Telegram calls this URL directly whenever a user sends a message -- no
# always-on process needed, and no 24-hour container timeout to worry about.
# Modal spins up a container on demand for each incoming update and can
# scale down to zero between messages.
#
# This is a class (not a plain @app.function) so the python-telegram-bot
# Application is built ONCE per container lifetime via @modal.enter(),
# instead of being rebuilt from scratch on every single incoming message.
# While a container stays warm between consecutive messages, those messages
# reuse the same already-initialized Application -- avoiding real per-message
# overhead (network setup, internal PTB initialization) that a fresh
# build_telegram_application() + initialize() + shutdown() cycle added on
# every request.
@app.cls(
    secrets=get_modal_secrets(),
    volumes={"/root/skills/state_vol": state_volume},
    scaledown_window=300,
)
class TelegramWebhookHandler:
    @modal.enter()
    async def setup(self):
        init_db()
        telegram_token = require_env_var("TELEGRAM_BOT_TOKEN")
        self.application = build_telegram_application(telegram_token)
        await self.application.initialize()

    @modal.exit()
    async def teardown(self):
        await self.application.shutdown()

    @modal.fastapi_endpoint(method="POST")
    async def webhook(self, payload: dict, request: Request):
        # Verify the request actually came from Telegram, not some random
        # caller who guessed the URL. Telegram sends this header on every
        # webhook call when a secret_token was set via set_webhook (see
        # register_webhook()).
        expected_secret = get_env_var("TELEGRAM_WEBHOOK_SECRET")
        received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if expected_secret and received_secret != expected_secret:
            logger.warning("Rejected webhook call with invalid/missing secret token.")
            return JSONResponse(status_code=401, content={"ok": False, "error": "unauthorized"})

        update = Update.de_json(payload, self.application.bot)
        await self.application.process_update(update)
        return {"ok": True}


def _get_deployed_webhook_url() -> str:
    """Looks up the currently-DEPLOYED webhook URL via modal.Cls.from_name(),
    which references the already-deployed class -- NOT the ephemeral app
    that a plain `modal run` would otherwise spin up (see the warning in
    register_webhook's docstring below for why that distinction matters).
    """
    webhook_cls = modal.Cls.from_name("telegram-interview-coach", "TelegramWebhookHandler")
    return webhook_cls().webhook.get_web_url()


@app.local_entrypoint()
def register_webhook():
    """Run once after `modal deploy` to point Telegram at the deployed
    webhook URL: `modal run bot.py::register_webhook`

    IMPORTANT: this looks up the URL via modal.Cls.from_name(), which
    references the already-DEPLOYED class. It deliberately does NOT
    reference the local `TelegramWebhookHandler` object directly -- doing
    that from within `modal run` spins up a separate, temporary ephemeral
    app (URL ending in "-dev") that gets torn down the moment this command
    exits, which would register a URL that stops working immediately after.
    """
    url = _get_deployed_webhook_url()
    if not url:
        print("Could not resolve the webhook URL -- did you run `modal deploy bot.py` first?")
        return
    if "-dev" in url:
        print(f"WARNING: resolved URL looks like an ephemeral dev URL: {url}")
        print("This should not happen when using Cls.from_name() against a deployed app -- double check `modal deploy` succeeded.")

    secret = get_env_var("TELEGRAM_WEBHOOK_SECRET")
    if not secret:
        print(
            "WARNING: TELEGRAM_WEBHOOK_SECRET is not set. The webhook will still "
            "work, but anyone who finds the URL could send fake updates to your "
            "bot. Set TELEGRAM_WEBHOOK_SECRET in your .env and Modal secret, then "
            "re-run this command."
        )

    from telegram import Bot

    async def _register():
        bot = Bot(token=require_env_var("TELEGRAM_BOT_TOKEN"))
        await bot.set_webhook(url=url, secret_token=secret or None)
        info = await bot.get_webhook_info()
        print(f"Webhook registered: {info.url}")
        print(f"Pending update count: {info.pending_update_count}")

    asyncio.run(_register())


@app.local_entrypoint()
def check_webhook():
    """Read-only diagnostic: shows Telegram's current view of the webhook
    WITHOUT re-registering it. Crucially surfaces last_error_message, which
    tells you exactly why Telegram couldn't deliver updates (e.g. a 401 from
    a bad secret token, a 500 from an exception in your handler, a timeout,
    etc). Run: `modal run bot.py::check_webhook`

    Also prints the currently-DEPLOYED URL (via Cls.from_name, not the
    ephemeral one this `modal run` invocation would otherwise create) so you
    can visually confirm it matches what Telegram has on file.
    """
    deployed_url = _get_deployed_webhook_url()
    print(f"Currently deployed URL: {deployed_url}")

    from telegram import Bot

    async def _check():
        bot = Bot(token=require_env_var("TELEGRAM_BOT_TOKEN"))
        info = await bot.get_webhook_info()
        print(f"Telegram has registered: {info.url or '(none set)'}")
        print(f"URLs match: {info.url == deployed_url}")
        print(f"Pending update count: {info.pending_update_count}")
        print(f"Last error date: {info.last_error_date}")
        print(f"Last error message: {info.last_error_message}")
        print(f"Max connections: {info.max_connections}")
        print(f"Has custom certificate: {info.has_custom_certificate}")

    asyncio.run(_check())

# ============================================================
# MODAL CLOUD CRON SCHEDULER & WORKER
# ============================================================
@app.cls(
    secrets=get_modal_secrets(),
    volumes={"/root/skills/state_vol": state_volume},
    timeout=86400,
    scaledown_window=3600,
)
class TelegramBot:
    @modal.enter()
    def start_up(self):
        init_db()

    @modal.method()
    def run(self):
        main()


# Scheduled 8:00 AM Daily Reminder & Backup Trigger
@app.function(
    secrets=get_modal_secrets(),
    volumes={"/root/skills/state_vol": state_volume},
    # 8:00 AM SGT = 00:00 UTC (Singapore is UTC+8, no daylight saving).
    # modal.Cron schedules always run in UTC, not local time.
    schedule=modal.Cron("0 0 * * *"),
)
def daily_reminder_8am():
    """Performs daily SQLite backup and pushes 8:00 AM interview prompt to Telegram."""
    logger.info("Executing 8:00 AM daily job (Backup + Reminder)...")
    init_db()

    backup_sqlite_db(retention_days=30)

    chat_id = require_env_var("TELEGRAM_CHAT_ID")
    telegram_token = require_env_var("TELEGRAM_BOT_TOKEN")
    groq_api_key = require_env_var("GROQ_API_KEY")

    client = OpenAI(api_key=groq_api_key, base_url="https://api.groq.com/openai/v1", timeout=30.0)
    prompt = f"{build_system_prompt()}\n\nGenerate a single concise 8 AM interview warm-up question based on the highest priority topic."

    question, error = safe_llm_call(client, [{"role": "user", "content": prompt}])
    if error:
        logger.error(f"Skipping 8 AM push -- LLM call failed: {error}")
        return

    bot_app = Application.builder().token(telegram_token).build()
    asyncio.run(
        bot_app.bot.send_message(
            chat_id=chat_id,
            text=f"☀️ <b>8:00 AM Interview Warm-Up</b>\n\n{format_for_telegram(question)}",
            parse_mode=ParseMode.HTML,
        )
    )
    logger.info("Sent 8:00 AM daily question successfully.")


# ============================================================
# DATABASE RESET (manual, run on demand)
# ============================================================
@app.function(
    secrets=get_modal_secrets(),
    volumes={"/root/skills/state_vol": state_volume},
)
def reset_db(confirm: bool = False):
    """Wipes state.db (and its backups) from the Modal Volume and creates a
    fresh, empty database in its place. This is a REMOTE Modal function
    (not a local_entrypoint) so it runs in the same container context that
    owns the volume mount -- guaranteeing the delete is committed properly
    rather than operating on a local path that isn't actually the volume.

    Run with: `modal run bot.py::reset_db --confirm`

    Without --confirm, this only reports what it WOULD delete and does
    nothing -- a safety check against wiping progress by accident.
    """
    if not confirm:
        existed = DB_PATH.exists()
        print(f"DRY RUN -- state.db exists: {existed}. No changes made.")
        print("Re-run with --confirm to actually delete it, e.g.:")
        print("  modal run bot.py::reset_db --confirm")
        return

    deleted_any = False
    if DB_PATH.exists():
        DB_PATH.unlink()
        deleted_any = True
        print(f"Deleted {DB_PATH}")

    if BACKUP_DIR.exists():
        for backup_file in BACKUP_DIR.glob("state_backup_*.db"):
            backup_file.unlink()
            deleted_any = True
            print(f"Deleted {backup_file}")

    if not deleted_any:
        print("Nothing to delete -- no existing state.db or backups found.")

    init_db()
    commit_volume_if_cloud()
    print("Created a fresh, empty state.db with all tables initialized.")

# ============================================================
# LOCAL TESTING ENTRYPOINT
# ============================================================
@app.local_entrypoint()
def test_local():
    print("=== 1. INITIALIZING LOCAL DB ===")
    init_db()

    print("\n=== 2. TESTING 8 AM DAILY REMINDER & BACKUP ===")
    daily_reminder_8am.local()

    print("\n=== 3. CHECKING DB SUMMARY ===")
    print(get_db_summary())

    print("\n=== 4. STARTING TELEGRAM BOT LOCAL WORKER ===")
    print("Press Ctrl+C to stop testing.")
    main()