# Program members report

<!-- license-disclaimer -->
> ⚠️ **No warranty — use at your own risk.** This software is provided "as is", **without warranty of any kind**, express or implied, and the authors are not liable for any claim, damages or other liability arising from its use. You are responsible for reviewing the code and testing it before relying on it, for the API calls it makes against your Marketo quota, and for handling any personal data it exports in line with the law and your own policies. Run it against a sandbox or a small scope first. See [LICENSE](LICENSE) (MIT).

For every Marketo **program created inside a date window**, pulls every **member** of that
program with their **program status** (Invited, Registered, Attended, Opened, Clicked, …),
and writes spreadsheet-ready CSVs: a full roster, an engaged-only roster, and a per-program
summary. Read-only — nothing is written to Marketo.

Only the lead's **email** is exported (plus the program, status and join date).

## Setup

Requires **Python 3.9+**.

```bash
cd program-members-report
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then edit .env with your credentials
```

Verify the credentials before a real run — this prints an access token (a long string)
if everything is correct:

```bash
python auth.py
```

## Getting your Marketo API credentials

You need an **API-only user** and a **Custom service**. Everything below is in the
Marketo **Admin** area and requires an admin to do once. (Marketo calls the pair of
values a *Client ID* and *Client Secret*.)

1. **Role** — Admin → Users & Roles → Roles → **New Role**. Under **Access API**,
   tick the permissions this pull needs (all read-only):
  - Read-Only Asset   (list programs)
  - Read-Only Person  (read the members of a program and their program status)

   *Permission labels differ slightly between Marketo versions — for example
   "Read-Only Person" is "Read-Only Lead" in older instances.*
2. **User** — Admin → Users & Roles → Users → **Invite New User**. Tick **API Only**
   and assign the role from step 1.
3. **Service** — Admin → Integration → **LaunchPoint** → **New** → **New Service**.
   Display Name: anything; Service: **Custom**; API Only User: the user from step 2.
   Then open the service and choose **View Details** — that shows the
   **Client ID** and **Client Secret**.
4. **REST endpoint** — Admin → Integration → **Web Services** → *REST API* section →
   **Endpoint**, e.g. `https://123-ABC-456.mktorest.com/rest`. Use the host only —
   drop the trailing `/rest` (the tools add paths themselves, and strip it if left on).
5. **`MARKETO_APP_BASE`** *(optional)*: the host in your browser's address bar while logged in
   to Marketo, e.g. `https://app-ab12.marketo.com`. Only used to build clickable links.

Put the values in this folder's `.env` (see **Setup**). `.env` is git-ignored, so your
secret is never committed.

## Run

`--start` and `--end` are required. They are dates (UTC); a program is included when its
**created date** falls in the window, **both ends inclusive** (`--end` runs through 23:59:59).

```bash
# Smoke test first: only the first 3 programs in the window
python program_members_report.py --start 2025-01-01 --end 2025-12-31 --limit 3

# Full run
python program_members_report.py --start 2025-01-01 --end 2025-12-31
```

| Flag | Required | Purpose |
| --- | --- | --- |
| `--start YYYY-MM-DD` | yes | First day of the window (programs created on/after, UTC). |
| `--end YYYY-MM-DD` | yes | Last day of the window, inclusive. |
| `--limit N` | no | Only process the first N in-window programs (oldest first). |
| `--stage STAGE` | no (`all`) | `programs`, `members`, `report`, or `all`. `report` rebuilds the CSVs from cache with **zero API calls**. |
| `--refresh` | no | Ignore the cache and refetch everything from Marketo. |
| `--passive-status S` | no | A status meaning "was sent / invited", not engaged. Repeat for several. Default: `Member`, `Invited`. |
| `--non-human-status S` | no | A status for non-human activity, e.g. security-scanner clicks. Repeat for several. Default: `Bot Click`. |

Run `python program_members_report.py --help` for the same list.

**Cost.** One call per page of programs (200 per page) plus roughly one call per ~340 members.
A window with 700,000 memberships took about 2,000 calls and 10 minutes. The client is throttled
to about 4 calls per second to stay under Marketo's rate limit.

> Marketo allows **50,000 REST API calls per day** and **100 calls per 20 seconds** on a standard subscription (higher limits can be purchased). Other integrations on your instance share this quota, so check how many calls a run will make (stated below) before starting a large one.

## What it creates

All files are written under this folder. `output/` holds the reports; `raw/` holds the cache.
Both are git-ignored because they contain email addresses.

| File | Contents |
| --- | --- |
| `output/program_members_ALL_<start>_to_<end>.csv` | **Every membership.** One row per member per program. |
| `output/program_members_ENGAGED_<start>_to_<end>.csv` | The same, with the passive statuses (default `Member`, `Invited`) removed. `Bot Click` rows stay, under their own status. |
| `output/program_summary_<start>_to_<end>.csv` | One row per program, including programs with zero members. |
| `output/programs_in_window.csv` | The programs that matched the date filter — use it to sanity-check the window. |
| `raw/programs_all.json` | Cache of every program in the instance. |
| `raw/members/<programId>.json` | Cache of each program's members and raw membership data. |

**Member files** (`ALL` and `ENGAGED`) — 6 columns:
`program_name`, `program_id`, `program_channel`, `email`, `program_member_status`, `membership_date`.
Program columns repeat on every row, so the file pivots by program × status in a spreadsheet.
Rows are grouped by program (oldest created first), then sorted by status and email.

**Summary file** — `program_name`, `program_id`, `program_type`, `program_channel`,
`program_status`, `program_folder`, `program_created_at`, `total_members`, `engaged_total`,
`engagement_rate`, `program_url`, then one `status: <name>` count column for every status found.
`engaged_total` excludes the passive and non-human statuses; `engagement_rate` is
`engaged_total / total_members`. The `status:` columns sum to `total_members`.

### Google Sheets and large files

Sheets accepts a CSV of up to about 100 MB and 10 million cells per spreadsheet. The 6-column
member files are sized for that (about 6 cells per membership). If a window is larger than that,
split it into several runs by date range, or use the `ENGAGED` file.

## Caching and re-runs

Every program's members are cached in `raw/members/`, so an interrupted run resumes where it
stopped, and a second run with a different window re-uses everything already fetched. The
cache does not expire — use `--refresh` when you need current membership.

## Notes

- Date basis is the program's **created date**, not when it ran or when people joined. A program
  created before the window that is still active inside it is **not** included.
- All Marketo timestamps are UTC.
- A program that returns an error (deleted, archived, or a type with no membership) is recorded
  with an `error` key in its cache file and skipped, rather than stopping the run.
- `Bot Click` is Marketo's flag for clicks from security scanners, not people. It is shown in
  the `ENGAGED` file but excluded from `engaged_total` and `engagement_rate`.

## API endpoints used (all `GET`)

- `/identity/oauth/token` — access token
- `/rest/asset/v1/programs.json` — all programs, paged
- `/rest/v1/leads/programs/{programId}.json` — a program's members, paged by `nextPageToken`
- `/rest/v1/stats/usage.json` — optional, used by `python marketo_client.py` to show quota use

## Troubleshooting

| Message | Meaning / fix |
| --- | --- |
| `missing required config value(s)` | `.env` is missing or incomplete — see **Setup**. |
| `[auth] token request failed: invalid_client` | Wrong Client ID/Secret, or the service's API-only user is disabled. |
| `Access denied` / error `603` | The API role lacks **Read-Only Person** or **Read-Only Asset**. |
| `--start and --end must be dates in YYYY-MM-DD format` | Use e.g. `2025-01-31`, not `01/31/2025`. |

---

## License and disclaimer

This project is released under the [MIT License](LICENSE) and comes with **no warranty and no support**. It is an independent project, not affiliated with or endorsed by Adobe; Adobe and Marketo are trademarks of Adobe Inc.
