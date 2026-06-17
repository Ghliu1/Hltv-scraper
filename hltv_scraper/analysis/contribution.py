"""A transparent, role-aware player-contribution model.

The goal stated for this project is to "model a player's contribution to a
team". HLTV's own Rating 2.x is a black-box weighting of KAST/impact/ADR/KPR/
survival. Here we build an *explainable* contribution score from the component
statistics we store, so the weighting can be inspected, tuned, and compared to
HLTV rating — and so contribution can be decomposed by facet (fragging, entry,
clutch, utility, survival).

This is deliberately a simple, documented linear model rather than a fitted
black box: with the per-period component stats persisted, anyone can later
regress these against round-win / match-win outcomes to *learn* the weights.
The function here provides a sensible prior and the plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional

from ..db import Database


# Default facet weights (sum ~1.0). Tunable; exposed so callers can override.
DEFAULT_WEIGHTS: Dict[str, float] = {
    "fragging": 0.34,    # raw output: rating/ADR/KPR
    "entry": 0.18,       # opening duels won
    "clutch": 0.12,      # 1vX conversions
    "utility": 0.14,     # flashes/util damage/assists
    "trade_survival": 0.12,  # not dying / being saved appropriately
    "consistency": 0.10,  # KAST — round-to-round presence
}


@dataclass
class ContributionBreakdown:
    player_id: int
    period_start: str
    period_end: str
    score: float
    facets: Dict[str, float]
    sample_maps: Optional[int]

    def as_dict(self) -> dict:
        d = {
            "player_id": self.player_id,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "contribution": round(self.score, 4),
            "sample_maps": self.sample_maps,
        }
        for k, v in self.facets.items():
            d[f"facet_{k}"] = round(v, 4)
        return d


def _safe(v, default=0.0) -> float:
    return float(v) if v is not None else default


def _facet_scores(row) -> Dict[str, float]:
    """Map a player_stat_periods row to normalised facet scores in ~[0, 2].

    Each facet is scaled so that an "average tier-1 player" lands near 1.0,
    using rough league-wide anchors. These anchors are documented constants so
    the model stays interpretable.
    """
    # Anchors (~tier-1 averages) used to normalise each component to ~1.0.
    rating = _safe(row["rating"], 1.0)
    adr = _safe(row["adr"], 75.0)
    kpr = _safe(row["kpr"], 0.68)
    kast = _safe(row["kast"], 0.70)

    opening_kills = _safe(row["opening_kills"])
    opening_deaths = _safe(row["opening_deaths"])
    opening_total = opening_kills + opening_deaths
    entry_winrate = (opening_kills / opening_total) if opening_total else 0.5

    clutches = sum(_safe(row[c]) for c in (
        "clutches_1v1", "clutches_1v2", "clutches_1v3",
        "clutches_1v4", "clutches_1v5"))
    # Weight harder clutches more.
    clutch_weighted = (
        _safe(row["clutches_1v1"]) * 1
        + _safe(row["clutches_1v2"]) * 2
        + _safe(row["clutches_1v3"]) * 3
        + _safe(row["clutches_1v4"]) * 4
        + _safe(row["clutches_1v5"]) * 5
    )
    maps = _safe(row["maps_played"], 0.0)
    clutch_per_map = (clutch_weighted / maps) if maps else 0.0

    util_dmg = _safe(row["utility_damage_pr"], 0.0)
    flash_assists_pr = (_safe(row["flash_assists"]) / maps) if maps else 0.0

    dpr = _safe(row["dpr"], 0.65)
    saved = _safe(row["saved_by_teammate_pr"], 0.0)

    return {
        # Blend of overall rating and raw damage/frag output.
        "fragging": 0.6 * (rating / 1.0) + 0.25 * (adr / 75.0) + 0.15 * (kpr / 0.68),
        # Entry success vs a 0.5 baseline, scaled and volume-aware.
        "entry": (entry_winrate / 0.5) * min(1.5, 0.5 + opening_kills / max(maps, 1) / 0.15),
        # Clutch contribution per map vs a ~0.6 weighted-clutch/map anchor.
        "clutch": clutch_per_map / 0.6 if clutch_per_map else 0.0,
        # Utility: util damage vs ~5/round anchor plus flash assists.
        "utility": 0.6 * (util_dmg / 5.0) + 0.4 * (flash_assists_pr / 0.5),
        # Survival: lower DPR is better; being saved indicates good positioning.
        "trade_survival": (0.65 / dpr) if dpr else 1.0,
        # Consistency: KAST vs 0.70 anchor.
        "consistency": kast / 0.70,
    }


def score_period(row, weights: Optional[Dict[str, float]] = None
                 ) -> ContributionBreakdown:
    weights = weights or DEFAULT_WEIGHTS
    facets = _facet_scores(row)
    score = sum(weights.get(k, 0.0) * v for k, v in facets.items())
    return ContributionBreakdown(
        player_id=row["player_id"],
        period_start=row["period_start"],
        period_end=row["period_end"],
        score=score,
        facets=facets,
        sample_maps=row["maps_played"],
    )


def compute_contributions(db: Database,
                          weights: Optional[Dict[str, float]] = None,
                          min_maps: int = 0,
                          ranking_filter: Optional[str] = None
                          ) -> List[ContributionBreakdown]:
    sql = "SELECT * FROM player_stat_periods WHERE 1=1"
    params: list = []
    if min_maps:
        sql += " AND COALESCE(maps_played, 0) >= ?"
        params.append(min_maps)
    if ranking_filter:
        sql += " AND ranking_filter = ?"
        params.append(ranking_filter)
    rows = db.query(sql, params)
    return [score_period(r, weights) for r in rows]


def player_timeline(db: Database, player_id: int,
                    weights: Optional[Dict[str, float]] = None
                    ) -> List[ContributionBreakdown]:
    """Contribution over consecutive periods — a player's career arc."""
    rows = db.query(
        "SELECT * FROM player_stat_periods WHERE player_id = ? "
        "ORDER BY period_start",
        (player_id,),
    )
    return [score_period(r, weights) for r in rows]
