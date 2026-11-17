# ♠ Discord Texas Hold'em Bot

A Python Discord bot for no-limit Texas Hold'em with persistent chip wallets, daily rewards, real card artwork, private hole cards, turn timers, and proper side-pot handling.

![Showdown table](docs/preview-showdown.png)

## Features

- **`/profile [user]`** — view your own or another player's avatar, username, wallet, daily availability/cooldown, and poker progress: games played, hands played, hands won, hand win rate, biggest pot won, and net chips.
- **`/daily`** — claim **1,000** chips every 24 hours.
- **`/balance`** — check how many chips you have.
- **`/hands`** — see every poker hand from royal flush down to high card, with examples, what each hand beats, tie/kicker rules, and the hand-ranking chart. Every game start posts an enlarged copy of the chart in the channel.
- **`/creategame [buy_in] [hands]`** — opens a table in the channel and joins the host automatically. The buy-in (default **500**) becomes the host's starting stack. Set `hands` to close the table after that many hands (default is unlimited).
- **`join game`** — typed in the channel, or use the **Join game** button. Does nothing if no table exists.
- **`start game`** — the host starts the game by typing this or pressing **Start game**.
- **Full no-limit Hold'em** — blinds, four betting rounds, all-ins, side pots, split pots, min-raise rules, and showdowns are handled by [PokerKit](https://github.com/uoftcprg/pokerkit).
- **Chat actions** — during your turn, type exactly `check`, `call`, `raise <amount>`, `all in`, or `fold` in the channel. Partial messages such as `call now` are ignored. The action buttons still work.
- **Private refusals** — errors that only concern you (not your turn, not enough chips to join, not the host) are ephemeral when triggered from a button or slash command, and sent by DM when triggered by a chat command, with a ❌ on your message. The bot only falls back to a channel reply if your DMs are closed. `/daily` replies are ephemeral too, so personal wallet details never push the table up.
- **Private cards** — a **View my cards** button sits on the table message, so no extra message is posted below the table. Pressing it (or using `/cards`) shows your hole cards and current best hand in an ephemeral reply that only you can see. No DMs required. All other bot messages are public by default.
- **Public table** — a rendered table image shows stacks, bets, the button, community cards, pot, folded/removed players, and hole cards only at showdown. The embed above it shows the pot, blinds, the current board, the player on the clock with their call and raise options, and a running log of every action in the hand. Action confirmations are private, and whenever newer messages would push the table up, it is re-posted at the bottom of the channel so the live table is always the last thing on screen.
- **Turn timer** — each player has **60 seconds**. Missing the deadline auto-folds.
- **Timeout removal** — after more than **3** timeouts (the 4th), the player is removed from the table and their remaining stack is forfeited to the pot.
- **`/endgame`** — the host or a server admin closes the table immediately. Remaining stacks are cashed back into wallets.
- **Chip wallet** — balances are stored in PostgreSQL via `DATABASE_URL` and survive bot restarts.
- **Crypto RNG** — the deck is shuffled with `secrets.SystemRandom`, and PokerKit draws from that single shuffled deck (so burn cards never duplicate dealt cards).
- **Bot profile** — the bot shows `Dealing cards | /help` as its status, and its slash commands are registered globally so Discord lists them in the profile's **Commands** tab.

![Table](docs/preview-table.png)

## Setup

Requires **Python 3.11+** and a **PostgreSQL** database.

1. Create and activate a virtual environment, then install dependencies:

   ```bash
   python -m venv .venv
   # Windows:
   .venv/Scripts/activate
   # macOS / Linux:
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Create a bot at the [Discord Developer Portal](https://discord.com/developers/applications):
   - Open **Bot** and reset/copy the token.
   - Enable **Message Content Intent** under **Privileged Gateway Intents**. This is required for the typed `join game` / `start game` commands.
   - Invite it with the `bot` and `applications.commands` scopes and these permissions: **View Channels**, **Send Messages**, **Embed Links**, **Attach Files**, **Read Message History**.

3. Create a PostgreSQL database and configure environment variables:

   ```bash
   cp .env.example .env
   # edit .env and set DISCORD_TOKEN=... and DATABASE_URL=postgresql://...
   ```

   `DATABASE_URL` is required. The bot creates its `users` table automatically on startup. Slash commands are synced globally, which is what makes Discord list them in the bot's profile under **Commands** (Discord can take a while to show global updates). For instant command updates while developing, set `GUILD_ID` in `.env` to your server ID; the bot then also registers a copy of the commands in that server. Global syncs are only sent when the command set changes, because Discord rate limits them.

4. Run the bot from the project root:

   ```bash
   python -m pokerbot
   ```

5. In a server channel:

   ```
   /daily
   /creategame
   join game
   start game
   ```

## Commands

| Command | Who | What it does |
| --- | --- | --- |
| `/daily` | everyone | Claims 1,000 chips every 24 hours and shows your balance. |
| `/balance` | everyone | Shows your current chip balance. |
| `/hands` | everyone | Lists every hand from strongest to weakest with example cards, what each hand beats, and the hand-ranking chart. Every game start posts an enlarged copy of the chart. |
| `/profile [user]` | everyone | Shows your profile, or another member's, including avatar, username, wallet, daily status/cooldown, and poker stats (games, hands, hand wins/rate, biggest pot, net chips). Public message. |
| `/help` | everyone | Rules, command list, and a generated hand-ranking chart. |
| `/creategame [buy_in] [hands]` | everyone | Opens a table. Buy-in range is 100–100,000; default 500. `hands` (1–1,000) closes the table after that many hands; omit it to play until one player is left. One table per channel. |
| `join game` (typed) | everyone | Joins the lobby. Requires enough chips; the buy-in is deducted. |
| `start game` (typed) | host | Starts the game. Also available as the **Start game** button. |
| `check` / `call` / `fold` (typed) | player whose turn it is | Acts in the hand. The whole message must match, ignoring case and surrounding spaces. |
| `all in`, `all-in`, or `allin` (typed) | player whose turn it is | Moves all-in. |
| `raise <amount>` or `bet <amount>` (typed) | player whose turn it is | Raises to the given total (commas allowed). `raise to <amount>` also works. |
| Buttons | players in hand | Fold, Check/Call, Raise (opens a modal), All-in. |
| **View my cards** | players in hand | Opens an ephemeral, user-only view of your hole cards and current best hand. |
| `/cards` | players in hand | Same private view, available at any time during a hand. |
| `/endgame` | host or admin | Immediately closes the table and cashes out remaining stacks. |

## Rules in brief

- Each player starts with a stack equal to the buy-in. Blinds scale with the buy-in: `small = buy_in / 50`, `big = 2 × small` (500 → 10/20).
- Two hole cards each; five community cards are dealt as the flop (3), turn (1), and river (1).
- Betting rounds: pre-flop, flop, turn, river. No-limit; a raise must be at least the big blind or the previous raise.
- All-in bets and side pots are resolved automatically.
- Hands continue until one player has chips left, a set `hands` limit is reached, or the host/admin runs `/endgame`. Busting a buy-in means the chips are lost; cashing out returns your remaining stack to your wallet.
- The buy-in is taken when you join, and remaining chips are credited back when the table closes. If the bot is shut down gracefully, active tables are cashed out.

### Timeouts

Every turn has a **60-second** limit. If you do not act:

1. Your hand is auto-folded.
2. The timeout is recorded for the rest of that game.
3. On timeout **4** (more than 3), you are removed from the table. Your remaining stack is added to the pot as dead money; the current hand's winner receives it.

## Help artwork

`/help` includes this generated chart:

![Hand rankings](docs/preview-help.png)

Private hole-card view:

![Hole cards](docs/preview-hand.png)

## Recorded players and servers

The bot keeps an audience log in the same PostgreSQL database; the tables are
created automatically on startup.

| Table | Columns |
| --- | --- |
| `users` | `user_id`, `chips`, `last_daily`, `username`, `display_name`, `added_at`, `updated_at` |
| `discord_guilds` | `guild_id`, `name`, `added_at`, `removed_at` |
| `guild_events` | `id`, `guild_id`, `name`, `event` (`added`/`removed`), `occurred_at` |

- A player's name is stored on their wallet row in `users` the first time they
  use a command or act in a hand, and refreshed at most once an hour.
- `/daily` uses a rolling 24-hour cooldown stored in `users.last_daily`: each
  successful claim sets the timer back to 24 hours, so the reset time follows
  the player instead of a fixed UTC day boundary.
- A server is recorded when the bot joins it. The list is reconciled on every
  reconnect, so servers the bot was kicked from while it was offline are still
  marked. `added_at` is the start of the current tenure and `removed_at` is
  `NULL` while the bot is in the server.
- `guild_events` is an append-only history, so an earlier kick is still visible
  after the bot is re-added.

```sql
SELECT user_id, username, display_name, added_at FROM users ORDER BY added_at;
SELECT * FROM discord_guilds ORDER BY added_at;
SELECT * FROM guild_events   ORDER BY occurred_at;
```

## Project layout

```
pokerbot/
  bot.py        Discord client, setup hook, graceful shutdown cash-out
  cog.py        /daily, /balance, /hands, /profile, /help, /creategame, /endgame, /cards
  game.py       table orchestration, timers, timeouts, embeds, cashier
  engine.py     poker rules wrapper, seating/blinds rotation, CSPRNG-shuffled deck
  hands.py      hand-ranking embed used by /hands
  ui.py         lobby/action buttons and raise modal
  visuals.py    Pillow table, hole-card, and hand-ranking renders
  store.py      PostgreSQL chip wallet, poker stats, player/server tracking
assets/cards/   MIT-licensed playing card PNGs + LICENSE
tests/          engine, wallet, and headless game-loop tests
```

## Tests

```bash
python -m unittest discover -s tests -v
```

The store tests need `DATABASE_URL` (or `TEST_DATABASE_URL`) pointing at PostgreSQL; they create and drop an isolated temporary schema. They are skipped when no database is configured. The suite covers deck determinism, heads-up blinds/action order, raise validation, all-in side-pot chip conservation, showdown reveals, timeout forfeiture, daily/debit/credit behavior, and the full Discord game loop with fake Discord objects.

## Credits

- Poker rules and hand evaluation: [PokerKit](https://github.com/uoftcprg/pokerkit) (MIT).
- Card faces: [playing-cards-assets](https://github.com/hayeah/playing-cards-assets) (MIT, from public-domain vector playing cards). See `assets/cards/LICENSE`.
- Discord API: [discord.py](https://github.com/Rapptz/discord.py).
- Image rendering: [Pillow](https://python-pillow.org/).

Chips are play money only.
