"""Workflow recipes: planner guidance and benchmark cases in one file each."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from awspilot.models import Check, Plan

RECIPE_DIR = Path(__file__).parent / "library"


class Recipe(BaseModel):
    id: str
    area: str
    request: str
    guidance: list[str] = Field(default_factory=list)
    # Ground truth for the benchmark. Never shown to the planner.
    checks: list[Check]
    # A known-good plan. Used to test the checks and for offline demos, never shown
    # to the planner.
    reference: Plan | None = None
    # Rough time for a practiced person to do this by hand in the console.
    baseline_minutes: float | None = None
    # Set when the workflow can't run on an emulator, with the reason.
    requires_real_aws: str | None = None


@lru_cache(maxsize=1)
def load_all() -> tuple[Recipe, ...]:
    recipes = [
        Recipe.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in sorted(RECIPE_DIR.glob("*.yaml"))
    ]
    ids = [r.id for r in recipes]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate recipe ids in the library")
    return tuple(recipes)


def get(recipe_id: str) -> Recipe:
    for recipe in load_all():
        if recipe.id == recipe_id:
            return recipe
    raise KeyError(f"no recipe {recipe_id!r}")


_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"a", "an", "the", "with", "and", "to", "for", "of", "in", "on", "that", "create", "named"}


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower())) - _STOP


def match(request: str, limit: int = 3) -> list[Recipe]:
    """Recipes most relevant to a request, by word overlap."""
    wanted = _words(request)
    scored = [
        (len(wanted & _words(f"{r.area} {r.id} {r.request}")), r) for r in load_all()
    ]
    scored = [s for s in scored if s[0] >= 2]
    scored.sort(key=lambda s: (-s[0], s[1].id))
    return [r for _, r in scored[:limit]]
