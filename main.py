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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from xml.etree import ElementTree

import requests
from bs4 import BeautifulSoup


LOGGER = logging.getLogger("metal-greece")
ROOT = Path(__file__).resolve().parent
OUTPUT_FILE = ROOT / "shows.json"
REQUEST_TIMEOUT = 45
EVENT_HORIZON_DAYS = 365

# These are landing pages, rather than fragile individual event URLs. Sites
# can change their layout without requiring a code change here.
DEFAULT_SOURCES = {
	"Rocking.gr Agenda": "https://www.rocking.gr/agenda",
	"Rocking.gr Future Agenda": "https://www.rocking.gr/agenda",
	"Metal Storm Events": "https://www.metalstorm.net/events/country/greece",
	"Concerts-Metal Greece": "https://www.concerts-metal.com/country/Greece",
	"Songkick Athens Metal": "https://www.songkick.com/metro-areas/28965-greece-athens/genre/metal",
	"Bandsintown Athens Metal": "https://www.bandsintown.com/c/athens-greece?genre=metal",
	"Metalwar.gr": "https://metalwar.gr/",
	"More.com Greece Music": "https://www.more.com/gr-el/tickets/music/",
	"More.com Music Sitemap": "https://www.more.com/googlesitemap.xml",
	"Gagarin 205": "https://gagarin205.gr/events/",
	"Gagarin 205 Announcements": "https://gagarin205.gr/",
	"Fuzz Club": "https://www.fuzzclub.gr/events/list/",
	"Floyd": "https://www.floyd.gr/events/list/",
	"Eightball Club": "https://eightballclub.gr/",
	"Kyttaro Live": "https://www.kyttarolive.gr/",
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
METAL_VENUES = re.compile(
	r"\b(temple|fuzz|gagarin|eightball|arch club|architektoniki|kyttaro|"
	r"principal|an club|floyd|we cultural|gazarte)\b",
	re.IGNORECASE,
)
GREEK_CITIES = {
	"athens": "athens",
	"αθηνα": "athens",
	"αθήνα": "athens",
	"αθηναs": "athens",
	"thessaloniki": "thessaloniki",
	"θεσσαλονικη": "thessaloniki",
	"θεσσαλονίκη": "thessaloniki",
	"πάτρα": "other",
}
GREEK_MONTHS = {
	"ιανουαρίου": 1,
	"φεβρουαρίου": 2,
	"μαρτίου": 3,
	"απριλίου": 4,
	"μαΐου": 5,
	"ιουνίου": 6,
	"ιουλίου": 7,
	"αυγούστου": 8,
	"σεπτεμβρίου": 9,
	"οκτωβρίου": 10,
	"νοεμβρίου": 11,
	"δεκεμβρίου": 12,
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


def parse_display_date(value: str, default_year: int | None = None) -> str | None:
	"""Parse dates rendered in event-card text, such as 'Sat, Sep 26, 2026'."""
	value = re.sub(r"\s+", " ", value).strip()
	match = re.search(
		r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+([A-Za-z]{3,9})\s+(\d{1,2})(?:,\s*(\d{4}))?",
		value,
		re.IGNORECASE,
	)
	if not match:
		return None
	year = int(match.group(3) or default_year or date.today().year)
	try:
		return datetime.strptime(f"{match.group(1)} {match.group(2)} {year}", "%b %d %Y").date().isoformat()
	except ValueError:
		try:
			return datetime.strptime(f"{match.group(1)} {match.group(2)} {year}", "%B %d %Y").date().isoformat()
		except ValueError:
			return None


def parse_month_day(value: str, default_year: int | None = None) -> str | None:
	"""Parse dates such as 'October 7', '7 October', or '22.09.2026'."""
	value = re.sub(r"\s+", " ", value).strip()
	numeric = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", value)
	if numeric:
		try:
			return date(int(numeric.group(3)), int(numeric.group(2)), int(numeric.group(1))).isoformat()
		except ValueError:
			return None
	year = default_year or date.today().year
	english = re.search(
		r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,?\s+(\d{4}))?\b",
		value,
		re.IGNORECASE,
	)
	if english:
		try:
			candidate = datetime.strptime(
				f"{english.group(0).replace(',', '')} {'' if english.group(2) else year}".strip(),
				"%B %d %Y",
			).date()
			if candidate < date.today() and not english.group(2):
				candidate = candidate.replace(year=candidate.year + 1)
			return candidate.isoformat()
		except ValueError:
			return None
	greek = re.search(r"\b(\d{1,2})\s+(" + "|".join(GREEK_MONTHS) + r")\b", value, re.IGNORECASE)
	if greek:
		return parse_greek_date(greek.group(1), greek.group(2), year)
	return None


def parse_greek_date(day: str, month: str, default_year: int | None = None) -> str | None:
	month_number = GREEK_MONTHS.get(month.casefold().strip())
	if not month_number:
		return None
	year = default_year or date.today().year
	try:
		candidate = date(year, month_number, int(day))
		if candidate < date.today() and year == date.today().year:
			candidate = date(year + 1, month_number, int(day))
		return candidate.isoformat()
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


def city_label(city: str) -> str:
	return {
		"athens": "Athens",
		"thessaloniki": "Thessaloniki",
		"other": "Other city",
	}.get(city, "Other city")


def clean_venue(venue: str, city: str) -> str:
	venue = re.sub(r"\s+", " ", venue).strip(" ,") or "Venue TBA"
	label = city_label(city)
	venue = re.sub(r"\s*,?\s*(?:Athens|Αθήνα|Thessaloniki|Θεσσαλονίκη|Πάτρα|Patra)\s*$", "", venue, flags=re.IGNORECASE)
	return f"{venue}, {label}"


def canonical_band(value: str) -> str:
	value = value.casefold()
	value = re.sub(r"\b(special guests?|with special guests?|live in (athens|greece)|athens|thessaloniki)\b", "", value)
	return re.sub(r"[^a-z0-9α-ω]+", "", value)


def clean_band_name(value: str) -> str:
	value = re.sub(r"\s+", " ", value).strip()
	if "|" in value and re.search(r"\b\d{1,2}\b", value):
		value = value.split("|", 1)[0].strip()
	return re.split(
		r"\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Δευτέρα|Τρίτη|Τετάρτη|Πέμπτη|Παρασκευή|Σάββατο|Κυριακή)\b.*$",
		value,
		maxsplit=1,
		flags=re.IGNORECASE,
	)[0].strip(" -|–—")


def within_horizon(event_date: str) -> bool:
	try:
		candidate = date.fromisoformat(event_date)
	except ValueError:
		return False
	return date.today() <= candidate <= date.today() + timedelta(days=EVENT_HORIZON_DAYS)


def normalize_event(event: dict[str, Any], source: str, page_url: str) -> dict[str, str] | None:
	name = clean_band_name(as_text(event.get("name")))
	event_date = parse_date(event.get("startDate"))
	location = event.get("location", {})
	venue = as_text(location.get("name") if isinstance(location, dict) else location)
	address = location.get("address", {}) if isinstance(location, dict) else {}
	if isinstance(address, dict):
		venue = ", ".join(part for part in [venue, as_text(address.get("addressLocality"))] if part)
	searchable = f"{name} {venue} {as_text(event.get('description'))}"
	if not name or not event_date or not within_horizon(event_date):
		return None
	if not METAL_WORDS.search(searchable):
		return None
	link = event.get("url") or page_url
	return {
		"date": event_date,
		"band": name,
		"venue": clean_venue(venue, city_for(searchable)),
		"city": city_for(searchable),
		"link": urljoin(page_url, str(link)),
		"source": source,
	}


def normalize_card(
	name: str,
	event_date: str | None,
	venue: str,
	city: str,
	link: str,
	source: str,
	page_url: str,
	*,
	allow_without_metal_word: bool = False,
) -> dict[str, str] | None:
	"""Normalize an event found in ordinary HTML instead of JSON-LD."""
	name = clean_band_name(name)
	searchable = f"{name} {venue} {city}"
	if not name or not event_date or not within_horizon(event_date):
		return None
	if not allow_without_metal_word and not (METAL_WORDS.search(searchable) or METAL_VENUES.search(venue)):
		return None
	return {
		"date": event_date,
		"band": name,
		"venue": clean_venue(venue, city_for(searchable)),
		"city": city_for(searchable),
		"link": urljoin(page_url, link),
		"source": source,
	}


def scrape_rocking_agenda(soup: BeautifulSoup, source: str, page_url: str) -> list[dict[str, str]]:
	shows = []
	for anchor in soup.select('a[href*="/agenda/20"]'):
		match = re.search(r"/agenda/(\d{4})/(\d{1,2})/(\d{1,2})/", anchor.get("href", ""))
		if not match:
			continue
		text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
		parts = re.split(r"\s+@\s+", text, maxsplit=1)
		if len(parts) != 2:
			continue
		name, location = parts
		original_name = name
		name = re.split(
			r"\s+(?:Δευτέρα|Τρίτη|Τετάρτη|Πέμπτη|Παρασκευή|Σάββατο|Κυριακή|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+",
			name,
			maxsplit=1,
			flags=re.IGNORECASE,
		)[0].strip()
		name = re.sub(r"\s+(?:Athens|Αθήνα|Θεσσαλονίκη|Thessaloniki|Πάτρα|Patra)\s*$", "", name, flags=re.IGNORECASE).strip()
		city_match = re.search(r"\b(Athens|Αθήνα|Θεσσαλονίκη|Thessaloniki|Πάτρα|Patra)\b", f"{original_name} {location}", re.IGNORECASE)
		city = city_match.group(1) if city_match else ""
		venue = re.sub(r"\s*(Athens|Αθήνα|Θεσσαλονίκη|Thessaloniki|Πάτρα|Patra)\b.*$", "", location, flags=re.IGNORECASE).strip()
		venue = re.sub(r"\s+(?:Από\s+)?\d+(?:\s*e)?(?:\s*/\s*\d+(?:\s*e)?)?\s*$", "", venue, flags=re.IGNORECASE).strip()
		show = normalize_card(
			name,
			f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}",
			venue,
			city,
			anchor.get("href", ""),
			source,
			page_url,
		)
		if show:
			shows.append(show)
	return shows


def scrape_bandsintown(soup: BeautifulSoup, source: str, page_url: str) -> list[dict[str, str]]:
	shows = []
	for anchor in soup.select('a[href*="/e/"]'):
		href = anchor.get("href", "")
		text = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
		if not text or not re.search(r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),?\s+[A-Za-z]{3,9}\s+\d{1,2}", text):
			continue
		date_value = parse_display_date(text)
		parts = re.split(r"\s+•\s+", text)
		name = parts[0].split(", ", 1)[0].strip()
		venue = parts[-1].strip() if len(parts) > 1 else ""
		show = normalize_card(
			name,
			date_value,
			venue,
			"athens",
			href,
			source,
			page_url,
			allow_without_metal_word=True,
		)
		if show:
			shows.append(show)
	return shows


def scrape_metalwar_feed(xml: str, source: str, page_url: str) -> list[dict[str, str]]:
	"""Extract future live dates announced in Metalwar's WordPress RSS feed."""
	root = ElementTree.fromstring(xml)
	shows = []
	for item in root.findall(".//item"):
		categories = " ".join(category.text or "" for category in item.findall("category"))
		title = item.findtext("title", default="")
		description = item.findtext("description", default="")
		content = BeautifulSoup(f"{categories} {title} {description}", "html.parser").get_text(" ", strip=True)
		if not re.search(r"\b(live|festival|tour|συναυλ|εμφάνιση|φεστιβάλ)\b", content, re.IGNORECASE):
			continue
		date_matches = list(re.finditer(r"(?<!\d)(\d{1,2})\s+(" + "|".join(GREEK_MONTHS) + r")(?=$|\s|[–—-])", content, re.IGNORECASE))
		for index, match in enumerate(date_matches):
			day, month = match.groups()
			event_date = parse_greek_date(day, month)
			if not event_date:
				continue
			name = title or "Metal event"
			name = name.split(":", 1)[0].strip()
			next_start = date_matches[index + 1].start() if index + 1 < len(date_matches) else len(content)
			date_context = content[match.end():next_start]
			cities = re.findall(r"Athens|Αθήνα|Θεσσαλονίκη|Thessaloniki", date_context, re.IGNORECASE)
			if not cities:
				cities = re.findall(r"Athens|Αθήνα|Θεσσαλονίκη|Thessaloniki", content, re.IGNORECASE)
			city = cities[0] if cities else ""
			link = item.findtext("link", default="")
			show = normalize_card(
				name,
				event_date,
				"Venue TBA",
				city,
				link,
				source,
				page_url,
				allow_without_metal_word=True,
			)
			if show:
				shows.append(show)
	return shows


def event_link_text(anchor: Any) -> str:
	return re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))


def scrape_official_calendar(
	soup: BeautifulSoup,
	source: str,
	page_url: str,
	*,
	city: str = "athens",
	link_pattern: str = "/event/",
	session: requests.Session | None = None,
) -> list[dict[str, str]]:
	"""Read event cards from an official venue or ticketing calendar."""
	shows = []
	seen_links = set()
	for anchor in soup.find_all("a", href=True):
		href = anchor["href"]
		if link_pattern not in href or href in seen_links:
			continue
		seen_links.add(href)
		name = event_link_text(anchor)
		if not name or name.lower() in {"event", "more", "read more", "buy tickets"}:
			continue
		context = name
		parent = anchor
		for _ in range(4):
			parent = parent.parent
			if parent:
				context = re.sub(r"\s+", " ", parent.get_text(" ", strip=True))
				if parse_month_day(context):
					break
		date_value = parse_month_day(context)
		detail_soup = None
		detail_is_metal = False
		if session:
			try:
				detail_response = session.get(urljoin(page_url, href), headers=HEADERS, timeout=REQUEST_TIMEOUT)
				detail_response.raise_for_status()
				detail_response.encoding = "utf-8"
				detail_soup = BeautifulSoup(detail_response.text, "html.parser")
				detail_text = detail_soup.get_text(" ", strip=True)
				date_value = parse_month_day(detail_text)
				detail_is_metal = bool(METAL_WORDS.search(detail_text))
				if detail_soup.find("h1"):
					name = event_link_text(detail_soup.find("h1"))
			except requests.RequestException:
				pass
		if not date_value:
			continue
		name = re.sub(
			r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}(?:,?\s+\d{4})?|\b\d{1,2}[./-]\d{1,2}[./-]\d{4}\b|\b\d{1,2}\s+(?:" + "|".join(GREEK_MONTHS) + r")\b",
			"",
			name,
			flags=re.IGNORECASE,
		).strip(" -|–—")
		name = re.split(
			r"\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|Δευτέρα|Τρίτη|Τετάρτη|Πέμπτη|Παρασκευή|Σάββατο|Κυριακή)\b.*$",
			name,
			maxsplit=1,
			flags=re.IGNORECASE,
		)[0].strip(" -|–—")
		if "|" in name and re.search(r"\b\d{1,2}\b", name):
			name = name.split("|", 1)[0].strip()
		show = normalize_card(name, date_value, "", city, href, source, page_url, allow_without_metal_word=detail_is_metal)
		if show:
			shows.append(show)
	return shows


def scrape_more_music(soup: BeautifulSoup, source: str, page_url: str) -> list[dict[str, str]]:
	shows = []
	for anchor in soup.find_all("a", href=True):
		if "/tickets/music/" not in anchor["href"]:
			continue
		text = event_link_text(anchor)
		date_value = parse_month_day(text)
		if not date_value:
			continue
		name = re.sub(
			r"^.*?(?:January|February|March|April|May|June|July|August|September|October|November|December|" + "|".join(GREEK_MONTHS) + r")\s+\d{1,2}(?:\s*-\s*\d+)?\s*",
			"",
			text,
			flags=re.IGNORECASE,
		).strip()
		show = normalize_card(name, date_value, "", "", anchor["href"], source, page_url)
		if show:
			shows.append(show)
	return shows


def scrape_more_sitemap(xml: bytes, source: str, page_url: str) -> list[dict[str, str]]:
	root = ElementTree.fromstring(xml)
	namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
	urls = [
		node.text
		for node in root.findall(".//s:loc", namespace)
		if node.text and "/gr-el/tickets/music/" in node.text
	]

	def fetch_event(url: str) -> dict[str, str] | None:
		try:
			response = requests.get(url, headers=HEADERS, timeout=(5, 12))
			response.raise_for_status()
			response.encoding = "utf-8"
			soup = BeautifulSoup(response.text, "html.parser")
			text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
			if not METAL_WORDS.search(text):
				return None
			date_value = parse_month_day(text)
			title = soup.find("h1") or soup.find("title")
			name = event_link_text(title) if title else ""
			city = "thessaloniki" if re.search(r"Thessaloniki|Θεσσαλονίκη", text, re.IGNORECASE) else "athens"
			return normalize_card(name, date_value, "Venue TBA", city, url, source, page_url, allow_without_metal_word=True)
		except (requests.RequestException, ElementTree.ParseError):
			return None

	shows = []
	with ThreadPoolExecutor(max_workers=12) as executor:
		for future in as_completed([executor.submit(fetch_event, url) for url in urls]):
			show = future.result()
			if show:
				shows.append(show)
	return shows


def scrape_rocking_future_months(soup: BeautifulSoup, source: str, page_url: str, session: requests.Session) -> list[dict[str, str]]:
	shows = []
	seen = set()
	for anchor in soup.find_all("a", href=True):
		href = urljoin(page_url, anchor["href"])
		if not re.search(r"/agenda/\d{4}/\d{1,2}(?:/(?:athens|thessaloniki))?$", href) or href in seen:
			continue
		seen.add(href)
		try:
			response = session.get(href, headers=HEADERS, timeout=REQUEST_TIMEOUT)
			response.raise_for_status()
			response.encoding = "utf-8"
			month_soup = BeautifulSoup(response.text, "html.parser")
			shows.extend(scrape_rocking_agenda(month_soup, source, response.url))
		except requests.RequestException:
			continue
	return shows


def scrape_eightball(soup: BeautifulSoup, source: str, page_url: str) -> list[dict[str, str]]:
	shows = []
	for anchor in soup.find_all("a", href=True):
		href = anchor["href"]
		if "/event/" not in href:
			continue
		text = event_link_text(anchor)
		date_value = parse_month_day(text)
		if not date_value:
			date_value = parse_month_day(href)
		if not date_value:
			continue
		name = re.split(
			r"\s+\d{1,2}(?:[./-]\d{1,2}(?:[./-]\d{4})?|\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*)\b.*$",
			text,
			maxsplit=1,
			flags=re.IGNORECASE,
		)[0].strip(" -|–—")
		if not name or re.match(r"^\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)", name, re.IGNORECASE):
			continue
		show = normalize_card(name, date_value, "Eightball Club", "thessaloniki", href, source, page_url, allow_without_metal_word=True)
		if show:
			shows.append(show)
	return shows


def scrape_event_detail(soup: BeautifulSoup, source: str, page_url: str, venue: str, city: str) -> list[dict[str, str]]:
	text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
	date_value = parse_month_day(text)
	if not date_value:
		return []
	title = soup.find("h1")
	name = event_link_text(title) if title else ""
	show = normalize_card(name, date_value, venue, city, page_url, source, page_url, allow_without_metal_word=True)
	if show:
		show["extra_links"] = [
			urljoin(page_url, anchor["href"])
			for anchor in soup.find_all("a", href=True)
			if "more.com/gr-el/tickets/" in anchor["href"]
		]
	return [show] if show else []


def scrape_discovered_details(soup: BeautifulSoup, source: str, page_url: str, session: requests.Session) -> list[dict[str, str]]:
	shows = []
	seen = set()
	for anchor in soup.find_all("a", href=True):
		href = urljoin(page_url, anchor["href"])
		if "/event/" not in href or href in seen:
			continue
		seen.add(href)
		try:
			response = session.get(href, headers=HEADERS, timeout=REQUEST_TIMEOUT)
			response.raise_for_status()
			response.encoding = "utf-8"
			detail_soup = BeautifulSoup(response.text, "html.parser")
			text = detail_soup.get_text(" ", strip=True)
			if not METAL_WORDS.search(text):
				continue
			date_value = parse_month_day(text)
			title = detail_soup.find("h1")
			name = event_link_text(title) if title else event_link_text(anchor)
			show = normalize_card(name, date_value, "Gagarin 205", "athens", href, source, page_url, allow_without_metal_word=True)
			if show:
				shows.append(show)
		except requests.RequestException:
			continue
	return shows


def scrape_source(source: str, url: str, session: requests.Session) -> list[dict[str, str]]:
	response = session.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
	response.raise_for_status()
	if source == "More.com Music Sitemap":
		return scrape_more_sitemap(response.content, source, response.url)
	response.encoding = "utf-8"
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
	if source == "Rocking.gr Agenda":
		shows.extend(scrape_rocking_agenda(soup, source, response.url))
	elif source == "Rocking.gr Future Agenda":
		shows.extend(scrape_rocking_future_months(soup, source, response.url, session))
	elif source == "Bandsintown Athens Metal":
		shows.extend(scrape_bandsintown(soup, source, response.url))
	elif source == "Metalwar.gr":
		feed_response = session.get("https://www.metalwar.gr/feed/", headers=HEADERS, timeout=REQUEST_TIMEOUT)
		feed_response.raise_for_status()
		feed_response.encoding = "utf-8"
		shows.extend(scrape_metalwar_feed(feed_response.text, source, feed_response.url))
	elif source == "More.com Greece Music":
		shows.extend(scrape_more_music(soup, source, response.url))
	elif source == "Gagarin 205":
		shows.extend(scrape_official_calendar(soup, source, response.url, link_pattern="/event/", session=session))
	elif source == "Gagarin 205 Announcements":
		shows.extend(scrape_discovered_details(soup, source, response.url, session))
	elif source == "Fuzz Club":
		shows.extend(scrape_official_calendar(soup, source, response.url, link_pattern="/event/", session=session))
	elif source == "Floyd":
		shows.extend(scrape_official_calendar(soup, source, response.url, link_pattern="/event/", session=session))
	elif source == "Eightball Club":
		shows.extend(scrape_eightball(soup, source, response.url))
	elif source == "Kyttaro Live":
		shows.extend(scrape_official_calendar(soup, source, response.url, link_pattern="/event/", session=session))
	return shows


def fingerprint(show: dict[str, str]) -> str:
	key = "|".join((show["date"], canonical_band(show["band"]), show["city"]))
	return hashlib.sha256(key.encode()).hexdigest()


def matching_key(show: dict[str, Any], records: dict[str, dict[str, Any]]) -> str:
	key = fingerprint(show)
	for existing_key, existing in records.items():
		if existing["date"] == show["date"] and existing["city"] == show["city"]:
			ratio = SequenceMatcher(None, canonical_band(existing["band"]), canonical_band(show["band"])).ratio()
			if ratio >= 0.88:
				return existing_key
	return key


def collect() -> list[dict[str, str]]:
	session = requests.Session()
	all_shows: dict[str, dict[str, str]] = {}
	for source, url in source_urls().items():
		try:
			found = scrape_source(source, url, session)
			for show in found:
				key = matching_key(show, all_shows)
				if key not in all_shows:
					show["links"] = []
					all_shows[key] = show
				links = all_shows[key]["links"]
				link_record = {"source": show["source"], "url": show["link"]}
				if link_record not in links:
					links.append(link_record)
				for extra_link in show.get("extra_links", []):
					extra_record = {"source": "Ticket page", "url": extra_link}
					if extra_record not in links:
						links.append(extra_record)
				all_shows[key]["venue"] = clean_venue(all_shows[key]["venue"], all_shows[key]["city"])
				all_shows[key].pop("link", None)
				all_shows[key].pop("extra_links", None)
			LOGGER.info("%s: found %d event(s)", source, len(found))
		except requests.RequestException as error:
			LOGGER.warning("%s unavailable: %s", source, error)
		except Exception:
			LOGGER.exception("Could not parse %s", source)
	# Collapse any records that arrived in different shapes, including legacy
	# records that still use the old single-link field.
	merged: dict[str, dict[str, Any]] = {}
	for show in all_shows.values():
		show["band"] = clean_band_name(show["band"])
		show["venue"] = clean_venue(show.get("venue", "Venue TBA"), show["city"])
		key = matching_key(show, merged)
		if key not in merged:
			show["links"] = list(show.get("links", []))
			merged[key] = show
		for link in show.get("links", []):
			if link not in merged[key]["links"]:
				merged[key]["links"].append(link)
		if show.get("link"):
			legacy_link = {"source": show.get("source", "Event"), "url": show["link"]}
			if legacy_link not in merged[key]["links"]:
				merged[key]["links"].append(legacy_link)
		merged[key].pop("link", None)
	return sorted(merged.values(), key=lambda item: (item["date"], item["band"].casefold()))


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
