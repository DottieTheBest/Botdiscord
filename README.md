# Discord Bot — Tickets, moderation, server management

Python bot (`discord.py` 2.x, slash commands) with an SQLite database.

## Installation

1. **Python 3.10+** required.
2. Install the dependencies: `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and put your token in `DISCORD_TOKEN`.
4. On the [Discord developer portal](https://discord.com/developers/applications) > your application > **Bot**,
   enable both **Privileged Gateway Intents**:
   - **Server Members Intent**
   - **Message Content Intent** (transcripts and anti-spam)
5. Invite the bot with the `bot` and `applications.commands` scopes. Easiest: the **Administrator** permission.
   Otherwise: Manage Channels, Manage Roles, Manage Messages, Manage Nicknames, Timeout Members,
   Kick Members, Ban Members, View Channels, Send Messages, Embed Links, Attach Files, Read Message History.
6. Run: `python bot.py`

Tip: set `DEV_GUILD_ID` in `.env` while testing, so commands show up instantly.
Without it, global syncing can take up to 1 hour.

## First-time setup

1. `/setup`: creates the **Staff** role, the **🎫 Tickets** category and the private `ticket-logs` and `mod-logs`
   channels (or reuses what already exists).
2. In your tickets welcome channel: `/ticket setup` posts the panel with the category menu.

Important: place the bot's role **above** the roles it needs to moderate (Server Settings > Roles).

## Commands

| Category | Commands |
|---|---|
| Tickets | `/ticket create`, `/ticket setup`, `/close`, `/add`, `/remove`, `/rename`, `/transcript`, **Close** and **Claim** buttons |
| Moderation | `/ban`, `/kick`, `/timeout`, `/warn`, `/warnings`, `/clear`, `/slowmode`, `/lock`, `/unlock`, `/purge` |
| Server | `/setup`, `/serverinfo`, `/userinfo`, `/role`, `/nick`, `/embed`, `/say`, `/announce`, `/poll` |
| Chat | `/chat text size font`: the bot sends a real text message |
| Giveaways | `/giveaway start`, `end`, `reroll`, `cancel`, `pause`, `resume`, `list`, `info` |

## Defaults (editable in `config.py`)

- **Ticket categories**: Support, Purchase, Complaint, Partnership, Other. Maximum 3 open tickets per member.
- **Progressive sanctions**: 3 warns = 1 h timeout, 5 = 24 h timeout, 7 = kick, 10 = ban.
- **Anti-spam**: 5 messages in 5 s, 3 identical messages, or 5 mentions in one message. The bot deletes the
  messages, adds a warning and applies a 5 min timeout (unless a progressive sanction applies).
  Members with the "Manage Messages" permission are ignored.
- **Logs**: sanctions go to `mod-logs`, closed tickets (with an HTML transcript) go to `ticket-logs`.
  The transcript is also sent by DM to the ticket author.
- **Staff** = the configured Staff role, or anyone with "Manage Server".

## Notes

- `/ticket` is a group: use `/ticket create` and `/ticket setup` (Discord doesn't allow a command to be both
  usable on its own and with subcommands).
- `/timeout` accepts `10m`, `2h`, `1d`, `1h30m`; a duration of `0` removes the timeout.
- `/chat` sends **real text** (copyable). Discord doesn't allow Arial, Comic Sans or pixel sizes in a message, so:
  **size** = 5 levels (tiny, normal, small, medium, large) and **font** = writing style
  (bold, italic, underline, strikethrough, code, cursive, gothic, double-struck, typewriter, sans-serif bold).
  Accented letters are kept; no mention (`@everyone`, roles) is triggered.
- `/poll`: button poll (2 to 5 options), one vote per person, clicking again removes the vote.
  Polls and ticket buttons keep working after a restart.
- `/say` never triggers mentions (`@everyone`, roles); use `/announce` for an announcement with a mention.
- Ticket categories were renamed to English keys (`purchase`, `complaint`, `partnership`, `other`). Tickets already
  stored with the old French keys still display correctly.

## Giveaways

- `/giveaway start prize duration [winners] [condition] [image] [description]`: starts a giveaway in the current
  channel (duration `30s`, `10m`, `2h`, `3d`, `1h30m`). Members enter with the **🎉 Enter** button; clicking again
  offers to leave. The condition is a required role (checked on click **and** at the draw). The image is re-uploaded
  with the message so it doesn't expire.
- `/giveaway end`: ends immediately and draws the winners. `/giveaway cancel`: cancels with no winner.
- `/giveaway pause` / `resume`: the remaining time is kept during the pause and the button is disabled.
- `/giveaway reroll [winners]`: draws new winners among eligible entrants, never picking someone already drawn.
- `/giveaway list` and `/giveaway info`: available to everyone (replies visible only to you).
- Management commands are staff-only. Without the `giveaway` option, they target the relevant giveaway of the
  current channel; otherwise pick one via autocomplete (or paste the message ID).
- Giveaways survive a restart (those that expired during downtime are ended when the bot comes back).
  Deleting the message of a running giveaway cancels it. Limits are in `config.py` (25 running, 20 winners, 60 days).
