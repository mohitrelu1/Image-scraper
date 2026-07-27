"""
main.py

Single-file image scraper: logging setup, image-URL parsing utilities, the
ProductScraper class, and the CLI entry point all live here.

Reads data/data.csv, scrapes every product listed in it, and writes
output/images.csv (one row per product image: sku, url, image_url).

By default every photo on a product page is kept — primary, Extra Image,
and Packaging Image thumbnails all get their own output row, each built
with the correct CloudFront folder for its type. Pass --primary-only if
you ever want the old reduced behavior (main photo only) back.

Run it with:
    python main.py             # full run, every image kept
    python main.py --test 20   # only the first 20 valid input rows
    python main.py --primary-only   # keep only each product's main photo
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

__version__ = "1.1.0"

# ==========================================================================
# Configuration
# ==========================================================================

# --- Paths -----------------------------------------------------------------
INPUT_PATH = Path("data") / "data.csv"
OUTPUT_PATH = Path("output") / "images.csv"
LOG_FILE = Path("output") / "scraper.log"

# --- Logging -----------------------------------------------------------------
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

LOG_MAX_BYTES = 5_000_000
LOG_BACKUP_COUNT = 3

PACKAGE_LOGGER_NAME = "image_scraper"

# --- Input / output CSV columns --------------------------------------------
OUTPUT_COLUMNS = ["url", "sku", "image_url"]

SKU_COLUMN_CANDIDATES = ("sku", "manu_sku")
URL_COLUMN_CANDIDATES = ("url", "prod_page_url")

# --- Image type -------------------------------------------------------------
# Every thumbnail on a product page carries its type as the 2nd argument
# to swapImage(), and the site's own JS picks a different CloudFront
# folder per type:
#
#     <img class="smallimage"
#          onclick="swapImage('wcp-fs23_extra08.jpg','Extra Image', this)">
#
#     type == 'Packaging Image' -> folder = 'package_photos/package_'
#     type == 'primary'         -> folder = ''
#     type == 'Extra Image'     -> folder = 'extra_photos/extra_'
#
#     large_url = CLOUDFRONT_BASE + 'files/Primary/' + folder + 'large/' + filename
#
# main.py rebuilds this exact URL for EVERY thumbnail found (not just the
# primary one), so a product with 10 gallery thumbnails produces 10 output
# rows. Pass --primary-only to opt back into keeping only the main photo.
CLOUDFRONT_BASE = "https://d2b9vjwb3yw5iu.cloudfront.net/"

IMAGE_TYPE_PRIMARY = "primary"
IMAGE_TYPE_EXTRA = "Extra Image"
IMAGE_TYPE_PACKAGING = "Packaging Image"

# Type (lowercased) -> CloudFront folder segment. Matching is done
# case-insensitively since the site is not fully consistent about casing
# (e.g. "Primary" vs "primary" have both been seen in the wild).
IMAGE_TYPE_FOLDERS = {
    IMAGE_TYPE_PRIMARY.lower(): "",
    IMAGE_TYPE_EXTRA.lower(): "extra_photos/extra_",
    IMAGE_TYPE_PACKAGING.lower(): "package_photos/package_",
}

SWAPIMAGE_RE = re.compile(
    r"""swapImage\(\s*['"]([^'"]+)['"]\s*,\s*['"]([^'"]+)['"]"""
)

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
INITIAL_BACKOFF_SECONDS = 2.0
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

REQUEST_DELAY_SECONDS = 0.2

DEFAULT_VALIDATE_IMAGES = False
VALIDATE_TIMEOUT_SECONDS = 10


# ==========================================================================
# Logging setup
# ==========================================================================

_logger_configured = False


def _configure_package_logger() -> None:
    global _logger_configured
    if _logger_configured:
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

    _logger_configured = True


def get_logger(name: str) -> logging.Logger:
    _configure_package_logger()
    return logging.getLogger(f"{PACKAGE_LOGGER_NAME}.{name}")


utils_logger = get_logger("utils")
scraper_logger = get_logger("scraper")
logger = get_logger("main")


# ==========================================================================
# Image-URL parsing utilities
# ==========================================================================

def _looks_like_bot_protection(html: Optional[str]) -> bool:
    if not html:
        return False
    lowered = html.lower()
    return any(marker in lowered for marker in BOT_PROTECTION_MARKERS)


def _image_url_for(filename: str, img_type: str) -> Optional[str]:
    """
    Build the full-resolution CloudFront URL for a thumbnail, using the
    CloudFront folder that matches its image type.

    Args:
        filename: image filename parsed from swapImage() (e.g. "wtt-ps24.jpg").
        img_type: image type parsed from swapImage() (e.g. "Extra Image").

    Returns:
        The full-resolution image URL, or None if img_type isn't one of
        the known types (primary / Extra Image / Packaging Image).
    """
    folder = IMAGE_TYPE_FOLDERS.get(img_type.lower())
    if folder is None:
        return None
    return f"{CLOUDFRONT_BASE}files/Primary/{folder}large/{filename}"


def parse_image_urls(
    soup: Optional[BeautifulSoup],
    sku: str = "",
    url: str = "",
    primary_only: bool = False,
) -> list[str]:
    """
    Return a deduped list of high-resolution image URLs found on a product
    page. By default every thumbnail is kept (primary, Extra Image,
    Packaging Image); pass primary_only=True to keep just the main photo.
    Never raises.

    Args:
        soup: parsed product page, or None.
        sku: product SKU, for logging only.
        url: product page URL, for logging only.
        primary_only: if True, skip every non-primary thumbnail.

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

        if primary_only and img_type.lower() != IMAGE_TYPE_PRIMARY:
            utils_logger.debug(
                "%s: skipping %r (--primary-only) | sku=%s | url=%s",
                filename, img_type, sku, url,
            )
            continue

        key = (filename, img_type.lower())
        if key in seen:
            continue  # duplicate carousel copy
        seen.add(key)

        image_url = _image_url_for(filename, img_type)
        if image_url is None:
            utils_logger.warning(
                "%s: unrecognized image type %r, skipping | sku=%s | url=%s",
                filename, img_type, sku, url,
            )
            continue

        urls.append(image_url)

    return urls


# --------------------------------------------------------------------------
# Building and writing output rows
# --------------------------------------------------------------------------

def make_record(sku: str, url: str, image_url: str) -> dict[str, str]:
    return {"sku": sku, "url": url, "image_url": image_url}


def write_images_csv(rows: Iterable[dict[str, str]], path: Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    with output_path.open(mode="w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in OUTPUT_COLUMNS})
            written += 1

    utils_logger.info("Wrote %d image rows to %s", written, output_path)


# ==========================================================================
# ProductScraper — fetches one product page and finds its primary image URL.
# ==========================================================================

class ProductScraper:
    """Fetches a product page and extracts its primary high-resolution image URL."""

    def __init__(self, validate: bool = DEFAULT_VALIDATE_IMAGES, primary_only: bool = False) -> None:
        """
        Args:
            validate: if True, GET-check every constructed image URL
                before trusting it (see VALIDATE_TIMEOUT_SECONDS).
            primary_only: if True, keep only each product's main photo and
                drop every Extra Image / Packaging Image thumbnail.
        """
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.validate = validate
        self.primary_only = primary_only

    def _retry_after_seconds(self, response: requests.Response) -> Optional[float]:
        value = response.headers.get("Retry-After")
        if not value:
            return None
        try:
            return float(value)
        except ValueError:
            return None

    def fetch_page(self, url: str) -> Optional[str]:
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
        if not html:
            return None
        try:
            return BeautifulSoup(html, "lxml")
        except Exception as exc:
            scraper_logger.error("Failed to parse HTML: %s", exc)
            return None

    def _validate(self, image_url: str) -> tuple[bool, str]:
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
        Return every high-res image URL found on the page (primary, Extra
        Image, and Packaging Image, unless self.primary_only is set), or
        an empty list if none was found. Never raises.
        """
        candidate_urls = parse_image_urls(soup, sku=sku, url=url, primary_only=self.primary_only)
        if not candidate_urls:
            if _looks_like_bot_protection(html):
                scraper_logger.warning(
                    "Possible bot protection encountered (no thumbnails found, "
                    "and the page body matches a CAPTCHA/challenge pattern) | sku=%s | url=%s",
                    sku, url,
                )
            else:
                scraper_logger.warning("No images found for sku=%s url=%s", sku, url)
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
    for candidate in candidates:
        if candidate in (fieldnames or []):
            return candidate
    return None


def read_input_rows(path: Path) -> Iterator[dict[str, str]]:
    if not path.exists():
        logger.error("Input file not found: %s", path)
        return

    with path.open(newline="", encoding="utf-8-sig") as csv_file:
        reader = csv.DictReader(csv_file)
        sku_col = _find_column(reader.fieldnames, SKU_COLUMN_CANDIDATES)
        url_col = _find_column(reader.fieldnames, URL_COLUMN_CANDIDATES)

        if not sku_col or not url_col:
            logger.error("Could not find sku/url columns in %s (headers: %s)", path, reader.fieldnames)
            return

        for row_number, row in enumerate(reader, start=2):
            sku = (row.get(sku_col) or "").strip()
            url = (row.get(url_col) or "").strip()
            if not sku or not url:
                logger.warning("Skipping row %d: missing sku or url", row_number)
                continue
            yield {"sku": sku, "url": url}


def dedupe_input_rows(input_rows: list[dict[str, str]]) -> list[dict[str, str]]:
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
    products_with_images = 0
    products_without_images = 0

    for row in input_rows:
        sku, url = row["sku"], row["url"]

        try:
            result = scraper.scrape(sku, url)
        except Exception as exc:
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
    parser = argparse.ArgumentParser(
        description=(
            f"Scrape every image (primary, Extra Image, Packaging Image) for "
            f"each product listed in {INPUT_PATH}, and write one row per "
            f"image to {OUTPUT_PATH}."
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
            "(off by default — adds one extra request per product; use this "
            "for a safety check, e.g. after the site changes or on a first run)."
        ),
    )
    parser.add_argument(
        "--primary-only", action="store_true",
        help=(
            "Keep only each product's main photo and drop every Extra "
            "Image / Packaging Image thumbnail (off by default — every "
            "image on the page is kept)."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    return parser.parse_args(argv)


def main() -> int:
    args = parse_args()
    mode = "primary image only" if args.primary_only else "all images (primary + extras + packaging)"
    logger.info("Starting scrape (%s): %s -> %s", mode, INPUT_PATH, OUTPUT_PATH)

    input_rows = list(read_input_rows(INPUT_PATH))
    if not input_rows:
        logger.error("No valid input rows found in %s; nothing to scrape.", INPUT_PATH)
        return 1

    input_rows = dedupe_input_rows(input_rows)

    if args.test is not None:
        logger.info("--test %d given: limiting run to the first %d of %d rows", args.test, args.test, len(input_rows))
        input_rows = input_rows[: args.test]

    logger.info("Image validation: %s", "ON" if args.validate else "OFF")
    logger.info("Primary-only mode: %s", "ON" if args.primary_only else "OFF")

    scraper = ProductScraper(validate=args.validate, primary_only=args.primary_only)
    image_rows = generate_image_rows(scraper, input_rows)
    write_images_csv(image_rows, OUTPUT_PATH)

    logger.info("Done. Output written to %s", OUTPUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())