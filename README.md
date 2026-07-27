# Pokemon Hangman Discord Bot

A competitive text-based hangman game for Discord. A host starts a game,
friends join via a button, and everyone competes over 7 rounds guessing
random Pokemon names, abilities, moves, or items.

## How it plays

- `!hangman` starts a game. The host is auto-included as a player.
- A **Join Game** button appears for 20 seconds so others can join. If
  nobody else joins, it's solo mode.
- Each round, the bot picks a random entry from one of four categories
  (Pokemon, Ability, Move, Item) and shows it as underscores
  (e.g. `Life Orb` → `_ _ _ _   _ _ _`).
- Players type a **single letter** to guess a letter, or the **full
  answer** to guess the whole thing.
- Guessing the full answer correctly (or completing the last letter)
  scores 1 point and moves to the next round.
- Rounds time out after 120 seconds if nobody solves it (no point
  awarded).
- After 7 rounds, the bot announces final scores and the winner (or a
  tie).

## Setup

### 1. Create the bot on Discord

1. Go to https://discord.com/developers/applications and click **New
   Application**.
2. Open the **Bot** tab, click **Reset Token** to get your bot token
   (copy and keep it secret), and enable **Message Content Intent**
   under Privileged Gateway Intents.
3. Open **OAuth2 > URL Generator**, check the `bot` scope, and under Bot
   Permissions check: Send Messages, Read Message History, Embed Links.
4. Open the generated URL and invite the bot to your server.

### 2. Set up the project locally

```bash
cd pokemon-hangman-bot
python -m venv venv
source venv/bin/activate   # on Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Generate the word lists

```bash
python fetch_word_lists.py
```

This creates `data/pokemon.txt`, `data/abilities.txt`, `data/moves.txt`,
and `data/items.txt` by pulling data from the free PokeAPI. You only
need to run this once (re-run any time you want to refresh the lists).

### 4. Set your bot token

Either export it as an environment variable:

```bash
export DISCORD_BOT_TOKEN="your-token-here"   # on Windows: set DISCORD_BOT_TOKEN=your-token-here
```

...or create a `.env` file in the project folder:

```
DISCORD_BOT_TOKEN=your-token-here
```

### 5. Run it

```bash
python bot.py
```

You should see `Pokemon Hangman bot is ready.` in the console. Head to
your server and type `!hangman` to start a game.

## Hosting it 24/7

Once it works locally, you'll want it running somewhere that stays on:

- **Railway** or **Fly.io** — free/cheap tiers, deploy straight from a
  GitHub repo, good for beginners.
- **A VPS** (DigitalOcean, Linode, etc.) — run it in a `screen` or
  `tmux` session, or set it up as a systemd service.
- **A Raspberry Pi** at home — cheap, always-on, good if you don't mind
  it depending on your home internet.

## Customizing

All the key numbers live at the top of `bot.py`:

- `ROUNDS_PER_GAME` — how many rounds per game (default 7)
- `JOIN_WINDOW_SECONDS` — how long the join button stays open (default 20)
- `ROUND_TIMEOUT_SECONDS` — how long each round lasts before auto-reveal
  (default 120)
