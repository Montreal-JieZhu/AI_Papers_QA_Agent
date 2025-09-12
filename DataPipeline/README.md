# arXiv AI Paper Pipeline

A scheduled production-ready pipeline that:

1. scrapes **arXiv search results** (HTML) to collect metadata (title, authors, abstract, links, submit date)
2. **deduplicates** against a persistent base store
3. **downloads** only new PDFs
4. **extracts text** from PDFs to per-paper `.txt` files
5. **concatenates** all text into a single `all.txt`
6. **schedules** the job to run **every day at 07:00 local time** (via APScheduler + tzlocal)

---

## Features

* **Idempotent & incremental**: tracks seen papers in `base.json` via a stable `identity_key` (arXiv ID without version).
* **Robust HTTP**: retries with exponential backoff, timeouts, polite rate limiting, and a custom User-Agent.
* **Atomic I/O**: temp file + `os.replace` to avoid partial writes.
* **Safe filenames**: cross-platform sanitization and length limits.
* **Clear logging**: structured logs for each stage.
* **Daily schedule**: runs at 07:00 **local timezone** using `tzlocal`.

---

## Requirements

* Python **3.9+** (recommended)
* System packages: none required for typical installs.
  On some platforms, **PyMuPDF (fitz)** may need build tools; if install fails, see Troubleshooting.

**Python dependencies (minimal):**

```
requests>=2.31
beautifulsoup4>=4.12
PyMuPDF>=1.24
apscheduler>=3.10
tzlocal>=5.0
```

> Tip: If you prefer to auto-generate a minimal `requirements.txt` based on imports, use [`pipreqs`](https://github.com/bndr/pipreqs).

---

### Create environment

```
python -m venv venv
```

### Activate environment

Linux/macOS:
```
source venv/bin/activate
```

Windows: 
```
.\venv\Scripts\activate
```

### Install requirements

```
pip install -r requirements.txt
```

---

## Directory Layout

```
paper/
  base.json                # persistent store of *all seen* papers (for dedup)
  arxiv_search_result.json # latest scrape (ephemeral)
  pdf/                     # downloaded PDFs (deleted after text extraction)
  txt/                     # one TXT per paper (deleted after loaded to all.txt)
    all.txt                # concatenated text of all papers
```

---

## Quick Start

Run immediately:

```bash
python ArXiv_AI_Papers_PipelineV1.py
```

This will:

1. At scheduled time, it will trigger the process to fetch the latest `cs.ai` search results (50 items by default)
2. diff against `paper/base.json`
3. download the new PDFs
4. convert to `.txt`
5. merge into `paper/txt/all.txt`
6. update `paper/base.json`

## Workflow

```
[arXiv Search HTML] --requests--> [BeautifulSoup parse] --Paper dataclass-->
[Diff vs base.json by identity_key] --> [Download NEW PDFs]
           --> [PyMuPDF extract text] --> [Write per-paper .txt] --> [Merge to all.txt]
                                          
```

---

## Scheduling (07:00 Local Time)

The script already includes APScheduler + tzlocal. It will schedule `run_pipeline` at **07:00** using the machine’s **local timezone**. You can change the scheduled time as you need:

```python
if __name__ == "__main__":
    local_tz = get_localzone()  # tzinfo
    print("Scheduler timezone:", local_tz)
    scheduler = BlockingScheduler(timezone=local_tz)
    scheduler.add_job(run_pipeline, CronTrigger(hour=7, minute=0, timezone=local_tz))
    scheduler.start()
```

---

## Configuration

Edit the `Config` class to change behavior:

```python
class Config:
    SEARCH_URL = (
        "https://arxiv.org/search/?query=cs.ai&searchtype=all&abstracts=show&order=-announced_date_first&size=50"
    )
    BASE_DIR = Path("./paper")
    SCRAPE_OUTFILE = BASE_DIR / "arxiv_search_result.json"
    BASE_METADATA_FILE = BASE_DIR / "base.json"
    PDF_DIR = BASE_DIR / "pdf"
    TXT_DIR = BASE_DIR / "txt"
    CONCATENATED_TXT = TXT_DIR / "all.txt"
    REQUEST_TIMEOUT_SEC = 20
    REQUESTS_PER_SECOND = 1.5
    RETRY_TOTAL = 5
    RETRY_BACKOFF_FACTOR = 0.5
    LOG_LEVEL = logging.INFO
```

Common adjustments:

* **Change query:** point `SEARCH_URL` to any arXiv search you like (e.g., `cs.CL`, `stat.ML`, full-text, different page size).
* **Storage location:** set `BASE_DIR` to an absolute path.
* **Rate limits:** reduce `REQUESTS_PER_SECOND` if you see throttling.
* **Logging:** switch to `logging.DEBUG` during development.

---

Key implementation details:

* **identity\_key** is derived from `/abs/NNNN.NNNNN` (versionless) so updates to titles or versions won’t create duplicates.
* **Atomic writes**: JSON and text files use `.tmp` followed by `os.replace`.
* **Error handling**: failed downloads/conversions are logged and skipped without breaking the whole run.

---

## Usage Notes & Best Practices

* **Be polite to arXiv**: keep a reasonable `REQUESTS_PER_SECOND` and do not parallelize aggressively.
* **Keep `base.json`**: do not delete it unless you want a “fresh start” (which may re-download old papers).
* **Text quality**: PyMuPDF is solid, but academic PDFs can have layout artifacts. Post-processing (cleaning, de-dup, chunking) can be added after extraction.

---

## Troubleshooting

* **`PyMuPDF` install fails (Windows/macOS/Linux)**
  Upgrade `pip` and try again:

  ```bash
  python -m pip install --upgrade pip
  pip install --no-cache-dir PyMuPDF
  ```

  On Linux, ensure build tools and `libmupdf` are available (most wheels ship prebuilt).

* **ArXiv blocks or returns 429/5xx**
  Lower `REQUESTS_PER_SECOND`, keep retries, and avoid running too frequently.

---

## Security & Compliance

* **Respect arXiv’s terms of use**: fetch responsibly; don’t mirror at scale.
* No personal data is collected; outputs are academic texts from public PDFs.
* If distributing `all.txt`, respect each paper’s license.

---

## Acknowledgments

* [arXiv](https://arxiv.org/) for open access to research.
* [PyMuPDF](https://pymupdf.readthedocs.io/) for robust PDF text extraction.
* [APScheduler](https://apscheduler.readthedocs.io/) and [tzlocal](https://github.com/regebro/tzlocal) for reliable local-time scheduling.
