# Ariel Premium — Image Scraper

Scrapes the highest-resolution image URL for every product listed in
`data/data.csv` and writes a clean `url, sku, image_url` CSV — one row per
image, nothing else.

## Setup

```bash
pip install -r requirements.txt
```

No browser install needed — the thumbnail `onclick` attributes are already
present in the raw page HTML, so `requests` + `BeautifulSoup` is enough.

## Run

```bash
python main.py                    # full run, no per-image validation (fast)
python main.py --test 20          # quick test on the first 20 valid rows
python main.py --validate         # safety check: GET-verify every image URL before saving
python main.py --test 20 --validate   # flags combine freely
```

- Input:  `data/data.csv` — accepts either `sku`/`url` or `manu_sku`/`prod_page_url`
  headers (the real file uses the latter).
- Output: `output/images.csv` — columns: `url, sku, image_url`
- Log:    `output/scraper.log` — rotating file log (5MB x 3 backups), plus
  the same messages print to the console live.

## How image URLs are found

Each thumbnail on a product page looks like:

```html
<img class="smallimage"
     onclick="swapImage('wcp-fs23_extra08.jpg','Extra Image', this)">
```

The `onclick` already contains the filename and image type. The site's own
`swapImage()` JS builds the large-image URL like this:

```
type == 'Packaging Image' -> folder = 'package_photos/package_'
type == 'primary'         -> folder = ''
anything else (Extra)     -> folder = 'extra_photos/extra_'

large_url = CLOUDFRONT_BASE + 'files/Primary/' + folder + 'large/' + filename
```

`main.py` reads the `onclick` attributes straight out of the page source
and rebuilds the URL the same way, instead of clicking every thumbnail.

- Thumbnails with no `onclick` (placeholder padding images, video-play
  icons) are skipped automatically.
- Duplicate `(filename, type)` pairs — the page repeats the whole carousel
  in the DOM — are removed.
- Image types are matched **explicitly**: `Packaging Image`, `primary`,
  and `Extra Image` map to known folders; anything else (e.g. a future
  `Gallery Image` or `360 Image` type) is logged as a `WARNING` and
  skipped, rather than guessed as an "extra" image.
- Every constructed URL can optionally be checked with a `GET(stream=True)`
  request before being trusted — the body is never downloaded, only the
  status is read. This is **off by default** (a full run has thousands of
  products x ~8 images each, so validating all of them adds tens of
  thousands of extra requests and meaningfully slows the run down). Pass
  `--validate` to turn it on — worth doing on a first run against a new
  catalog, or after the site changes something. A URL that fails
  validation is logged with the sku, product URL, image URL, and failure
  reason, then dropped rather than saved as a broken link.

## Resilience

- Page fetches retry transient errors (429/500/502/503/504) with
  exponential backoff (2s, 4s, 8s) before giving up on that product. For a
  `429 Too Many Requests`, if the server sends a `Retry-After` header the
  scraper waits exactly that long instead of guessing; otherwise it falls
  back to the same backoff as the other retryable statuses.
- A short pause (`REQUEST_DELAY_SECONDS = 0.2`) runs between every product
  regardless of outcome, so a run of thousands of pages doesn't hammer the
  site back-to-back and is less likely to trigger rate limiting at all.
- 404 responses are not retried — logged and skipped immediately.
- 401/403 responses, and 200 responses where **no product thumbnails were
  found at all** and the body looks like a CAPTCHA/challenge page
  (Cloudflare interstitial, "verify you're human", etc.), are logged as
  `Possible bot protection encountered` instead of a generic failure —
  useful if the site ever starts blocking the scraper instead of serving
  real pages. This check only runs when zero images were extracted, so a
  normal product page that happens to embed an unrelated CAPTCHA widget
  (e.g. on a contact form elsewhere on the page) is never flagged.
- One product failing (bad HTML, network error, no images found) never
  stops the run; it's logged and the scraper moves to the next row.
- Rows in `data.csv` missing a sku or url are skipped with a warning
  rather than crashing the run.

## Verifying it worked

Run a small batch and spot-check the URLs by hand before trusting a full run:

```bash
python main.py --test 5 --validate
```

Open `output/images.csv`, pick a product's `image_url` values, and open a
few directly in your browser — you should land on the full-size image
(e.g. `.../files/Primary/large/wcp-fs23.jpg`), not a thumbnail. Also check
`output/scraper.log` for any `WARNING` lines about unknown image types or
failed validation — those flag anything the script couldn't confidently
resolve.

### Checking a single product

To spot-check one SKU without running the whole batch, use
`verify_product.py` — it reuses the exact same scraper as `main.py`, so
there's no risk of a hand-typed one-off snippet drifting from the real
logic (and no PowerShell/cmd quoting headaches):

```bash
python verify_product.py https://www.arielpremium.com/product/ALB-EF25
python verify_product.py https://www.arielpremium.com/product/ALB-EF25 --validate
```

It prints every unique image URL found for that product plus a total
count.

**Windows note:** paste multi-line Python (like the interactive-mode
block below) into an actual `python` REPL or a `.py` file — never into
`cmd.exe` or PowerShell directly. Neither shell understands Python's
indented blocks, so each line after the first gets parsed as a shell
command instead and fails with errors like `'oc' is not recognized...`.
`verify_product.py` avoids this entirely since it's a real script file.
