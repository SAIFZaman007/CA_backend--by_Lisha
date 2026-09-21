"""The shipped exercise catalogue: complete, unique, and every link verified-shaped."""

from collections import Counter

from slugify import slugify

from app.data.exercise_library import CATALOG
from app.models.enums import MuscleGroup


def test_slugs_are_unique():
    slugs = Counter(slugify(movement.name) for movement in CATALOG)
    assert [slug for slug, count in slugs.items() if count > 1] == []


def test_every_movement_has_a_video_link():
    for movement in CATALOG:
        assert movement.video_url.startswith("https://"), movement.name


def test_known_broken_links_are_fixed():
    """The link the coach reported now points at the real guide page."""
    by_name = {movement.name: movement.video_url for movement in CATALOG}
    assert by_name["Barbell Bench Press"].endswith("/barbell-bench-press.html")
    assert by_name["Plank"].endswith("/hover.html")


def test_every_muscle_group_is_covered_and_chest_is_deep():
    groups = Counter(movement.group for movement in CATALOG)
    assert set(groups) == set(MuscleGroup)
    assert groups[MuscleGroup.CHEST] >= 50
    assert len(CATALOG) >= 600