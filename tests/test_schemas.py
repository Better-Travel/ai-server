from osm.prompt import build_system_prompt, build_user_prompt
from osm.schemas import ALLOWED_TAGS


def test_allowed_tags_cover_food_sights_and_entertainment() -> None:
    assert {"restaurant", "cafe", "bar", "pub"} <= ALLOWED_TAGS["amenity"]
    assert {"museum", "attraction", "viewpoint"} <= ALLOWED_TAGS["tourism"]
    assert {"castle", "palace"} <= ALLOWED_TAGS["historic"]
    assert {"cinema", "theatre", "nightclub"} <= ALLOWED_TAGS["amenity"]


def test_system_prompt_lists_tags_and_rules() -> None:
    prompt = build_system_prompt()
    assert "restaurant" in prompt and "NO_QUERY:" in prompt
    assert "ALTERNATIVE" in prompt  # separate nwr per key, never chained
    assert "ISO 3166-1" in prompt


def test_user_prompt_contains_the_text() -> None:
    assert "castles in Krakow" in build_user_prompt("  castles in Krakow, Poland  ")
