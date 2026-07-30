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

DEFAULT_ROUNDS_PER_GAME = 7
MIN_ROUNDS = 1
MAX_ROUNDS = 20
JOIN_WINDOW_SECONDS = 12
ROUND_TIMEOUT_SECONDS = 100
VOWELS = set("AEIOU")

BLITZ1_TIME_LIMIT_SECONDS = 100
BLITZ2_WORDS_TO_GUESS = 5
PERSONAL_BEST_COUNT = 3
LEADERBOARD_SIZE = 10

# Point this at your Railway volume's mount path (e.g. "/data/blitz_scores.db")
# via the BLITZ_DB_PATH env var, or scores won't survive a redeploy/restart.
BLITZ_DB_PATH = os.getenv("BLITZ_DB_PATH", "data/blitz_scores.db")

intents = discord.Intents.default()
intents.message_content = True  # required to read letter/word guesses
bot = commands.Bot(command_prefix="!", intents=intents)

# channel_id -> HangmanGame
active_games: dict[int, "HangmanGame"] = {}

# channel_id -> BlitzTimeGame | BlitzSpeedGame
active_blitz_games: dict[int, "BlitzTimeGame | BlitzSpeedGame"] = {}


# ---------------------------------------------------------------------------
# Word list loading
# ---------------------------------------------------------------------------

def load_word_lists() -> dict[str, list[str]]:
    lists = {}
    for category, path in DATA_FILES.items():
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


# ---------------------------------------------------------------------------
# Persistent leaderboard storage (SQLite)
#
# One row per completed blitz round. Personal bests and the global
# leaderboard are both just queries over this one small table, so nothing
# is duplicated on disk. For blitz1, a higher score (words guessed) is
# better; for blitz2, a lower score (seconds elapsed) is better.
# ---------------------------------------------------------------------------

def _sort_order(mode: str) -> str:
    return "DESC" if mode == "blitz1" else "ASC"


def _better_than_op(mode: str) -> str:
    return ">" if mode == "blitz1" else "<"


def format_score(mode: str, score: float) -> str:
    if mode == "blitz1":
        words = int(score)
        return f"{words} word{'s' if words != 1 else ''}"
    return f"{score:.3f}s"


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
                mode TEXT NOT NULL CHECK(mode IN ('blitz1', 'blitz2')),
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                score REAL NOT NULL,
                achieved_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_blitz_mode_score ON blitz_scores(mode, score)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_blitz_mode_user ON blitz_scores(mode, user_id)")
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Synchronous on purpose - called once at startup before the bot logs in."""
    _init_db_sync()


def _record_score_sync(mode: str, user_id: int, username: str, score: float) -> tuple[int, int]:
    conn = _get_connection()
    try:
        conn.execute(
            "INSERT INTO blitz_scores (mode, user_id, username, score, achieved_at) VALUES (?, ?, ?, ?, ?)",
            (mode, user_id, username, score, datetime.now(timezone.utc).isoformat()),
        )
        op = _better_than_op(mode)
        personal_rank = conn.execute(
            f"SELECT COUNT(*) + 1 FROM blitz_scores WHERE mode = ? AND user_id = ? AND score {op} ?",
            (mode, user_id, score),
        ).fetchone()[0]
        global_rank = conn.execute(
            f"SELECT COUNT(*) + 1 FROM blitz_scores WHERE mode = ? AND score {op} ?",
            (mode, score),
        ).fetchone()[0]
        conn.commit()
        return personal_rank, global_rank
    finally:
        conn.close()


async def record_score(mode: str, user_id: int, username: str, score: float) -> tuple[int, int]:
    """Logs a completed round and returns (personal_rank, global_rank) for that score."""
    return await asyncio.to_thread(_record_score_sync, mode, user_id, username, score)


def _get_personal_bests_sync(mode: str, user_id: int, limit: int) -> list[tuple[float, str]]:
    conn = _get_connection()
    try:
        order = _sort_order(mode)
        rows = conn.execute(
            f"SELECT score, achieved_at FROM blitz_scores WHERE mode = ? AND user_id = ? "
            f"ORDER BY score {order} LIMIT ?",
            (mode, user_id, limit),
        ).fetchall()
        return rows
    finally:
        conn.close()


async def get_personal_bests(mode: str, user_id: int, limit: int = PERSONAL_BEST_COUNT) -> list[tuple[float, str]]:
    return await asyncio.to_thread(_get_personal_bests_sync, mode, user_id, limit)


def _get_leaderboard_sync(mode: str, limit: int) -> list[tuple[str, float, str]]:
    conn = _get_connection()
    try:
        order = _sort_order(mode)
        rows = conn.execute(
            f"SELECT username, score, achieved_at FROM blitz_scores WHERE mode = ? "
            f"ORDER BY score {order} LIMIT ?",
            (mode, limit),
        ).fetchall()
        return rows
    finally:
        conn.close()


async def get_leaderboard(mode: str, limit: int = LEADERBOARD_SIZE) -> list[tuple[str, float, str]]:
    return await asyncio.to_thread(_get_leaderboard_sync, mode, limit)


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

    def player_by_id(self, uid: int) -> discord.Member | None:
        return discord.utils.get(self.players, id=uid)

    def build_display(self) -> str:
        return render_word(self.current_word, self.guessed_letters)

    def is_fully_revealed(self) -> bool:
        return word_fully_revealed(self.current_word, self.guessed_letters)

    def wrong_letters_display(self) -> str:
        return format_wrong_letters(self.wrong_letters)

    async def start_round(self):
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
        await self.channel.send(embed=embed)

        if self._timeout_task:
            self._timeout_task.cancel()
        self._timeout_task = asyncio.create_task(self._round_timeout())

    async def _round_timeout(self):
        try:
            await asyncio.sleep(ROUND_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            return
        if not self.active:
            return
        await self.channel.send(
            f"⏰ Time's up! The answer was **{self.current_word}**. No points awarded this round."
        )
        await self._advance()

    async def handle_guess(self, message: discord.Message):
        if not self.active:
            return
        if message.author not in self.players:
            return

        content = message.content.strip()
        if not content or not content.replace(" ", "").isalpha():
            return

        if len(content) == 1:
            await self._handle_letter_guess(message.author, content.upper())
        else:
            await self._handle_word_guess(message.author, content)

    async def _handle_letter_guess(self, author: discord.Member, letter: str):
        if letter in VOWELS:
            await self.channel.send(f"Vowels are already revealed \u2014 `{self.build_display()}`")
            return
        if letter in self.guessed_letters:
            return  # silently ignore repeat guesses to avoid spamming the channel
        self.guessed_letters.add(letter)

        if letter in self.current_word.upper():
            await self.channel.send(f"✅ `{letter}` is in it! `{self.build_display()}`")
            if self.is_fully_revealed():
                await self._award_point(author, completed_via_letter=True)
        else:
            self.wrong_letters.add(letter)
            await self.channel.send(f"❌ `{letter}` is not in the word.\n{self.wrong_letters_display()}")

    async def _handle_word_guess(self, author: discord.Member, guess: str):
        if guess.upper() == self.current_word.upper():
            self.guessed_letters = {ch.upper() for ch in self.current_word if ch.isalpha()}
            await self._award_point(author, completed_via_letter=False)
        else:
            await self.channel.send(f"❌ Not quite, {author.display_name}.")

    async def _award_point(self, author: discord.Member, completed_via_letter: bool):
        if self.round_start_time is not None:
            self.guess_durations.append(time.monotonic() - self.round_start_time)
        self.scores[author.id] += 1
        verb = "completed the word with the final letter" if completed_via_letter else "guessed the full answer"
        await self.channel.send(
            f"🎉 **{author.display_name}** {verb}: **{self.current_word}**! Point awarded. "
            f"Score: {self.scores[author.id]}"
        )
        await self._advance()

    async def _advance(self):
        if self._timeout_task:
            self._timeout_task.cancel()
        if self.round_num >= self.rounds_per_game:
            await self.end_game()
        else:
            await self.start_round()

    async def end_game(self):
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

class BlitzTimeGame:
    """!blitz1 - guess as many words as possible within a fixed time limit."""

    def __init__(self, channel: discord.TextChannel, player: discord.Member, word_lists):
        self.channel = channel
        self.player = player
        self.word_lists = word_lists
        self.current_word = ""
        self.current_category = ""
        self.guessed_letters: set[str] = set()
        self.wrong_letters: set[str] = set()
        self.correct_count = 0
        self.active = True
        self._timeout_task: asyncio.Task | None = None

    async def start(self):
        self._timeout_task = asyncio.create_task(self._run_timer())
        await self._next_word()

    async def _run_timer(self):
        try:
            await asyncio.sleep(BLITZ1_TIME_LIMIT_SECONDS)
        except asyncio.CancelledError:
            return
        if not self.active:
            return
        await self.end_game()

    async def _next_word(self):
        self.current_category, self.current_word = pick_word(self.word_lists)
        self.guessed_letters = set(VOWELS)
        self.wrong_letters = set()

        embed = discord.Embed(
            title=f"⚡ Blitz! ({self.correct_count} guessed so far)",
            description=f"Category: **{self.current_category}**\n\n`{render_word(self.current_word, self.guessed_letters)}`",
            color=discord.Color.orange(),
        )
        embed.add_field(name="\u200b", value=format_wrong_letters(self.wrong_letters), inline=False)
        embed.set_footer(text=f"Vowels are pre-filled. You have {BLITZ1_TIME_LIMIT_SECONDS}s total. Type a letter or the full answer.")
        await self.channel.send(embed=embed)

    async def handle_guess(self, message: discord.Message):
        if not self.active or message.author.id != self.player.id:
            return

        content = message.content.strip()
        if not content or not content.replace(" ", "").isalpha():
            return

        if len(content) == 1:
            await self._handle_letter_guess(content.upper())
        else:
            await self._handle_word_guess(content)

    async def _handle_letter_guess(self, letter: str):
        if letter in VOWELS:
            await self.channel.send(f"Vowels are already revealed \u2014 `{render_word(self.current_word, self.guessed_letters)}`")
            return
        if letter in self.guessed_letters:
            return
        self.guessed_letters.add(letter)

        if letter in self.current_word.upper():
            await self.channel.send(f"✅ `{letter}` is in it! `{render_word(self.current_word, self.guessed_letters)}`")
            if word_fully_revealed(self.current_word, self.guessed_letters):
                await self._word_complete()
        else:
            self.wrong_letters.add(letter)
            await self.channel.send(f"❌ `{letter}` is not in the word.\n{format_wrong_letters(self.wrong_letters)}")

    async def _handle_word_guess(self, guess: str):
        if guess.upper() == self.current_word.upper():
            self.guessed_letters = {ch.upper() for ch in self.current_word if ch.isalpha()}
            await self._word_complete()
        else:
            await self.channel.send(f"❌ Not quite, {self.player.display_name}.")

    async def _word_complete(self):
        self.correct_count += 1
        await self.channel.send(f"🎉 **{self.current_word}**! That's {self.correct_count} so far.")
        if self.active:
            await self._next_word()

    async def end_game(self):
        self.active = False
        # end_game() can run *inside* self._timeout_task (the 100s timer
        # firing calls this directly) - cancelling it here would throw
        # CancelledError into this very coroutine at the next await below,
        # killing it before the score gets recorded or the embed gets sent.
        if self._timeout_task and self._timeout_task is not asyncio.current_task():
            self._timeout_task.cancel()
        active_blitz_games.pop(self.channel.id, None)

        personal_rank, global_rank = await record_score(
            "blitz1", self.player.id, self.player.display_name, float(self.correct_count)
        )

        embed = discord.Embed(
            title="⏰ Blitz Round Over!",
            description=f"**{self.player.display_name}** guessed **{self.correct_count}** word(s) in {BLITZ1_TIME_LIMIT_SECONDS} seconds!",
            color=discord.Color.gold(),
        )
        notes = []
        if personal_rank <= PERSONAL_BEST_COUNT:
            notes.append(f"🌟 New personal best \u2014 #{personal_rank} in your top {PERSONAL_BEST_COUNT}! (`!pb` to view)")
        if global_rank <= LEADERBOARD_SIZE:
            notes.append(f"🌍 That lands at #{global_rank} on the all-time leaderboard! (`!blitzboard1` to view)")
        if notes:
            embed.add_field(name="\u200b", value="\n".join(notes), inline=False)
        await self.channel.send(embed=embed)

    async def cancel(self, cancelled_by: discord.Member):
        self.active = False
        if self._timeout_task and self._timeout_task is not asyncio.current_task():
            self._timeout_task.cancel()
        active_blitz_games.pop(self.channel.id, None)
        await self.channel.send(f"🛑 Blitz round cancelled by **{cancelled_by.display_name}**.")


class BlitzSpeedGame:
    """!blitz2 - guess a fixed number of words as fast as possible, timed to the millisecond."""

    def __init__(self, channel: discord.TextChannel, player: discord.Member, word_lists, words_to_guess: int = BLITZ2_WORDS_TO_GUESS):
        self.channel = channel
        self.player = player
        self.word_lists = word_lists
        self.words_to_guess = words_to_guess
        self.current_word = ""
        self.current_category = ""
        self.guessed_letters: set[str] = set()
        self.wrong_letters: set[str] = set()
        self.correct_count = 0
        self.active = True
        self.start_time: float | None = None

    async def start(self):
        self.start_time = time.perf_counter()
        await self._next_word()

    async def _next_word(self):
        self.current_category, self.current_word = pick_word(self.word_lists)
        self.guessed_letters = set(VOWELS)
        self.wrong_letters = set()

        embed = discord.Embed(
            title=f"⚡ Speed Blitz! Word {self.correct_count + 1} of {self.words_to_guess}",
            description=f"Category: **{self.current_category}**\n\n`{render_word(self.current_word, self.guessed_letters)}`",
            color=discord.Color.orange(),
        )
        embed.add_field(name="\u200b", value=format_wrong_letters(self.wrong_letters), inline=False)
        embed.set_footer(text="Vowels are pre-filled. Type a letter or the full answer. Clock is running!")
        await self.channel.send(embed=embed)

    async def handle_guess(self, message: discord.Message):
        if not self.active or message.author.id != self.player.id:
            return

        content = message.content.strip()
        if not content or not content.replace(" ", "").isalpha():
            return

        if len(content) == 1:
            await self._handle_letter_guess(content.upper())
        else:
            await self._handle_word_guess(content)

    async def _handle_letter_guess(self, letter: str):
        if letter in VOWELS:
            await self.channel.send(f"Vowels are already revealed \u2014 `{render_word(self.current_word, self.guessed_letters)}`")
            return
        if letter in self.guessed_letters:
            return
        self.guessed_letters.add(letter)

        if letter in self.current_word.upper():
            await self.channel.send(f"✅ `{letter}` is in it! `{render_word(self.current_word, self.guessed_letters)}`")
            if word_fully_revealed(self.current_word, self.guessed_letters):
                await self._word_complete()
        else:
            self.wrong_letters.add(letter)
            await self.channel.send(f"❌ `{letter}` is not in the word.\n{format_wrong_letters(self.wrong_letters)}")

    async def _handle_word_guess(self, guess: str):
        if guess.upper() == self.current_word.upper():
            self.guessed_letters = {ch.upper() for ch in self.current_word if ch.isalpha()}
            await self._word_complete()
        else:
            await self.channel.send(f"❌ Not quite, {self.player.display_name}.")

    async def _word_complete(self):
        self.correct_count += 1
        if self.correct_count >= self.words_to_guess:
            await self.end_game()
        else:
            await self.channel.send(f"🎉 **{self.current_word}**! {self.correct_count}/{self.words_to_guess} done.")
            await self._next_word()

    async def end_game(self):
        self.active = False
        active_blitz_games.pop(self.channel.id, None)
        elapsed = time.perf_counter() - self.start_time

        personal_rank, global_rank = await record_score(
            "blitz2", self.player.id, self.player.display_name, elapsed
        )

        embed = discord.Embed(
            title="🏁 Speed Blitz Complete!",
            description=(
                f"**{self.player.display_name}** guessed **{self.current_word}** to finish "
                f"{self.words_to_guess} word(s)!"
            ),
            color=discord.Color.gold(),
        )
        embed.add_field(name="Your Time", value=f"{elapsed:.3f} seconds")
        notes = []
        if personal_rank <= PERSONAL_BEST_COUNT:
            notes.append(f"🌟 New personal best \u2014 #{personal_rank} in your top {PERSONAL_BEST_COUNT}! (`!pb` to view)")
        if global_rank <= LEADERBOARD_SIZE:
            notes.append(f"🌍 That lands at #{global_rank} on the all-time leaderboard! (`!blitzboard2` to view)")
        if notes:
            embed.add_field(name="\u200b", value="\n".join(notes), inline=False)
        await self.channel.send(embed=embed)

    async def cancel(self, cancelled_by: discord.Member):
        self.active = False
        active_blitz_games.pop(self.channel.id, None)
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
        is_player = ctx.author.id == blitz.player.id
        can_moderate = ctx.author.guild_permissions.manage_messages
        if not (is_player or can_moderate):
            await ctx.send("Only the player who started the blitz round (or a moderator) can stop it.")
            return
        await blitz.cancel(ctx.author)
        return

    await ctx.send("There's no game running in this channel.")


@bot.command(name="blitz1")
async def blitz1_start(ctx: commands.Context):
    if ctx.channel.id in active_games or ctx.channel.id in active_blitz_games:
        await ctx.send("A game is already running in this channel! Finish it before starting a new one.")
        return

    try:
        word_lists = load_word_lists()
    except (FileNotFoundError, ValueError) as e:
        await ctx.send(f"⚠️ Can't start a game: {e}")
        return

    game = BlitzTimeGame(ctx.channel, ctx.author, word_lists)
    active_blitz_games[ctx.channel.id] = game
    await ctx.send(
        f"⚡ **{ctx.author.display_name}** started a Blitz round! Guess as many words as you can in "
        f"{BLITZ1_TIME_LIMIT_SECONDS} seconds. Vowels are pre-filled. Go!"
    )
    await game.start()


@bot.command(name="blitz2")
async def blitz2_start(ctx: commands.Context):
    if ctx.channel.id in active_games or ctx.channel.id in active_blitz_games:
        await ctx.send("A game is already running in this channel! Finish it before starting a new one.")
        return

    try:
        word_lists = load_word_lists()
    except (FileNotFoundError, ValueError) as e:
        await ctx.send(f"⚠️ Can't start a game: {e}")
        return

    game = BlitzSpeedGame(ctx.channel, ctx.author, word_lists)
    active_blitz_games[ctx.channel.id] = game
    await ctx.send(
        f"⚡ **{ctx.author.display_name}** started a Speed Blitz! Guess {BLITZ2_WORDS_TO_GUESS} words as fast "
        f"as you can \u2014 the clock is running down to the millisecond. Go!"
    )
    await game.start()


@bot.command(name="pb")
async def personal_bests(ctx: commands.Context):
    blitz1_rows = await get_personal_bests("blitz1", ctx.author.id)
    blitz2_rows = await get_personal_bests("blitz2", ctx.author.id)

    def render(mode, rows):
        if not rows:
            return f"No scores yet \u2014 try `!{mode}`!"
        return "\n".join(f"**{i}.** {format_score(mode, score)}" for i, (score, _) in enumerate(rows, start=1))

    embed = discord.Embed(title=f"🏅 {ctx.author.display_name}'s Personal Bests", color=discord.Color.purple())
    embed.add_field(name="⚡ Blitz1 \u2014 Most Words in 100s", value=render("blitz1", blitz1_rows), inline=False)
    embed.add_field(name="🏁 Blitz2 \u2014 Fastest 5 Words", value=render("blitz2", blitz2_rows), inline=False)
    await ctx.send(embed=embed)


@bot.command(name="blitzboard1")
async def blitzboard1(ctx: commands.Context):
    rows = await get_leaderboard("blitz1")
    if not rows:
        await ctx.send("No Blitz1 scores yet \u2014 be the first with `!blitz1`!")
        return
    lines = [f"**{i}.** {username} \u2014 {format_score('blitz1', score)}" for i, (username, score, _) in enumerate(rows, start=1)]
    embed = discord.Embed(
        title="🌍 Blitz1 All-Time Leaderboard",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )
    await ctx.send(embed=embed)


@bot.command(name="blitzboard2")
async def blitzboard2(ctx: commands.Context):
    rows = await get_leaderboard("blitz2")
    if not rows:
        await ctx.send("No Blitz2 scores yet \u2014 be the first with `!blitz2`!")
        return
    lines = [f"**{i}.** {username} \u2014 {format_score('blitz2', score)}" for i, (username, score, _) in enumerate(rows, start=1)]
    embed = discord.Embed(
        title="🌍 Blitz2 All-Time Leaderboard",
        description="\n".join(lines),
        color=discord.Color.blue(),
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
