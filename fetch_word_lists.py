"""
Fetches Pokemon names, abilities, moves, and items from PokeAPI (a free,
public REST API for Pokemon data: https://pokeapi.co) and saves each as a
plain text file, one entry per line, in the data/ folder.

Run this once before starting the bot:
    python fetch_word_lists.py
"""

import os
import requests

OUTPUT_DIR = "data"

ENDPOINTS = {
    "pokemon.txt": "https://pokeapi.co/api/v2/pokemon?limit=2000",
    "abilities.txt": "https://pokeapi.co/api/v2/ability?limit=1000",
    "moves.txt": "https://pokeapi.co/api/v2/move?limit=1000",
    "items.txt": "https://pokeapi.co/api/v2/item?limit=3000",
}


def format_name(raw_name: str) -> str:
    """Convert API's 'life-orb' style names into 'Life Orb'."""
    return raw_name.replace("-", " ").title()


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    for filename, url in ENDPOINTS.items():
        print(f"Fetching {url} ...")
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        results = response.json()["results"]

        names = sorted({format_name(r["name"]) for r in results})

        filepath = os.path.join(OUTPUT_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(names))

        print(f"  Saved {len(names)} entries to {filepath}")

    print("\nDone! All four word list files are ready in the data/ folder.")


if __name__ == "__main__":
    main()
