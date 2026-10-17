from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class TourismValue(StrEnum):
    ATTRACTION = "attraction"
    MUSEUM = "museum"
    GALLERY = "gallery"
    VIEWPOINT = "viewpoint"
    ARTWORK = "artwork"
    ZOO = "zoo"
    AQUARIUM = "aquarium"
    THEME_PARK = "theme_park"


class HistoricValue(StrEnum):
    CASTLE = "castle"
    MONUMENT = "monument"
    MEMORIAL = "memorial"
    RUINS = "ruins"
    ARCHAEOLOGICAL_SITE = "archaeological_site"
    FORT = "fort"
    MANOR = "manor"
    PALACE = "palace"
    CITY_GATE = "city_gate"
    SHIP = "ship"
    CHURCH = "church"
    BUILDING = "building"
    DISTRICT = "district"


class AmenityValue(StrEnum):
    # food & drink
    RESTAURANT = "restaurant"
    CAFE = "cafe"
    FAST_FOOD = "fast_food"
    BAR = "bar"
    PUB = "pub"
    BIERGARTEN = "biergarten"
    FOOD_COURT = "food_court"
    ICE_CREAM = "ice_cream"
    # entertainment
    CINEMA = "cinema"
    THEATRE = "theatre"
    NIGHTCLUB = "nightclub"
    CASINO = "casino"
    ARTS_CENTRE = "arts_centre"
    EVENTS_VENUE = "events_venue"
    PLANETARIUM = "planetarium"
    MUSIC_VENUE = "music_venue"
    FOUNTAIN = "fountain"
    # sights (Warsaw's historic churches and synagogues are often tagged only this way)
    PLACE_OF_WORSHIP = "place_of_worship"


class LeisureValue(StrEnum):
    PARK = "park"
    GARDEN = "garden"
    NATURE_RESERVE = "nature_reserve"
    AMUSEMENT_ARCADE = "amusement_arcade"
    BOWLING_ALLEY = "bowling_alley"
    ESCAPE_GAME = "escape_game"
    MINIATURE_GOLF = "miniature_golf"
    WATER_PARK = "water_park"
    STADIUM = "stadium"
    ICE_RINK = "ice_rink"
    DANCE = "dance"


class NaturalValue(StrEnum):
    BEACH = "beach"
    PEAK = "peak"
    WATERFALL = "waterfall"
    CAVE_ENTRANCE = "cave_entrance"
    HOT_SPRING = "hot_spring"


class ManMadeValue(StrEnum):
    TOWER = "tower"
    LIGHTHOUSE = "lighthouse"


class ShopValue(StrEnum):
    BAKERY = "bakery"
    PASTRY = "pastry"
    CONFECTIONERY = "confectionery"
    DELI = "deli"
    WINE = "wine"


ALLOWED_TAGS: dict[str, set[str]] = {
    "tourism": {v.value for v in TourismValue},
    "historic": {v.value for v in HistoricValue},
    "amenity": {v.value for v in AmenityValue},
    "leisure": {v.value for v in LeisureValue},
    "natural": {v.value for v in NaturalValue},
    "man_made": {v.value for v in ManMadeValue},
    "shop": {v.value for v in ShopValue},
}

# Food and drink places; everything else in the catalog is kept for sights searches.
FOOD_AMENITIES = frozenset(
    {"restaurant", "cafe", "fast_food", "bar", "pub", "biergarten", "food_court", "ice_cream"}
)
# The catalog the query model sees for a sights search: no food amenities and no shops.
SIGHT_TAGS: dict[str, set[str]] = {
    key: (values - FOOD_AMENITIES if key == "amenity" else values)
    for key, values in ALLOWED_TAGS.items()
    if key != "shop"
}


class WikipediaText(BaseModel):
    title: str
    url: str
    extract: str


class WikipediaInfo(BaseModel):
    """The article's intro in each supported language; a language is None if it has no article."""

    pl: WikipediaText | None = None
    en: WikipediaText | None = None


class OverpassElement(BaseModel):
    id: int
    type: Literal["node", "way", "relation"]
    lat: float | None = None
    lon: float | None = None
    name: str | None = None
    tags: dict[str, str] = Field(default_factory=dict)
    wikipedia: WikipediaInfo | None = None


class PipelineResult(BaseModel):
    query: str
    results: list[OverpassElement]
    attempts: int
