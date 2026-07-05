"""
distance.py

Calculates driving distance/time from home_location (Veenendaal) to a
listing's city - using FREE, no-signup services instead of Google Maps:

  1. Nominatim (OpenStreetMap) - converts a city name into coordinates.
     Free, no API key. Usage policy asks for max ~1 request/second and a
     descriptive User-Agent, both of which this module respects.

  2. OSRM (Open Source Routing Machine) - calculates driving distance/time
     between two coordinates. Free public demo server, no API key.

Trade-off vs Google Maps: no public transit mode (car-only), and the free
public OSRM server can occasionally be slower/less available than a paid
API - acceptable for a personal tool scanning every few hours.

NOTE: Home coordinates (Veenendaal) are geocoded once and cached, since
they never change between runs.
"""

import logging
import time
from typing import Optional

import httpx

import storage

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OSRM_URL = "http://router.project-osrm.org/route/v1/driving/{coords}"

# Nominatim's usage policy requires a real, descriptive User-Agent
USER_AGENT = "marktplaats-iphone-monitor/1.0 (personal use script)"


class DistanceResult:
    def __init__(self, distance_km, duration_minutes, mode, raw_status):
        self.distance_km = distance_km
        self.duration_minutes = duration_minutes
        self.mode = mode
        self.raw_status = raw_status


def geocode(place_name: str):
    """
    Convert a place name (e.g. 'Rotterdam' or 'Veenendaal, Netherlands') to
    (lat, lon). Checks the persistent SQLite cache first (survives across
    runs, since this script restarts fresh each time via cron/Task
    Scheduler) before hitting the free Nominatim service - this avoids
    re-geocoding the same common cities (Rotterdam, Amsterdam, etc.) every
    single run, which is both faster and more considerate of Nominatim's
    free, shared, rate-limited service.
    """
    cached = storage.get_cached_coords(place_name)
    if cached is not None:
        return cached

    query = place_name if "netherlands" in place_name.lower() else f"{place_name}, Netherlands"

    try:
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=10.0) as client:
            resp = client.get(
                NOMINATIM_URL,
                params={"q": query, "format": "json", "limit": 1},
            )
            resp.raise_for_status()
            results = resp.json()

        if not results:
            logger.warning("Nominatim found no results for '%s'", query)
            return None

        lat = float(results[0]["lat"])
        lon = float(results[0]["lon"])
        storage.cache_coords(place_name, lat, lon)

        # Be polite to the free public Nominatim server (max ~1 req/sec policy)
        time.sleep(1.0)

        return (lat, lon)

    except Exception as exc:  # noqa: BLE001
        logger.warning("Geocoding failed for '%s': %s", place_name, exc)
        return None


def _route(origin_lat: float, origin_lon: float, dest_lat: float, dest_lon: float) -> DistanceResult:
    coords = f"{origin_lon},{origin_lat};{dest_lon},{dest_lat}"
    url = OSRM_URL.format(coords=coords)

    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params={"overview": "false"})
            resp.raise_for_status()
            data = resp.json()

        if data.get("code") != "Ok" or not data.get("routes"):
            logger.warning("OSRM returned no route: %s", data.get("code"))
            return DistanceResult(None, None, "driving", data.get("code", "no_route"))

        route = data["routes"][0]
        distance_km = route["distance"] / 1000
        duration_minutes = round(route["duration"] / 60)

        return DistanceResult(distance_km, duration_minutes, "driving", "OK")

    except Exception as exc:  # noqa: BLE001
        logger.warning("OSRM routing call failed: %s", exc)
        return DistanceResult(None, None, "driving", f"error: {exc}")


def get_driving_distance_from_coords(
    dest_lat: float, dest_lon: float, home_lat: float, home_lon: float
) -> DistanceResult:
    """
    Preferred path: Marktplaats' own data includes exact listing
    coordinates, and your home coordinates are fixed in config.yaml - so
    for the normal case, we skip Nominatim geocoding entirely and go
    straight to OSRM routing. Faster, more accurate (no risk of a city
    name resolving to the wrong place), and one less service to depend on.
    """
    if dest_lat is None or dest_lon is None:
        return DistanceResult(None, None, "driving", "missing_coordinates")
    return _route(home_lat, home_lon, dest_lat, dest_lon)


def get_driving_distance(destination: str, home_location: str) -> DistanceResult:
    """
    Fallback path: used only if a listing is somehow missing coordinates
    and we only have a city name to go on. Geocodes via Nominatim (cached
    persistently in SQLite so repeat cities are never looked up twice).
    """
    origin_coords = geocode(home_location)
    if origin_coords is None:
        return DistanceResult(None, None, "driving", "home_geocode_failed")

    dest_coords = geocode(destination)
    if dest_coords is None:
        return DistanceResult(None, None, "driving", "destination_geocode_failed")

    return _route(origin_coords[0], origin_coords[1], dest_coords[0], dest_coords[1])


def get_transit_time(destination: str, home_location: str) -> DistanceResult:
    """
    Not supported by the free OSRM/Nominatim combo (no public transit
    routing available for free). Returns a clear 'not supported' result
    rather than silently failing. If you want real OV times later, this is
    where a paid or 9292-based integration would go instead.
    """
    return DistanceResult(None, None, "transit", "not_supported_free_tier")
