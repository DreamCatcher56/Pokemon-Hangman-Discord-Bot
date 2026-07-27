"""
Pokemon Hangman Competition Bot
--------------------------------
A Discord bot game where a host starts a hangman round, other players can
join, and everyone competes over 7 rounds guessing random Pokemon names,
abilities, moves, or items pulled from text files.

Run fetch_word_lists.py first to generate the data/ text files.
"""

import os
import random
import asyncio

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

intents = discord.Intents.default()
intents.message_content = True  # required to read letter/word guesses
bot = commands.Bot(command_prefix="!", intents=intents)

# channel_id -> HangmanGame
active_games: dict[int, "HangmanGame"] = {}


# ---------------------------------------------------------------------------
# Word list loading
# ---------------------------------------------------------------------------

def load_word_lists() -> dict[str, list[str]]:
    lists = {}
    for category, path in DATA_FILES.items():
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Missing {path}. Run fetch_word_lists.py first to generate the word lists."
            )
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

    def player_by_id(self, uid: int) -> discord.Member | None:
        return discord.utils.get(self.players, id=uid)

    def build_display(self) -> str:
        word_parts = self.current_word.split(" ")
        rendered_parts = []
        for part in word_parts:
            rendered = " ".join(
                ch if (not ch.isalpha()) or ch.upper() in self.guessed_letters else "_"
                for ch in part
            )
            rendered_parts.append(rendered)
        return "   ".join(rendered_parts)

    def is_fully_revealed(self) -> bool:
        return all((not ch.isalpha()) or ch.upper() in self.guessed_letters for ch in self.current_word)

    def wrong_letters_display(self) -> str:
        if not self.wrong_letters:
            return "Letters not in the word: none yet"
        return "Letters not in the word: " + ", ".join(sorted(self.wrong_letters))

    async def start_round(self):
        self.round_num += 1
        self.current_category, self.current_word = pick_word(self.word_lists)
        self.guessed_letters = set(VOWELS)
        self.wrong_letters = set()

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
# Commands & events
# ---------------------------------------------------------------------------

@bot.command(name="hangman")
async def hangman_start(ctx: commands.Context, rounds: int = DEFAULT_ROUNDS_PER_GAME):
    if ctx.channel.id in active_games:
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
    if not game:
        await ctx.send("There's no game running in this channel.")
        return

    is_host = ctx.author.id == game.host.id
    can_moderate = ctx.author.guild_permissions.manage_messages
    if not (is_host or can_moderate):
        await ctx.send("Only the host who started the game (or a moderator) can stop it.")
        return

    await game.cancel(ctx.author)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    await bot.process_commands(message)

    game = active_games.get(message.channel.id)
    if game and game.active:
        await game.handle_guess(message)


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
