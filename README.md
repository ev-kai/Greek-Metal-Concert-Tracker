# Greek Metal Concert Tracker

A lightweight Python scraper that collects upcoming metal and rock concerts in Greece for a static website or event tracker.

It gathers events from major Greek music agendas, venue calendars, and ticketing platforms, deduplicates listings across sources, and outputs a clean `shows.json` file.

---

## How It Works

1. **Fetches Event Pages**: Crawls predefined landing pages, venue calendars, RSS feeds, and XML sitemaps.
2. **Extracts Event Data**: Prefers structured `schema.org` (JSON-LD) metadata, falling back to HTML parsing when needed.
3. **Cleans & Normalizes**: Converts English and Greek dates into ISO format (`YYYY-MM-DD`), normalizes venue names, and filters out non-metal shows.
4. **Deduplicates Listings**: Uses fuzzy matching on band names, dates, and cities to merge duplicates from different sources into a single entry with combined ticket links.
5. **Generates Output**: Saves the sorted schedule to `shows.json`.

---
## Design Philosophy: API-Less & Fragility-Resilient

Most event trackers rely on official APIs, which are often restricted, rate-limited, or entirely unavailable for independent cultural aggregators. Instead of fighting for API access, this project is built on **intentional web scraping**:

- **No API Keys Required**: The scraper directly parses public-facing landing pages, calendars, RSS feeds, and XML sitemaps using requests and BeautifulSoup.
- **Graceful Degradation**: By prioritizing structured `schema.org` (JSON-LD) metadata first—while maintaining robust HTML/text fallback parsers—the scraper adapts smoothly to minor layout changes without requiring constant code updates.
- **Cross-Source Consensus**: Instead of trusting a single point of failure, it crawls multiple independent sources and uses fuzzy string matching to merge duplicate listings, ensuring high data reliability without proprietary backing.

---
## Automation & Deployment

This project uses **GitHub Actions** and **Netlify** for automated updates:

1. **Scheduled Runs**: GitHub Actions runs the scraper automatically every 20 minutes to pull the latest show updates.
2. **Auto-Deploy**: Upon generating an updated `shows.json`, GitHub Actions pushes changes to the repository, triggering an automatic site rebuild on Netlify.

---
## Output Example
```json
[
  {
  "date": "2026-10-15",
  "band": "Rotting Christ",
  "venue": "Floyd, Athens",
  "city": "athens",
  "source": "Floyd",
  "links": [
      {
      "source": "Floyd",
      "url": "https://www.floyd.gr/event/example/"
      },
      {
      "source": "Ticket page",
      "url": "https://www.more.com/gr-el/tickets/music/example/"
      }
    ]
  }
]
```

