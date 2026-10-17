from typing import Any

import pytest
from pydantic import ValidationError

from osm.schemas import PlacesInput


def _place(key: str, values: list[str]) -> dict[str, Any]:
    return {
        "places": [
            {
                "category": "food_drink",
                "filters": [{"key": key, "values": values}],
                "area": "Paris",
                "country": "FR",
            }
        ]
    }


def test_valid_input() -> None:
    assert PlacesInput.model_validate(_place("amenity", ["cafe"]))


def test_value_not_valid_for_key() -> None:
    with pytest.raises(ValidationError):
        PlacesInput.model_validate(_place("tourism", ["cafe"]))


def test_unknown_key() -> None:
    with pytest.raises(ValidationError):
        PlacesInput.model_validate(_place("building", ["house"]))


def test_bbox_area() -> None:
    data = _place("amenity", ["bar"])
    data["places"][0]["area"] = {"south": 1, "west": 2, "north": 3, "east": 4}
    assert PlacesInput.model_validate(data)


def test_named_area_requires_country() -> None:
    data = _place("amenity", ["bar"])
    del data["places"][0]["country"]
    with pytest.raises(ValidationError):
        PlacesInput.model_validate(data)


def test_country_is_normalised_and_validated() -> None:
    data = _place("amenity", ["bar"])
    data["places"][0]["country"] = " pl "
    assert PlacesInput.model_validate(data).places[0].country == "PL"
    data["places"][0]["country"] = "Poland"
    with pytest.raises(ValidationError):
        PlacesInput.model_validate(data)


def test_bbox_does_not_need_country() -> None:
    data = _place("amenity", ["bar"])
    del data["places"][0]["country"]
    data["places"][0]["area"] = {"south": 1, "west": 2, "north": 3, "east": 4}
    assert PlacesInput.model_validate(data)


def test_prompt_says_filters_are_alternatives() -> None:
    from osm.prompt import build_system_prompt

    assert "ALTERNATIVE" in build_system_prompt()
