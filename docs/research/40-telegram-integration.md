# Research: Telegram Integration for Progress Notifications and Approval Gates

**Issue:** #40
**Milestone:** v2
**Feature:** feat:telegram
**Status:** Complete
**Date:** 2026-03-02

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Notification Event Taxonomy](#notification-event-taxonomy)
3. [Async Approval Gate Design](#async-approval-gate-design)
4. [Reuse vs. New Bot Tradeoff](#reuse-vs-new-bot-tradeoff)
5. [Rate Limit Analysis](#rate-limit-analysis)
6. [Security Requirements](#security-requirements)
7. [ntfy and Slack Comparison](#ntfy-and-slack-comparison)
8. [Recommended Implementation](#recommended-implementation)
9. [Follow-Up Research Recommendations](#follow-up-research-recommendations)
10. [Sources](#sources)

---

## Executive Summary

Telegram is a viable and recommended notification + approval-gate channel for conductor.
The Telegram Bot API supports inline keyboard messages with callback queries, which
can serve as asynchronous approval gates — conductor sends a message with [Yes]/[No]
buttons and polls for a callback query before proceeding with the blocked action.

**Key findings:**

1. **Notification events**: 8 conductor events warrant Telegram notifications, ranked by
   urgency. Only 3 require immediate attention (errors, orphaned work, usage at 80%).
   The remaining 5 are informational. [DOCUMENTED]

2. **Approval gate design**: Inline keyboard + callback query polling is the correct
   mechanism for async approval. Conductor sends a message, then polls `getUpdates` (or
   uses webhook) for a callback query. A 4-hour timeout for non-blocking confirmations;
   30-minute timeout for session-blocking decisions. [DOCUMENTED]

3. **Reuse vs. new bot**: A dedicated conductor bot is strongly recommended over reusing
   the breadministrator-toolkit bot. Shared bots create routing ambiguity, shared state,
   and deployment coupling. A new bot is a 5-minute setup via @BotFather. [INFERRED]

4. **Rate limits**: Telegram allows 30 messages/second across different chats, 1
   message/second to the same chat. For conductor's use case (single developer, one chat
   ID), the 1 msg/sec per-chat limit is the binding constraint. Concurrent agent
   completions must be queued, not fired simultaneously. [DOCUMENTED]

5. **Security**: Chat ID filtering (whitelist) and bot token protection are mandatory.
   Bot token stored as env var `TELEGRAM_BOT_TOKEN`; allowed chat IDs stored in config.
   No additional authentication is needed for a single-user deployment. [INFERRED]

6. **ntfy comparison**: ntfy is simpler for fire-and-forget notifications but does not
   support interactive approval gates (no inline buttons). Telegram is required for
   approval gate functionality. For notification-only use cases, ntfy is equally valid
   and easier to self-host. [DOCUMENTED]

---

## Notification Event Taxonomy

### Priority Classification

| Priority | Definition |
|----------|-----------|
| P0 — Blocking | Conductor is paused, waiting for human input |
| P1 — Urgent | Session error requiring immediate attention |
| P2 — High | Action completed that may need monitoring |
| P3 — Info | Status update, no action required |

### Event Table

| Event | Priority | Approval Required? | Message Type |
|-------|----------|--------------------|-------------|
| Orphaned work detected (in-progress with no PR) | P0 | Yes — Auto-clean? | Inline keyboard |
| Usage at 80% of window | P1 | No | Text + alert |
| Agent error / abandon | P1 | No | Text + alert |
| CI check failed on PR | P1 | No (log, continue) | Text |
| Agent dispatched | P3 | No | Text (can be silent) |
| PR created | P3 | No | Text |
| PR merged | P3 | No | Text |
| Session complete summary | P3 | No | Text (detailed) |

**Implementation note:** P3 events can be batched (send one summary message at session
end rather than per-event messages) to avoid chat spam during long conductor runs. P0/P1
events should always be sent immediately.

---

## Async Approval Gate Design

### Mechanism

Telegram's inline keyboard + callback query flow:

1. Conductor sends a message with `InlineKeyboardMarkup` (buttons: [Yes] [No])
2. User taps a button in Telegram — this triggers a `callback_query` update
3. Conductor polls `getUpdates` (long-polling) or receives via webhook for the callback
4. Conductor reads `callback_query.data` to determine the user's choice
5. Conductor calls `answerCallbackQuery` to dismiss the button loading state
6. Conductor proceeds or aborts based on the choice

### Implementation Pattern (Python)

```python
import asyncio
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TimedOut

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_CHAT_ID = int(os.environ["TELEGRAM_CHAT_ID"])

async def ask_approval(
    bot: Bot,
    question: str,
    timeout_seconds: int = 3600,  # 1 hour default
) -> bool:
    """Send an approval request and wait for user response."""
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Yes", callback_data="approve"),
            InlineKeyboardButton("No", callback_data="reject"),
        ]
    ])

    msg = await bot.send_message(
        chat_id=ALLOWED_CHAT_ID,
        text=question,
        reply_markup=keyboard,
    )

    # Poll for callback query
    deadline = asyncio.get_event_loop().time() + timeout_seconds
    offset = None

    while asyncio.get_event_loop().time() < deadline:
        updates = await bot.get_updates(
            offset=offset,
            timeout=30,
            allowed_updates=["callback_query"],
        )
        for update in updates:
            offset = update.update_id + 1
            if (
                update.callback_query
                and update.callback_query.message.message_id == msg.message_id
                and update.callback_query.message.chat.id == ALLOWED_CHAT_ID
            ):
                choice = update.callback_query.data
                await bot.answer_callback_query(update.callback_query.id)
                await bot.edit_message_text(
                    chat_id=ALLOWED_CHAT_ID,
                    message_id=msg.message_id,
                    text=f"{question}\n\n→ {'Approved' if choice == 'approve' else 'Rejected'}",
                )
                return choice == "approve"

    # Timeout — treat as rejection (safe default)
    await bot.edit_message_text(
        chat_id=ALLOWED_CHAT_ID,
        message_id=msg.message_id,
        text=f"{question}\n\n→ Timed out (treating as: No)",
    )
    return False
```

### Timeout Policy

| Decision type | Timeout | On timeout |
|---------------|---------|------------|
| Orphaned work auto-clean | 30 minutes | Skip cleanup, continue |
| Session configuration conflict | 4 hours | Abort session |
| PR merge approval (if gated) | 8 hours | Do not merge |

The timeout default should be **conservative** (reject/skip) rather than permissive
(approve). A timed-out approval should never result in a destructive action.

### Long-Polling vs. Webhook

For conductor's use case (single-instance, not always running), **long-polling is
preferred over webhooks**:
- Long-polling works without a public URL or HTTPS cert
- No persistent server needed — conductor polls when it needs an answer
- Works from developer laptops, CI runners, or any outbound-internet-capable machine

Webhooks are appropriate for always-on bots with public endpoints (not conductor's
current deployment model).

---

## Reuse vs. New Bot Tradeoff

### Option A: Reuse breadministrator-toolkit Bot

**Pros:**
- No new bot token to manage
- Existing chat ID and user trust established

**Cons:**
- Routing collision: How does the existing bot distinguish conductor messages from
  breadministrator messages? Requires custom message prefix or separate topic in
  a forum group.
- Deployment coupling: Conductor deployment depends on breadministrator-toolkit bot
  being operational.
- Shared rate limits: Both systems share the 1 msg/sec per-chat limit.
- Callback query routing: Both systems need to register callback query handlers —
  collision risk if both use similar callback data strings.

### Option B: Dedicated Conductor Bot

**Pros:**
- Complete isolation of routing, rate limits, and state
- Independent deployment — conductor bot fails separately from breadministrator bot
- 5-minute setup via @BotFather
- Can be given conductor-specific commands (`/status`, `/resume`, `/abort`)

**Cons:**
- One additional bot token to manage
- User must add the new bot to their chat

**Recommendation: Dedicated conductor bot (Option B).**

The complexity of callback routing and shared rate limits makes bot reuse more
trouble than it's worth. A dedicated bot is simpler to implement and operate.

---

## Rate Limit Analysis

### Telegram Bot API Limits

| Limit | Value | Applies to |
|-------|-------|------------|
| Messages per second (global) | 30 | All chats, all users |
| Messages per second (same chat) | 1 | Single chat ID |
| Messages per minute (group) | 20 | Group chats only |
| Max simultaneous connections | 100 | Long-polling / webhook |
| Paid broadcast | 1000 msg/sec | Requires Telegram Stars |

### Analysis for Conductor

Conductor sends all notifications to **one chat ID** (the operator's personal chat or
a dedicated conductor group). The binding constraint is **1 message/second to the same
chat**.

During concurrent agent completions, conductor may want to send multiple notifications
simultaneously (e.g., 3 agents complete within the same second). Without rate limiting,
this would trigger 429 errors from Telegram.

**Solution:** A notification queue with a rate limiter:

```python
import asyncio
from collections import deque

class NotificationQueue:
    """Rate-limited Telegram notification sender (1 msg/sec per chat)."""

    def __init__(self, bot: Bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id
        self._queue: deque = deque()
        self._running = False

    async def send(self, text: str, **kwargs) -> None:
        """Enqueue a message for rate-limited delivery."""
        self._queue.append((text, kwargs))
        if not self._running:
            asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        self._running = True
        while self._queue:
            text, kwargs = self._queue.popleft()
            await self.bot.send_message(
                chat_id=self.chat_id, text=text, **kwargs
            )
            await asyncio.sleep(1.1)  # 1.1s gap to stay under 1 msg/sec limit
        self._running = False
```

**Impact on approval gates:** P0 approval gate messages should bypass the queue and
send immediately (they are already infrequent). P3 informational notifications should
be queued. P1 urgent alerts use a priority queue position (head of queue).

---

## Security Requirements

### Bot Token Protection

- Store `TELEGRAM_BOT_TOKEN` as an environment variable, never in source code
- Rotate the token via @BotFather if the token is ever exposed
- The token grants full control of the bot — it is equivalent to an API key

### Chat ID Filtering (Whitelist)

Without filtering, any Telegram user who knows the bot's username can send it messages.
A whitelist prevents unauthorized users from triggering conductor approval gates.

```python
ALLOWED_CHAT_IDS = {
    int(x) for x in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if x
}

async def check_allowed(update: Update) -> bool:
    """Reject updates from unauthorized chats."""
    chat_id = (
        update.message.chat.id if update.message
        else update.callback_query.message.chat.id if update.callback_query
        else None
    )
    return chat_id in ALLOWED_CHAT_IDS
```

### Message Integrity

For a single-user personal deployment, message authentication (HMAC signatures on
callback data) is not required. The chat ID whitelist is sufficient: only the operator's
Telegram account can send messages to the conductor bot's chat.

For multi-user or team deployments where multiple operators may share a conductor
deployment, add user ID filtering within the allowed chat (only accept callbacks from
specific Telegram user IDs).

### Rate-Limiting Abuse

A malicious actor who knows the conductor bot's username and chat ID cannot trigger
arbitrary approvals: the approval messages are initiated by conductor (the bot sends to
the chat, not the other way around), and callback queries are only valid for messages
sent by the bot itself. The `message_id` check in the polling loop prevents replay attacks.

---

## ntfy and Slack Comparison

### ntfy

| Capability | ntfy | Telegram |
|------------|------|---------|
| Fire-and-forget notifications | Yes (excellent) | Yes |
| Interactive approval gates (buttons) | No | Yes |
| Self-hostable | Yes | No (API is cloud-only) |
| Mobile app | Yes (Android, iOS) | Yes |
| Setup complexity | Very low (1 curl command) | Low (bot setup via @BotFather) |
| Rate limits | None for self-hosted | 1 msg/sec per chat |
| Persistent history | Yes (topic-based) | Yes |

ntfy is the better choice if conductor needs **only notifications** (no approval gates).
For conductor's full feature set (notifications + approval gates), **Telegram is required**.

### Slack

| Capability | Slack | Telegram |
|------------|-------|---------|
| Interactive approval gates | Yes (Block Kit buttons) | Yes |
| Free tier | Message history limited | Free, unlimited |
| Team-oriented | Yes | Personal-focused |
| Bot setup | Complex (OAuth app registration) | Simple (@BotFather) |
| Webhook support | Yes | Yes |
| Personal developer use | Overkill | Natural fit |

Slack is better suited for team deployments where conductor notifications need to reach
a dev team channel. For personal conductor deployments, Telegram's simplicity and free
tier make it superior.

### Recommendation

- **Personal deployments**: Telegram (notification + approval gates) or ntfy (notification-only)
- **Team deployments**: Slack (Block Kit approval gates) or Telegram (if team already uses it)
- **CI/server deployments with no approval gates**: ntfy (simplest, self-hostable)

---

## Recommended Implementation

### Phase 1: Notifications Only

```python
# src/composer/notify.py
import os
import asyncio
from telegram import Bot

class ConductorNotifier:
    def __init__(self):
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.bot = Bot(token=token) if token else None
        self.chat_id = int(os.environ.get("TELEGRAM_CHAT_ID", "0"))
        self.enabled = bool(token and self.chat_id)

    async def notify(self, text: str) -> None:
        if not self.enabled:
            return
        await self.bot.send_message(chat_id=self.chat_id, text=text)

    async def notify_dispatch(self, issue_number: int, branch: str) -> None:
        await self.notify(f"🔧 Agent dispatched for #{issue_number} on {branch}")

    async def notify_pr_created(self, issue_number: int, pr_url: str) -> None:
        await self.notify(f"✅ PR created for #{issue_number}: {pr_url}")

    async def notify_error(self, issue_number: int, error: str) -> None:
        await self.notify(f"❌ Error on #{issue_number}: {error}")
```

### Phase 2: Approval Gates

Extend with the `ask_approval()` function from the Async Approval Gate Design section.
Wire into conductor's startup check for orphaned work:

```python
async def handle_orphaned_work(notifier: ConductorNotifier, issue: int) -> bool:
    return await notifier.ask_approval(
        question=f"Orphaned work found: #{issue} is in-progress with no PR.\n"
                 f"Auto-clean? (remove in-progress label, delete branch)",
        timeout_seconds=1800,  # 30 minutes
    )
```

### Conductor Config Integration

```toml
# conductor.toml
[notifications]
enabled = true
telegram_bot_token_env = "TELEGRAM_BOT_TOKEN"
telegram_chat_id_env = "TELEGRAM_CHAT_ID"
# Optional: ntfy fallback if Telegram is unavailable
ntfy_url = "https://ntfy.sh/my-conductor-topic"

# Events that trigger notifications
notify_on = ["dispatch", "pr_created", "pr_merged", "error", "session_complete"]
# Events that require approval before proceeding
gate_on = ["orphaned_work"]
```

---

## Follow-Up Research Recommendations

**[WONT_RESEARCH] Telegram group/forum topic support for multi-repo notifications**
Topic threads per repo would be useful but are an optimization. Start with a single
chat ID for all conductor notifications. Filing separate topics per repo adds routing
complexity without clearing a current-milestone blocker.

**[WONT_RESEARCH] Slack Block Kit implementation for team deployments**
Out of scope for v2 (personal deployment focus). File as v3 feature if team demand emerges.

**[WONT_RESEARCH] Telegram Stars purchase for paid broadcast rate limit**
30 messages/second global limit is irrelevant for a single-developer deployment.
The 1 msg/sec per-chat limit is the only relevant constraint, handled by the queue.

---

## Sources

- [Telegram Bot API Documentation](https://core.telegram.org/bots/api)
- [Telegram Bots FAQ — Rate Limits](https://core.telegram.org/bots/faq)
- [python-telegram-bot v22.x Documentation — CallbackQuery](https://docs.python-telegram-bot.org/telegram.callbackquery.html)
- [python-telegram-bot Inline Keyboard Example](https://github.com/python-telegram-bot/python-telegram-bot/blob/master/examples/inlinekeyboard2.py)
- [AIORateLimiter — python-telegram-bot v22.0](https://docs.python-telegram-bot.org/en/v22.0/telegram.ext.aioratelimiter.html)
- [Telegram API Rate Limits Guide — BytePlus](https://www.byteplus.com/en/topic/450604)
- [gramio Rate Limit Guide](https://gramio.dev/rate-limits)
- [ntfy Documentation](https://docs.ntfy.sh/)
- [ntfy Integrations](https://docs.ntfy.sh/integrations/)
- [ntfy vs Apprise comparison — XDA Developers](https://www.xda-developers.com/reasons-use-apprise-instead-of-ntfy-gotify/)
- [Apprise: Multi-Platform Notifications — One Up Time](https://oneuptime.com/blog/post/2026-02-08-how-to-run-apprise-in-docker-for-multi-platform-notifications/view)
- [Keyboard Buttons in Telegram Bot — GeeksforGeeks](https://www.geeksforgeeks.org/python/keyboard-buttons-in-telegram-bot-using-python/)

**Cross-references:**
- `05-logging-observability.md` — conductor event taxonomy for logging (subset overlaps with notification events)
- `08-usage-scheduling.md` — usage-at-80% event triggers the P1 notification
