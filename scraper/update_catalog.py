"""Keeps the AQW wiki item catalog (items.json) in sync with aqwwiki.wikidot.com.

The wiki's item listing is read newest-edit-first, 50 items per page. Every
result already contains the item's page content and tags, so no item pages are
fetched. New or changed items (compared by a fingerprint of their content and
tags) are saved, and the run stops after STOP_AFTER_UNCHANGED items in a row
that are already stored unchanged: everything further down is older.

The first run (empty catalog) reads the whole listing. A run that hits its time
limit records the page it reached; the next run catches up on new edits at the
top first, then continues from there.

Usage:
    python update_catalog.py --catalog items.json [--time-limit-minutes 330]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag

WIKI_ORIGIN = "http://aqwwiki.wikidot.com"

# Every item page, newest edit first. Pages that aren't items (NPCs, quests,
# shops, ...) are excluded, as in the extension's own searches.
LISTING_PATH = (
    "/search-items-by-tag/parent/-=/tags/"
    "-npc%20-war%20-location%20-monster%20-cutscene%20-minigame%20-disambiguation"
    "%20-bolbadge%20-cpbadge%20-quest%20-shop%20-enhancement%20-_index%20-_redirect"
    "/perPage/250/order/updated_at%20desc"
)
PER_PAGE = 250  # Wikidot caps ListPages at 250 per page

# More than 1: an edit that changes nothing we store (e.g. source formatting)
# still moves an item to the top of the listing.
STOP_AFTER_UNCHANGED = 25

PAUSE_BETWEEN_PAGES = 1.0  # seconds; be polite to Wikidot
REQUEST_TIMEOUT = 60
REQUEST_ATTEMPTS = 3
USER_AGENT = "AQW-Lodestar-catalog/1.0 (daily item index for the AQW Lodestar extension)"

CATALOG_VERSION = 2
IMAGE_BLOCKLIST = ("/image-tags/", "acsmall", "aclarge", "raresmall", "legendsmall", "membersmall", "map", "npc")
ITEM_LINK = re.compile(r"^/[^/:#?]+$")  # single-segment wiki path: no system or category pages

# Rarity keys, matching the extension's shared/rarity.js.
RARITY_TEXT_RULES = (
    ("upgrade", "upgrade"),
    ("seasonal", "seasonal"),
    ("limited quantity", "lqs"),
    ("legendary", "legendary"),
    ("benevolent", "benevolent"),
    ("champion", "champion"),
    ("collector", "collector"),
    ("secret", "secret"),
    ("hidden", "secret"),
    ("rare", "rare"),  # last, so "Seasonal Rare" isn't plain "Rare"
)
RARITY_TAG_FALLBACKS = ("seasonal", "rare", "lqs")


def log(message: str) -> None:
    print(message, flush=True)


# ---------- fetching ----------

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


def fetch_html(url: str) -> str:
    last_error: Exception | None = None
    for attempt in range(1, REQUEST_ATTEMPTS + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            response.encoding = response.encoding or "utf-8"
            return response.text
        except requests.RequestException as error:
            last_error = error
            if attempt < REQUEST_ATTEMPTS:
                time.sleep(5 * attempt)
    raise RuntimeError(f"{url}: {last_error}")


# ---------- parsing one result ----------

def tidy(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def absolute(href: str) -> str:
    return urljoin(WIKI_ORIGIN + "/", href)


def is_item_image(src: str | None) -> bool:
    if not src:
        return False
    lower = src.lower()
    return not any(word in lower for word in IMAGE_BLOCKLIST)


def find_images(content: Tag) -> list[str]:
    """Male/female tab images if present, otherwise the best single image."""
    tab_images = []
    for tab in content.select(".yui-content > div"):
        img = tab.find("img")
        src = img.get("src") if img else None
        if is_item_image(src):
            tab_images.append(absolute(src))
    if tab_images:
        return tab_images[:2]

    candidates = [img.get("src") for img in content.find_all("img")]
    candidates = [src for src in candidates if is_item_image(src)]
    pick = next((src for src in candidates if "imgur.com" in src.lower()), candidates[0] if candidates else None)
    return [absolute(pick)] if pick else []


def find_label(content: Tag, pattern: re.Pattern) -> Tag | None:
    return next((strong for strong in content.find_all("strong") if pattern.match(strong.get_text())), None)


def extract_field(content: Tag, label: str) -> str | None:
    """Text after "<strong>Label:</strong>" up to the next <br> or label."""
    wanted = label.lower()
    strong = next(
        (el for el in content.find_all("strong") if el.get_text().strip().rstrip(":").strip().lower() == wanted),
        None,
    )
    if strong is None:
        return None
    parts = []
    for node in strong.next_siblings:
        if isinstance(node, Tag) and node.name in ("br", "strong"):
            break
        parts.append(node.get_text() if isinstance(node, Tag) else str(node))
    return tidy("".join(parts)) or None


def parse_locations(content: Tag) -> list[dict]:
    """"Location: <a>Map</a> - <a>Shop</a>", or a "Locations:" label followed by a list."""
    label = find_label(content, re.compile(r"^\s*Locations?\s*:?\s*$", re.I))
    paragraph = label.find_parent("p") if label else None
    if paragraph is None:
        return []

    def to_entry(links: list[Tag]) -> dict:
        return {"name": " - ".join(tidy(a.get_text()) for a in links), "paths": [a.get("href") for a in links]}

    inline = []
    for node in label.next_siblings:
        if not isinstance(node, Tag):
            continue
        if node.name in ("br", "strong"):
            break
        if node.name == "a":
            inline.append(node)
    if inline:
        return [to_entry(inline)]

    listing = paragraph.find_next_sibling("ul")
    if listing is None:
        return []
    entries = []
    for li in listing.find_all("li", recursive=False):
        links = li.find_all("a")
        if links:
            entries.append(to_entry(links))
    return entries


def get_rarity(rarity_text: str | None, tags: list[str]) -> str | None:
    if "pseudo-rare" in tags:
        return "pseudo-rare"
    text = (rarity_text or "").lower()
    if text:
        for match, key in RARITY_TEXT_RULES:
            if match in text:
                return key
    return next((tag for tag in RARITY_TAG_FALLBACKS if tag in tags), None)


def fingerprint(content: Tag, tags: list[str]) -> str:
    """Hash of everything the result shows. Scripts are left out: their generated
    ids change between renders without the page changing."""
    clone = BeautifulSoup(str(content), "lxml")
    for script in clone.find_all("script"):
        script.decompose()
    parts = [
        tidy(clone.get_text()),
        *[a.get("href") for a in clone.find_all("a", href=True)],
        *[img.get("src") for img in clone.find_all("img", src=True)],
        ",".join(tags),
    ]
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:16]


def parse_result(block: Tag) -> tuple[str, dict] | None:
    title_link = next(
        (a for a in block.select("p strong a[href]") if ITEM_LINK.match(a.get("href", ""))),
        None,
    )
    content = block.select_one(".item-content")
    if title_link is None or content is None:
        return None

    tags = [a.get_text().strip() for a in block.select(".item-tags-tooltip a")]
    tags = [tag for tag in tags if tag and not tag.startswith("_")]

    return title_link["href"], {
        "name": tidy(title_link.get_text()),
        "tags": tags,
        "rarity": get_rarity(extract_field(content, "Rarity"), tags),
        "price": extract_field(content, "Price"),
        "locations": parse_locations(content),
        "images": find_images(content),
        "hash": fingerprint(content, tags),
    }


# ---------- listing pages ----------

def parse_listing_html(html: str) -> dict:
    """Results on one listing page, plus "961 results" and "page 1 of 20"."""
    soup = BeautifulSoup(html, "lxml")
    results = [r for r in (parse_result(b) for b in soup.select("#page-content .list-pages-item")) if r]

    box = soup.select_one("#page-content .list-pages-box")
    total = re.search(r"([\d,]+)\s+results?", box.get_text(), re.I) if box else None
    pager = soup.select_one(".pager .pager-no")
    pages = re.search(r"of\s+(\d+)", pager.get_text(), re.I) if pager else None
    return {
        "results": results,
        "total": int(total.group(1).replace(",", "")) if total else None,
        "pages": int(pages.group(1)) if pages else None,
    }


def fetch_listing_page(page: int) -> dict:
    return parse_listing_html(fetch_html(f"{WIKI_ORIGIN}{LISTING_PATH}/p/{page}"))


# ---------- sync ----------

def empty_catalog() -> dict:
    return {"version": CATALOG_VERSION, "generatedAt": None, "meta": {"scanUntilPage": None}, "items": {}}


def sync_catalog(catalog: dict, deadline: float = float("inf"), fetch_page=fetch_listing_page, pause: float = PAUSE_BETWEEN_PAGES) -> dict:
    """Mutates catalog in place and returns run statistics."""
    meta = catalog.setdefault("meta", {})
    items = catalog.setdefault("items", {})
    first_run = not items and meta.get("scanUntilPage") is None
    # Don't stop early before this page: a previous run was cut short there,
    # or this is the first run and the whole listing is needed.
    must_reach = float("inf") if first_run else (meta.get("scanUntilPage") or 0)

    stats = {"pages": 0, "added": 0, "updated": 0, "unchanged": 0, "stoppedBy": None}
    last_page = None
    unchanged_in_a_row = 0
    jumped = False
    page = 1

    while True:
        if time.monotonic() > deadline:
            stats["stoppedBy"] = "time limit"
            break
        if last_page is not None and page > last_page:
            stats["stoppedBy"] = "end of listing"
            break

        try:
            listing = fetch_page(page)
        except Exception as error:  # remember where we were, like the time limit
            stats["stoppedBy"] = "error"
            stats["error"] = str(error)
            break
        stats["pages"] += 1
        if listing["pages"] is not None:
            last_page = listing["pages"]
        if not listing["results"]:
            stats["stoppedBy"] = "end of listing"
            break

        for path, item in listing["results"]:
            stored = items.get(path)
            if stored and stored.get("hash") == item["hash"]:
                stats["unchanged"] += 1
                unchanged_in_a_row += 1
                continue
            items[path] = item
            stats["updated" if stored else "added"] += 1
            unchanged_in_a_row = 0

        if unchanged_in_a_row >= STOP_AFTER_UNCHANGED:
            if page >= must_reach:
                stats["stoppedBy"] = "caught up"
                break
            # Resuming a cut-short run: the newest edits are in, so jump to
            # where that run stopped.
            if must_reach != float("inf") and not jumped:
                jumped = True
                unchanged_in_a_row = 0
                page = int(must_reach)
                time.sleep(pause)
                continue

        page += 1
        time.sleep(pause)

    if stats["stoppedBy"] in ("time limit", "error"):
        meta["scanUntilPage"] = max(page, 0 if must_reach == float("inf") else int(must_reach))
        log(f"Stopped at page {page} ({stats.get('error') or stats['stoppedBy']}); the next run continues from there.")
    else:
        meta["scanUntilPage"] = None

    stats["items"] = len(items)
    catalog["version"] = CATALOG_VERSION
    catalog["generatedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    meta["lastRun"] = {"at": catalog["generatedAt"], **stats}
    return stats


# ---------- reading and writing ----------

def load_catalog(path: Path) -> dict:
    if not path.exists() or path.stat().st_size == 0:
        return empty_catalog()
    catalog = json.loads(path.read_text("utf-8"))
    return catalog if isinstance(catalog.get("items"), dict) else empty_catalog()


def save_catalog(catalog: dict, path: Path) -> None:
    """One item per line, sorted by path, so daily git diffs stay small."""
    dump = lambda value: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    header = {key: value for key, value in catalog.items() if key != "items"}
    lines = [f"{dump(item_path)}:{dump(item)}" for item_path, item in sorted(catalog["items"].items())]
    body = "{" + dump(header)[1:-1] + ',"items":{\n' + ",\n".join(lines) + "\n}}\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(body, "utf-8")
    tmp.replace(path)


def write_step_summary(stats: dict) -> None:
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_file:
        return
    with open(summary_file, "a", encoding="utf-8") as summary:
        summary.write(
            "### Item catalog\n\n"
            f"| Added | Updated | Unchanged | Pages read | Items in catalog | Stopped by |\n"
            f"|---|---|---|---|---|---|\n"
            f"| {stats['added']} | {stats['updated']} | {stats['unchanged']} | {stats['pages']} | {stats['items']} | {stats['stoppedBy']} |\n"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog", type=Path, default=Path("items.json"), help="catalog file to update (created if missing)")
    parser.add_argument("--time-limit-minutes", type=float, default=330, help="stop and save after this long (default 330)")
    args = parser.parse_args()

    catalog = load_catalog(args.catalog)
    log(f"Loaded {len(catalog['items'])} items from {args.catalog}")
    deadline = time.monotonic() + args.time_limit_minutes * 60

    stats = sync_catalog(catalog, deadline)
    save_catalog(catalog, args.catalog)  # saved even after an error, so progress is kept
    log(f"Done: {stats}")
    write_step_summary(stats)
    return 1 if stats["stoppedBy"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
