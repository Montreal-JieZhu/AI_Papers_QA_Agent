"""
ArXiv AI Paper Pipeline V1
==========================================================

What this script does
---------------------
1) Scrape an arXiv *search results* page (HTML) to collect paper metadata
   (title, authors, abstract, URLs, submitted date).
2) Compare the fresh metadata with a persistent "base" store to find
   truly *new* papers that we haven't processed before.
3) Download only those new papers as PDFs.
4) Convert each PDF to plain text (UTF‑8) and, on success, delete the PDF
   to save space (configurable).
5) Concatenate all per‑paper TXT files into a single `all.txt` file.
6) Update the base metadata so the next run remains incremental/idempotent.
7) Run the pipeline once a day automatically by a scheduler.

Why scrape HTML instead of the arXiv API?
-----------------------------------------
- You provided an HTML scraping approach. This refactor keeps the same
  source for compatibility, but makes parsing more defensive and the
  whole pipeline safer and clearer.
- If you later prefer the official API, you can swap `fetch_search_metadata`
  with an API client while preserving the rest of the pipeline.

Usage
-----
- Set `SEARCH_URL` to any arXiv search results URL (e.g., cs.AI latest):
  https://arxiv.org/search/?query=cs.ai&searchtype=all&abstracts=show&order=-announced_date_first&size=50

- Run the script directly:
    python arxiv_pipeline.py

- Adjust configurable options in `Config` below.

Dependencies
------------
- requests
- beautifulsoup4
- PyMuPDF (imported as "fitz")

Install:
    pip install requests beautifulsoup4 PyMuPDF

Directory layout (defaults)
---------------------------
./paper/
  base.json                      # persistent metadata store of *all seen* papers
  arxiv_search_result.json       # latest scrape result (ephemeral)
  pdf/                           # PDFs (deleted after converting to txt by default)
  txt/                           # one TXT per paper

Notes on design / best practices
--------------------------------
- Clear typing and dataclasses for paper records.
- Robust HTTP with retry/backoff, timeouts, and explicit User‑Agent.
- Atomic writes via temporary files + `os.replace`.
- Safe filenames (cross‑platform) based on title + arXiv id.
- Idempotent runs: never re‑download already‑processed papers.
- Logging instead of print; structured, informative messages.
- Small helpers isolated and documented; easy to unit test.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Dict, Any

import fitz  # PyMuPDF
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# pip install apscheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from tzlocal import get_localzone

# ---------------------------- Configuration ---------------------------- #

class Config:
    """Central place for all tweakable knobs.

    Adjust these defaults to fit your environment and preferences.
    """

    # Source: arXiv search results list page (HTML). It always show the papers ordered by the announcement date desc.
    SEARCH_URL: str = (
        "https://arxiv.org/search/?query=cs.ai&searchtype=all&abstracts=show&order=-announced_date_first&size=50"
    )

    # Base working directory
    BASE_DIR: Path = Path("./paper")

    # Metadata files: base.json has all metadata of loaded papers. arxiv_search_result.json only has the latest 50 paper's metadata
    SCRAPE_OUTFILE: Path = BASE_DIR / "arxiv_search_result.json"
    BASE_METADATA_FILE: Path = BASE_DIR / "base.json"

    # Artifacts: The paths to pdf files and txt files. But those files will be deleted, if the text are loaded into all.txt.
    PDF_DIR: Path = BASE_DIR / "pdf"
    TXT_DIR: Path = BASE_DIR / "txt"
    CONCATENATED_TXT: Path = TXT_DIR / "all.txt"

    # Behavior Variables: In order the paper provider doesn't prevent us from downloading papers.    
    REQUEST_TIMEOUT_SEC: int = 20
    REQUESTS_PER_SECOND: float = 1.5  # polite crawling: ~1 req / 0.67 s

    # HTTP retry/backoff
    RETRY_TOTAL: int = 5
    RETRY_BACKOFF_FACTOR: float = 0.5  # exponential backoff base

    # Logging
    LOG_LEVEL: int = logging.INFO
	
# ------------------------------ Data Model ----------------------------- #

@dataclass(frozen=True)
class Paper:
    """Normalized paper metadata record.

    `identity_key` is used for deduplication and should be stable across runs.
    We derive it from the arXiv ABS URL (preferred) or the PDF URL.
    """

    title: str
    abs_url: str
    pdf_url: str
    authors: List[str]
    abstract: str
    submitted_date_raw: str  # original raw text, e.g. "Submitted 12 Aug 2025"
    identity_key: str  # canonical key used to detect duplicates

    @staticmethod
    def from_scraped(
        title: str,
        abs_url: str,
        pdf_url: str,
        authors: List[str],
        abstract: str,
        submitted_date_raw: str,
    ) -> "Paper":
        key = _derive_identity_key(abs_url, pdf_url)
        return Paper(
            title=title,
            abs_url=abs_url,
            pdf_url=pdf_url,
            authors=authors,
            abstract=abstract,
            submitted_date_raw=submitted_date_raw,
            identity_key=key,
        )
		
# ------------------------------- Logging -------------------------------- #

def setup_logging(level: int = Config.LOG_LEVEL) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
	
# ---------------------------- HTTP Utilities --------------------------- #

def build_http_session(cfg: Config = Config) -> requests.Session:
    """Create a `requests` session with retry/backoff and helpful headers."""
    session = requests.Session()

    retry = Retry(
        total=cfg.RETRY_TOTAL,
        read=cfg.RETRY_TOTAL,
        connect=cfg.RETRY_TOTAL,
        backoff_factor=cfg.RETRY_BACKOFF_FACTOR,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "arxiv-pipeline/1.0 (+https://github.com/your-org/your-repo)"
            )
        }
    )
    return session


def polite_sleep(cfg: Config = Config) -> None:
    """Respectful delay between requests to avoid hammering arXiv."""
    time.sleep(max(0.0, 1.0 / cfg.REQUESTS_PER_SECOND))
	
# ------------------------- Scraping / Extraction ------------------------ #

def fetch_search_metadata(search_url: str, session: requests.Session) -> List[Paper]:
    """Fetch and parse paper metadata from an arXiv search results page.

    Parameters
    ----------
    search_url : str
        arXiv search results URL (with your query params already set).
    session : requests.Session
        Configured HTTP session with retry/backoff.

    Returns
    -------
    List[Paper]
        List of normalized paper metadata entries.
    """
    logging.info("Fetching search results: %s", search_url)
    resp = session.get(search_url, timeout=Config.REQUEST_TIMEOUT_SEC)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    papers: List[Paper] = []
    results = soup.find_all("li", class_="arxiv-result")
    logging.info("Found %d result items", len(results))

    for item in results:
        try:
            title_tag = item.find("p", class_="title")
            title = (title_tag.get_text(strip=True) if title_tag else "").strip()

            list_title = item.find("p", class_="list-title")
            # Prefer ABS link (canonical ID holder)
            abs_a = list_title.find("a") if list_title else None
            abs_url = abs_a["href"].strip() if abs_a and abs_a.has_attr("href") else ""

            # A separate link for PDF often exists; if not, derive from ABS link
            pdf_a = (list_title.find("a", string=re.compile(r"^pdf$", re.I)) if list_title else None)
            pdf_url = (
                pdf_a["href"].strip()
                if pdf_a and pdf_a.has_attr("href")
                else _try_abs_to_pdf(abs_url)
            )

            authors_p = item.find("p", class_="authors")
            authors = [a.get_text(strip=True) for a in authors_p.find_all("a")] if authors_p else []

            abstract_span = item.find("span", class_="abstract-full")
            abstract = abstract_span.get_text(strip=True) if abstract_span else ""

            date_p = item.find("p", class_="is-size-7")
            submitted_raw = (
                date_p.get_text(" ", strip=True) if date_p else ""
            )

            paper = Paper.from_scraped(
                title=title,
                abs_url=abs_url,
                pdf_url=pdf_url,
                authors=authors,
                abstract=abstract,
                submitted_date_raw=submitted_raw,
            )
            papers.append(paper)
        except Exception as e:
            logging.warning("Error parsing a search result: %s", e, exc_info=False)
            continue

    return papers


def _try_abs_to_pdf(abs_url: str) -> str:
    """Best effort to convert an arXiv ABS URL to a PDF URL."""
    # ABS: https://arxiv.org/abs/2501.12345v2  ->  PDF: https://arxiv.org/pdf/2501.12345.pdf
    m = re.search(r"/abs/(\d+\.\d+)(v\d+)?$", abs_url)
    if m:
        return f"https://arxiv.org/pdf/{m.group(1)}.pdf"
    return ""


def _derive_identity_key(abs_url: str, pdf_url: str) -> str:
    """Compute a stable identity key for a paper.

    Prefer the arXiv numeric id (without version) extracted from the ABS URL.
    Fallback to the numeric id extracted from the PDF URL.
    """
    for url in (abs_url, pdf_url):
        m = re.search(r"/(?:abs|pdf)/(\d+\.\d+)", url or "")
        if m:
            return m.group(1)  # e.g., "2501.12345"
    # Last resort: use the URL itself (less stable but better than nothing)
    return abs_url or pdf_url
	
# ------------------------- Files / Persistence -------------------------- #

def ensure_dirs(*paths: Path) -> None:
    for p in paths:
        p.mkdir(parents=True, exist_ok=True)


def load_json_list(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def safe_filename(name: str, extra_key: str = "") -> str:
    """Return a filesystem‑safe filename derived from `name` + optional key."""
    base = f"{name} {extra_key}".strip() if extra_key else name
    # Collapse whitespace and remove disallowed characters
    base = re.sub(r"\s+", " ", base).strip()
    base = re.sub(r"[\\/*?:\"<>|]", "_", base)
    # Truncate to avoid super‑long filenames on Windows/macOS
    return base[:200]
	
# ------------------------------ Diff logic ----------------------------- #

def compute_new_papers(
    existing: List[Dict[str, Any]], fresh: List[Paper]
) -> List[Paper]:
    """Return only papers whose identity_key is not in the existing base."""
    seen_keys = {rec.get("identity_key") for rec in existing}
    new_items = [p for p in fresh if p.identity_key not in seen_keys]
    logging.info("%d new papers detected (out of %d fresh)", len(new_items), len(fresh))
    return new_items
	
	
# ------------------------------ Download ------------------------------- #

def download_pdfs(papers: Iterable[Paper], session: requests.Session, cfg: Config = Config) -> None:
    ensure_dirs(cfg.PDF_DIR)
    for i, p in enumerate(papers, start=1):
        if not p.pdf_url:
            logging.warning("[%d] Missing PDF URL for: %s", i, p.title)
            continue
        filename = safe_filename(p.title, extra_key=p.identity_key) + ".pdf"
        dest = cfg.PDF_DIR / filename
        if dest.exists():
            logging.info("[%d] Already exists, skipping: %s", i, dest.name)
            continue

        logging.info("[%d] Downloading: %s", i, p.pdf_url)
        try:
            resp = session.get(p.pdf_url, stream=True, timeout=cfg.REQUEST_TIMEOUT_SEC)
            resp.raise_for_status()
            tmp = dest.with_suffix(".pdf.part")
            with tmp.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1024 * 64):
                    if chunk:
                        f.write(chunk)
            os.replace(tmp, dest)
        except Exception as e:
            logging.error("Failed to download %s | %s", p.pdf_url, e)
            # Clean partial file if present
            try:
                if tmp.exists():
                    tmp.unlink(missing_ok=True)
            except Exception:
                pass
        finally:
            polite_sleep(cfg)
			
# --------------------------- PDF → Text stage -------------------------- #

def convert_pdfs_to_txt_and_cleanup(cfg: Config = Config) -> None:
    ensure_dirs(cfg.TXT_DIR)

    pdf_files = sorted(cfg.PDF_DIR.glob("*.pdf"))
    if not pdf_files:
        logging.info("No PDFs to convert in %s", cfg.PDF_DIR.resolve())
        return

    for i, pdf_path in enumerate(pdf_files, start=1):
        txt_name = pdf_path.with_suffix(".txt").name
        txt_path = cfg.TXT_DIR / txt_name
        tmp_txt = txt_path.with_suffix(".txt.part")

        logging.info("[%d/%d] Extracting text: %s", i, len(pdf_files), pdf_path.name)
        try:
            doc = fitz.open(pdf_path)
            try:
                with tmp_txt.open("w", encoding="utf-8", newline="\n") as out:
                    for page in doc:
                        # `get_text()` default is the same as 'text'
                        out.write(page.get_text())
                os.replace(tmp_txt, txt_path)
                logging.info("✅ Wrote %s", txt_path.name)
            finally:
                doc.close()

            pdf_path.unlink()
        except Exception as e:
            logging.error("Failed to convert %s: %s", pdf_path.name, e)
            try:
                tmp_txt.unlink(missing_ok=True)
            except Exception:
                pass

# -------------------------- Concatenate stage -------------------------- #

def concatenate_txts(cfg: Config = Config) -> None:
    files = sorted(p for p in cfg.TXT_DIR.glob("*.txt") if p.name != cfg.CONCATENATED_TXT.name)
    if not files:
        logging.info("No TXT files to merge in %s", cfg.TXT_DIR.resolve())
        return

    tmp_out = cfg.CONCATENATED_TXT.with_suffix(cfg.CONCATENATED_TXT.suffix + ".tmp")
    separator = "\n\n------------------------------\n\n"

    logging.info("Merging %d TXT files into %s", len(files), cfg.CONCATENATED_TXT.name)
    written: List[Path] = []
    try:
        with tmp_out.open("w", encoding="utf-8", newline="\n") as out:
            for idx, p in enumerate(files):
                out.write(p.read_text(encoding="utf-8", errors="ignore"))
                if idx < len(files) - 1:
                    out.write(separator)
                written.append(p)
        os.replace(tmp_out, cfg.CONCATENATED_TXT)
        logging.info("Merged successfully → %s", cfg.CONCATENATED_TXT.resolve())
        # Optionally delete per‑paper text files now that we have a single big file.
        for p in written:
            try:
                p.unlink()
            except Exception as e:
                logging.warning("Failed to delete %s: %s", p, e)
    except Exception as e:
        logging.error("Concatenation failed: %s", e)
        try:
            tmp_out.unlink(missing_ok=True)
        except Exception:
            pass	

# ------------------------------ Orchestration -------------------------- #

def run_pipeline(cfg: Config = Config) -> None:
    setup_logging(cfg.LOG_LEVEL)

    ensure_dirs(cfg.BASE_DIR, cfg.PDF_DIR, cfg.TXT_DIR)

    session = build_http_session(cfg)

    # 1) Fetch fresh search metadata
    fresh_papers = fetch_search_metadata(cfg.SEARCH_URL, session)

    # Save the raw scrape for audit/debug
    atomic_write_json(cfg.SCRAPE_OUTFILE, [asdict(p) for p in fresh_papers])
    logging.info("Saved fresh scrape → %s", cfg.SCRAPE_OUTFILE)

    # 2) Load base metadata and diff
    base_records = load_json_list(cfg.BASE_METADATA_FILE)
    new_papers = compute_new_papers(base_records, fresh_papers)

    # 3) Download PDFs for new papers
    download_pdfs(new_papers, session, cfg)

    # 4) Convert PDFs to TXT (and delete PDFs on success)
    convert_pdfs_to_txt_and_cleanup(cfg)

    # 5) Concatenate all TXT into one file
    concatenate_txts(cfg)

    # 6) Update base metadata (prepend new items for recency)
    if new_papers:
        updated = [asdict(p) for p in new_papers] + base_records
        atomic_write_json(cfg.BASE_METADATA_FILE, updated)
        logging.info(
            "Base metadata updated with %d new records → %s",
            len(new_papers),
            cfg.BASE_METADATA_FILE,
        )
    else:
        logging.info("No new metadata to add to base store.")

if __name__ == "__main__":   
    local_tz = get_localzone()          # tzinfo
    print("Scheduler timezone:", local_tz)
    scheduler = BlockingScheduler(timezone=local_tz)
    scheduler.add_job(run_pipeline, CronTrigger(hour=7, minute=0, timezone=local_tz))  # Everyday at 7:00am in local timezone
    scheduler.start()		