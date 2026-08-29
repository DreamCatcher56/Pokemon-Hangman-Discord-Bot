"""
Pokemon Hangman Competition Bot
--------------------------------
A Discord bot game where a host starts a hangman round, other players can
join, and everyone competes over 7 rounds guessing random Pokemon names,
abilities, moves, or items pulled from text files.
"""


import os
import random
import asyncio
import time
import sqlite3
from collections import Counter
from datetime import datetime, timezone


import discord
from discord.ext import commands


try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional; you can also just set the env var directly


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


DATA_FILES = {
    "Pokémon": "data/pokemon.txt",
    "Ability": "data/abilities.txt",
    "Move": "data/moves.txt",
    "Item": "data/items.txt",
}

# Same categories as regular hangman, except Items are pulled from a
# separate curated list for the blitz modes.
BLITZ_DATA_FILES = {
    **DATA_FILES,
    "Item": "data/blitzitems.txt",
}

DEFAULT_ROUNDS_PER_GAME = 7
MIN_ROUNDS = 1
MAX_ROUNDS = 20
JOIN_WINDOW_SECONDS = 12
ROUND_TIMEOUT_SECONDS = 100          # for regular multiplayer hangman
CLASSIC_ROUND_TIMEOUT = 150          # for classic solo hangman (CHANGE: increased from 100)
VOWELS = set("AEIOU")
CONSONANTS = set("BCDFGHJKLMNPQRSTVWXYZ")


# Time penalty (in seconds) added to a Blitz player's score for each
# *incorrect single-letter* guess. Rarer letters cost less since a wrong
# guess on them is less "avoidable" - guessing common letters wrong is
# what this is meant to discourage blind-spamming.
#
# Tier 1 - 4.0s - E, A, R, O, I
# Tier 2 - 3.0s - T, L, S, C, N
# Tier 3 - 2.0s - M, D, U, P, G, K, H, B
# Tier 4 - 1.0s - Y, F, V, W, Z, X, J, Q
BLITZ_TIER_BASE_PENALTY: dict[int, float] = {
    1: 4.0,
    2: 3.0,
    3: 2.0,
    4: 1.0,
}


BLITZ_LETTER_TIER: dict[str, int] = {
    **{letter: 1 for letter in "EAROI"},
    **{letter: 2 for letter in "TLSCN"},
    **{letter: 3 for letter in "MDUPGKHB"},
    **{letter: 4 for letter in "YFVWZXJQ"},
}


# Base penalty per letter, kept for anything that just wants the flat
# first-guess cost (e.g. display text) without the escalation logic.
BLITZ_LETTER_PENALTIES: dict[str, float] = {
    letter: BLITZ_TIER_BASE_PENALTY[tier] for letter, tier in BLITZ_LETTER_TIER.items()
}


BLITZ_WORDS_TO_GUESS = 5
BLITZ_IDLE_TIMEOUT_SECONDS = 45  # auto-penalty cancel if no guess for this long
ROLLING_AVERAGE_WINDOW = 5  # skill metric = avg of a player's last N games, per mode
LEADERBOARD_SIZE = 10


# Point this at your Railway volume's mount path (e.g. "/data/blitz_scores.db")
# via the BLITZ_DB_PATH env var, or scores won't survive a redeploy/restart.
BLITZ_DB_PATH = os.getenv("BLITZ_DB_PATH", "data/blitz_scores.db")


intents = discord.Intents.default()
intents.message_content = True  # required to read letter/word guesses
bot = commands.Bot(command_prefix="!", intents=intents)


# channel_id -> HangmanGame
active_games: dict[int, "HangmanGame"] = {}

# channel_id -> BlitzSpeedGame
active_blitz_games: dict[int, "BlitzSpeedGame"] = {}

# channel_id -> ClassicGame
active_classic_games: dict[int, "ClassicGame"] = {}



def fire_and_forget(coro) -> asyncio.Task:
    """Schedules a coroutine (typically a channel.send()) without awaiting
    it. Used for guess-feedback messages so a slow Discord API round-trip
    (or rate-limit backoff) doesn't hold up the per-game lock and delay
    processing the next guess. Errors are logged instead of vanishing
    silently as an "exception never retrieved" warning."""
    task = asyncio.create_task(coro)

    def _log_if_failed(t: asyncio.Task):
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            print(f"⚠️ Background message send failed: {exc!r}")

    task.add_done_callback(_log_if_failed)
    return task




# ---------------------------------------------------------------------------
# Word list loading
# ---------------------------------------------------------------------------


def load_word_lists(data_files: dict[str, str] = DATA_FILES) -> dict[str, list[str]]:
    lists = {}
    for category, path in data_files.items():
        if not os.path.exists(path):
            raise FileNotFoundError(f"Missing {path}.")
        with open(path, "r", encoding="utf-8") as f:
            words = [line.strip() for line in f if line.strip()]
        if not words:
            raise ValueError(f"{path} is empty.")
        lists[category] = words
    return lists




def pick_word(word_lists: dict[str, list[str]]) -> tuple[str, str]:
    category = random.choice(list(word_lists.keys()))
    word = random.choice(word_lists[category])
    return category, word




# ---------------------------------------------------------------------------
# Shared word-rendering helpers (used by the regular game and blitz modes)
# ---------------------------------------------------------------------------


def render_word(word: str, guessed_letters: set[str]) -> str:
    word_parts = word.split(" ")
    rendered_parts = []
    for part in word_parts:
        rendered = " ".join(
            ch if (not ch.isalpha()) or ch.upper() in guessed_letters else "_"
            for ch in part
        )
        rendered_parts.append(rendered)
    return "   ".join(rendered_parts)




def word_fully_revealed(word: str, guessed_letters: set[str]) -> bool:
    return all((not ch.isalpha()) or ch.upper() in guessed_letters for ch in word)




def format_wrong_letters(wrong_letters: set[str]) -> str:
    if not wrong_letters:
        return "Letters not in the word: none yet"
    return "Letters not in the word: " + ", ".join(sorted(wrong_letters))




def build_time_interval_fields(word_times: list[tuple[str, float, float]], max_chars: int = 1000) -> list[tuple[str, str]]:
    """Turns a list of (word, duration_seconds, cumulative_seconds) into
    (field_name, field_value) pairs, splitting into multiple fields if
    needed to stay under Discord's 1024-char embed field limit."""
    lines = [
        f"Word {i}: **{word}** \u2014 {duration:.3f}s (total: {cumulative:.3f}s)"
        for i, (word, duration, cumulative) in enumerate(word_times, start=1)
    ]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        added_len = len(line) + 1  # + newline
        if current and current_len + added_len > max_chars:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(line)
        current_len += added_len
    if current:
        chunks.append("\n".join(current))

    fields = []
    for i, chunk in enumerate(chunks):
        name = "Time Intervals" if i == 0 else "Time Intervals (cont.)"
        fields.append((name, chunk))
    return fields




def format_score(score: float) -> str:
    return f"{score:.3f}s"




# ---------------------------------------------------------------------------
# Persistent leaderboard storage (SQLite)
#
# One row per completed blitz round. The skill metric is a player's ROLLING
# AVERAGE over their last ROLLING_AVERAGE_WINDOW games in a given mode (not
# their single best-ever score) - this is what both !pb and the leaderboard
# commands rank by, so a single lucky short-word run can't dominate. Lower
# average time is better, same as a single score.
# ---------------------------------------------------------------------------


def _get_connection() -> sqlite3.Connection:
    db_dir = os.path.dirname(BLITZ_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    return sqlite3.connect(BLITZ_DB_PATH)




def _init_db_sync():
    conn = _get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS blitz_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                score REAL NOT NULL,
                achieved_at TEXT NOT NULL
            )
            """
        )

        # Older deployments created this table with a
        # CHECK(mode IN ('blitz1', 'blitz2')) constraint. SQLite can't drop
        # a CHECK constraint with ALTER TABLE, so detect it and rebuild the
        # table (preserving all existing rows) whenever new modes need to
        # be inserted.
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'blitz_scores'"
        ).fetchone()
        if row and row[0] and "CHECK" in row[0].upper():
            conn.execute("ALTER TABLE blitz_scores RENAME TO blitz_scores_old")
            conn.execute(
                """
                CREATE TABLE blitz_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    score REAL NOT NULL,
                    achieved_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO blitz_scores (id, mode, user_id, username, score, achieved_at) "
                "SELECT id, mode, user_id, username, score, achieved_at FROM blitz_scores_old"
            )
            conn.execute("DROP TABLE blitz_scores_old")

        # blitz1/blitz2/blitz2p were retired in favor of blitz/blitzp with a
        # rolling-average skill metric. Old scores under the retired mode
        # names don't carry over - clear them out so everyone starts fresh
        # under the new modes instead of leaving orphaned rows sitting in
        # the database.
        # REMOVED: we now only keep 'blitz' mode; delete blitzp as well.
        conn.execute("DELETE FROM blitz_scores WHERE mode IN ('blitz1', 'blitz2', 'blitz2p', 'blitzp')")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_blitz_mode_score ON blitz_scores(mode, score)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_blitz_mode_user ON blitz_scores(mode, user_id)")

        # Classic mode table
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS classic_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                score INTEGER NOT NULL,
                achieved_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_classic_user ON classic_scores(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_classic_score ON classic_scores(score DESC)")

        conn.commit()
    finally:
        conn.close()




def init_db():
    """Synchronous on purpose - called once at startup before the bot logs in."""
    _init_db_sync()




def _record_score_sync(mode: str, user_id: int, username: str, score: float) -> tuple[float, int, int, list[float]]:
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO blitz_scores (mode, user_id, username, score, achieved_at) VALUES (?, ?, ?, ?, ?)",
            (mode, user_id, username, score, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

        recent_scores = [
            row[0]
            for row in conn.execute(
                "SELECT score FROM blitz_scores WHERE mode = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
                (mode, user_id, ROLLING_AVERAGE_WINDOW),
            ).fetchall()
        ]
        avg_score = sum(recent_scores) / len(recent_scores)
        games_counted = len(recent_scores)

        rank = conn.execute(
            """
            WITH ranked AS (
                SELECT user_id, score,
                       ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id DESC) AS rn
                FROM blitz_scores
                WHERE mode = ?
            ),
            averages AS (
                SELECT user_id, AVG(score) AS avg_score
                FROM ranked
                WHERE rn <= ?
                GROUP BY user_id
            )
            SELECT COUNT(*) + 1 FROM averages WHERE avg_score < ?
            """,
            (mode, ROLLING_AVERAGE_WINDOW, avg_score),
        ).fetchone()[0]

        return avg_score, games_counted, rank, recent_scores
    finally:
        conn.close()




async def record_score(mode: str, user_id: int, username: str, score: float) -> tuple[float, int, int, list[float]]:
    """Logs a completed round and returns (rolling_average, games_counted,
    current_leaderboard_rank, recent_scores) for that player in that mode.
    recent_scores is the list of the last up to ROLLING_AVERAGE_WINDOW scores
    (newest first) that the rolling average is based on."""
    return await asyncio.to_thread(_record_score_sync, mode, user_id, username, score)




def _get_rolling_average_sync(mode: str, user_id: int) -> tuple[float, int] | None:
    conn = _get_connection()
    try:
        recent_scores = [
            row[0]
            for row in conn.execute(
                "SELECT score FROM blitz_scores WHERE mode = ? AND user_id = ? ORDER BY id DESC LIMIT ?",
                (mode, user_id, ROLLING_AVERAGE_WINDOW),
            ).fetchall()
        ]
        if not recent_scores:
            return None
        return sum(recent_scores) / len(recent_scores), len(recent_scores)
    finally:
        conn.close()




async def get_rolling_average(mode: str, user_id: int) -> tuple[float, int] | None:
    return await asyncio.to_thread(_get_rolling_average_sync, mode, user_id)




def _get_average_leaderboard_sync(mode: str, limit: int) -> list[tuple[str, float, int]]:
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            WITH ranked AS (
                SELECT user_id, username, score,
                       ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY id DESC) AS rn
                FROM blitz_scores
                WHERE mode = ?
            ),
            averages AS (
                SELECT user_id, AVG(score) AS avg_score, COUNT(*) AS games_counted
                FROM ranked
                WHERE rn <= ?
                GROUP BY user_id
            )
            SELECT ranked.username, averages.avg_score, averages.games_counted
            FROM averages
            JOIN ranked ON ranked.user_id = averages.user_id AND ranked.rn = 1
            ORDER BY averages.avg_score ASC
            LIMIT ?
            """,
            (mode, ROLLING_AVERAGE_WINDOW, limit),
        ).fetchall()
        return rows
    finally:
        conn.close()




async def get_average_leaderboard(mode: str, limit: int = LEADERBOARD_SIZE) -> list[tuple[str, float, int]]:
    return await asyncio.to_thread(_get_average_leaderboard_sync, mode, limit)




def _get_best_scores_sync(mode: str, user_id: int, limit: int = 5) -> list[float]:
    """Returns the player's fastest scores for a mode, best (lowest) first."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT score FROM blitz_scores WHERE mode = ? AND user_id = ? ORDER BY score ASC LIMIT ?",
            (mode, user_id, limit),
        ).fetchall()
        return [row[0] for row in rows]
    finally:
        conn.close()




async def get_best_scores(mode: str, user_id: int, limit: int = 5) -> list[float]:
    return await asyncio.to_thread(_get_best_scores_sync, mode, user_id, limit)




# ---------- Classic mode database helpers ----------
def _record_classic_score_sync(user_id: int, username: str, score: int) -> tuple[int, int, int]:
    """Insert a classic session and return (personal_best, total_games_played, rank)."""
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO classic_scores (user_id, username, score, achieved_at) VALUES (?, ?, ?, ?)",
            (user_id, username, score, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

        pb = conn.execute(
            "SELECT MAX(score) FROM classic_scores WHERE user_id = ?",
            (user_id,)
        ).fetchone()[0] or 0

        games_played = conn.execute(
            "SELECT COUNT(*) FROM classic_scores WHERE user_id = ?",
            (user_id,)
        ).fetchone()[0]

        # Rank = number of users with a strictly higher personal best + 1
        rank = conn.execute(
            """
            SELECT COUNT(DISTINCT user_id) + 1
            FROM classic_scores
            WHERE user_id IN (
                SELECT user_id
                FROM classic_scores
                GROUP BY user_id
                HAVING MAX(score) > ?
            )
            """,
            (score,)
        ).fetchone()[0]

        return pb, games_played, rank
    finally:
        conn.close()




async def record_classic_score(user_id: int, username: str, score: int) -> tuple[int, int, int]:
    return await asyncio.to_thread(_record_classic_score_sync, user_id, username, score)




def _get_classic_pb_sync(user_id: int) -> tuple[int, int]:
    conn = _get_connection()
    try:
        pb = conn.execute(
            "SELECT MAX(score) FROM classic_scores WHERE user_id = ?",
            (user_id,)
        ).fetchone()[0] or 0
        games = conn.execute(
            "SELECT COUNT(*) FROM classic_scores WHERE user_id = ?",
            (user_id,)
        ).fetchone()[0]
        return pb, games
    finally:
        conn.close()




async def get_classic_pb(user_id: int) -> tuple[int, int]:
    return await asyncio.to_thread(_get_classic_pb_sync, user_id)




def _get_classic_leaderboard_sync(limit: int) -> list[tuple[str, int, int]]:
    """Returns (username, personal_best, games_played) for each user, ordered by PB descending."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            """
            SELECT username, MAX(score) as pb, COUNT(*) as games
            FROM classic_scores
            GROUP BY user_id
            ORDER BY pb DESC
            LIMIT ?
            """,
            (limit,)
        ).fetchall()
        return rows
    finally:
        conn.close()




async def get_classic_leaderboard(limit: int = LEADERBOARD_SIZE) -> list[tuple[str, int, int]]:
    return await asyncio.to_thread(_get_classic_leaderboard_sync, limit)




# ---------------------------------------------------------------------------
# Join view (button-based lobby)
# ---------------------------------------------------------------------------


class JoinView(discord.ui.View):
    def __init__(self, host: discord.Member, timeout: int):
        super().__init__(timeout=timeout)
        self.players: list[discord.Member] = [host]

    @discord.ui.button(label="Join Game", style=discord.ButtonStyle.green, emoji="🎮")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user in self.players:
            await interaction.response.send_message("You're already in the game!", ephemeral=True)
            return
        self.players.append(interaction.user)
        await interaction.response.send_message(
            f"**{interaction.user.display_name}** joined the game! ({len(self.players)} players)",
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True




BLITZ_DIFFICULTY_SELECT_TIMEOUT_SECONDS = 20




class BlitzDifficultyView(discord.ui.View):
    """Shown when !blitz is invoked so the player can pick Easy
    or Hard mode before the round starts. Only the player who ran the
    command can make the selection. Defaults to Hard mode if the player
    doesn't pick anything before the view times out."""

    def __init__(self, player: discord.Member, timeout: int = BLITZ_DIFFICULTY_SELECT_TIMEOUT_SECONDS):
        super().__init__(timeout=timeout)
        self.player = player
        self.difficulty: str = "hard"
        self.message: discord.Message | None = None
        self._decided = asyncio.Event()

    async def _choose(self, interaction: discord.Interaction, difficulty: str, label: str):
        if interaction.user.id != self.player.id:
            await interaction.response.send_message(
                "Only the player who ran the command can pick the mode.", ephemeral=True
            )
            return
        self.difficulty = difficulty
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"Mode selected: **{label}**. Starting your Speed Blitz...", view=self
        )
        self._decided.set()
        self.stop()

    @discord.ui.button(label="Easy Mode", style=discord.ButtonStyle.green, emoji="🌱")
    async def easy(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "easy", "🌱 Easy")

    @discord.ui.button(label="Hard Mode", style=discord.ButtonStyle.red, emoji="🔥")
    async def hard(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "hard", "🔥 Hard")

    async def on_timeout(self):
        if self._decided.is_set():
            return
        for item in self.children:
            item.disabled = True
        # No selection in time - default to Hard mode (the recorded, ranked
        # ruleset) rather than silently leaving the player without a game.
        if self.message is not None:
            try:
                await self.message.edit(
                    content="No mode selected in time \u2014 defaulting to **🔥 Hard** mode. Starting your Speed Blitz...",
                    view=self,
                )
            except discord.HTTPException:
                pass




# ---------------------------------------------------------------------------
# Category selection view for !hangman (button-based)
# ---------------------------------------------------------------------------
class CategorySelectView(discord.ui.View):
    """Buttons to choose which word list to use for the regular hangman game."""
    def __init__(self, author: discord.Member, timeout: int = 30):
        super().__init__(timeout=timeout)
        self.author = author
        self.selected_category: str | None = None  # None means "All"
        self._decided = asyncio.Event()
        self.message: discord.Message | None = None

    async def _choose(self, interaction: discord.Interaction, category: str | None, label: str):
        if interaction.user.id != self.author.id:
            await interaction.response.send_message(
                "Only the person who started the game can choose the word list.",
                ephemeral=True
            )
            return
        self.selected_category = category  # None for All, or string for a specific category
        # Disable all buttons
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"Word list chosen: **{label}**. Now starting the join lobby...",
            view=self
        )
        self._decided.set()
        self.stop()

    @discord.ui.button(label="All", style=discord.ButtonStyle.primary, emoji="📚")
    async def all_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, None, "All")

    @discord.ui.button(label="Pokémon", style=discord.ButtonStyle.success, emoji="🐾")
    async def pokemon_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "Pokémon", "Pokémon")

    @discord.ui.button(label="Ability", style=discord.ButtonStyle.secondary, emoji="✨")
    async def ability_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "Ability", "Ability")

    @discord.ui.button(label="Move", style=discord.ButtonStyle.danger, emoji="⚔️")
    async def move_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "Move", "Move")

    @discord.ui.button(label="Item", style=discord.ButtonStyle.blurple, emoji="🎒")
    async def item_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._choose(interaction, "Item", "Item")

    async def on_timeout(self):
        if self._decided.is_set():
            return
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    content="No selection made in time. Defaulting to **All** categories.",
                    view=self
                )
            except discord.HTTPException:
                pass
        self.selected_category = None  # default to All
        self._decided.set()
        self.stop()


# ---------------------------------------------------------------------------
# Game logic
# ---------------------------------------------------------------------------


class HangmanGame:
    def __init__(self, channel: discord.TextChannel, players: list[discord.Member], word_lists, host: discord.Member, rounds_per_game: int):
        self.channel = channel
        self.players = players
        self.word_lists = word_lists
        self.host = host
        self.rounds_per_game = rounds_per_game
        self.scores = {p.id: 0 for p in players}
        self.round_num = 0
        self.current_word = ""
        self.current_category = ""
        self.guessed_letters: set[str] = set()
        self.wrong_letters: set[str] = set()
        self.active = True
        self._timeout_task: asyncio.Task | None = None
        self.round_start_time: float | None = None
        self.guess_durations: list[float] = []
        # Discord dispatches each incoming message as its own concurrent task,
        # so two rapid-fire guesses can both be "in flight" at once. This lock
        # forces guess processing (and round-timeout processing) to run one
        # at a time so a word/round can never be completed twice.
        self._lock = asyncio.Lock()

    def player_by_id(self, uid: int) -> discord.Member | None:
        return discord.utils.get(self.players, id=uid)

    def build_display(self) -> str:
        return render_word(self.current_word, self.guessed_letters)

    def is_fully_revealed(self) -> bool:
        return word_fully_revealed(self.current_word, self.guessed_letters)

    def wrong_letters_display(self) -> str:
        return format_wrong_letters(self.wrong_letters)

    def _prepare_round(self) -> discord.Embed:
        """Synchronously advances round state and builds the round-start
        embed, without sending it. Kept separate from the send so callers
        can guarantee a preceding announcement message is fully sent first,
        instead of racing it against this embed as two independent
        background tasks."""
        self.round_num += 1
        self.current_category, self.current_word = pick_word(self.word_lists)
        self.guessed_letters = set(VOWELS)
        self.wrong_letters = set()
        self.round_start_time = time.monotonic()

        embed = discord.Embed(
            title=f"Round {self.round_num} of {self.rounds_per_game}",
            description=f"Category: **{self.current_category}**\n\n`{self.build_display()}`",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="\u200b", value=self.wrong_letters_display(), inline=False)
        embed.set_footer(text=f"Vowels are pre-filled. {ROUND_TIMEOUT_SECONDS}s to guess. Type a letter or the full answer.")
        return embed

    async def start_round(self, prior_announcement: str | None = None):
        embed = self._prepare_round()

        async def _dispatch():
            if prior_announcement:
                await self.channel.send(prior_announcement)
            await self.channel.send(embed=embed)
        fire_and_forget(_dispatch())

        if self._timeout_task:
            self._timeout_task.cancel()
        self._timeout_task = asyncio.create_task(self._round_timeout())

    async def _round_timeout(self):
        try:
            await asyncio.sleep(ROUND_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if not self.active:
                return
            timeout_text = f"⏰ Time's up! The answer was **{self.current_word}**. No points awarded this round."
            await self._advance(prior_announcement=timeout_text)

    async def handle_guess(self, message: discord.Message):
        if not self.active:
            return
        if message.author not in self.players:
            return

        content = message.content.strip()
        if not content or not content.replace(" ", "").isalpha():
            return

        async with self._lock:
            if not self.active:
                return
            if len(content) == 1:
                await self._handle_letter_guess(message, content.upper())
            else:
                await self._handle_word_guess(message, content)

    async def _handle_letter_guess(self, message: discord.Message, letter: str):
        author = message.author
        if letter in VOWELS:
            fire_and_forget(self.channel.send(f"Vowels are already revealed \u2014 `{self.build_display()}`"))
            return
        if letter in self.guessed_letters:
            return  # silently ignore repeat guesses to avoid spamming the channel
        self.guessed_letters.add(letter)

        if letter in self.current_word.upper():
            fire_and_forget(self.channel.send(
                f"✅ `{letter}` is in it! `{self.build_display()}`\n{self.wrong_letters_display()}"
            ))
            if self.is_fully_revealed():
                await self._award_point(author, completed_via_letter=True)
        else:
            self.wrong_letters.add(letter)
            fire_and_forget(message.add_reaction("❌"))

    async def _handle_word_guess(self, message: discord.Message, guess: str):
        author = message.author
        if guess.upper() == self.current_word.upper():
            self.guessed_letters = {ch.upper() for ch in self.current_word if ch.isalpha()}
            await self._award_point(author, completed_via_letter=False)
        else:
            fire_and_forget(message.add_reaction("❌"))

    async def _award_point(self, author: discord.Member, completed_via_letter: bool):
        if self.round_start_time is not None:
            self.guess_durations.append(time.monotonic() - self.round_start_time)
        self.scores[author.id] += 1
        verb = "completed the word with the final letter" if completed_via_letter else "guessed the full answer"
        announce_text = (
            f"🎉 **{author.display_name}** {verb}: **{self.current_word}**! Point awarded. "
            f"Score: {self.scores[author.id]}"
        )
        await self._advance(prior_announcement=announce_text)

    async def _advance(self, prior_announcement: str | None = None):
        # _advance() can run *inside* self._timeout_task (a natural round
        # timeout calls this directly) - cancelling it here would throw
        # CancelledError into this very coroutine at the next await below.
        if self._timeout_task and self._timeout_task is not asyncio.current_task():
            self._timeout_task.cancel()
        if self.round_num >= self.rounds_per_game:
            await self.end_game(prior_announcement=prior_announcement)
        else:
            await self.start_round(prior_announcement=prior_announcement)

    async def end_game(self, prior_announcement: str | None = None):
        self.active = False
        active_games.pop(self.channel.id, None)

        ranking = sorted(self.scores.items(), key=lambda x: x[1], reverse=True)
        lines = [f"{self.player_by_id(uid).display_name}: **{score}**" for uid, score in ranking]

        top_score = ranking[0][1]
        winners = [self.player_by_id(uid).display_name for uid, score in ranking if score == top_score]

        embed = discord.Embed(title="🏆 Game Over!", description="\n".join(lines), color=discord.Color.gold())
        if len(self.players) == 1:
            embed.add_field(name="Final Score", value=f"{winners[0]} finished with {top_score} point(s)!")
            if self.guess_durations:
                avg_seconds = sum(self.guess_durations) / len(self.guess_durations)
                guess_word = "guess" if len(self.guess_durations) == 1 else "guesses"
                embed.add_field(
                    name="Average Guess Time",
                    value=f"{avg_seconds:.2f}s per correct guess (across {len(self.guess_durations)} {guess_word})",
                    inline=False,
                )
        elif len(winners) == 1:
            embed.add_field(name="Winner", value=winners[0])
        else:
            embed.add_field(name="It's a tie!", value=", ".join(winners))

        if prior_announcement:
            await self.channel.send(prior_announcement)
        await self.channel.send(embed=embed)

    async def cancel(self, cancelled_by: discord.Member):
        self.active = False
        if self._timeout_task:
            self._timeout_task.cancel()
        active_games.pop(self.channel.id, None)
        await self.channel.send(f"🛑 Game cancelled by **{cancelled_by.display_name}**. No winner this round.")




# ---------------------------------------------------------------------------
# Blitz solo game modes
# ---------------------------------------------------------------------------


# Words with this many letters (or fewer) are considered "short" for the
# purposes of the Blitz starting reveal - short words only get a single
# random consonant revealed instead of a vowel + consonant, since a vowel
# alone already gives away a lot of a short word.
BLITZ_SHORT_WORD_LETTER_THRESHOLD = 5




def _least_frequent_letters(letter_counts: Counter, candidate_pool: set[str]) -> list[str]:
    """Given a Counter of letter -> occurrence count for a word, and a pool
    of letters to consider (e.g. VOWELS or the consonants), returns the
    letters from that pool that are present in the word and tied for the
    FEWEST occurrences. A letter that repeats more than others in the same
    pool is excluded, so the free reveal never lands on the word's most
    "obvious" letter. If every present letter in the pool ties (including
    the case where only one distinct letter from the pool appears at all),
    all of them are returned."""
    present_counts = {letter: count for letter, count in letter_counts.items() if letter in candidate_pool}
    if not present_counts:
        return []
    min_count = min(present_counts.values())
    return [letter for letter, count in present_counts.items() if count == min_count]




def pick_blitz_starting_letters(word: str) -> set[str]:
    """Reveals starting letters for a Blitz word. For words with more than
    BLITZ_SHORT_WORD_LETTER_THRESHOLD letters, reveals one random vowel and
    one random consonant that actually appear in the word (instead of all
    vowels), so the free starting info isn't the same predictable set every
    round. For words at or under that threshold, reveals only a single
    random consonant (no vowel) to raise the difficulty on short words.

    In both cases, the letter is chosen only from among the least-frequently
    -occurring letters in its category (vowel or consonant) within the word:
    a letter that repeats more than other vowels/consonants in the same word
    is never revealed, since that would give away more "for free" than a
    letter that only appears once. If every candidate ties on occurrence
    count (including words with no repeats at all, or where only one
    distinct vowel/consonant appears), the choice is random among them same
    as before.

    Falls back gracefully if the word happens to have no vowels or no
    consonants. Since guessed_letters is a set of letters (not positions),
    every occurrence of the revealed letter is displayed automatically."""
    letter_counts = Counter(ch.upper() for ch in word if ch.isalpha())
    letter_count = sum(letter_counts.values())

    starting: set[str] = set()
    is_short_word = letter_count <= BLITZ_SHORT_WORD_LETTER_THRESHOLD

    if is_short_word:
        consonant_candidates = _least_frequent_letters(letter_counts, CONSONANTS)
        if consonant_candidates:
            starting.add(random.choice(consonant_candidates))
        else:
            # No consonants at all (e.g. a word made entirely of vowels) -
            # fall back to a least-frequent vowel so something is revealed.
            vowel_candidates = _least_frequent_letters(letter_counts, VOWELS)
            if vowel_candidates:
                starting.add(random.choice(vowel_candidates))
    else:
        vowel_candidates = _least_frequent_letters(letter_counts, VOWELS)
        if vowel_candidates:
            starting.add(random.choice(vowel_candidates))
        consonant_candidates = _least_frequent_letters(letter_counts, CONSONANTS)
        if consonant_candidates:
            starting.add(random.choice(consonant_candidates))
    return starting




def pick_easy_blitz_starting_letters(word: str) -> set[str]:
    """Easy-mode reveal: every vowel that appears in the word is
    pre-revealed (not just one), matching the original simpler Blitz mode."""
    return {ch.upper() for ch in word if ch.isalpha() and ch.upper() in VOWELS}




class BlitzSpeedGame:
    """!blitz - guess a fixed number of words as fast as possible, timed to the millisecond."""

    def __init__(
        self,
        channel: discord.TextChannel,
        player: discord.Member,
        word_lists,
        words_to_guess: int = BLITZ_WORDS_TO_GUESS,
        difficulty: str = "hard",
    ):
        self.channel = channel
        self.player = player
        self.word_lists = word_lists
        self.words_to_guess = words_to_guess
        # REMOVED: mode parameter - always "blitz"
        # "easy" or "hard". Easy: all vowels pre-revealed, unlimited free
        # letter guesses (no time penalty), and the run is never saved to
        # the leaderboard/rolling average. Hard: the full current ruleset
        # (short-word single-consonant reveal, tiered escalating penalties)
        # and results are recorded as normal.
        self.difficulty = difficulty
        self.current_word = ""
        self.current_category = ""
        self.guessed_letters: set[str] = set()
        self.wrong_letters: set[str] = set()
        self.correct_count = 0
        self.active = True
        self.start_time: float | None = None
        self._lock = asyncio.Lock()
        self.word_start_time: float | None = None
        self.word_times: list[tuple[str, float, float]] = []
        self._idle_task: asyncio.Task | None = None
        # Wrong single-letter guesses add time here, per BLITZ_LETTER_TIER /
        # BLITZ_TIER_BASE_PENALTY. current_word_penalty resets each word;
        # total_penalty accumulates across the whole run and is folded into
        # the final score.
        self.current_word_penalty: float = 0.0
        self.total_penalty: float = 0.0
        # Tracks how many wrong guesses have landed in each penalty tier
        # *this round* (word). Each additional wrong guess within the same
        # tier in the same round costs 50% more than the last, to punish
        # spam-guessing a tier for max info. Resets every new word.
        self.tier_wrong_counts: dict[int, int] = {}

    def _reset_idle_timeout(self):
        """(Re)start the AFK timer. Any valid guess activity should call this."""
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        if self.active:
            self._idle_task = asyncio.create_task(self._idle_timeout())

    def _cancel_idle_timeout(self):
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
        self._idle_task = None

    async def _idle_timeout(self):
        try:
            await asyncio.sleep(BLITZ_IDLE_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if not self.active:
                return
            # For easy mode, do a clean cancel without penalty
            if self.difficulty == "easy":
                await self._clean_cancel(reason="⏰ Inactivity timeout (Easy mode – no penalty).")
                return
            # Same penalty formula as a mid-run player cancel.
            elapsed_so_far = (time.perf_counter() - self.start_time) if self.start_time is not None else 0.0
            words_remaining = self.words_to_guess - self.correct_count
            penalty_score = elapsed_so_far + self.total_penalty + (words_remaining * 60.0)  # CHANGE: 60s per word
            await self.end_game(forced_score=penalty_score, cancelled=True, idle=True)

    async def start(self):
        self.start_time = time.perf_counter()
        self._reset_idle_timeout()
        await self._next_word()

    def _prepare_next_word(self) -> discord.Embed:
        """Synchronously advances to the next word and builds its embed,
        without sending it. Kept separate from the send so callers can
        guarantee a preceding "word complete" message is fully sent first."""
        self.current_category, self.current_word = pick_word(self.word_lists)
        if self.difficulty == "easy":
            self.guessed_letters = pick_easy_blitz_starting_letters(self.current_word)
        else:
            self.guessed_letters = pick_blitz_starting_letters(self.current_word)
        self.wrong_letters = set()
        self.word_start_time = time.perf_counter()
        self.current_word_penalty = 0.0
        self.tier_wrong_counts = {}

        embed = discord.Embed(
            title=f"⚡ Speed Blitz ({'Easy' if self.difficulty == 'easy' else 'Hard'})! "
            f"Word {self.correct_count + 1} of {self.words_to_guess}",
            description=f"Category: **{self.current_category}**\n\n`{render_word(self.current_word, self.guessed_letters)}`",
            color=discord.Color.orange(),
        )
        embed.add_field(name="\u200b", value=format_wrong_letters(self.wrong_letters), inline=False)
        if self.difficulty == "easy":
            footer_text = (
                "🌱 Easy Mode — all vowels revealed to start. Guess as many letters as you want, "
                "no time penalty. (Not saved to the leaderboard.) Clock is running!"
            )
        else:
            letter_count = sum(1 for ch in self.current_word if ch.isalpha())
            if letter_count <= BLITZ_SHORT_WORD_LETTER_THRESHOLD:
                reveal_note = "One random consonant revealed to start (short word)."
            else:
                reveal_note = "One vowel + one consonant revealed to start."
            footer_text = (
                f"{reveal_note} Wrong single-letter guesses cost time (common letters cost more), "
                "and repeat wrong guesses in the same letter tier cost 50% more each time. Clock is running!"
            )
        embed.set_footer(text=footer_text)
        return embed

    async def _next_word(self, prior_announcement: str | None = None):
        embed = self._prepare_next_word()

        async def _dispatch():
            if prior_announcement:
                await self.channel.send(prior_announcement)
            await self.channel.send(embed=embed)
        fire_and_forget(_dispatch())

    async def handle_guess(self, message: discord.Message):
        if not self.active or message.author.id != self.player.id:
            return

        content = message.content.strip()
        if not content or not content.replace(" ", "").isalpha():
            return

        async with self._lock:
            if not self.active:
                return
            # Any valid guess attempt counts as activity and resets the AFK timer.
            self._reset_idle_timeout()
            if len(content) == 1:
                await self._handle_letter_guess(message, content.upper())
            else:
                await self._handle_word_guess(message, content)

    async def _handle_letter_guess(self, message: discord.Message, letter: str):
        if letter in self.guessed_letters:
            return  # already revealed (starting letter or previously guessed) - no penalty
        self.guessed_letters.add(letter)

        if letter in self.current_word.upper():
            fire_and_forget(self.channel.send(
                f"✅ `{letter}` is in it! `{render_word(self.current_word, self.guessed_letters)}`\n"
                f"{format_wrong_letters(self.wrong_letters)}"
            ))
            if word_fully_revealed(self.current_word, self.guessed_letters):
                await self._word_complete()
        else:
            self.wrong_letters.add(letter)
            if self.difficulty == "easy":
                penalty = 0.0
            else:
                tier = BLITZ_LETTER_TIER.get(letter)
                if tier is not None:
                    base_penalty = BLITZ_TIER_BASE_PENALTY[tier]
                    prior_wrong_in_tier = self.tier_wrong_counts.get(tier, 0)
                    # Each prior wrong guess in this tier this round adds
                    # another 50% of the base penalty on top.
                    penalty = base_penalty * (1 + 0.5 * prior_wrong_in_tier)
                    self.tier_wrong_counts[tier] = prior_wrong_in_tier + 1
                else:
                    penalty = 0.0
            self.current_word_penalty += penalty
            self.total_penalty += penalty
            if self.difficulty == "easy":
                feedback = f"❌ `{letter}` isn't in it \u2014 no penalty (easy mode)\n"
            else:
                feedback = f"❌ `{letter}` isn't in it \u2014 +{penalty:.1f}s penalty\n"
            fire_and_forget(self.channel.send(
                feedback + format_wrong_letters(self.wrong_letters)
            ))

    async def _handle_word_guess(self, message: discord.Message, guess: str):
        if guess.upper() == self.current_word.upper():
            self.guessed_letters = {ch.upper() for ch in self.current_word if ch.isalpha()}
            await self._word_complete()
        else:
            fire_and_forget(message.add_reaction("❌"))

    async def _word_complete(self):
        if self.word_start_time is not None:
            now = time.perf_counter()
            elapsed_word = (now - self.word_start_time) + self.current_word_penalty
            cumulative = (now - self.start_time) + self.total_penalty
            self.word_times.append((self.current_word, elapsed_word, cumulative))
        self.correct_count += 1
        if self.correct_count >= self.words_to_guess:
            await self.end_game()
        else:
            announce_text = f"🎉 **{self.current_word}**! {self.correct_count}/{self.words_to_guess} done."
            await self._next_word(prior_announcement=announce_text)

    async def end_game(self, forced_score: float | None = None, cancelled: bool = False, idle: bool = False):
        self.active = False
        self._cancel_idle_timeout()
        active_blitz_games.pop(self.channel.id, None)
        elapsed = forced_score if forced_score is not None else (
            (time.perf_counter() - self.start_time) + self.total_penalty
        )

        is_hard = self.difficulty == "hard"
        if is_hard and not cancelled and not idle:
            # Only record if it's hard and not a cancellation/timeout.
            # (For cancellations we don't record at all, we just send a penalty message)
            avg_score, games_counted, rank, recent_scores = await record_score(
                "blitz", self.player.id, self.player.display_name, elapsed
            )
            window_note = (
                f"last {games_counted} game{'s' if games_counted != 1 else ''}"
                if games_counted < ROLLING_AVERAGE_WINDOW
                else f"last {ROLLING_AVERAGE_WINDOW} games"
            )
            scores_display = ", ".join(f"{s:.3f}s" for s in recent_scores)

        if cancelled or idle:
            if idle:
                title = "🛑 Speed Blitz Timed Out"
                if self.difficulty == "easy":
                    reason = f"**{self.player.display_name}** went inactive for {BLITZ_IDLE_TIMEOUT_SECONDS}s. (Easy mode – no penalty applied.)"
                else:
                    reason = (
                        f"**{self.player.display_name}** went inactive for {BLITZ_IDLE_TIMEOUT_SECONDS}s. "
                        f"Penalty score recorded: **{elapsed:.3f}s** "
                        f"(time so far + {self.total_penalty:.1f}s in letter penalties + "
                        f"{self.words_to_guess - self.correct_count} remaining word(s) × 60s)."
                    )
            else:
                title = "🛑 Speed Blitz Cancelled"
                if self.difficulty == "easy":
                    reason = f"**{self.player.display_name}** stopped the round early. (Easy mode – no penalty applied.)"
                else:
                    reason = (
                        f"**{self.player.display_name}** stopped the round early. "
                        f"Penalty score recorded: **{elapsed:.3f}s** "
                        f"(time so far + {self.total_penalty:.1f}s in letter penalties + "
                        f"{self.words_to_guess - self.correct_count} remaining word(s) × 60s)."
                    )
            embed = discord.Embed(title=title, description=reason, color=discord.Color.red())
            # We do NOT record any score for cancellations/timeouts.
        else:
            embed = discord.Embed(
                title="🏁 Speed Blitz Complete!",
                description=(
                    f"**{self.player.display_name}** guessed **{self.current_word}** to finish "
                    f"{self.words_to_guess} word(s)!"
                ),
                color=discord.Color.gold(),
            )
            embed.add_field(name="This Game", value=f"{elapsed:.3f} seconds")
            if self.total_penalty > 0:
                embed.add_field(name="Penalty Time", value=f"+{self.total_penalty:.1f}s from wrong letter guesses")
            for name, value in build_time_interval_fields(self.word_times):
                embed.add_field(name=name, value=value, inline=False)

        if is_hard and not cancelled and not idle:
            # Only show rolling average and rank for completed hard games
            embed.add_field(name="Rolling Average", value=f"{format_score(avg_score)} ({window_note})")
            embed.add_field(
                name=f"Last {games_counted} Score{'s' if games_counted != 1 else ''}",
                value=scores_display or "—",
                inline=False,
            )
            embed.add_field(
                name="\u200b",
                value=f"🌍 You're currently #{rank} on the leaderboard! (`!lb` to view)",
                inline=False,
            )
        elif self.difficulty == "easy":
            embed.add_field(
                name="\u200b",
                value="🌱 Easy Mode — this score isn't saved to the leaderboard or your rolling average. "
                "Play `!blitz` in Hard mode for that to count!",
                inline=False,
            )
        # For cancellations, we don't add extra fields.

        await self.channel.send(embed=embed)

    async def _clean_cancel(self, reason: str | None = None):
        """Cleanly cancel the game without recording any score (used for Easy mode cancellation)."""
        self.active = False
        self._cancel_idle_timeout()
        active_blitz_games.pop(self.channel.id, None)
        await self.channel.send(f"🛑 Speed Blitz cancelled. {reason or ''}")

    async def cancel(self, cancelled_by: discord.Member):
        # Only the player who started the blitz can receive a penalty score.
        # Moderators / others stopping the game just cancel cleanly.
        is_starter = cancelled_by.id == self.player.id
        elapsed_so_far = (time.perf_counter() - self.start_time) if self.start_time is not None else 0.0
        words_remaining = self.words_to_guess - self.correct_count

        # Grace period: cancel within 5s of start with zero words completed
        # is treated as a misclick and is not penalized / not recorded.
        in_grace = (
            is_starter
            and elapsed_so_far < 5.0
            and self.correct_count == 0
        )

        # If easy mode, always clean cancel (no penalty, no score)
        if self.difficulty == "easy":
            await self._clean_cancel(reason="Easy mode – no penalty applied.")
            return

        if is_starter and not in_grace:
            # Penalty: time elapsed so far + accumulated letter penalties + (words remaining) × 60s  # CHANGE: 60s
            penalty_score = elapsed_so_far + self.total_penalty + (words_remaining * 60.0)
            await self.end_game(forced_score=penalty_score, cancelled=True)
            return

        # Clean cancel (moderator, or starter within grace window)
        self.active = False
        self._cancel_idle_timeout()
        active_blitz_games.pop(self.channel.id, None)
        if in_grace:
            await self.channel.send(
                f"🛑 Speed Blitz cancelled by **{cancelled_by.display_name}** "
                f"(within the 5s grace period — no penalty applied)."
            )
        else:
            await self.channel.send(f"🛑 Speed Blitz cancelled by **{cancelled_by.display_name}**.")




# ---------------------------------------------------------------------------
# Classic solo hangman
# ---------------------------------------------------------------------------


class ClassicGame:
    """Solo classic hangman – 7 wrong guesses, 150s per round, no pre‑revealed letters."""
    def __init__(self, channel: discord.TextChannel, player: discord.Member, word_lists: dict[str, list[str]]):
        self.channel = channel
        self.player = player
        self.word_lists = word_lists
        self.active = True
        self.score = 0
        self.current_word = ""
        self.current_category = ""
        self.guessed_letters: set[str] = set()
        self.wrong_letters: set[str] = set()
        self.wrong_guesses = 0
        self.max_wrong = 7  # CHANGED: from 6 to 7
        self.round_start_time: float | None = None
        self._timeout_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    def render_display(self) -> str:
        return render_word(self.current_word, self.guessed_letters)

    def is_fully_revealed(self) -> bool:
        return word_fully_revealed(self.current_word, self.guessed_letters)

    async def start_round(self, prior_announcement: str | None = None):
        self.current_category, self.current_word = pick_word(self.word_lists)
        self.guessed_letters = set()
        self.wrong_letters = set()
        self.wrong_guesses = 0
        self.round_start_time = time.monotonic()

        embed = discord.Embed(
            title="Classic Hangman",
            description=f"Category: **{self.current_category}**\n\n`{self.render_display()}`",
            color=discord.Color.blue(),
        )
        embed.add_field(
            name="Wrong guesses",
            value=f"{self.wrong_guesses}/{self.max_wrong}\nLetters: {self.format_wrong_letters()}",
            inline=False,
        )
        embed.set_footer(text=f"Guess a letter or the full word. {CLASSIC_ROUND_TIMEOUT}s per round.")

        if prior_announcement:
            await self.channel.send(prior_announcement)
        await self.channel.send(embed=embed)

        if self._timeout_task:
            self._timeout_task.cancel()
        self._timeout_task = asyncio.create_task(self._round_timeout())

    def format_wrong_letters(self) -> str:
        return ", ".join(sorted(self.wrong_letters)) if self.wrong_letters else "None yet"

    async def _round_timeout(self):
        try:
            await asyncio.sleep(CLASSIC_ROUND_TIMEOUT)
        except asyncio.CancelledError:
            return
        async with self._lock:
            if not self.active:
                return
            await self.end_game(reason="⏰ Time's up! You took too long on a word.", failed=True)

    async def handle_guess(self, message: discord.Message):
        if not self.active or message.author.id != self.player.id:
            return
        content = message.content.strip()
        if not content or not content.replace(" ", "").isalpha():
            return

        async with self._lock:
            if not self.active:
                return
            if len(content) == 1:
                await self._handle_letter_guess(content.upper())
            else:
                await self._handle_word_guess(content)

    async def _handle_letter_guess(self, letter: str):
        if letter in self.guessed_letters:
            return
        self.guessed_letters.add(letter)

        if letter in self.current_word.upper():
            if self.is_fully_revealed():
                self.score += 1
                announce = f"✅ You completed the word: **{self.current_word}**! +1 point (total: {self.score})"
                await self._advance(prior_announcement=announce)
            else:
                await self.channel.send(
                    f"✅ `{letter}` is in it!\n`{self.render_display()}`\n"
                    f"Wrong guesses: {self.wrong_guesses}/{self.max_wrong} – {self.format_wrong_letters()}"
                )
        else:
            self.wrong_guesses += 1
            self.wrong_letters.add(letter)
            await self.channel.send(
                f"❌ `{letter}` is not in the word. Wrong guesses: {self.wrong_guesses}/{self.max_wrong}"
            )
            if self.wrong_guesses >= self.max_wrong:
                await self.end_game(
                    reason=f"💀 You used all {self.max_wrong} wrong guesses! The word was **{self.current_word}**.",
                    failed=True,
                )

    async def _handle_word_guess(self, guess: str):
        if guess.upper() == self.current_word.upper():
            self.score += 1
            self.guessed_letters = {ch.upper() for ch in self.current_word if ch.isalpha()}
            announce = f"🎉 Correct! The word was **{self.current_word}**! +1 point (total: {self.score})"
            await self._advance(prior_announcement=announce)
        else:
            self.wrong_guesses += 1
            await self.channel.send(f"❌ That's not the word. Wrong guesses: {self.wrong_guesses}/{self.max_wrong}")
            if self.wrong_guesses >= self.max_wrong:
                await self.end_game(
                    reason=f"💀 You used all {self.max_wrong} wrong guesses! The word was **{self.current_word}**.",
                    failed=True,
                )

    async def _advance(self, prior_announcement: str | None = None):
        if self._timeout_task:
            self._timeout_task.cancel()
        await self.start_round(prior_announcement)

    async def end_game(self, reason: str | None = None, failed: bool = False):
        self.active = False
        if self._timeout_task:
            self._timeout_task.cancel()
        active_classic_games.pop(self.channel.id, None)

        pb, games_played, rank = await record_classic_score(
            self.player.id, self.player.display_name, self.score
        )

        embed = discord.Embed(
            title="🏁 Classic Hangman Over!",
            description=reason or "Game ended.",
            color=discord.Color.red() if failed else discord.Color.gold(),
        )
        embed.add_field(name="Words Guessed", value=str(self.score))
        embed.add_field(name="Personal Best", value=f"{pb} words (in {games_played} games)")
        embed.add_field(name="Leaderboard Rank", value=f"#{rank} overall")
        await self.channel.send(embed=embed)

    async def cancel(self, cancelled_by: discord.Member):
        if cancelled_by.id != self.player.id:
            await self.channel.send("Only the player who started the game can stop it.")
            return
        self.active = False
        if self._timeout_task:
            self._timeout_task.cancel()
        active_classic_games.pop(self.channel.id, None)
        await self.end_game(reason="🛑 Game stopped by player.", failed=False)




# ---------------------------------------------------------------------------
# Commands & events
# ---------------------------------------------------------------------------


@bot.command(name="hangman")
async def hangman_start(ctx: commands.Context, rounds: int = DEFAULT_ROUNDS_PER_GAME):
    if ctx.channel.id in active_games or ctx.channel.id in active_blitz_games or ctx.channel.id in active_classic_games:
        await ctx.send("A game is already running in this channel! Finish it before starting a new one.")
        return

    if not (MIN_ROUNDS <= rounds <= MAX_ROUNDS):
        await ctx.send(f"Rounds must be between {MIN_ROUNDS} and {MAX_ROUNDS}. Try `!hangman {DEFAULT_ROUNDS_PER_GAME}`.")
        return

    # --- Category selection view (buttons) ---
    select_view = CategorySelectView(author=ctx.author, timeout=30)
    msg = await ctx.send(
        f"**{ctx.author.display_name}**, choose the word list for this Hangman game:",
        view=select_view
    )
    select_view.message = msg
    await select_view.wait()

    # After selection or timeout, get the chosen category (None means All)
    chosen_category = select_view.selected_category  # None = All

    try:
        if chosen_category is None:
            word_lists = load_word_lists()  # All categories
        else:
            # Load only the chosen category
            full_lists = load_word_lists()
            if chosen_category not in full_lists:
                await ctx.send(f"⚠️ Category '{chosen_category}' not found. Defaulting to All.")
                word_lists = full_lists
            else:
                word_lists = {chosen_category: full_lists[chosen_category]}
    except (FileNotFoundError, ValueError) as e:
        await ctx.send(f"⚠️ Can't start a game: {e}")
        return

    # Now create the join view
    view = JoinView(host=ctx.author, timeout=JOIN_WINDOW_SECONDS)
    await ctx.send(
        f"🎮 **{ctx.author.display_name}** started a Pokémon Hangman game! ({rounds} round{'s' if rounds != 1 else ''})\n"
        f"Click **Join Game** below to compete. Starting in {JOIN_WINDOW_SECONDS} seconds...",
        view=view,
    )
    await view.wait()

    players = view.players
    game = HangmanGame(ctx.channel, players, word_lists, host=ctx.author, rounds_per_game=rounds)
    active_games[ctx.channel.id] = game

    mode = "Solo" if len(players) == 1 else "Competition"
    names = ", ".join(p.display_name for p in players)
    await ctx.send(f"**{mode} mode!** Players: {names}\n{rounds} rounds. Good luck!")
    await game.start_round()




@hangman_start.error
async def hangman_error(ctx, error):
    if isinstance(error, commands.BadArgument):
        await ctx.send(f"Rounds must be a whole number, e.g. `!hangman 8`. Defaulting is `!hangman` for {DEFAULT_ROUNDS_PER_GAME} rounds.")
        return
    await ctx.send(f"Something went wrong starting the game: {error}")




# ---------------------------------------------------------------------------
# Category-locked practice modes - regular hangman, but every word comes
# from just one category. Shares all the same mechanics (join window,
# scoring, solo average-guess-time, etc.) via the shared helper below.
# ---------------------------------------------------------------------------


async def _start_category_hangman(
    ctx: commands.Context,
    rounds: int,
    category_key: str,
    category_label: str,
    command_name: str,
):
    if ctx.channel.id in active_games or ctx.channel.id in active_blitz_games or ctx.channel.id in active_classic_games:
        await ctx.send("A game is already running in this channel! Finish it before starting a new one.")
        return

    if not (MIN_ROUNDS <= rounds <= MAX_ROUNDS):
        await ctx.send(f"Rounds must be between {MIN_ROUNDS} and {MAX_ROUNDS}. Try `!{command_name} {DEFAULT_ROUNDS_PER_GAME}`.")
        return

    try:
        word_lists = {category_key: load_word_lists()[category_key]}
    except (FileNotFoundError, ValueError) as e:
        await ctx.send(f"⚠️ Can't start a game: {e}")
        return

    view = JoinView(host=ctx.author, timeout=JOIN_WINDOW_SECONDS)
    await ctx.send(
        f"🎮 **{ctx.author.display_name}** started a {category_label}-only Hangman game! "
        f"({rounds} round{'s' if rounds != 1 else ''})\n"
        f"Click **Join Game** below to compete. Starting in {JOIN_WINDOW_SECONDS} seconds...",
        view=view,
    )
    await view.wait()

    players = view.players
    game = HangmanGame(ctx.channel, players, word_lists, host=ctx.author, rounds_per_game=rounds)
    active_games[ctx.channel.id] = game

    mode = "Solo" if len(players) == 1 else "Competition"
    names = ", ".join(p.display_name for p in players)
    await ctx.send(f"**{mode} mode!** Players: {names}\n{rounds} rounds of {category_label} only. Good luck!")
    await game.start_round()




async def _category_hangman_error(ctx: commands.Context, error, command_name: str):
    if isinstance(error, commands.BadArgument):
        await ctx.send(
            f"Rounds must be a whole number, e.g. `!{command_name} 8`. "
            f"Defaulting is `!{command_name}` for {DEFAULT_ROUNDS_PER_GAME} rounds."
        )
        return
    await ctx.send(f"Something went wrong starting the game: {error}")




@bot.command(name="hangmanpokemon")
async def hangman_pokemon(ctx: commands.Context, rounds: int = DEFAULT_ROUNDS_PER_GAME):
    await _start_category_hangman(ctx, rounds, "Pokémon", "Pokémon", "hangmanpokemon")




@hangman_pokemon.error
async def hangman_pokemon_error(ctx, error):
    await _category_hangman_error(ctx, error, "hangmanpokemon")




@bot.command(name="hangmanitems")
async def hangman_items(ctx: commands.Context, rounds: int = DEFAULT_ROUNDS_PER_GAME):
    await _start_category_hangman(ctx, rounds, "Item", "Items", "hangmanitems")




@hangman_items.error
async def hangman_items_error(ctx, error):
    await _category_hangman_error(ctx, error, "hangmanitems")




@bot.command(name="hangmanmoves")
async def hangman_moves(ctx: commands.Context, rounds: int = DEFAULT_ROUNDS_PER_GAME):
    await _start_category_hangman(ctx, rounds, "Move", "Moves", "hangmanmoves")




@hangman_moves.error
async def hangman_moves_error(ctx, error):
    await _category_hangman_error(ctx, error, "hangmanmoves")




@bot.command(name="hangmanabilities")
async def hangman_abilities(ctx: commands.Context, rounds: int = DEFAULT_ROUNDS_PER_GAME):
    await _start_category_hangman(ctx, rounds, "Ability", "Abilities", "hangmanabilities")




@hangman_abilities.error
async def hangman_abilities_error(ctx, error):
    await _category_hangman_error(ctx, error, "hangmanabilities")




@bot.command(name="hangmanstop")
async def hangman_stop(ctx: commands.Context):
    game = active_games.get(ctx.channel.id)
    if game:
        is_host = ctx.author.id == game.host.id
        can_moderate = ctx.author.guild_permissions.manage_messages
        if not (is_host or can_moderate):
            await ctx.send("Only the host who started the game (or a moderator) can stop it.")
            return
        await game.cancel(ctx.author)
        return

    blitz = active_blitz_games.get(ctx.channel.id)
    if blitz:
        # Only the player who started the blitz can stop it (no moderator override).
        if ctx.author.id != blitz.player.id:
            await ctx.send("Only the player who started the blitz round can stop it.")
            return
        await blitz.cancel(ctx.author)
        return

    classic = active_classic_games.get(ctx.channel.id)
    if classic:
        if ctx.author.id != classic.player.id:
            await ctx.send("Only the player who started the classic game can stop it.")
            return
        await classic.cancel(ctx.author)
        return

    await ctx.send("There's no game running in this channel.")




async def _prompt_blitz_difficulty(ctx: commands.Context) -> str:
    """Sends the Easy/Hard mode picker and waits for the player's choice
    (defaults to "hard" if the view times out with no click). Returns
    "easy" or "hard"."""
    view = BlitzDifficultyView(player=ctx.author)
    msg = await ctx.send(
        f"**{ctx.author.display_name}**, pick your Speed Blitz mode:\n"
        f"🌱 **Easy** \u2014 all vowels revealed, unlimited free letter guesses, no time penalty. "
        f"Not saved to the leaderboard.\n"
        f"🔥 **Hard** \u2014 minimal reveal, tiered wrong-guess penalties that escalate on repeats. "
        f"Counts toward your rolling average and the leaderboard.",
        view=view,
    )
    view.message = msg
    await view.wait()
    return view.difficulty




@bot.command(name="blitz")
async def blitz_start(ctx: commands.Context):
    if ctx.channel.id in active_games or ctx.channel.id in active_blitz_games or ctx.channel.id in active_classic_games:
        await ctx.send("A game is already running in this channel! Finish it before starting a new one.")
        return

    try:
        word_lists = load_word_lists(BLITZ_DATA_FILES)
    except (FileNotFoundError, ValueError) as e:
        await ctx.send(f"⚠️ Can't start a game: {e}")
        return

    difficulty = await _prompt_blitz_difficulty(ctx)

    if ctx.channel.id in active_games or ctx.channel.id in active_blitz_games or ctx.channel.id in active_classic_games:
        await ctx.send(
            "Another game started in this channel while you were picking a mode \u2014 "
            "try again once it's done."
        )
        return

    game = BlitzSpeedGame(ctx.channel, ctx.author, word_lists, difficulty=difficulty)
    active_blitz_games[ctx.channel.id] = game
    difficulty_label = "🌱 Easy" if difficulty == "easy" else "🔥 Hard"
    await ctx.send(
        f"⚡ **{ctx.author.display_name}** started a Speed Blitz ({difficulty_label} mode)! "
        f"Guess {BLITZ_WORDS_TO_GUESS} words as fast as you can \u2014 the clock is running down to "
        f"the millisecond. Go!"
    )
    await game.start()


# REMOVED: !blitzp command entirely


@bot.command(name="classic")
async def classic_start(ctx: commands.Context):
    if ctx.channel.id in active_games or ctx.channel.id in active_blitz_games or ctx.channel.id in active_classic_games:
        await ctx.send("A game is already running in this channel! Finish it before starting a new one.")
        return

    try:
        word_lists = load_word_lists()  # uses all four categories
    except (FileNotFoundError, ValueError) as e:
        await ctx.send(f"⚠️ Can't start a game: {e}")
        return

    game = ClassicGame(ctx.channel, ctx.author, word_lists)
    active_classic_games[ctx.channel.id] = game
    await ctx.send(f"🧩 **{ctx.author.display_name}** started a Classic Hangman game! You have 7 wrong guesses per word and 150s per round. Good luck!")
    await game.start_round()




@bot.command(name="pb")
async def personal_bests(ctx: commands.Context):
    blitz_avg = await get_rolling_average("blitz", ctx.author.id)
    blitz_best = await get_best_scores("blitz", ctx.author.id, limit=5)
    classic_pb, classic_games = await get_classic_pb(ctx.author.id)

    def render_avg(result: tuple[float, int] | None, command_name: str) -> str:
        if result is None:
            return f"No scores yet \u2014 try `!{command_name}`!"
        avg_score, games_counted = result
        window_note = (
            f"last {games_counted} game{'s' if games_counted != 1 else ''}"
            if games_counted < ROLLING_AVERAGE_WINDOW
            else f"last {ROLLING_AVERAGE_WINDOW} games"
        )
        return f"{format_score(avg_score)} ({window_note})"

    def render_best(scores: list[float], command_name: str) -> str:
        if not scores:
            return f"No scores yet \u2014 try `!{command_name}`!"
        return ", ".join(f"{s:.3f}s" for s in scores)

    embed = discord.Embed(title=f"🏅 {ctx.author.display_name}'s Stats", color=discord.Color.purple())
    embed.add_field(name="⚡ Blitz — Rolling Average", value=render_avg(blitz_avg, "blitz"), inline=False)
    embed.add_field(name="⚡ Blitz — Best 5 Scores", value=render_best(blitz_best, "blitz"), inline=False)
    embed.add_field(name="🧩 Classic Hangman — Personal Best", value=f"{classic_pb} words (in {classic_games} games)", inline=False)
    await ctx.send(embed=embed)




def format_classic_lines(rows: list[tuple[str, int, int]]) -> str:
    if not rows:
        return "_No scores yet_"
    return "\n".join(
        f"**{i}.** {username} \u2014 {score} words (in {games} game{'s' if games != 1 else ''})"
        for i, (username, score, games) in enumerate(rows, start=1)
    )




@bot.command(name="lb")
async def leaderboard(ctx: commands.Context):
    blitz_rows = await get_average_leaderboard("blitz")
    classic_rows = await get_classic_leaderboard()

    def format_blitz_lines(rows: list[tuple[str, float, int]]) -> str:
        if not rows:
            return "_No scores yet_"
        return "\n".join(
            f"**{i}.** {username} \u2014 {format_score(avg_score)} (avg of {games_counted})"
            for i, (username, avg_score, games_counted) in enumerate(rows, start=1)
        )

    embed = discord.Embed(
        title="🌍 Leaderboards",
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="⚡ Blitz — Rolling Average (last 5 games)",
        value=format_blitz_lines(blitz_rows) if blitz_rows else "_No scores yet — try `!blitz`!_",
        inline=False,
    )
    embed.add_field(
        name="🧩 Classic Hangman — Personal Best",
        value=format_classic_lines(classic_rows),
        inline=False,
    )
    await ctx.send(embed=embed)




@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    game = active_games.get(message.channel.id)
    if game and game.active:
        await game.handle_guess(message)
        return

    blitz = active_blitz_games.get(message.channel.id)
    if blitz and blitz.active:
        await blitz.handle_guess(message)
        return

    classic = active_classic_games.get(message.channel.id)
    if classic and classic.active:
        await classic.handle_guess(message)
        return




@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print(f"Blitz leaderboard DB: {os.path.abspath(BLITZ_DB_PATH)}")
    print("Pokemon Hangman bot is ready. Use !hangman in a server to start a game.")




if __name__ == "__main__":
    init_db()
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_BOT_TOKEN environment variable is not set. "
            "Set it to your bot's token before running this script."
        )
    bot.run(token)