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
JOIN_WINDOW_SECONDS = 20
ROUND_TIMEOUT_SECONDS = 100
VOWELS = set("AEIOU")

BLITZ1_TIME_LIMIT_SECONDS = 100
BLITZ2_WORDS_TO_GUESS = 5

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
        if self._timeout_task:
            self._timeout_task.cancel()
        active_blitz_games.pop(self.channel.id, None)

        embed = discord.Embed(
            title="⏰ Blitz Round Over!",
            description=f"**{self.player.display_name}** guessed **{self.correct_count}** word(s) in {BLITZ1_TIME_LIMIT_SECONDS} seconds!",
            color=discord.Color.gold(),
        )
        await self.channel.send(embed=embed)

    async def cancel(self, cancelled_by: discord.Member):
        self.active = False
        if self._timeout_task:
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

        embed = discord.Embed(
            title="🏁 Speed Blitz Complete!",
            description=(
                f"**{self.player.display_name}** guessed **{self.current_word}** to finish "
                f"{self.words_to_guess} word(s)!"
            ),
            color=discord.Color.gold(),
        )
        embed.add_field(name="Your Time", value=f"{elapsed:.3f} seconds")
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
    print("Pokemon Hangman bot is ready. Use !hangman in a server to start a game.")


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_BOT_TOKEN environment variable is not set. "
            "Set it to your bot's token before running this script."
        )
    bot.run(token)
