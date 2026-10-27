# Riya's job agent

A scheduled tool for Riya's Windows laptop. Every morning it:

1. finds new life-sciences, biotech and healthcare jobs,
2. scores them against her profile,
3. applies automatically where the application form allows it,
4. finds a contact at each company and emails them after she applies.

It also writes an Excel tracker with High / Medium / Low, Applied, Manual and Contacts tabs.

## What's still pending: only Riya's data and the keys

All code is written and tested (`pytest`: 118 tests, no network or keys needed). The job-board detection and
parsing were checked against the live boards: 103 companies with a supported board, about 3,500 open jobs.
To go live, Riya needs to do the following:

| # | What | Where | Notes |
| --- | --- | --- | --- |
| 1 | Name, email, phone, city/state, postal code, LinkedIn | `kb\answers.yaml` → `identity` | Required for any application |
| 2 | Confirm the work-authorization answers | `kb\answers.yaml` → `work_authorization` | The defaults assume OPT with sponsorship needed later. Check `authorized_without_sponsorship` and `work_authorization_status`. These are never written by AI. |
| 3 | OPT/EAD dates, earliest start date | `kb\answers.yaml` → `availability` | Recommended |
| 4 | Look over the EEO answers (default: decline) and the remaining answers | `kb\answers.yaml` | Optional |
| 5 | `ANTHROPIC_API_KEY` | `.env` | From console.anthropic.com. Set a monthly spend limit there too. |
| 6 | `APOLLO_API_KEY` | `.env` | Apollo → Settings → Integrations → API. The plan must include API access. |
| 7 | `CONTACT_EMAIL` | `.env` | Her email (sent as the User-Agent contact to job boards) |
| 8 | Gmail OAuth client, then `auth-gmail` | `credentials\client_secret.json` | See [Connect Gmail](#connect-gmail) |
| 9 | Turn features on, step by step | `config\config.yaml` | See [Going live](#going-live) |

`answers.yaml`, `.env`, the resumes, `credentials\` and `data\` are git-ignored, so none of this is pushed.
Riya pastes the keys herself; they should never be shared in chat or email.

```
Windows Task Scheduler
├─ 06:00  python -m jobagent run
│   1. companies  sync config/companies.csv into the database
│   2. edits      read Riya's Status / Notes edits back from the workbook
│   3. batches    collect finished scoring batches (batch mode only)
│   4. fetch      every company's job board, concurrently (Greenhouse, Lever, Ashby, Workable, SmartRecruiters)
│   5. filter     US only, entry level, relevant title, posted < 45 days, no "no sponsorship" language
│   6. hydrate    full descriptions where the board omits them; re-filter
│   7. score      Claude Haiku 5.5 scores new or changed jobs 0-100 and picks the track
│   8. apply      Playwright fills Greenhouse / Lever / Ashby forms (dry run until switched on)
│   9. outreach   Apollo finds a contact per company and Gmail drafts or sends an email
│  10. export     rebuild output/riya_job_tracker.xlsx
│  11. summary    one summary email to Riya
│  12. maintain   weekly database clean-up
└─ 10:45  python -m jobagent outreach
    Gmail sync (replies, bounces), follow-ups on day 4 and 9, new emails inside 09:00-17:30 on weekdays
```

## Folder structure

```
riya-job-agent/
├── README.md
├── pyproject.toml · requirements.txt · requirements-dev.txt
├── .env.example                API keys template (copy to .env, never committed)
├── config/
│   ├── config.yaml             all settings: filters, models, budget, apply, outreach, logging
│   └── companies.csv           211 target companies + detected job board
├── kb/                         Riya's knowledge base
│   ├── candidate.md            shared facts: education, experience, logistics, visa
│   ├── profile_business.md     business track
│   ├── profile_research.md     research track (industry R&D + academic)
│   ├── lists.yaml              avoid / dream companies, already applied/contacted, warm connections
│   ├── answers.example.yaml    application answers template
│   ├── answers.yaml            (git-ignored) Riya's real answers
│   └── resumes/                (git-ignored) resume_business.pdf, resume_research.pdf
├── src/jobagent/
│   ├── cli.py · pipeline.py    commands and the daily run
│   ├── config.py · kb.py · models.py · text.py · location.py · http.py · db.py · logging_setup.py
│   ├── companies.py            companies.csv + job-board detection
│   ├── filters.py              rule-based filters (no AI cost)
│   ├── fetchers/               one module per job board
│   ├── scoring/                prompts, Claude scorer (realtime + batch), token costs
│   ├── apply/
│   │   ├── extract_fields.js   reads a form's fields, labels, options, CAPTCHAs (read-only)
│   │   ├── mapper.py           maps each field to an answers.yaml value, the AI writer, or Manual
│   │   ├── writer.py           Claude Sonnet 5.5 for open questions and cover letters
│   │   ├── documents.py        cover letter PDF
│   │   ├── browser.py          Playwright driver
│   │   └── engine.py           job selection, quotas, fill → screenshot → submit → confirm
│   ├── outreach/
│   │   ├── apollo.py           domain lookup, people search (free), email reveal (credits)
│   │   ├── gmail.py            OAuth, drafts, sending, thread sync
│   │   ├── writer.py           first email (Claude) + follow-up templates
│   │   ├── engine.py           contacts, caps, cooldowns, follow-ups, replies, bounces
│   │   └── summary.py          daily summary email
│   └── export/workbook.py      Excel tracker
├── scripts/
│   ├── setup_windows.ps1       one-time setup on Riya's laptop
│   ├── register_task.ps1       creates the two daily scheduled tasks
│   ├── run_daily.ps1           what the scheduled tasks run (-Mode run | outreach)
│   └── setup_unix.sh           development setup on macOS / Linux
├── tests/                      pytest suite: live-captured boards and forms, fake Claude, mocked Apollo/Gmail
├── credentials/ (git-ignored)  client_secret.json, gmail_token.json
├── data/        (git-ignored)  jobagent.db, browser_profile/
├── output/      (git-ignored)  riya_job_tracker.xlsx, screenshots/, cover letters
└── logs/        (git-ignored)  see Logging
```

## Setup on Windows (Riya's laptop)

1. Install [Git for Windows](https://git-scm.com/download/win) and clone the repo, e.g. into `Documents\riya-job-agent`.
2. In PowerShell, from the repo folder:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
   ```
   This does the following:
   - installs Python 3.12 if needed (with her OK)
   - creates `.venv` and installs the packages
   - downloads the Playwright browser
   - creates `.env` and `kb\answers.yaml` from their templates
3. Put the two resumes in `kb\resumes\` as `resume_business.pdf` and `resume_research.pdf`.
4. Fill in `.env` and `kb\answers.yaml` (the pending table above).
5. Check everything, including the network and the keys:
   ```powershell
   .venv\Scripts\python -m jobagent doctor --online
   ```
6. First small run, then open `output\riya_job_tracker.xlsx`:
   ```powershell
   .venv\Scripts\python -m jobagent run --limit 30
   ```
7. Connect Gmail and go live step by step (below), then schedule:
   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
   ```
   This creates `RiyaJobAgent` at 06:00 and `RiyaJobAgentOutreach` at 10:45; change them with `-Time` and
   `-OutreachTime`. In the power settings, allow wake timers while plugged in. Auto-apply opens a visible
   browser window, so the tasks run while Riya is signed in.

`config\companies.csv` already has the boards detected. Run `detect` only after adding companies.

### Connect Gmail

One-time setup, about 10 minutes:

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and create a project (any name).
2. Under **APIs & Services → Library**, enable the **Gmail API**.
3. Under **OAuth consent screen**:
   - choose External
   - add the app name and her email
   - add her Gmail address as a **test user**
4. Under **Credentials**, choose **Create credentials → OAuth client ID → Desktop app**. Download the JSON and
   save it as `credentials\client_secret.json`.
5. Run:
   ```powershell
   .venv\Scripts\python -m jobagent auth-gmail
   ```
   A browser opens. She signs in and allows access to "compose" and "read". The token is saved to
   `credentials\gmail_token.json` and refreshes itself. If Google ever expires it, run the command again.

The tool can create drafts, send, and read its own threads to detect replies and bounces. It cannot delete mail.

### Going live

Turn things on one at a time in `config\config.yaml`:

| Step | Setting | Check |
| --- | --- | --- |
| 1. Inspect a form | none | `python -m jobagent inspect-form <job application URL>` shows how each field would be filled, without typing anything |
| 2. Dry-run applications | none | `python -m jobagent apply --dry-run --limit 3` fills 3 forms without submitting; check `output\screenshots\` and the Dry Run tab |
| 3. Real applications | `apply.enabled: true`, `apply.dry_run: false` | Start with `apply --submit --limit 3` and confirm the emails from the companies |
| 4. Outreach as drafts | `outreach.enabled: true` (`mode: draft`) | Drafts appear in Gmail; Riya reviews and sends them herself |
| 5. Outreach sending | `outreach.mode: send` | Daily caps: 30 new emails, 40 follow-ups, 30 Apollo reveals |

## How auto-apply works

- **Selection:** today's High matches first, then Medium, split 60/40 between the business and research
  tracks. The daily total is 25-50 (`run.daily_application_target`). There are at most 2 applications per company in
  30 days, and the avoid list is honoured.
- **Answers:**
  - Identity, links, education, work authorization, sponsorship and EEO answers come only from
    `answers.yaml`.
  - Open questions ("Why this company?") and cover letters are written by Claude Sonnet 5.5 from her profile.
  - Sensitive or unrecognised required questions send the job to the **Manual** tab, with the reason and the
    prepared answers.
- **Safety:**
  - Visible CAPTCHAs, unsupported boards (Workday, iCIMS, etc.) and failed required fields go to Manual.
  - CAPTCHAs are never solved or bypassed.
  - Every form is screenshotted before submitting and again on the confirmation page.
  - It pauses 45-120 s between applications and stops after 150 minutes.
- **Status:** every job ends up as `applied`, `dry_run`, `manual` or `failed`, with the reason recorded.

## How outreach works

- **Contacts:**
  - Apollo people search (free, no emails) ranks hiring managers and recruiters for the job's track.
  - Only the top match is revealed (uses credits), and only `verified` emails are used.
  - If no contact is found, the Contacts tab says "(find manually)".
- **Emails:**
  - One email per company within 14 days, sent only after she has applied.
  - It is short, personal, and mentions the role she applied to, with an opt-out line.
  - Follow-ups go out on day 4 and day 9 in the same thread.
  - A reply or bounce stops the sequence, and a bounced address is suppressed.
- **Summary:** each morning an email lists applications, items for Manual, replies and spend.

## Daily use

- Open `output\riya_job_tracker.xlsx`. Edit only the yellow **Status** and **Notes** columns:
  - `skip` hides a job.
  - `apply` queues a Low match.
  - `applied (manual)` moves it to Applied.
  - `interviewing`, `rejected` and `offer` track progress.

  Edits are read back before each rebuild.
- **Manual** tab: open the link, use the prepared answers, then set Status to `applied (manual)`.
- Close Excel before 6 AM. If the file is open, the run writes a timestamped copy instead.
- To add companies, append rows to `config\companies.csv` with `ats` set to `unknown`, then run `detect`.

## Commands

| Command | What it does |
| --- | --- |
| `python -m jobagent run` | Full daily pipeline |
| `run --no-score` | Fetch + filter only (no AI cost) |
| `run --no-fetch` | Re-filter and score what is already stored |
| `run --limit 50` | Score at most 50 jobs this run |
| `run --company "Benchling"` | Only fetch the named company (repeatable) |
| `python -m jobagent apply [--limit N] [--dry-run \| --submit]` | Apply to today's selection without fetching |
| `python -m jobagent outreach` | Gmail sync, follow-ups, new emails |
| `python -m jobagent inspect-form URL [--track research]` | Show how a form would be filled; types nothing |
| `python -m jobagent auth-gmail` | One-time Gmail sign-in |
| `python -m jobagent detect [--redetect]` | Find job boards for companies marked `unknown` |
| `python -m jobagent export` | Rebuild the workbook from the database |
| `python -m jobagent stats` | Database and spend summary |
| `python -m jobagent doctor [--online]` | Setup check: keys, answers.yaml, resumes, Playwright, Gmail token |

Exit codes: `0` ok, `1` partial (a stage failed but the workbook was written), `2` failed, `3` another run holds the lock.

## Logging

| File | Contents |
| --- | --- |
| `logs/jobagent.log` | Human-readable log, rotated at midnight, kept 30 days |
| `logs/jobagent.jsonl` | The same events as JSON lines (run id, stage, counters) |
| `logs/errors.log` | Warnings and errors only |
| `logs/runs/<run_id>.log` | Everything from one run at DEBUG level |
| `logs/scheduler.log` | Start, end, mode, exit code and duration of each scheduled run |

Every line carries the run id and stage. Each stage logs its duration and counters, and API keys are masked.
For more detail, set `logging.level: DEBUG` (or `JOBAGENT_LOG_LEVEL=DEBUG` in `.env`).

## Performance

- **Network:**
  - one HTTP/2 connection pool
  - up to 24 requests at once, with per-host limits (Workable is paced slowly because it rate-limits hard)
  - retries with backoff that honour `Retry-After`
  - ETags, so unchanged boards cost no download
- **Database:**
  - SQLite in WAL mode, with one transaction per board
  - excluded jobs drop their descriptions, and a weekly `VACUUM` runs
- **AI:**
  - Only new or changed postings that pass the free filters are scored.
  - The profile sits in a cached system prompt.
  - `scoring.mode: batch` halves the price.
  - A daily budget cap (`budget.max_daily_usd`) stops new requests.
- **Measured:** 30,000 simulated jobs took 14 s on the first run and 7.5 s with no changes.

## Costs

The estimates below use the per-million-token prices in `config.yaml`.

| Model | Used for | Input | Output |
| --- | --- | --- | --- |
| Claude Haiku 5.5 | Scoring | $0.10 | $0.50 |
| Claude Sonnet 5.5 | Answers, cover letters, emails | $2 | $10 |

That comes to about $0.07 per application, including scoring. At 25-50 applications a day, expect roughly
$2-4 a day.

Every request is logged in `llm_usage`, and the dashboard shows month-to-date spend. Apollo reveals use
Apollo credits, capped at 30 a day.

## Development

```bash
bash scripts/setup_unix.sh          # or the Windows script
.venv/bin/python -m pytest -q       # 118 tests, no network or keys needed
```

The tests use job-board responses and application forms (Greenhouse, Lever, Ashby) captured from live sites,
a fake Claude client, a fake browser, and Apollo/Gmail mocked at the HTTP level.

## Roadmap

- Workday support (most of the 108 companies marked `unsupported`), at first by sending them to Manual
  with prepared answers.
- LinkedIn connection-note drafts for warm contacts.
