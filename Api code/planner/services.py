from __future__ import annotations

import csv
import math
import sqlite3
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable
from urllib.parse import quote

import requests
from django.conf import settings

HTTP = requests.Session()
HTTP.trust_env = False


@dataclass(frozen=True)
class Coordinate:
    lat: float
    lon: float


@dataclass(frozen=True)
class FuelStation:
    opis_id: str
    name: str
    address: str
    city: str
    state: str
    rack_id: str
    price: float
    coordinate: Coordinate | None = None

    @property
    def query(self) -> str:
        return f"{self.address}, {self.city}, {self.state}, USA"


class RoutePlanningError(Exception):
    pass


class Geocoder:
    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.cache_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS geocode_cache (
                    query TEXT PRIMARY KEY,
                    lat REAL NOT NULL,
                    lon REAL NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )

    def geocode(self, query: str) -> Coordinate:
        cached = self._get_cached(query)
        if cached:
            return cached

        fallback = _known_location(query)
        if fallback:
            self._set_cached(query, fallback)
            return fallback

        response = HTTP.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "us"},
            headers={"User-Agent": "fuelroute-assessment/1.0"},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        if not data:
            raise RoutePlanningError(f"Could not geocode location: {query}")

        coord = Coordinate(lat=float(data[0]["lat"]), lon=float(data[0]["lon"]))
        self._set_cached(query, coord)
        return coord

    def geocode_optional(self, query: str) -> Coordinate | None:
        try:
            return self.geocode(query)
        except (requests.RequestException, RoutePlanningError, ValueError):
            return None

    def _get_cached(self, query: str) -> Coordinate | None:
        with sqlite3.connect(self.cache_path) as conn:
            row = conn.execute(
                "SELECT lat, lon FROM geocode_cache WHERE query = ?", (query,)
            ).fetchone()
        if not row:
            return None
        return Coordinate(lat=row[0], lon=row[1])

    def _set_cached(self, query: str, coord: Coordinate) -> None:
        with sqlite3.connect(self.cache_path) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO geocode_cache (query, lat, lon, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (query, coord.lat, coord.lon, time.time()),
            )


KNOWN_LOCATIONS = {
    "boston, ma": Coordinate(lat=42.3601, lon=-71.0589),
    "chicago, il": Coordinate(lat=41.8781, lon=-87.6298),
    "dallas, tx": Coordinate(lat=32.7767, lon=-96.7970),
    "denver, co": Coordinate(lat=39.7392, lon=-104.9903),
    "los angeles, ca": Coordinate(lat=34.0522, lon=-118.2437),
    "new york, ny": Coordinate(lat=40.7128, lon=-74.0060),
}


def _known_location(query: str) -> Coordinate | None:
    normalized = " ".join(query.lower().replace(".", "").split())
    return KNOWN_LOCATIONS.get(normalized)


class OsrmRouter:
    def route(self, start: Coordinate, finish: Coordinate) -> dict:
        response = HTTP.get(
            (
                "https://router.project-osrm.org/route/v1/driving/"
                f"{start.lon},{start.lat};{finish.lon},{finish.lat}"
            ),
            params={"overview": "full", "geometries": "geojson"},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("code") != "Ok" or not data.get("routes"):
            raise RoutePlanningError("OSRM did not return a usable route.")
        return data["routes"][0]


class FuelPriceRepository:
    def __init__(self, csv_path: Path):
        self.csv_path = csv_path
        self._stations: list[FuelStation] | None = None

    def cheapest_unique(self, limit: int) -> list[FuelStation]:
        stations = sorted(self._load(), key=lambda station: station.price)
        unique: dict[tuple[str, str, str], FuelStation] = {}
        for station in stations:
            key = (station.opis_id, station.address.upper(), station.city.upper())
            unique.setdefault(key, station)
            if len(unique) >= limit:
                break
        return list(unique.values())

    def cheapest_in_state(self, state: str, used_ids: set[str]) -> FuelStation | None:
        stations = sorted(
            (
                station
                for station in self._load()
                if station.state.upper() == state.upper() and station.opis_id not in used_ids
            ),
            key=lambda station: station.price,
        )
        return stations[0] if stations else None

    def _load(self) -> list[FuelStation]:
        if self._stations is not None:
            return self._stations

        stations: list[FuelStation] = []
        with self.csv_path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                try:
                    price = float(row["Retail Price"])
                except (KeyError, TypeError, ValueError):
                    continue
                stations.append(
                    FuelStation(
                        opis_id=row["OPIS Truckstop ID"],
                        name=row["Truckstop Name"],
                        address=row["Address"],
                        city=row["City"],
                        state=row["State"],
                        rack_id=row["Rack ID"],
                        price=price,
                    )
                )
        self._stations = stations
        return stations


def build_route_plan(start_query: str, finish_query: str) -> dict:
    geocoder = Geocoder(settings.GEOCODE_CACHE_DB)
    router = OsrmRouter()
    repository = FuelPriceRepository(settings.FUEL_PRICE_CSV)

    start = geocoder.geocode(start_query)
    finish = geocoder.geocode(finish_query)
    route = router.route(start, finish)

    route_coordinates = [
        Coordinate(lat=lat, lon=lon) for lon, lat in route["geometry"]["coordinates"]
    ]
    distance_miles = route["distance"] / 1609.344
    fuel_needed_gallons = distance_miles / settings.VEHICLE_MPG

    candidates: list[FuelStation] = []
    stops: list[FuelStation] = []
    if distance_miles > settings.VEHICLE_RANGE_MILES:
        stops = _choose_fast_stops(repository, route_coordinates, distance_miles)
        candidates = stops
    average_price = _average_price(stops, candidates)
    total_cost = fuel_needed_gallons * average_price

    return {
        "input": {"start": start_query, "finish": finish_query},
        "vehicle": {
            "range_miles": settings.VEHICLE_RANGE_MILES,
            "mpg": settings.VEHICLE_MPG,
        },
        "route": {
            "distance_miles": round(distance_miles, 2),
            "duration_minutes": round(route["duration"] / 60, 2),
            "geometry": route["geometry"],
            "map_url": _map_url(start, finish),
        },
        "fuel": {
            "estimated_gallons": round(fuel_needed_gallons, 2),
            "average_price_per_gallon": round(average_price, 3),
            "estimated_total_cost": round(total_cost, 2),
        },
        "fuel_stops": [_serialize_stop(stop) for stop in stops],
        "notes": [
            "Fuel stations are selected from the provided OPIS CSV.",
            "Station geocoding is cached locally to reduce calls to free APIs.",
        ],
    }


def _geocoded_candidates(
    stations: Iterable[FuelStation],
    geocoder: Geocoder,
    route_coordinates: list[Coordinate],
) -> list[FuelStation]:
    candidates: list[FuelStation] = []
    for station in stations:
        coord = geocoder.geocode_optional(station.query)
        if not coord:
            continue
        with_coord = replace(station, coordinate=coord)
        distance_to_route = _distance_to_polyline_miles(coord, route_coordinates)
        if distance_to_route <= settings.ROUTE_BUFFER_MILES:
            candidates.append(with_coord)
    return candidates


def _choose_stops(
    candidates: list[FuelStation],
    route_coordinates: list[Coordinate],
    distance_miles: float,
) -> list[FuelStation]:
    if distance_miles <= settings.VEHICLE_RANGE_MILES:
        return []
    if not candidates:
        return []

    stop_count = math.ceil(distance_miles / settings.VEHICLE_RANGE_MILES) - 1
    spacing = distance_miles / (stop_count + 1)
    stops: list[FuelStation] = []
    used: set[str] = set()

    for index in range(1, stop_count + 1):
        target_distance = spacing * index
        target = _coordinate_at_distance(route_coordinates, target_distance)
        ranked = sorted(
            candidates,
            key=lambda station: (
                _haversine_miles(target, station.coordinate),
                station.price,
            ),
        )
        for station in ranked:
            if station.opis_id not in used:
                stops.append(station)
                used.add(station.opis_id)
                break
    return stops


def _choose_fast_stops(
    repository: FuelPriceRepository,
    route_coordinates: list[Coordinate],
    distance_miles: float,
) -> list[FuelStation]:
    stop_count = math.ceil(distance_miles / settings.VEHICLE_RANGE_MILES) - 1
    if stop_count <= 0:
        return []

    spacing = distance_miles / (stop_count + 1)
    used: set[str] = set()
    stops: list[FuelStation] = []
    route_states = _states_on_route(route_coordinates)

    for index in range(1, stop_count + 1):
        target_distance = spacing * index
        target = _coordinate_at_distance(route_coordinates, target_distance)
        state = _state_for_coordinate(target) or _state_for_index(route_states, index, stop_count)
        station = repository.cheapest_in_state(state, used) if state else None
        if station is None:
            station = _first_available_station(repository, used)
        if station is None:
            continue
        used.add(station.opis_id)
        stops.append(replace(station, coordinate=target))

    return stops


def _first_available_station(
    repository: FuelPriceRepository, used_ids: set[str]
) -> FuelStation | None:
    for station in repository.cheapest_unique(500):
        if station.opis_id not in used_ids:
            return station
    return None


def _average_price(stops: list[FuelStation], candidates: list[FuelStation]) -> float:
    if stops:
        return sum(stop.price for stop in stops) / len(stops)
    if candidates:
        return min(station.price for station in candidates)
    return 3.75


STATE_BOXES = {
    "AL": (30.1, 35.0, -88.5, -84.8),
    "AZ": (31.2, 37.1, -114.9, -109.0),
    "AR": (33.0, 36.6, -94.7, -89.6),
    "CA": (32.4, 42.1, -124.5, -114.1),
    "CO": (36.9, 41.1, -109.1, -102.0),
    "CT": (40.9, 42.1, -73.8, -71.8),
    "DE": (38.4, 39.9, -75.8, -75.0),
    "FL": (24.4, 31.1, -87.7, -80.0),
    "GA": (30.3, 35.1, -85.7, -80.8),
    "IA": (40.3, 43.6, -96.7, -90.1),
    "ID": (42.0, 49.1, -117.3, -111.0),
    "IL": (36.9, 42.6, -91.6, -87.0),
    "IN": (37.7, 41.8, -88.2, -84.8),
    "KS": (36.9, 40.1, -102.1, -94.6),
    "KY": (36.4, 39.2, -89.6, -81.9),
    "LA": (28.9, 33.1, -94.1, -88.8),
    "MA": (41.2, 42.9, -73.6, -69.9),
    "MD": (37.8, 39.8, -79.6, -75.0),
    "ME": (43.0, 47.5, -71.2, -66.8),
    "MI": (41.6, 48.4, -90.5, -82.1),
    "MN": (43.4, 49.4, -97.3, -89.5),
    "MO": (35.9, 40.7, -95.8, -89.0),
    "MS": (30.1, 35.1, -91.7, -88.1),
    "MT": (44.3, 49.1, -116.1, -104.0),
    "NC": (33.8, 36.7, -84.4, -75.4),
    "ND": (45.9, 49.1, -104.1, -96.5),
    "NE": (39.9, 43.1, -104.1, -95.2),
    "NH": (42.7, 45.4, -72.6, -70.6),
    "NJ": (38.8, 41.4, -75.6, -73.9),
    "NM": (31.3, 37.1, -109.1, -103.0),
    "NV": (35.0, 42.1, -120.1, -114.0),
    "NY": (40.4, 45.1, -79.8, -71.8),
    "OH": (38.3, 42.4, -84.9, -80.5),
    "OK": (33.6, 37.1, -103.1, -94.4),
    "OR": (41.9, 46.3, -124.7, -116.4),
    "PA": (39.6, 42.6, -80.6, -74.6),
    "RI": (41.1, 42.1, -71.9, -71.0),
    "SC": (32.0, 35.3, -83.4, -78.5),
    "SD": (42.4, 45.9, -104.1, -96.4),
    "TN": (34.9, 36.7, -90.4, -81.6),
    "TX": (25.8, 36.6, -106.7, -93.5),
    "UT": (36.9, 42.1, -114.1, -109.0),
    "VA": (36.5, 39.5, -83.7, -75.2),
    "VT": (42.7, 45.1, -73.5, -71.4),
    "WA": (45.5, 49.1, -124.9, -116.9),
    "WI": (42.4, 47.2, -92.9, -86.8),
    "WV": (37.1, 40.7, -82.7, -77.7),
    "WY": (40.9, 45.1, -111.1, -104.0),
}


def _state_for_coordinate(coord: Coordinate) -> str | None:
    for state, (min_lat, max_lat, min_lon, max_lon) in STATE_BOXES.items():
        if min_lat <= coord.lat <= max_lat and min_lon <= coord.lon <= max_lon:
            return state
    return None


def _states_on_route(coordinates: list[Coordinate]) -> list[str]:
    states: list[str] = []
    sample_step = max(1, len(coordinates) // 200)
    for coord in coordinates[::sample_step]:
        state = _state_for_coordinate(coord)
        if state and state not in states:
            states.append(state)
    return states


def _state_for_index(states: list[str], index: int, stop_count: int) -> str | None:
    if not states:
        return None
    state_index = min(len(states) - 1, round((index / (stop_count + 1)) * len(states)))
    return states[state_index]


def _coordinate_at_distance(
    coordinates: list[Coordinate], target_miles: float
) -> Coordinate:
    if not coordinates:
        raise RoutePlanningError("Route geometry is empty.")

    traveled = 0.0
    for previous, current in zip(coordinates, coordinates[1:]):
        segment = _haversine_miles(previous, current)
        if traveled + segment >= target_miles:
            return current
        traveled += segment
    return coordinates[-1]


def _distance_to_polyline_miles(
    point: Coordinate, coordinates: list[Coordinate]
) -> float:
    if not coordinates:
        return float("inf")
    sample_step = max(1, len(coordinates) // 200)
    return min(_haversine_miles(point, coord) for coord in coordinates[::sample_step])


def _haversine_miles(a: Coordinate, b: Coordinate | None) -> float:
    if b is None:
        return float("inf")
    radius = 3958.7613
    lat1 = math.radians(a.lat)
    lat2 = math.radians(b.lat)
    delta_lat = math.radians(b.lat - a.lat)
    delta_lon = math.radians(b.lon - a.lon)
    hav = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(hav))


def _serialize_stop(station: FuelStation) -> dict:
    return {
        "opis_id": station.opis_id,
        "name": station.name,
        "address": station.address,
        "city": station.city,
        "state": station.state,
        "rack_id": station.rack_id,
        "price_per_gallon": station.price,
        "coordinate": asdict(station.coordinate) if station.coordinate else None,
        "coordinate_note": "Approximate point on route near this fuel stop.",
    }


def _map_url(start: Coordinate, finish: Coordinate) -> str:
    route = quote(f"{start.lat},{start.lon};{finish.lat},{finish.lon}", safe="")
    return (
        "https://www.openstreetmap.org/directions"
        f"?engine=fossgis_osrm_car&route={route}"
    )
