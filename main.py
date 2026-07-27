"""
main.py

Single-file image scraper: logging setup, image-URL parsing utilities, the
ProductScraper class, and the CLI entry point all live here.

Reads data/data.csv, scrapes every product listed in it, and writes
output/images.csv (one row per product image: sku, url, image_url).

Run it with:
    python main.py             # full run
    python main.py --test 20   # only the first 20 valid input rows
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterable, Iterator, Optional

import requests
from bs4 import BeautifulSoup

__version__ = "1.0.0"

# ==========================================================================
# Configuration
#
# Every tunable value the script uses lives here, in one place, instead of
# being scattered next to whichever function happens to use it. Grouped by
# the area of the script they configure.
# ==========================================================================

# --- Paths -----------------------------------------------------------------
INPUT_PATH = Path("data") / "data.csv"
OUTPUT_PATH = Path("output") / "images.csv"
LOG_FILE = Path("output") / "scraper.log"

# --- Logging -----------------------------------------------------------------
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# 5 MB per file, keep 3 backups (scraper.log.1, .2, .3) before overwriting
# the oldest. Prevents unbounded growth on large runs (e.g. thousands of
# products) while still keeping recent history on disk.
LOG_MAX_BYTES = 5_000_000
LOG_BACKUP_COUNT = 3

# All loggers hang off this single package-level logger, e.g.
# "image_scraper.scraper", "image_scraper.main". Handlers are attached HERE
# ONLY (not to the true Python root logger). Child loggers use their
# default propagate=True, so records flow up to this logger and hit its
# handlers exactly once. This logger itself has propagate=False, so those
# records stop here and never continue up to the real root logger — that
# insulates us from duplicate output if this script is ever imported into
# a larger app (Django, Flask, etc.) that configures the real root logger
# with its own handlers.
PACKAGE_LOGGER_NAME = "image_scraper"

# --- Input / output CSV columns --------------------------------------------
OUTPUT_COLUMNS = ["url", "sku", "image_url"]

# The real data.csv uses different header names than a generic spec might,
# so both are accepted here instead of hard-failing on a header mismatch.
SKU_COLUMN_CANDIDATES = ("sku", "manu_sku")
URL_COLUMN_CANDIDATES = ("url", "prod_page_url")

# --- Image-type -> folder mapping -------------------------------------------
# Each thumbnail on a product page looks like:
#
#     <img class="smallimage"
#          onclick="swapImage('wcp-fs23_extra08.jpg','Extra Image', this)">
#
# The onclick already tells us the filename + image type. The page's own
# swapImage() JS builds the large-image URL like this:
#
#     type == 'Packaging Image' -> folder = 'package_photos/package_'
#     type == 'primary'         -> folder = ''
#     type == 'Extra Image'     -> folder = 'extra_photos/extra_'
#
#     large_url = CLOUDFRONT_BASE + 'files/Primary/' + folder + 'large/' + filename
#
# We read the onclick attributes straight out of the static HTML (no
# browser needed — these attributes are already present in the page
# source) and rebuild the URL ourselves, mirroring swapImage() exactly.
CLOUDFRONT_BASE = "https://d2b9vjwb3yw5iu.cloudfront.net/"

IMAGE_TYPE_PRIMARY = "primary"
IMAGE_TYPE_EXTRA = "Extra Image"
IMAGE_TYPE_PACKAGING = "Packaging Image"

# The type mapping is intentionally explicit rather than "anything unknown
# defaults to Extra" — a future type the site introduces (e.g. "Gallery
# Image") gets logged and skipped instead of silently mapped to the wrong
# folder. Thumbnails with no onclick (padding placeholders, video-play
# icons) are skipped, and duplicate filenames — the page duplicates the
# whole carousel in the DOM — are removed.
KNOWN_TYPE_FOLDERS = {
    IMAGE_TYPE_PACKAGING: "package_photos/package_",
    IMAGE_TYPE_PRIMARY: "",
    IMAGE_TYPE_EXTRA: "extra_photos/extra_",
}

# matches: swapImage('filename.jpg','Type', this)  (single/double quotes, spacing varies)
SWAPIMAGE_RE = re.compile(
    r"""swapImage\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]"""
)

# Short, well-known phrases used by CAPTCHA/challenge pages (Cloudflare,
# reCAPTCHA, hCaptcha, generic "verify you're human" interstitials). This
# is a heuristic, not a guarantee — it exists only to turn a silent
# "0 images found" into an actionable log line when the site starts
# blocking the scraper instead of serving real product pages.
BOT_PROTECTION_MARKERS = (
    "captcha",
    "checking your browser",
    "verify you are human",
    "verify you're human",
    "attention required! | cloudflare",
    "access denied",
)

# --- HTTP / scraping behavior ------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT_SECONDS = 15
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 2.0  # doubles each retry: 2s, 4s, 8s
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}  # transient — worth retrying; 404/403 are not

# Pause between products so a run of thousands of pages doesn't hammer the
# site back-to-back. Modest cost on total runtime, meaningfully reduces
# the chance of tripping rate limiting in the first place.
REQUEST_DELAY_SECONDS = 0.2

# GET-check (stream=True, body never read) every constructed image URL
# before trusting it, so a folder-naming change on the site shows up as a
# logged warning instead of a silently broken link in the output. GET is
# used instead of HEAD because some CDN configs don't support HEAD
# reliably, while GET is universally supported.
#
# Off by default: for a full run (thousands of products x ~8 images each)
# this is tens of thousands of extra HTTP requests, which meaningfully
# slows the run down. Turn it on with --validate when you want the safety
# check — e.g. after the site changes something, or for a first run
# against a catalog you haven't scraped before.
DEFAULT_VALIDATE_IMAGES = False
VALIDATE_TIMEOUT_SECONDS = 10


# ==========================================================================
# Logging setup
#
# Every part of this file calls get_logger() instead of calling
# logging.getLogger() directly or using print(). This guarantees a single,
# consistent log format (timestamp, level, module, message) across the
# whole run, and routes logs to both the console and a persistent file
# without repeating configuration in multiple places.
# ==========================================================================

_configured = False


def _configure_package_logger() -> None:
    """
    Set up the package-level logger once with two handlers:
      - StreamHandler        -> stdout, for live progress while the scraper runs.
      - RotatingFileHandler  -> output/scraper.log, for a persistent audit trail
        that rotates once it hits LOG_MAX_BYTES instead of growing forever.

    Guarded by a module-level flag so repeated calls to get_logger()
    don't attach duplicate handlers, which would otherwise cause every
    log line to be printed multiple times.
    """
    global _configured
    if _configured:
        return

    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    package_logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    package_logger.setLevel(logging.INFO)
    package_logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    package_logger.addHandler(console_handler)
    package_logger.addHandler(file_handler)

    _configured = True


def get_logger(name: str) -> logging.Logger:
    """
    Return a module-scoped logger, e.g.:
        logger = get_logger("scraper")
        logger = get_logger("main")

    This ensures the package logger is configured exactly once, then
    returns a child logger namespaced under "image_scraper" (e.g.
    "image_scraper.scraper") so log lines are attributed to the part of
    the script that emitted them. The child keeps the default
    propagate=True, so its records flow up to the "image_scraper" logger's
    handlers — it must NOT be set to False here, or records would have
    nowhere to go, since handlers live on the package logger, not on each
    child.

    Args:
        name: short, module-scoped suffix (e.g. "scraper", "utils", "main").

    Returns:
        A logging.Logger namespaced under "image_scraper.<name>".
    """
    _configure_package_logger()
    return logging.getLogger(f"{PACKAGE_LOGGER_NAME}.{name}")


utils_logger = get_logger("utils")
scraper_logger = get_logger("scraper")
logger = get_logger("main")


# ==========================================================================
# Image-URL parsing utilities
# ==========================================================================

def _looks_like_bot_protection(html: Optional[str]) -> bool:
    """
    Return True if html appears to be a CAPTCHA/challenge page rather than
    a real product page, based on BOT_PROTECTION_MARKERS. Heuristic only.

    Args:
        html: raw page HTML, or None.

    Returns:
        True if a known bot-protection marker phrase is found, else False.
    """
    if not html:
        return False
    lowered = html.lower()
    return any(marker in lowered for marker in BOT_PROTECTION_MARKERS)


def _folder_for_type(img_type: str) -> Optional[str]:
    """
    Return the folder prefix for a known image type, or None for an
    unrecognized one. Returning None (instead of guessing "extra") means
    an unfamiliar type — e.g. a future "Gallery Image" or "360 Image" —
    gets logged and skipped by the caller rather than silently mapped to
    the wrong folder.

    Args:
        img_type: the image type string parsed from a swapImage() call
            (e.g. "primary", "Extra Image", "Packaging Image").

    Returns:
        The CloudFront folder prefix for that type, or None if unknown.
    """
    return KNOWN_TYPE_FOLDERS.get(img_type)


def _large_url_for(filename: str, img_type: str) -> Optional[str]:
    """
    Build the full-resolution CloudFront URL for one thumbnail.

    Args:
        filename: image filename parsed from swapImage() (e.g. "wcp-fs23_extra08.jpg").
        img_type: image type parsed from swapImage() (e.g. "Extra Image").

    Returns:
        The full-resolution image URL, or None if img_type is unrecognized.
    """
    folder = _folder_for_type(img_type)
    if folder is None:
        return None
    return f"{CLOUDFRONT_BASE}files/Primary/{folder}large/{filename}"


def parse_image_urls(soup: Optional[BeautifulSoup], primary_only: bool = False) -> list[str]:
    """
    Return a deduped list of high-resolution image URLs found on a product
    page. Never raises.

    Args:
        soup: parsed product page, or None.
        primary_only: if True, skip every "Extra Image" and "Packaging
            Image" thumbnail and keep only the single primary product photo.

    Returns:
        A list of full-resolution image URLs, in page order, with
        duplicate (filename, type) pairs removed.
    """
    if soup is None:
        return []

    try:
        thumbnails = soup.select("img.smallimage")
    except Exception as exc:
        utils_logger.warning("Could not select thumbnails: %s", exc)
        return []

    seen: set[tuple[str, str]] = set()
    urls: list[str] = []
    for thumb in thumbnails:
        onclick = thumb.get("onclick")
        if not onclick:
            continue  # placeholder / no-image thumb, skip

        match = SWAPIMAGE_RE.search(onclick)
        if not match:
            continue
        filename = match.group(1).strip()
        img_type = match.group(2).strip()

        if primary_only and img_type != IMAGE_TYPE_PRIMARY:
            continue

        key = (filename, img_type)
        if key in seen:
            continue  # duplicate carousel copy
        seen.add(key)

        image_url = _large_url_for(filename, img_type)
        if image_url is None:
            utils_logger.warning("Unknown image type %r for %r, skipping", img_type, filename)
            continue

        urls.append(image_url)

    return urls


# --------------------------------------------------------------------------
# Building and writing output rows
# --------------------------------------------------------------------------

def make_record(sku: str, url: str, image_url: str) -> dict[str, str]:
    """
    Build one output row dict. Centralizes the row shape so every place
    that produces an output record (and OUTPUT_COLUMNS) agrees on it.

    Args:
        sku: product SKU.
        url: product page URL.
        image_url: full-resolution image URL.

    Returns:
        A dict with keys "sku", "url", "image_url".
    """
    return {"sku": sku, "url": url, "image_url": image_url}


def write_images_csv(rows: Iterable[dict[str, str]], path: Path) -> None:
    """
    Write image rows to a CSV file, creating the output folder if needed.

    Args:
        rows: iterable of dicts shaped like make_record()'s output.
        path: destination CSV path.
    """
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with open(output_path, mode="w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in OUTPUT_COLUMNS})
            written += 1

    utils_logger.info("Wrote %d image rows to %s", written, output_path)


# ==========================================================================
# ProductScraper — fetches one product page and finds its high-res image
# URLs. All URL-building logic lives above, not here — this class's only
# job is fetching + parsing the page.
# ==========================================================================

class ProductScraper:
    """Fetches a product page and extracts its high-resolution image URLs."""

    def __init__(self, validate: bool = DEFAULT_VALIDATE_IMAGES, primary_only: bool = False) -> None:
        """
        Args:
            validate: if True, GET-check every constructed image URL
                before trusting it (see VALIDATE_TIMEOUT_SECONDS).
            primary_only: if True, only keep each product's primary photo.
        """
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.validate = validate
        self.primary_only = primary_only

    def _retry_after_seconds(self, response: requests.Response) -> Optional[float]:
        """
        Parse a 429 response's Retry-After header, if present. Returns None
        if the header is missing or isn't a plain integer number of
        seconds (the HTTP-date form exists but is rare from CDNs/APIs and
        not worth the extra parsing here) — callers fall back to normal
        exponential backoff in that case.

        Args:
            response: the 429 response to inspect.

        Returns:
            Seconds to wait, or None if unavailable/unparseable.
        """
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def fetch_page(self, url: str) -> Optional[str]:
        """
        Download a page's HTML, retrying transient errors with backoff.
        Never raises.

        Args:
            url: product page URL to fetch.

        Returns:
            The page's HTML text, or None if it could not be fetched
            (404, bot protection, or retries exhausted).
        """
        backoff = INITIAL_BACKOFF_SECONDS

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)

                if response.status_code == 404:
                    scraper_logger.error("404 Not Found for %s", url)
                    return None

                if response.status_code in (401, 403):
                    scraper_logger.error(
                        "Possible bot protection encountered (HTTP %d) for %s",
                        response.status_code, url,
                    )
                    return None

                if response.status_code in RETRYABLE_STATUS_CODES:
                    wait_seconds = backoff
                    if response.status_code == 429:
                        wait_seconds = self._retry_after_seconds(response) or backoff
                    scraper_logger.warning(
                        "Retryable status %d (attempt %d/%d) for %s — waiting %.1fs",
                        response.status_code, attempt, MAX_RETRIES, url, wait_seconds,
                    )
                    if attempt == MAX_RETRIES:
                        scraper_logger.error("Giving up on %s after %d attempts", url, MAX_RETRIES)
                        return None
                    time.sleep(wait_seconds)
                    backoff *= 2
                    continue

                response.raise_for_status()
                return response.text

            except requests.exceptions.Timeout:
                scraper_logger.warning("Timeout (attempt %d/%d) for %s", attempt, MAX_RETRIES, url)
            except requests.exceptions.ConnectionError as exc:
                scraper_logger.warning("Connection error (attempt %d/%d) for %s: %s", attempt, MAX_RETRIES, url, exc)
            except requests.exceptions.HTTPError as exc:
                scraper_logger.error("HTTP error for %s: %s", url, exc)
                return None
            except requests.exceptions.RequestException as exc:
                scraper_logger.error("Request failed for %s: %s", url, exc)
                return None

            if attempt == MAX_RETRIES:
                scraper_logger.error("Giving up on %s after %d attempts", url, MAX_RETRIES)
                return None
            time.sleep(backoff)
            backoff *= 2

        return None

    def parse_html(self, html: Optional[str]) -> Optional[BeautifulSoup]:
        """
        Parse raw HTML into a BeautifulSoup tree. Never raises.

        Args:
            html: raw page HTML, or None.

        Returns:
            A BeautifulSoup tree, or None if html is empty or unparseable.
        """
        if not html:
            return None
        try:
            return BeautifulSoup(html, "lxml")
        except Exception as exc:
            scraper_logger.error("Failed to parse HTML: %s", exc)
            return None

    def _validate(self, image_url: str) -> tuple[bool, str]:
        """
        Check that one image URL actually resolves.

        Uses GET with stream=True rather than HEAD: some CDNs/edge
        configs don't support HEAD reliably, while GET is universally
        supported. stream=True means we don't download the image body —
        we only read the status line/headers, then close the connection
        immediately, so this stays cheap.

        Args:
            image_url: the constructed full-resolution image URL to check.

        Returns:
            (True, "<status> OK") if the URL resolves successfully,
            otherwise (False, "<reason>").
        """
        try:
            response = self.session.get(
                image_url, stream=True, timeout=VALIDATE_TIMEOUT_SECONDS, allow_redirects=True
            )
            response.close()
            if response.ok:
                return True, f"{response.status_code} OK"
            return False, f"HTTP {response.status_code}"
        except requests.exceptions.RequestException as exc:
            return False, str(exc)

    def extract_images(self, soup: Optional[BeautifulSoup], html: Optional[str], sku: str, url: str) -> list[str]:
        """
        Return every valid high-res image URL on the page. Never raises.

        Args:
            soup: parsed product page, or None.
            html: raw page HTML (used only to detect bot-protection pages
                when no thumbnails are found).
            sku: product SKU, for logging.
            url: product page URL, for logging.

        Returns:
            A list of validated (if self.validate) or raw candidate
            image URLs. Empty if none were found or none passed validation.
        """
        candidate_urls = parse_image_urls(soup, primary_only=self.primary_only)
        if not candidate_urls:
            if _looks_like_bot_protection(html):
                scraper_logger.warning(
                    "Possible bot protection encountered (no product thumbnails found, "
                    "and the page body matches a CAPTCHA/challenge pattern) | sku=%s | url=%s",
                    sku, url,
                )
            else:
                scraper_logger.warning("No image thumbnails found for sku=%s url=%s", sku, url)
            return []

        if not self.validate:
            return candidate_urls

        valid_urls = []
        for image_url in candidate_urls:
            ok, reason = self._validate(image_url)
            if ok:
                valid_urls.append(image_url)
            else:
                scraper_logger.warning(
                    "Image validation failed | sku=%s | product_url=%s | image_url=%s | reason=%s",
                    sku, url, image_url, reason,
                )

        return valid_urls

    def scrape(self, sku: str, url: str) -> dict[str, object]:
        """
        Fetch + parse one product. Never raises.

        Args:
            sku: product SKU.
            url: product page URL.

        Returns:
            {"sku": sku, "url": url, "images": [...]} — "images" is empty
            if the page could not be fetched, parsed, or had no images.
        """
        scraper_logger.info("Scraping %s (%s)", sku, url)

        html = self.fetch_page(url)
        if html is None:
            return {"sku": sku, "url": url, "images": []}

        soup = self.parse_html(html)
        if soup is None:
            return {"sku": sku, "url": url, "images": []}

        return {"sku": sku, "url": url, "images": self.extract_images(soup, html, sku, url)}


# ==========================================================================
# CLI entry point
# ==========================================================================

def _find_column(fieldnames: Optional[list[str]], candidates: tuple[str, ...]) -> Optional[str]:
    """
    Return the first candidate header name present in fieldnames, or None.

    Args:
        fieldnames: CSV header names as read by csv.DictReader, or None.
        candidates: acceptable header names, in preference order.

    Returns:
        The first matching header name, or None if none match.
    """
    for candidate in candidates:
        if candidate in (fieldnames or []):
            return candidate
    return None


def read_input_rows(path: Path) -> Iterator[dict[str, str]]:
    """
    Read data.csv and yield {"sku", "url"} dicts, skipping rows missing either.

    Args:
        path: path to the input CSV.

    Yields:
        {"sku": str, "url": str} for each valid row.
    """
    if not path.exists():
        logger.error("Input file not found: %s", path)
        return

    with open(path, newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        sku_col = _find_column(reader.fieldnames, SKU_COLUMN_CANDIDATES)
        url_col = _find_column(reader.fieldnames, URL_COLUMN_CANDIDATES)

        if not sku_col or not url_col:
            logger.error("Could not find sku/url columns in %s (headers: %s)", path, reader.fieldnames)
            return

        for row_number, row in enumerate(reader, start=2):  # header is row 1
            sku = (row.get(sku_col) or "").strip()
            url = (row.get(url_col) or "").strip()
            if not sku or not url:
                logger.warning("Skipping row %d: missing sku or url", row_number)
                continue
            yield {"sku": sku, "url": url}


def dedupe_input_rows(input_rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """
    Drop rows whose url has already been seen, keeping the first
    occurrence and preserving order. Dedup by url (not sku) because url
    is what actually gets fetched — if two rows point to the same page,
    scraping it twice only produces duplicate output rows.

    Args:
        input_rows: rows as produced by read_input_rows().

    Returns:
        The input rows with same-url duplicates removed, in original order.
    """
    seen_urls: set[str] = set()
    unique_rows: list[dict[str, str]] = []

    for row in input_rows:
        if row["url"] in seen_urls:
            continue
        seen_urls.add(row["url"])
        unique_rows.append(row)

    removed = len(input_rows) - len(unique_rows)
    if removed:
        logger.info("Removed %d duplicate input row(s) (same url seen more than once)", removed)

    return unique_rows


def generate_image_rows(scraper: ProductScraper, input_rows: list[dict[str, str]]) -> Iterator[dict[str, str]]:
    """
    Scrape every input row and yield one output dict per image found.

    Args:
        scraper: configured ProductScraper instance.
        input_rows: rows as produced by read_input_rows()/dedupe_input_rows().

    Yields:
        Records shaped like make_record()'s output, one per image found.
    """
    products_with_images = 0
    products_without_images = 0

    for row in input_rows:
        sku, url = row["sku"], row["url"]

        try:
            result = scraper.scrape(sku, url)
        except Exception as exc:
            # scraper.scrape() is designed to never raise, but if it ever
            # does, one bad product must not kill the run for the rest.
            logger.error("Unexpected error scraping %s (%s): %s", sku, url, exc)
            continue

        image_urls = result["images"]
        if not image_urls:
            products_without_images += 1
        else:
            products_with_images += 1
            for image_url in image_urls:
                yield make_record(sku, url, image_url)

        time.sleep(REQUEST_DELAY_SECONDS)

    logger.info(
        "Image summary: %d products with images | %d with no images",
        products_with_images,
        products_without_images,
    )


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    """
    Parse command-line arguments.

    Args:
        argv: argument list to parse, or None to use sys.argv.

    Returns:
        Parsed argparse.Namespace with "test", "validate", "primary_only".
    """
    parser = argparse.ArgumentParser(
        description=(
            f"Scrape product images listed in {INPUT_PATH} and write "
            f"one row per image to {OUTPUT_PATH}."
        ),
    )
    parser.add_argument(
        "--test", "-n", type=int, default=None, metavar="N",
        help="Only scrape the first N valid input rows, for a quick test run "
             "instead of the full catalog.",
    )
    parser.add_argument(
        "--validate", action="store_true",
        help=(
            "GET-check every constructed image URL before saving it "
            "(off by default — adds one extra request per image, which "
            "adds up over thousands of products; use this for a safety "
            "check, e.g. after the site changes or on a first run)."
        ),
    )
    parser.add_argument(
        "--primary-only", action="store_true",
        help=(
            "Only keep each product's primary photo; skip all Extra Image "
            "and Packaging Image thumbnails entirely (off by default — "
            "normally every photo on the page is kept)."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    return parser.parse_args(argv)


def main() -> int:
    """
    Run the full scrape: read input, dedupe, scrape every product, write output.

    Returns:
        0 on success, 1 if there was nothing valid to scrape.
    """
    args = parse_args()
    logger.info("Starting scrape: %s -> %s", INPUT_PATH, OUTPUT_PATH)

    input_rows = list(read_input_rows(INPUT_PATH))
    if not input_rows:
        logger.error("No valid input rows found in %s; nothing to scrape.", INPUT_PATH)
        return 1

    input_rows = dedupe_input_rows(input_rows)

    if args.test is not None:
        logger.info("--test %d given: limiting run to the first %d of %d rows", args.test, args.test, len(input_rows))
        input_rows = input_rows[: args.test]

    logger.info("Image validation: %s", "ON" if args.validate else "OFF")
    logger.info("Primary-only mode: %s", "ON (extras/packaging skipped)" if args.primary_only else "OFF (all images kept)")

    scraper = ProductScraper(validate=args.validate, primary_only=args.primary_only)
    image_rows = generate_image_rows(scraper, input_rows)
    write_images_csv(image_rows, OUTPUT_PATH)

    logger.info("Done. Output written to %s", OUTPUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())