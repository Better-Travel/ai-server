from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class TourismValue(StrEnum):
    ATTRACTION = "attraction"
    MUSEUM = "museum"
    GALLERY = "gallery"
    VIEWPOINT = "viewpoint"
    ARTWORK = "artwork"
    ZOO = "zoo"
    AQUARIUM = "aquarium"
    THEME_PARK = "theme_park"
    INFORMATION = "information"


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


OsmKey = Literal["tourism", "historic", "amenity", "leisure", "natural", "man_made", "shop"]

ALLOWED_TAGS: dict[str, set[str]] = {
    "tourism": {v.value for v in TourismValue},
    "historic": {v.value for v in HistoricValue},
    "amenity": {v.value for v in AmenityValue},
    "leisure": {v.value for v in LeisureValue},
    "natural": {v.value for v in NaturalValue},
    "man_made": {v.value for v in ManMadeValue},
    "shop": {v.value for v in ShopValue},
}


class Category(StrEnum):
    ATTRACTIONS = "attractions"
    FOOD_DRINK = "food_drink"
    ENTERTAINMENT = "entertainment"


class TagFilter(BaseModel):
    key: OsmKey
    values: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _values_belong_to_key(self) -> "TagFilter":
        unknown = set(self.values) - ALLOWED_TAGS[self.key]
        if unknown:
            raise ValueError(f"unknown values for {self.key!r}: {sorted(unknown)}")
        return self


class BBox(BaseModel):
    south: float = Field(ge=-90, le=90)
    west: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)


class PlaceRequest(BaseModel):
    category: Category
    filters: list[TagFilter] = Field(min_length=1)
    area: str | BBox
    country: str | None = Field(default=None, description="ISO 3166-1 alpha-2 code, e.g. PT")
    name: str | None = None
    require_name: bool = True

    @field_validator("country")
    @classmethod
    def _country_is_alpha2(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip().upper()
        if len(value) != 2 or not value.isalpha():
            raise ValueError("country must be an ISO 3166-1 alpha-2 code such as 'PT'")
        return value

    @model_validator(mode="after")
    def _named_area_needs_country(self) -> "PlaceRequest":
        if isinstance(self.area, str) and self.country is None:
            raise ValueError(
                "country is required when area is a place name (names like 'Lisbon' are ambiguous)"
            )
        return self


class PlacesInput(BaseModel):
    places: list[PlaceRequest] = Field(min_length=1)


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
