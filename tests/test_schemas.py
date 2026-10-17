import pytest
from pydantic import ValidationError

from osm.schemas import PlacesInput


def _place(key: str, values: list[str]) -> dict:
    return {"places": [{"category": "food_drink", "filters": [{"key": key, "values": values}], "area": "Paris", "country": "FR"}]}


def test_valid_input():
    assert PlacesInput.model_validate(_place("amenity", ["cafe"]))


def test_value_not_valid_for_key():
    with pytest.raises(ValidationError):
        PlacesInput.model_validate(_place("tourism", ["cafe"]))


def test_unknown_key():
    with pytest.raises(ValidationError):
        PlacesInput.model_validate(_place("building", ["house"]))


def test_bbox_area():
    data = _place("amenity", ["bar"])
    data["places"][0]["area"] = {"south": 1, "west": 2, "north": 3, "east": 4}
    assert PlacesInput.model_validate(data)


def test_named_area_requires_country():
    data = _place("amenity", ["bar"])
    del data["places"][0]["country"]
    with pytest.raises(ValidationError):
        PlacesInput.model_validate(data)


def test_country_is_normalised_and_validated():
    data = _place("amenity", ["bar"])
    data["places"][0]["country"] = " pt "
    assert PlacesInput.model_validate(data).places[0].country == "PT"
    data["places"][0]["country"] = "Portugal"
    with pytest.raises(ValidationError):
        PlacesInput.model_validate(data)


def test_bbox_does_not_need_country():
    data = _place("amenity", ["bar"])
    del data["places"][0]["country"]
    data["places"][0]["area"] = {"south": 1, "west": 2, "north": 3, "east": 4}
    assert PlacesInput.model_validate(data)
