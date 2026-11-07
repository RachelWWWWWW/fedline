"""Fetch bills and US Code titles from their official sources, politely."""

from __future__ import annotations

import io
import json
import os
import re
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass

USER_AGENT = "fedline/0.1 (bill redlining tool)"
API = "https://api.congress.gov/v3"
USC_DOWNLOADS = "https://uscode.house.gov/download.html"
USC_BASE = "https://uscode.house.gov/"
_RETRY_STATUS = {429, 500, 502, 503, 504}

_BILL_TYPES = {
    "house-bill": "hr", "senate-bill": "s",
    "house-joint-resolution": "hjres", "senate-joint-resolution": "sjres",
    "house-concurrent-resolution": "hconres", "senate-concurrent-resolution": "sconres",
    "house-resolution": "hres", "senate-resolution": "sres",
}


class SourceError(Exception):
    """A problem getting source material, worded for the person running the tool."""


@dataclass
class BillRef:
    congress: int
    bill_type: str
    number: int
    version: str | None = None

    @property
    def filename(self) -> str:
        return f"{self.bill_type.upper()}{self.number}_{self.congress}"


def parse_bill_url(url: str) -> BillRef:
    match = re.match(
        r"^https?://(?:www\.)?congress\.gov/bill/(\d+)(?:st|nd|rd|th)-congress/([a-z-]+)/(\d+)"
        r"(?:/text(?:/([a-z0-9]+))?)?/?(?:[?#].*)?$", url.strip())
    if not match or match.group(2) not in _BILL_TYPES:
        raise SourceError("That doesn't look like a bill URL. Expected something like "
                          "https://www.congress.gov/bill/119th-congress/house-bill/10453/text")
    return BillRef(int(match.group(1)), _BILL_TYPES[match.group(2)], int(match.group(3)), match.group(4))


def _get(url: str, headers: dict | None = None, attempts: int = 4) -> bytes:
    """GET a URL, backing off on rate limits and server errors."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    delay = 2.0
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as err:
            if err.code not in _RETRY_STATUS or attempt == attempts - 1:
                raise
            retry_after = err.headers.get("Retry-After", "")
            time.sleep(float(retry_after) if retry_after.isdigit() else delay)
        except urllib.error.URLError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
        delay *= 2
    raise SourceError(f"Couldn't fetch {url}")


def fetch_bill_xml(ref: BillRef) -> tuple[bytes, str]:
    """Return a bill's XML and version code. Uses the version in the URL, or else the latest."""
    key = os.environ.get("CONGRESS_API_KEY")
    if not key:
        raise SourceError("CONGRESS_API_KEY isn't set. Get a free key at https://api.congress.gov/sign-up/")
    url = f"{API}/bill/{ref.congress}/{ref.bill_type}/{ref.number}/text?format=json&limit=250"
    try:
        data = json.loads(_get(url, {"X-Api-Key": key}))
    except urllib.error.HTTPError as err:
        if err.code == 404:
            raise SourceError(f"Congress.gov has no bill {ref.bill_type.upper()} {ref.number} "
                              f"in the {ref.congress}th Congress.") from err
        raise SourceError(f"The Congress.gov API returned an error ({err.code}).") from err
    except urllib.error.URLError as err:
        raise SourceError(f"Couldn't reach the Congress.gov API: {err.reason}") from err
    versions = []
    for version in data.get("textVersions", []):
        for fmt in version.get("formats", []):
            if "XML" in fmt.get("type", ""):
                code = re.search(r"BILLS-\d+[a-z]+\d+([a-z0-9]+)\.xml", fmt["url"])
                if code:
                    versions.append((version.get("date") or "", code.group(1), fmt["url"]))
    if not versions:
        raise SourceError("Congress.gov doesn't have XML text for this bill yet.")
    if ref.version:
        matching = [v for v in versions if v[1] == ref.version]
        if not matching:
            codes = ", ".join(sorted(v[1] for v in versions))
            raise SourceError(f"This bill has no version '{ref.version}' in XML. Available: {codes}")
        chosen = matching[0]
    else:
        chosen = max(versions, key=lambda v: v[0])
    try:
        return _get(chosen[2]), chosen[1]
    except urllib.error.URLError:
        name = f"BILLS-{ref.congress}{ref.bill_type}{ref.number}{chosen[1]}"
        try:
            return _get(f"https://www.govinfo.gov/content/pkg/{name}/xml/{name}.xml"), chosen[1]
        except urllib.error.URLError as err:
            raise SourceError(f"Couldn't download the bill text: {err}") from err


def fetch_code_title(number: int) -> bytes:
    """Download one title's XML from the current House release point."""
    try:
        page = _get(USC_DOWNLOADS).decode("utf-8", "replace")
        match = re.search(rf'href="([^"]*xml_usc{number:02d}@[^"]*\.zip)"', page)
        if not match:
            raise SourceError(f"Couldn't find Title {number} on {USC_DOWNLOADS}")
        archive = _get(USC_BASE + match.group(1).lstrip("/"))
    except urllib.error.URLError as err:
        raise SourceError(f"Couldn't download Title {number} of the Code: {err}") from err
    with zipfile.ZipFile(io.BytesIO(archive)) as package:
        names = [n for n in package.namelist() if n.lower().endswith(".xml")]
        if len(names) != 1:
            raise SourceError(f"Expected one XML file in the Title {number} download, found {len(names)}.")
        return package.read(names[0])
