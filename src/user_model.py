"""How real users pick, for estimating the win rate a slate will produce.

The random-pick baseline (each tier's average player) understates users: they
lean hard toward the likeliest scorers and toward star names. Fitted on four
weeks of real picks (9/29-10/25), a user's chance of picking a player in a
tier is proportional to

    exp( w1 * log(p / p_best)          # chance of scoring vs the tier's best
       + w2 * [cheapest in the tier] + w3 * [2nd-3rd] + w4 * [4th-5th]
       + w5 * star_appeal )            # how much more he gets picked than his
                                       #   odds explain, from past nights

and the tier's expected hit rate for users is the pick-weighted average of p.
Combined across the three tiers, it predicted each week's luck-free expected
win rate within ~0.5 points, including a week left out of the fit.

Weights live in config.yaml (contest.user_model_weights); star appeal per
player in user_appeal.json beside this file. Refit both every few weeks.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from .config import Config


def norm_name(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(name))
    ascii_only = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", ascii_only)).strip().lower()


@lru_cache(maxsize=4)
def load_appeal(path: str) -> dict[str, float]:
    p = Path(path)
    if not p.is_absolute():
        p = Path(__file__).parent / p
    try:
        return json.loads(p.read_text()).get("appeal", {})
    except (OSError, ValueError):
        return {}


def expected_pick_prob(players: list, cfg: Config) -> float | None:
    """Users' expected hit rate in one tier: the pick-weighted chance of
    scoring, given who is in the tier. None for an empty tier."""
    if not players:
        return None
    w = cfg.contest.user_model_weights
    appeal = load_appeal(cfg.contest.user_appeal_file)
    ranked = sorted(players, key=lambda p: -p.true_prob)
    best = ranked[0].true_prob or 1e-9
    utils = []
    for rank, p in enumerate(ranked, start=1):
        u = (w[0] * math.log(max(p.true_prob, 1e-9) / best)
             + w[1] * (rank == 1) + w[2] * (2 <= rank <= 3) + w[3] * (4 <= rank <= 5)
             + w[4] * appeal.get(norm_name(p.name), 0.0))
        utils.append(u)
    top = max(utils)
    weights = [math.exp(u - top) for u in utils]
    total = sum(weights)
    return sum(wt * p.true_prob for wt, p in zip(weights, ranked)) / total
