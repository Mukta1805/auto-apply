# CLAUDE.md: Riya's job agent

You are working on Riya Sancheti's job-search tool, on her own Windows laptop. Riya is the user. The tool was
built by Mukta (her friend) with Claude. This file tells you what the tool is, the rules you must keep, and how to
set it up and run it with Riya. Read it fully before doing anything.

If Riya's first message is anything like "set this up", follow **Setup runbook** below step by step.

---

## 1. What this tool does

A daily scheduled tool (Windows Task Scheduler) that:

1. **Finds jobs** at about 211 life-sciences, biotech and healthcare companies (`config/companies.csv`). It reads their
   public job boards: Greenhouse, Lever, Ashby, Workable and SmartRecruiters.
2. **Filters** them with free rules: US only, entry level, relevant title, posted within 45 days, and no
   "no sponsorship" language.
3. **Scores** each remaining job with Claude Haiku 5.5 against Riya's profile, 0-100. Each job gets a track:
   **business** (BD, strategy, market access, product, consulting, operations) or **research** (industry R&D and
   academic or institute research).
4. **Auto-applies** with Playwright on Greenhouse, Lever and Ashby forms. Everything else goes to the Manual tab.
5. **Outreach:** Apollo finds a recruiter or hiring manager. Gmail drafts or sends a short email after she applies,
   with follow-ups on day 4 and day 9.
6. **Excel tracker** `output/riya_job_tracker.xlsx` with these tabs: Dashboard, High, Medium, Low, Applied, Dry Run,
   Manual and Contacts. There is also a daily summary email to Riya.

Two scheduled tasks run it: `RiyaJobAgent` at 06:00 (`python -m jobagent run`) and `RiyaJobAgentOutreach` at
10:45 (`python -m jobagent outreach`). Both go through `scripts/run_daily.ps1`.

### Riya, in brief (full detail in `kb/`)

- **Education:** MS in Biomedical Engineering at Columbia, graduating December 23, 2026 (GPA 3.91). B.Tech in
  Bioengineering from MIT-ADT University.
- **Start and location:** can start in early January 2027. Based in New York City, open to anywhere in the US.
- **Visa:** F-1. She will be on OPT (STEM OPT eligible), is authorized to work, and **will need H-1B sponsorship
  in the future**.
- **Salary:** minimum $80-90K base.
- **Resumes:** `kb/resumes/resume_business.pdf` and `kb/resumes/resume_research.pdf` (the research one includes
  the Columbia lab role).

## 2. Ground rules (always)

1. **Secrets.** Never ask Riya to paste an API key, token or password into the chat, and never print or echo
   `.env`.
   - Let her edit `.env` herself by running `notepad .env`.
   - To check a key is set, use `python -m jobagent doctor`, which shows only set or not set.
   - Never commit `.env`, `kb/answers.yaml`, `kb/resumes/*`, `credentials/`, `data/`, `output/` or `logs/`. They are
     git-ignored, so keep it that way.
2. **Legal, work-authorization and EEO answers come only from `kb/answers.yaml`.** Never have Claude or code invent
   them, and never change them without Riya saying so explicitly.
   - The current answers: authorized to work = Yes; authorized without sponsorship = No; needs sponsorship now or in
     future = Yes; EEO = "Decline to self-identify".
3. **No real submissions or sends without Riya's explicit OK in chat.**
   - `apply.dry_run: true` and `outreach.mode: draft` stay on until she has reviewed the dry-run screenshots and the
     Gmail drafts and says to switch.
   - Never run `apply --submit` on your own.
4. **Never solve, bypass or work around CAPTCHAs** or bot detection. Jobs with a visible CAPTCHA go to Manual. That
   is the intended behaviour, not a bug to fix.
5. **Don't create accounts on job sites** or type Riya's passwords anywhere. Workday, iCIMS and similar sites go to
   Manual.
6. **Before changing code**, run the tests: `.venv\Scripts\python -m pytest -q`. There are 118 tests, and they need no
   network or keys. Add or adjust a test for every fix.
7. **Keep it polite to job boards.** Don't raise the concurrency or remove the pacing in `config/config.yaml`
   (`fetch.host_limits`). Workable in particular rate-limits hard.

## 3. Setup runbook (do this with Riya, in order)

Use PowerShell from the repo folder. Explain each step in one line before running it. **Stop and wait for Riya**
wherever it says WAIT.

### Step 0: check where you are
- `git status` and `git log --oneline -3`: confirm this is the repo and it is clean.
- Check the personal files that are **not in git**:
  - `kb\answers.yaml` (her filled-in answers)
  - `kb\resumes\resume_business.pdf`
  - `kb\resumes\resume_research.pdf`
- If any are missing, WAIT. Ask Riya to copy them in from the `riya-job-agent.zip` that Mukta sent: same paths
  inside the zip. If `answers.yaml` can't be found, setup will create it from the template. In that case, fill in
  the identity fields with Riya:
  - name: Riya Sancheti
  - email: riyasancheti398@gmail.com
  - phone: +1 332 278 0555
  - city / state / postal code: New York / NY / 10025
  - LinkedIn: https://www.linkedin.com/in/riyansancheti/ (ask her to confirm this exact URL)

  Then show her the `work_authorization` block and get her OK.

### Step 1: install
```powershell
powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1
```
- This installs Python 3.12 via winget if needed (it asks first), creates `.venv`, installs packages and runs
  `playwright install chromium`. It also creates `.env` (and `answers.yaml` if missing) from templates, then runs
  `doctor`.
- If PowerShell blocks scripts, the `-ExecutionPolicy Bypass` above covers it. Don't change the machine-wide policy.
- If winget is missing, ask Riya to install Python 3.12 from python.org and tick "Add python.exe to PATH". Then
  re-run.

### Step 2: keys (WAIT for Riya)
Ask her to run `notepad .env` and fill in these three values herself, then save:
- **`ANTHROPIC_API_KEY`:** from console.anthropic.com → API Keys. Suggest setting a monthly spend limit there;
  expected spend is about $2-4/day once applying.
- **`APOLLO_API_KEY`:** Apollo → Settings → Integrations → API Keys → Create new key. Use a **scoped** key (not
  master) with these endpoints:
  - People API Search
  - People Enrichment / Match
  - Organization Search
- **`CONTACT_EMAIL`:** riyasancheti398@gmail.com

Then run `.venv\Scripts\python -m jobagent doctor --online`. Everything should be OK except Gmail. If Apollo
fails with 401 or 403, the key is missing an endpoint, or her plan lacks API access. Tell her which.

### Step 3: first run (scoring only, no applying)
```powershell
.venv\Scripts\python -m jobagent run --limit 30
```
- It fetches every board (a few minutes) and scores 30 jobs (cents).
- Open `output\riya_job_tracker.xlsx` with `start output\riya_job_tracker.xlsx`, and ask Riya whether the High and
  Medium matches look right.
- If scores feel off, the fix is in `kb/profile_business.md`, `kb/profile_research.md` or `kb/lists.yaml`, not in
  the code.
- Then a full scoring run: `.venv\Scripts\python -m jobagent run`.

### Step 4: connect Gmail (WAIT for Riya; she clicks through Google)
Guide her through these steps:
1. Go to https://console.cloud.google.com and create a project (any name, e.g. "job agent").
2. Under APIs & Services → Library, enable the **Gmail API**.
3. Under OAuth consent screen, choose External. Add an app name and her email as support contact. Add
   **riyasancheti398@gmail.com as a Test user**.
4. Under Credentials → Create credentials → OAuth client ID, choose **Desktop app**. Download the JSON and save it as
   `credentials\client_secret.json` (create the folder if needed).

Then run `.venv\Scripts\python -m jobagent auth-gmail`. A browser opens; she signs in and allows access. The
"Google hasn't verified this app" warning is expected for a personal test app: Advanced → Continue. The command
warns if she signed in with a different account than `answers.yaml` says.

### Step 5: check forms (nothing is typed)
Pick one Greenhouse, one Lever and one Ashby job from the High tab, and use their apply URLs:
```powershell
.venv\Scripts\python -m jobagent inspect-form "<apply URL>" --track business
```
This opens the page in a visible browser, reads the form, and prints how each field would be filled. Look for
fields mapped wrongly or left unmapped, and fix them in `src/jobagent/apply/mapper.py`. Add a test to
`tests/test_mapper.py`, ideally from a saved form capture as in `tests/fixtures/forms/`.

### Step 6: dry-run applications (fills forms, does NOT submit)
```powershell
.venv\Scripts\python -m jobagent apply --dry-run --limit 3
```
- Then show Riya the screenshots in `output\screenshots\` and the **Dry Run** tab.
- **This is the first time the browser automation runs for real.** It was built and tested against captured copies
  of real forms, but never live. Expect small fixes, for example a dropdown not selecting, a file upload not
  attaching, or a slow page. Debug with `logs\runs\<latest>.log` and `logs\errors.log`. Re-run until the screenshots
  look right.

### Step 7: go live, gradually (each needs Riya's explicit OK)
1. **First real submissions.** Riya says go. Run `.venv\Scripts\python -m jobagent apply --submit --limit 3`, then
   check the confirmation emails and screenshots with her.
2. **Daily applying.** In `config\config.yaml`, set `apply.enabled: true` and `apply.dry_run: false`.
3. **Outreach as drafts.** Set `outreach.enabled: true` and keep `mode: draft`. Run
   `.venv\Scripts\python -m jobagent outreach`, then ask her to review the drafts in Gmail.
4. **Automatic sending.** Only if she wants it, set `outreach.mode: send`. The caps are 30 new emails, 40 follow-ups
   and 30 Apollo reveals per day.

### Step 8: schedule
```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1
```
This creates `RiyaJobAgent` at 06:00 and `RiyaJobAgentOutreach` at 10:45. Tell Riya:
- Keep the laptop plugged in overnight, with wake timers allowed (Power Options → Advanced → Sleep → Allow wake
  timers).
- Stay signed in, because the apply browser needs her desktop session.
- Close Excel before 6 AM.

Test with `Start-ScheduledTask -TaskName RiyaJobAgent` and check `logs\scheduler.log`.

## 4. Daily use (what to tell Riya)

- In the tracker, she edits only the yellow **Status** and **Notes** columns. Status values:
  - `skip` hides a job.
  - `apply` queues a Low match.
  - `applied (manual)` moves a job to Applied.
  - `interviewing`, `rejected` and `offer` track progress.
- **Manual** tab: open the link, use the prepared answers, then set Status to `applied (manual)`.
- To add companies, append rows to `config/companies.csv` with `ats=unknown`, then run `python -m jobagent detect`.
- Her own lists live in `kb/lists.yaml`: companies to avoid, dream companies, warm connections, and people already
  contacted.

## 5. Code map

| Path | What |
| --- | --- |
| `src/jobagent/cli.py` | Commands: run, apply, outreach, inspect-form, auth-gmail, detect, export, stats, doctor |
| `src/jobagent/pipeline.py` | The daily run, stage by stage (each stage is timed and logged; one failing stage gives exit 1, not a crash) |
| `src/jobagent/fetchers/` | One module per job board |
| `src/jobagent/companies.py` | Job-board detection (slug tiers, name matching); tested on live data in `tests/test_real_data.py` |
| `src/jobagent/filters.py` | Rule filters (free) |
| `src/jobagent/scoring/` | Haiku scorer, prompts, cost accounting |
| `src/jobagent/apply/extract_fields.js` | Read-only form reader (labels, required, options, CAPTCHAs) |
| `src/jobagent/apply/mapper.py` | Field → answer rules; `SENSITIVE` questions go to Manual |
| `src/jobagent/apply/browser.py` | Playwright driver (fill, select, combobox, upload, submit, confirmation detection) |
| `src/jobagent/apply/engine.py` | Which jobs to apply to today (quotas, caps) and the per-job flow |
| `src/jobagent/apply/writer.py` | Sonnet writes open-question answers and cover letters |
| `src/jobagent/outreach/` | `apollo.py`, `gmail.py`, `writer.py` (emails), `engine.py` (caps, cooldowns, follow-ups, replies), `summary.py` |
| `src/jobagent/export/workbook.py` | The Excel tracker |
| `src/jobagent/db.py` | SQLite, the source of truth (`data/jobagent.db`); the workbook is rebuilt from it |
| `config/config.yaml` | Every setting |
| `kb/` | Riya's profile, answers, lists and resumes |

**Models:**

| Use | Model | Settings |
| --- | --- | --- |
| Scoring | `claude-haiku-5-5` | effort low, 1h prompt cache |
| Writing answers, cover letters and emails | `claude-sonnet-5-5` | |

Both use JSON-schema structured output. No `temperature` parameter: the current API rejects it.

**Logs:** `logs/jobagent.log` (daily), `logs/errors.log`, `logs/runs/<run_id>.log` (DEBUG detail for one run),
`logs/scheduler.log`.

**Exit codes:** 0 ok, 1 partial, 2 failed, 3 locked (another run is going).

## 6. Status and known limits

**Verified against the live job boards:** detection and parsing at 103 companies (about 3,500 jobs), and reading
real Greenhouse, Lever and Ashby forms.

**Not yet run for real** (first time is on this laptop):
- Playwright filling and submitting
- Apollo API calls
- Gmail API calls
- Real Claude API calls

All of these are covered by tests with fakes and mocks.

**108 companies are `unsupported`** (mostly Workday). Their jobs show up in the tracker for manual applying. Adding
a Workday fetcher would be the most useful next feature: listing only, with applying staying Manual.

**Optional answers still empty:** `identity.address_line1`, `work_authorization.opt_ead_expected_start/end`. Ask Riya
when a form needs them.

## 7. Git

The repo is `https://github.com/Mukta1805/auto-apply` (private), branch `main`. Commit small, tested changes with
clear messages. Run `git status` before every commit to be sure no personal files are staged. Pull before starting
work, since Mukta may push fixes.
