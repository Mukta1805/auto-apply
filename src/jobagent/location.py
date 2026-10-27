"""Decide whether a posting is in the US (or remote-US) from messy location strings."""

from __future__ import annotations

import re

US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR", "california": "CA", "colorado": "CO",
    "connecticut": "CT", "delaware": "DE", "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY", "louisiana": "LA",
    "maine": "ME", "maryland": "MD", "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR", "pennsylvania": "PA",
    "rhode island": "RI", "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}
_ABBREV = set(US_STATES.values())
US_CITIES = {
    "new york", "nyc", "brooklyn", "boston", "cambridge", "somerville", "waltham", "lexington", "watertown",
    "san francisco", "south san francisco", "san diego", "san jose", "palo alto", "menlo park", "redwood city",
    "mountain view", "sunnyvale", "emeryville", "berkeley", "oakland", "foster city", "san carlos", "brisbane",
    "hayward", "fremont", "alameda", "los angeles", "pasadena", "irvine", "seattle", "bothell", "redmond",
    "chicago", "philadelphia", "pittsburgh", "baltimore", "rockville", "gaithersburg", "bethesda",
    "washington dc", "washington, d.c.", "raleigh", "durham", "research triangle", "chapel hill", "austin",
    "houston", "dallas", "denver", "boulder", "salt lake city", "nashville", "atlanta", "miami", "minneapolis",
    "st. louis", "saint louis", "indianapolis", "princeton", "new haven", "stamford", "jersey city", "newark",
    "madison", "ann arbor", "phoenix", "portland", "la jolla", "carlsbad", "thousand oaks", "tarrytown",
}
NON_US = {
    "canada", "toronto", "vancouver", "montreal", "ontario", "united kingdom", "uk", "england", "london",
    "cambridge, uk", "oxford", "scotland", "ireland", "dublin", "germany", "berlin", "munich", "france", "paris",
    "netherlands", "amsterdam", "leiden", "switzerland", "zurich", "basel", "geneva", "spain", "barcelona",
    "madrid", "portugal", "lisbon", "porto", "italy", "milan", "denmark", "copenhagen", "sweden", "stockholm",
    "norway", "finland", "belgium", "austria", "poland", "warsaw", "israel", "tel aviv", "india", "bangalore",
    "bengaluru", "hyderabad", "pune", "mumbai", "delhi", "singapore", "china", "shanghai", "beijing",
    "hong kong", "japan", "tokyo", "korea", "seoul", "australia", "sydney", "melbourne", "brazil",
    "mexico", "argentina", "colombia", "emea", "apac", "latam", "europe",
}
US_MARKERS = re.compile(r"\b(united states|usa|u\.s\.a?\.?|us-based|us only|(?<![a-z])us(?![a-z]))\b", re.I)
REMOTE = re.compile(r"\b(remote|anywhere|distributed|work from home|wfh)\b", re.I)
_STATE_ABBR = re.compile(r"(?:,|\s-)\s*([A-Z]{2})\b")


def classify(location: str, country_hint: str | None = None, remote_hint: bool | None = None) -> tuple[str | None, bool]:
    """Return (country, is_remote). country is "US", "XX" (non-US) or None (unknown)."""
    loc = location or ""
    low = loc.lower()
    is_remote = bool(remote_hint) or bool(REMOTE.search(loc))
    if country_hint:
        c = country_hint.strip().upper()
        if c in {"US", "USA", "UNITED STATES", "UNITED STATES OF AMERICA"}:
            return "US", is_remote
        if len(c) == 2 or len(c) > 3:
            return "XX", is_remote
    if US_MARKERS.search(loc):
        return "US", is_remote
    for abbr in _STATE_ABBR.findall(loc):
        if abbr in _ABBREV:
            return "US", is_remote
    if re.search(r"united kingdom|\b(uk|england|scotland)\b", low):
        return "XX", is_remote
    tokens = re.split(r"[,;/|()\-–]+", low)
    tokens = [t.strip() for t in tokens if t.strip()]
    if any(t in NON_US for t in tokens):
        # A multi-location posting that also lists a US place still counts as US.
        if any(t in US_CITIES or t in US_STATES for t in tokens):
            return "US", is_remote
        return "XX", is_remote
    if any(t in US_CITIES or t in US_STATES for t in tokens) or any(c in low for c in ("san francisco", "new york")):
        return "US", is_remote
    return None, is_remote
