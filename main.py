"""Collect upcoming metal shows in Greece for the static tracker.

The scraper prefers schema.org Event data because it is more stable than
CSS selectors. Sources can be replaced or extended with METAL_SOURCE_URLS,
a comma-separated environment variable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


LOGGER = logging.getLogger("metal-greece")
ROOT = Path(__file__).resolve().parent
OUTPUT_FILE = ROOT / "shows.json"
REQUEST_TIMEOUT = 20

# These are landing pages, rather than fragile individual event URLs. Sites
# can change their layout without requiring a code change here.
DEFAULT_SOURCES = {
	"Rocking.gr Agenda": "https://www.rocking.gr/agenda",
	"Metal Storm Events": "https://www.metalstorm.net/events/country/greece",
	"Concerts-Metal Greece": "https://www.concerts-metal.com/country/Greece",
	"Songkick Athens Metal": "https://www.songkick.com/metro-areas/28965-greece-athens/genre/metal",
	"Bandsintown Athens Metal": "https://www.bandsintown.com/c/athens-greece?genre=metal",
	"Metalwar.gr": "https://metalwar.gr/",
}

HEADERS = {
	"User-Agent": (
		"Mozilla/5.0 (compatible; MetalGreeceBot/1.0; "
		"+https://github.com/your-account/metal-greece)"
	),
	"Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}
METAL_WORDS = re.compile(
	r"\b(metal|rock|doom|death|black|thrash|power|progressive|\bcore|gothic|"
	r"hardcore|punk|sludge|stoner|grind|industrial)\b",
	re.IGNORECASE,
)
GREEK_CITIES = {
	"athens": "athens",
	"αθηνα": "athens",
	"αθηναs": "athens",
	"thessaloniki": "thessaloniki",
	"θεσσαλονικη": "thessaloniki",
}


def source_urls() -> dict[str, str]:
	configured = os.getenv("METAL_SOURCE_URLS")
	if not configured:
		return DEFAULT_SOURCES
	return {
		f"Custom source {index}": url.strip()
		for index, url in enumerate(configured.split(","), start=1)
		if url.strip()
	}


def parse_date(value: Any) -> str | None:
	"""Return an ISO date from common schema.org date formats."""
	if not isinstance(value, str):
		return None
	match = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", value)
	if not match:
		return None
	try:
		return date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
	except ValueError:
		return None


def as_text(value: Any) -> str:
	if isinstance(value, str):
		return BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
	if isinstance(value, dict):
		return str(value.get("name", ""))
	return ""


def event_objects(value: Any) -> list[dict[str, Any]]:
	if isinstance(value, dict):
		found = []
		if "@graph" in value:
			found.extend(event_objects(value["@graph"]))
		event_type = value.get("@type", "")
		types = event_type if isinstance(event_type, list) else [event_type]
		if any(str(item).lower().endswith("event") for item in types):
			found.append(value)
		return found
	if isinstance(value, list):
		found = []
		for item in value:
			found.extend(event_objects(item))
		return found
	return []


def city_for(text: str) -> str:
	normalized = text.casefold()
	for city_name, city_slug in GREEK_CITIES.items():
		if city_name in normalized:
			return city_slug
	return "other"


def normalize_event(event: dict[str, Any], source: str, page_url: str) -> dict[str, str] | None:
	name = as_text(event.get("name"))
	event_date = parse_date(event.get("startDate"))
	location = event.get("location", {})
	venue = as_text(location.get("name") if isinstance(location, dict) else location)
	address = location.get("address", {}) if isinstance(location, dict) else {}
	if isinstance(address, dict):
		venue = ", ".join(part for part in [venue, as_text(address.get("addressLocality"))] if part)
	searchable = f"{name} {venue} {as_text(event.get('description'))}"
	if not name or not event_date or event_date < date.today().isoformat():
		return None
	if not METAL_WORDS.search(searchable):
		return None
	link = event.get("url") or page_url
	return {
		"date": event_date,
		"band": name,
		"venue": venue or "Venue TBA",
		"city": city_for(searchable),
		"link": urljoin(page_url, str(link)),
		"source": source,
	}


def scrape_source(source: str, url: str, session: requests.Session) -> list[dict[str, str]]:
	response = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
	response.raise_for_status()
	soup = BeautifulSoup(response.text, "html.parser")
	shows = []
	for script in soup.select('script[type="application/ld+json"]'):
		try:
			payload = json.loads(script.string or script.get_text())
		except json.JSONDecodeError:
			continue
		for event in event_objects(payload):
			normalized = normalize_event(event, source, response.url)
			if normalized:
				shows.append(normalized)
	return shows


def fingerprint(show: dict[str, str]) -> str:
	key = "|".join(show[field].casefold() for field in ("date", "band", "venue"))
	return hashlib.sha256(key.encode()).hexdigest()


def collect() -> list[dict[str, str]]:
	session = requests.Session()
	all_shows: dict[str, dict[str, str]] = {}
	for source, url in source_urls().items():
		try:
			found = scrape_source(source, url, session)
			for show in found:
				all_shows[fingerprint(show)] = show
			LOGGER.info("%s: found %d event(s)", source, len(found))
		except requests.RequestException as error:
			LOGGER.warning("%s unavailable: %s", source, error)
		except Exception:
			LOGGER.exception("Could not parse %s", source)
	return sorted(all_shows.values(), key=lambda item: (item["date"], item["band"].casefold()))


def write_output(shows: list[dict[str, str]]) -> None:
	OUTPUT_FILE.write_text(json.dumps(shows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
	logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
	shows = collect()
	if not shows:
		LOGGER.warning("No shows were found; keeping the previous output file.")
		return
	write_output(shows)
	LOGGER.info("Wrote %d show(s) to %s", len(shows), OUTPUT_FILE)


if __name__ == "__main__":
	main()
