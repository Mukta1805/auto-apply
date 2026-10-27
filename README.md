# Riya's job agent

A daily, scheduled tool that finds new life-sciences, biotech and healthcare jobs, filters out the obvious misses,
scores each remaining role against Riya's profile with Claude, and writes an Excel tracker with High / Medium /
Low matches. Auto-apply and email outreach are phase 2.

```
Windows Task Scheduler (daily 6 AM)
└─ python -m jobagent run
   1. companies  sync config/companies.csv into the database
   2. edits      read Riya's Status / Notes edits back from the workbook
   3. batches    collect scoring batches finished since the last run (batch mode only)
   4. fetch      pull every company's job board concurrently (Greenhouse, Lever, Ashby, Workable, SmartRecruiters)
   5. filter     rules: US only, entry level, relevant title, posted < 45 days, no "no sponsorship" language
   6. hydrate    fetch full descriptions only for candidates whose board omits them (SmartRecruiters)
   7. filter2    re-check hydrated jobs (sponsorship language lives in descriptions)
   8. score      Claude Haiku 5.5 scores new or changed jobs 0-100 and picks the track
   9. export     rebuild output/riya_job_tracker.xlsx
  10. maintain   weekly database clean-up
```

## Folder structure

```
riya-job-agent/
├── README.md
├── pyproject.toml              package + dependencies
├── requirements.txt
├── requirements-dev.txt
├── .env.example                API keys template (copy to .env, never committed)
├── config/
│   ├── config.yaml             all settings: filters, models, budget, pricing, logging
│   └── companies.csv           target companies + their job board (filled in by `detect`)
├── kb/                         Riya's knowledge base
│   ├── candidate.md            shared facts: education, experience, logistics, visa
│   ├── profile_business.md     business track: positioning, target roles, skills
│   ├── profile_research.md     research track: positioning, target roles, skills
│   ├── lists.yaml              avoid / dream companies, already applied, warm connections
│   ├── answers.example.yaml    application answers template (copy to answers.yaml, never committed)
│   └── resumes/                resume_business.pdf, resume_research.pdf (never committed)
├── src/jobagent/
│   ├── cli.py                  commands: run, detect, export, stats, doctor
│   ├── pipeline.py             the daily run, stage by stage
│   ├── config.py · kb.py · models.py · text.py · location.py
│   ├── http.py                 shared async HTTP client (HTTP/2, retries, per-host limits, ETags)
│   ├── db.py                   SQLite store (source of truth)
│   ├── companies.py            companies.csv + automatic job-board detection
│   ├── filters.py              rule-based filters (no AI cost)
│   ├── logging_setup.py        log files, run ids, stage timing, secret masking
│   ├── fetchers/               one module per job board
│   ├── scoring/                prompts, Claude scorer (realtime + batch), token costs
│   ├── export/                 Excel workbook
│   ├── apply/                  phase 2: Playwright form filling
│   └── outreach/               phase 2: Apollo contacts + Gmail
├── scripts/
│   ├── setup_windows.ps1       one-time setup on Riya's laptop
│   ├── register_task.ps1       creates the daily scheduled task
│   ├── run_daily.ps1           what the scheduled task runs
│   └── setup_unix.sh           development setup on macOS / Linux
├── tests/                      pytest suite with job-board fixtures and a fake Claude client
├── data/        (git-ignored)  jobagent.db
├── output/      (git-ignored)  riya_job_tracker.xlsx
└── logs/        (git-ignored)  see Logging
```

## Setup on Windows (Riya's laptop)

1. Install [Git for Windows](https://git-scm.com/download/win), then clone the repo into e.g. `Documents\riya-job-agent`.
2. In PowerShell, from the repo folder:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
   ```
   This installs Python 3.12 if needed (with your OK), creates `.venv`, installs packages, and creates `.env`
   and `kb\answers.yaml` from their templates.
3. Open `.env` in Notepad and paste `ANTHROPIC_API_KEY` and `CONTACT_EMAIL`. Set a monthly spend limit in the
   Anthropic console as a second safety net.
4. Check everything, including the network and the key:
   ```powershell
   .venv\Scripts\python -m jobagent doctor --online
   ```
5. Find each company's job board (about 5 minutes; writes the results into `config\companies.csv`):
   ```powershell
   .venv\Scripts\python -m jobagent detect
   ```
6. First small run, then open `output\riya_job_tracker.xlsx`:
   ```powershell
   .venv\Scripts\python -m jobagent run --limit 30
   ```
7. Schedule the daily run (wakes the laptop at 6 AM when plugged in; runs at next start-up if it was off):
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1 -Time 06:00
   ```
   Laptop power settings: allow wake timers while plugged in.

## Daily use

- Open `output\riya_job_tracker.xlsx`. Edit only the yellow **Status** and **Notes** columns:
  `skip` hides a job, `apply` queues a Low match, `applied (manual)`, `interviewing`, `rejected`, `offer` track progress.
  Edits are read back before each rebuild, so they are never lost.
- Close Excel before 6 AM. If the file is open, the run writes a timestamped copy instead and logs a warning.
- Add companies by appending rows to `config\companies.csv` with `ats` set to `unknown`, then run `detect`.

## Commands

| Command | What it does |
| --- | --- |
| `python -m jobagent run` | Full daily pipeline |
| `run --no-score` | Fetch + filter only (no AI cost) |
| `run --no-fetch` | Re-filter and score what is already stored |
| `run --limit 50` | Score at most 50 jobs this run |
| `run --company "Benchling"` | Only fetch the named company (repeatable) |
| `python -m jobagent detect [--redetect]` | Find job boards for companies marked `unknown` (`--redetect` retries `unsupported`) |
| `python -m jobagent export` | Rebuild the workbook from the database |
| `python -m jobagent stats` | Database and spend summary |
| `python -m jobagent doctor [--online]` | Setup check |

Exit codes: `0` ok, `1` partial (a stage failed but the workbook was written), `2` failed, `3` another run holds the lock.

## Logging

| File | Contents |
| --- | --- |
| `logs/jobagent.log` | Human-readable log, rotated at midnight, kept 30 days |
| `logs/jobagent.jsonl` | The same events as JSON lines (run id, stage, counters) for analysis |
| `logs/errors.log` | Warnings and errors only (failed boards, API errors), size-rotated |
| `logs/runs/<run_id>.log` | Everything from one run at DEBUG level, including per-company fetch timings |
| `logs/scheduler.log` | Start, end, exit code and duration of each scheduled run |

Every line carries the run id and pipeline stage; each stage logs its duration and counters
(e.g. `stage finished: companies=211, ok=180, error=2, jobs_seen=5400, jobs_new=310, duration_s=41.2`).
API keys are masked in every log. Set `logging.level: DEBUG` in `config/config.yaml` (or `JOBAGENT_LOG_LEVEL=DEBUG`
in `.env`) for more detail.

## Performance

- **Network:** one HTTP/2 connection pool; up to 24 requests at once, at most 6 per job-board host;
  retries with exponential backoff that honour `Retry-After`; conditional requests (ETag) so unchanged
  boards cost no download.
- **Database:** SQLite in WAL mode; each board is written in one transaction with `executemany`;
  results are stored as each board arrives, so network and database work overlap. Excluded jobs drop their
  descriptions, and a weekly `VACUUM` keeps the file compact.
- **AI:** only new or changed postings that pass the free rule filters are scored; descriptions are trimmed;
  Riya's profile sits in a cached system prompt; the first request warms the cache before the rest fan out
  (8 at a time). `scoring.mode: batch` halves the price when results can wait. A daily budget cap stops
  new requests.
- **Measured** (simulated 200 boards x 150 jobs, 150 ms latency): first run 14 s for 30,000 jobs; a run with
  no changes 7.5 s.

## Costs

Scoring uses Claude Haiku 5.5 (`$0.10` input / `$0.50` output per million tokens at the time of writing; see
`pricing:` in `config.yaml`). A typical job costs well under one cent to score. Every request is logged with its
tokens and cost in the `llm_usage` table, the dashboard shows month-to-date spend, and `budget.max_daily_usd`
caps a day's spend.

## Development

```bash
bash scripts/setup_unix.sh          # or the Windows script
.venv/bin/python -m pytest -q       # 45 tests, no network or API key needed
```

## Roadmap

- **Phase 2a, auto-apply:** Playwright fillers for Greenhouse, Lever and Ashby forms; answers from
  `kb/answers.yaml`; custom questions written with Claude Sonnet 5.5; dry-run screenshots before submit is
  enabled; CAPTCHAs and complex sites go to the Manual tab.
- **Phase 2b, outreach:** contacts via the Apollo API (verified emails only); Gmail API drafts, then sending
  with a daily cap, follow-ups on day 4 and day 9, and reply detection.
