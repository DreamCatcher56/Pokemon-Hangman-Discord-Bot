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
ROUND_TIMEOUT_SECONDS = 100
VOWELS = set("AEIOU")

BLITZ_WORDS_TO_GUESS = 5
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
        conn.execute("DELETE FROM blitz_scores WHERE mode IN ('blitz1', 'blitz2', 'blitz2p')")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_blitz_mode_score ON blitz_scores(mode, score)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_blitz_mode_user ON blitz_scores(mode, user_id)")

        # Lifetime personal stats (words solved + games fully completed by mode).
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_stats (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                total_words_solved INTEGER NOT NULL DEFAULT 0,
                games_completed_hangman INTEGER NOT NULL DEFAULT 0,
                games_completed_blitz INTEGER NOT NULL DEFAULT 0,
                games_completed_blitzp INTEGER NOT NULL DEFAULT 0
            )
            """
        )
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


# ---------------------------------------------------------------------------
# Lifetime personal stats (words solved + games completed)
# ---------------------------------------------------------------------------

def _ensure_user_stats_row(conn: sqlite3.Connection, user_id: int, username: str):
    conn.execute(
        """
        INSERT INTO user_stats (user_id, username, total_words_solved,
                                games_completed_hangman, games_completed_blitz, games_completed_blitzp)
        VALUES (?, ?, 0, 0, 0, 0)
        ON CONFLICT(user_id) DO UPDATE SET username = excluded.username
        """,
        (user_id, username),
    )


def _increment_words_solved_sync(user_id: int, username: str, count: int = 1):
    conn = _get_connection()
    try:
        _ensure_user_stats_row(conn, user_id, username)
        conn.execute(
            "UPDATE user_stats SET total_words_solved = total_words_solved + ?, username = ? WHERE user_id = ?",
            (count, username, user_id),
        )
        conn.commit()
    finally:
        conn.close()


async def increment_words_solved(user_id: int, username: str, count: int = 1):
    """Adds to a player's lifetime correct-word total. Called whenever that
    player is the one who solves a word (regular hangman or blitz), including
    words solved before an early cancel."""
    await asyncio.to_thread(_increment_words_solved_sync, user_id, username, count)


def _increment_games_completed_sync(user_id: int, username: str, mode: str):
    """mode is one of: 'hangman', 'blitz', 'blitzp'."""
    column = {
        "hangman": "games_completed_hangman",
        "blitz": "games_completed_blitz",
        "blitzp": "games_completed_blitzp",
    }.get(mode)
    if column is None:
        raise ValueError(f"Unknown games-completed mode: {mode!r}")
    conn = _get_connection()
    try:
        _ensure_user_stats_row(conn, user_id, username)
        conn.execute(
            f"UPDATE user_stats SET {column} = {column} + 1, username = ? WHERE user_id = ?",
            (username, user_id),
        )
        conn.commit()
    finally:
        conn.close()


async def increment_games_completed(user_id: int, username: str, mode: str):
    """Increments the fully-completed game counter for a mode. Only call this
    when a game actually finishes all its rounds/words (not on cancel/penalty)."""
    await asyncio.to_thread(_increment_games_completed_sync, user_id, username, mode)


def _get_user_stats_sync(user_id: int) -> dict | None:
    conn = _get_connection()
    try:
        row = conn.execute(
            """
            SELECT total_words_solved, games_completed_hangman,
                   games_completed_blitz, games_completed_blitzp
            FROM user_stats WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "total_words_solved": row[0],
            "games_completed_hangman": row[1],
            "games_completed_blitz": row[2],
            "games_completed_blitzp": row[3],
        }
    finally:
        conn.close()


async def get_user_stats(user_id: int) -> dict | None:
    return await asyncio.to_thread(_get_user_stats_sync, user_id)


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
        # Lifetime word counter — counts even if the game is later cancelled.
        await increment_words_solved(author.id, author.display_name)
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

        # Fully completed regular hangman — count for every participant.
        for player in self.players:
            await increment_games_completed(player.id, player.display_name, "hangman")

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

class BlitzSpeedGame:
    """!blitz / !blitzp - guess a fixed number of words as fast as possible, timed to the millisecond."""

    def __init__(
        self,
        channel: discord.TextChannel,
        player: discord.Member,
        word_lists,
        words_to_guess: int = BLITZ_WORDS_TO_GUESS,
        mode: str = "blitz",
    ):
        self.channel = channel
        self.player = player
        self.word_lists = word_lists
        self.words_to_guess = words_to_guess
        self.mode = mode  # "blitz" or "blitzp" - which leaderboard/rolling average this run counts toward
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

    async def start(self):
        self.start_time = time.perf_counter()
        await self._next_word()

    def _prepare_next_word(self) -> discord.Embed:
        """Synchronously advances to the next word and builds its embed,
        without sending it. Kept separate from the send so callers can
        guarantee a preceding "word complete" message is fully sent first."""
        self.current_category, self.current_word = pick_word(self.word_lists)
        self.guessed_letters = set(VOWELS)
        self.wrong_letters = set()
        self.word_start_time = time.perf_counter()

        embed = discord.Embed(
            title=f"⚡ Speed Blitz! Word {self.correct_count + 1} of {self.words_to_guess}",
            description=f"Category: **{self.current_category}**\n\n`{render_word(self.current_word, self.guessed_letters)}`",
            color=discord.Color.orange(),
        )
        embed.add_field(name="\u200b", value=format_wrong_letters(self.wrong_letters), inline=False)
        embed.set_footer(text="Vowels are pre-filled. Type a letter or the full answer. Clock is running!")
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
            if len(content) == 1:
                await self._handle_letter_guess(message, content.upper())
            else:
                await self._handle_word_guess(message, content)

    async def _handle_letter_guess(self, message: discord.Message, letter: str):
        if letter in VOWELS:
            fire_and_forget(self.channel.send(f"Vowels are already revealed \u2014 `{render_word(self.current_word, self.guessed_letters)}`"))
            return
        if letter in self.guessed_letters:
            return
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
            fire_and_forget(message.add_reaction("❌"))

    async def _handle_word_guess(self, message: discord.Message, guess: str):
        if guess.upper() == self.current_word.upper():
            self.guessed_letters = {ch.upper() for ch in self.current_word if ch.isalpha()}
            await self._word_complete()
        else:
            fire_and_forget(message.add_reaction("❌"))

    async def _word_complete(self):
        if self.word_start_time is not None:
            now = time.perf_counter()
            elapsed_word = now - self.word_start_time
            cumulative = now - self.start_time
            self.word_times.append((self.current_word, elapsed_word, cumulative))
        self.correct_count += 1
        # Lifetime word counter — counts even if the run is later cancelled.
        await increment_words_solved(self.player.id, self.player.display_name)
        if self.correct_count >= self.words_to_guess:
            await self.end_game()
        else:
            announce_text = f"🎉 **{self.current_word}**! {self.correct_count}/{self.words_to_guess} done."
            await self._next_word(prior_announcement=announce_text)

    async def end_game(self, forced_score: float | None = None, cancelled: bool = False):
        self.active = False
        active_blitz_games.pop(self.channel.id, None)
        elapsed = forced_score if forced_score is not None else (time.perf_counter() - self.start_time)

        # Only fully finished runs (not penalty cancels) count as a completed game.
        if not cancelled:
            await increment_games_completed(self.player.id, self.player.display_name, self.mode)

        avg_score, games_counted, rank, recent_scores = await record_score(
            self.mode, self.player.id, self.player.display_name, elapsed
        )
        window_note = (
            f"last {games_counted} game{'s' if games_counted != 1 else ''}"
            if games_counted < ROLLING_AVERAGE_WINDOW
            else f"last {ROLLING_AVERAGE_WINDOW} games"
        )

        # recent_scores is newest-first (ORDER BY id DESC); keep that order
        scores_display = ", ".join(f"{s:.3f}s" for s in recent_scores)

        if cancelled:
            embed = discord.Embed(
                title="🛑 Speed Blitz Cancelled (Penalty Applied)",
                description=(
                    f"**{self.player.display_name}** stopped the round early. "
                    f"Penalty score recorded: **{elapsed:.3f}s** "
                    f"(time so far + {self.words_to_guess - self.correct_count} remaining word(s) × 30s)."
                ),
                color=discord.Color.red(),
            )
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
            for name, value in build_time_interval_fields(self.word_times):
                embed.add_field(name=name, value=value, inline=False)

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
        await self.channel.send(embed=embed)

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

        if is_starter and not in_grace:
            # Penalty: time elapsed so far + (words remaining) × 30s
            penalty_score = elapsed_so_far + (words_remaining * 30.0)
            await self.end_game(forced_score=penalty_score, cancelled=True)
            return

        # Clean cancel (moderator, or starter within grace window)
        self.active = False
        active_blitz_games.pop(self.channel.id, None)
        if in_grace:
            await self.channel.send(
                f"🛑 Speed Blitz cancelled by **{cancelled_by.display_name}** "
                f"(within the 5s grace period — no penalty applied)."
            )
        else:
            await self.channel.send(f"🛑 Speed Blitz cancelled by **{cancelled_by.display_name}**.")


# ---------------------------------------------------------------------------
# Commands & events
# ---------------------------------------------------------------------------

@bot.command(name="hangman")
async def hangman_start(ctx: commands.Context, rounds: int = DEFAULT_ROUNDS_PER_GAME):
    if ctx.channel.id in active_games or ctx.channel.id in active_blitz_games:
        await ctx.send("A game is already running in this channel! Finish it before starting a new one.")
        return

    if not (MIN_ROUNDS <= rounds <= MAX_ROUNDS):
        await ctx.send(f"Rounds must be between {MIN_ROUNDS} and {MAX_ROUNDS}. Try `!hangman {DEFAULT_ROUNDS_PER_GAME}`.")
        return

    try:
        word_lists = load_word_lists()
    except (FileNotFoundError, ValueError) as e:
        await ctx.send(f"⚠️ Can't start a game: {e}")
        return

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
    if ctx.channel.id in active_games or ctx.channel.id in active_blitz_games:
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

    await ctx.send("There's no game running in this channel.")


@bot.command(name="blitz")
async def blitz_start(ctx: commands.Context):
    if ctx.channel.id in active_games or ctx.channel.id in active_blitz_games:
        await ctx.send("A game is already running in this channel! Finish it before starting a new one.")
        return

    try:
        word_lists = load_word_lists(BLITZ_DATA_FILES)
    except (FileNotFoundError, ValueError) as e:
        await ctx.send(f"⚠️ Can't start a game: {e}")
        return

    game = BlitzSpeedGame(ctx.channel, ctx.author, word_lists, mode="blitz")
    active_blitz_games[ctx.channel.id] = game
    await ctx.send(
        f"⚡ **{ctx.author.display_name}** started a Speed Blitz! Guess {BLITZ_WORDS_TO_GUESS} words as fast "
        f"as you can \u2014 the clock is running down to the millisecond. Go!"
    )
    await game.start()


@bot.command(name="blitzp")
async def blitz_phone_start(ctx: commands.Context):
    if ctx.channel.id in active_games or ctx.channel.id in active_blitz_games:
        await ctx.send("A game is already running in this channel! Finish it before starting a new one.")
        return

    try:
        word_lists = load_word_lists(BLITZ_DATA_FILES)
    except (FileNotFoundError, ValueError) as e:
        await ctx.send(f"⚠️ Can't start a game: {e}")
        return

    game = BlitzSpeedGame(ctx.channel, ctx.author, word_lists, mode="blitzp")
    active_blitz_games[ctx.channel.id] = game
    await ctx.send(
        f"⚡ **{ctx.author.display_name}** started a Speed Blitz (Phone)! Guess {BLITZ_WORDS_TO_GUESS} words as "
        f"fast as you can \u2014 the clock is running down to the millisecond. Scores here go to the phone "
        f"leaderboard. Go!"
    )
    await game.start()


@bot.command(name="pb")
async def personal_bests(ctx: commands.Context):
    blitz_avg = await get_rolling_average("blitz", ctx.author.id)
    blitzp_avg = await get_rolling_average("blitzp", ctx.author.id)
    stats = await get_user_stats(ctx.author.id)

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

    total_words = stats["total_words_solved"] if stats else 0
    games_hangman = stats["games_completed_hangman"] if stats else 0
    games_blitz = stats["games_completed_blitz"] if stats else 0
    games_blitzp = stats["games_completed_blitzp"] if stats else 0

    embed = discord.Embed(
        title=f"🏅 {ctx.author.display_name}'s Stats",
        color=discord.Color.purple(),
    )
    embed.add_field(
        name="📝 Words Solved",
        value=f"**{total_words}** correct word{'s' if total_words != 1 else ''} across all modes",
        inline=False,
    )
    embed.add_field(
        name="🎮 Games Completed",
        value=(
            f"Regular Hangman: **{games_hangman}**\n"
            f"Blitz: **{games_blitz}**\n"
            f"Blitz (Phone): **{games_blitzp}**"
        ),
        inline=False,
    )
    embed.add_field(name="⚡ Blitz Rolling Average", value=render_avg(blitz_avg, "blitz"), inline=False)
    embed.add_field(name="📱 Blitz (Phone) Rolling Average", value=render_avg(blitzp_avg, "blitzp"), inline=False)
    await ctx.send(embed=embed)


@bot.command(name="lb")
async def leaderboard(ctx: commands.Context):
    blitz_rows = await get_average_leaderboard("blitz")
    blitzp_rows = await get_average_leaderboard("blitzp")

    def format_lines(rows: list[tuple[str, float, int]]) -> str:
        if not rows:
            return "_No scores yet_"
        return "\n".join(
            f"**{i}.** {username} \u2014 {format_score(avg_score)} (avg of {games_counted})"
            for i, (username, avg_score, games_counted) in enumerate(rows, start=1)
        )

    embed = discord.Embed(
        title="🌍 Blitz Leaderboards \u2014 Rolling Average",
        color=discord.Color.blue(),
    )
    embed.add_field(
        name="⚡ Blitz",
        value=format_lines(blitz_rows) if blitz_rows else "_No scores yet — try `!blitz`!_",
        inline=False,
    )
    embed.add_field(
        name="📱 Blitz (Phone)",
        value=format_lines(blitzp_rows) if blitzp_rows else "_No scores yet — try `!blitzp`!_",
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
