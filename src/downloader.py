"""BedrockDownloader: scrapes Mojang's download page and installs the server binary."""

import logging
import re
import stat
import zipfile
from pathlib import Path

import requests
from tqdm import tqdm

from config import (
    BEDROCK_URL_PATTERN,
    DOWNLOAD_CHUNK_SIZE,
    DOWNLOAD_TIMEOUT,
    MAX_RETRIES,
    MOJANG_DOWNLOAD_PAGE,
    REQUEST_TIMEOUT,
    RETRY_BACKOFF,
    USER_AGENT,
    VERSION_FILE_NAME,
)

logger = logging.getLogger(__name__)


class BedrockDownloader:
    def __init__(self, cache_dir: Path) -> None:
        self._cache = cache_dir
        self._cache.mkdir(parents=True, exist_ok=True)
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

    def get_latest_version_url(self) -> tuple[str, str]:
        """
        Return (version, download_url) for the latest Bedrock Dedicated Server.
        Primary source: Minecraft Wiki API (reliable, no JS rendering required).
        Fallback: Mojang download page scrape.
        """
        try:
            return self._version_from_wiki()
        except Exception as exc:
            logger.warning("Wiki API failed (%s), falling back to Mojang page...", exc)
            return self._version_from_mojang_page()

    def _version_from_wiki(self) -> tuple[str, str]:
        """Query the Minecraft Wiki MediaWiki API for the latest Linux server URL."""
        logger.info("Fetching latest Bedrock server version from Minecraft Wiki...")
        # Use a plain session for the wiki — browser-spoofing headers cause 403 there
        resp = requests.get(
            "https://minecraft.wiki/api.php",
            params={
                "action": "parse",
                "page": "Bedrock_Dedicated_Server",
                "prop": "wikitext",
                "format": "json",
            },
            headers={"User-Agent": "bedmin/1.0 (Linux)"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()

        wikitext = resp.json()["parse"]["wikitext"]["*"]
        versions = re.findall(
            r"https://www\.minecraft\.net/bedrockdedicatedserver/bin-linux/"
            r"bedrock-server-([\d.]+)\.zip",
            wikitext,
        )
        if not versions:
            raise RuntimeError("No Linux server URLs found in wiki page.")

        latest = sorted(versions, key=lambda v: tuple(int(x) for x in v.split(".")))[-1]
        url = f"https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-{latest}.zip"
        logger.info("Latest version (from wiki): %s", latest)
        return latest, url

    def _version_from_mojang_page(self) -> tuple[str, str]:
        """Scrape the Mojang download page for the latest Linux server URL."""
        logger.info("Fetching latest Bedrock server version from Mojang page...")
        resp: requests.Response | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self._session.get(MOJANG_DOWNLOAD_PAGE, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                break
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES:
                    raise RuntimeError(
                        f"Failed to fetch Mojang download page after {MAX_RETRIES} attempts: {exc}\n\n"
                        "Pass a direct URL to skip version detection:\n"
                        "  bedmin server create --name NAME "
                        "--url https://www.minecraft.net/bedrockdedicatedserver/bin-linux/bedrock-server-X.Y.Z.zip"
                    ) from exc
                import time
                logger.warning(
                    "Attempt %d/%d failed, retrying in %ds...", attempt, MAX_RETRIES, RETRY_BACKOFF * attempt
                )
                time.sleep(RETRY_BACKOFF * attempt)

        assert resp is not None
        match = re.search(BEDROCK_URL_PATTERN, resp.text)
        if not match:
            raise RuntimeError(
                f"Could not find Bedrock Linux download URL on {MOJANG_DOWNLOAD_PAGE}. "
                "The page may require JavaScript rendering."
            )

        url = match.group(0)
        ver_match = re.search(r"bedrock-server-([\d.]+)\.zip", url)
        version = ver_match.group(1) if ver_match else "unknown"
        logger.info("Latest version (from Mojang page): %s", version)
        return version, url

    def download(self, url: str, version: str, dest_dir: Path, force: bool = False) -> Path:
        """
        Download and extract the server to dest_dir.
        Uses a local cache so repeated installs of the same version skip re-downloading.
        Returns dest_dir.
        """
        zip_path = self._get_cached_zip(version)
        if zip_path and not force:
            logger.info("Using cached download for version %s", version)
        else:
            zip_path = self._download_zip(url, version)

        logger.info("Extracting server to %s ...", dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
        self._extract(zip_path, dest_dir)

        # Mark executable
        binary = dest_dir / "bedrock_server"
        if binary.exists():
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

        # Write version file
        (dest_dir / VERSION_FILE_NAME).write_text(version)
        logger.info("Server version %s installed at %s", version, dest_dir)
        return dest_dir

    def get_installed_version(self, server_path: Path) -> str | None:
        version_file = server_path / VERSION_FILE_NAME
        if version_file.exists():
            return version_file.read_text().strip()
        return None

    # --- Private ---

    def _download_zip(self, url: str, version: str) -> Path:
        logger.info("Downloading Bedrock server %s ...", version)
        resp: requests.Response | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = self._session.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT)
                resp.raise_for_status()
                break
            except requests.RequestException as exc:
                if attempt == MAX_RETRIES:
                    raise RuntimeError(f"Download failed after {MAX_RETRIES} attempts: {exc}") from exc
                import time
                time.sleep(RETRY_BACKOFF * attempt)

        assert resp is not None
        total = int(resp.headers.get("content-length", 0))
        zip_path = self._cache / f"bedrock-server-{version}.zip"
        tmp_path = zip_path.with_suffix(".tmp")

        with tmp_path.open("wb") as f, tqdm(
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            desc=f"bedrock-server-{version}.zip",
        ) as bar:
            for chunk in resp.iter_content(DOWNLOAD_CHUNK_SIZE):
                f.write(chunk)
                bar.update(len(chunk))

        tmp_path.rename(zip_path)
        logger.debug("Cached zip at %s", zip_path)
        return zip_path

    def _extract(self, zip_path: Path, dest_dir: Path) -> None:
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.namelist()
            for member in tqdm(members, desc="Extracting", unit="file"):
                zf.extract(member, dest_dir)

    def _get_cached_zip(self, version: str) -> Path | None:
        path = self._cache / f"bedrock-server-{version}.zip"
        return path if path.exists() else None
