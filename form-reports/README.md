# Form reports

<!-- license-disclaimer -->
> ⚠️ **No warranty — use at your own risk.** This software is provided "as is", **without warranty of any kind**, express or implied, and the authors are not liable for any claim, damages or other liability arising from its use. You are responsible for reviewing the code and testing it before relying on it, for the API calls it makes against your Marketo quota, and for handling any personal data it exports in line with the law and your own policies. Run it against a sandbox or a small scope first. See [LICENSE](LICENSE) (MIT).

A set of read-only reports about **forms** in your Marketo instance: every form
submission, which pages forms live on, which forms sit in a campaign folder, and
reply-list membership. Nothing is ever written back to Marketo.

| Script | What it produces |
| --- | --- |
| [`form_fill_report.py`](#1-form_fill_reportpy--form-fills-for-a-year-bulk-export) | Every form submission for a year, one row per submission (bulk export) |
| [`form_fills_month_get.py`](#2-form_fills_month_getpy--form-fills-for-one-month-get-only) | Every form submission for one month, using GET requests only |
| [`combine_form_fills.py`](#3-combine_form_fillspy--merge-monthly-files) | Merges monthly files into one date-range file (local, no API calls) |
| [`form_pages_report.py`](#4-form_pages_reportpy--pages-per-form) | One row per form: fills, and the pages it is filled out on / lives on |
| [`form_page_links_report.py`](#5-form_page_links_reportpy--every-page-per-form) | One row per form **and** page, with links |
| [`campaign_forms_report.py`](#6-campaign_forms_reportpy--forms-in-a-folder) | One row per form stored in, or embedded under, a folder |
| [`campaign_form_map_report.py`](#7-campaign_form_map_reportpy--inferred-program-to-form-map) | Programs matched to their likely form (inferred) |
| [`reply_list_report.py`](#8-reply_list_reportpy--reply-list-membership) | Member counts for static lists that share a name prefix |

## Setup

Requires **Python 3.9+**.

```bash
cd form-reports
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
  - Read-Only Activity  (Fill Out Form activities, bulk activity export)
  - Read-Only Asset     (forms, folders, programs, landing pages, static lists)
  - Read-Only Person    (static-list membership — only for reply_list_report.py)

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

### Optional settings

Two optional values in `.env` (both documented in `.env.example`) tailor the reports to
your instance. Neither is required.

- `FORM_PAGE_URL_FIELDS` — if your forms have a hidden field that captures the page URL,
  list its name here (comma separated) and it is used ahead of Marketo's built-in
  `Webpage URL` / `Referrer URL` attributes.
- `ADOBE_EXPERIENCE_TENANT` — if you use Marketo inside Adobe Experience Cloud, set your
  tenant slug and landing-page links become Adobe deep links. Otherwise the landing
  page's public URL is used.

## Where output goes

Everything is written under **`output/`** in this folder (change with `--out-dir`). The
folder is created for you and is git-ignored. The bulk report also keeps a cache in
`output/.work/`, which reports 4, 5 and 7 read.

> Marketo allows **50,000 REST API calls per day** and **100 calls per 20 seconds** on a standard subscription (higher limits can be purchased). Other integrations on your instance share this quota, so check how many calls a run will make (stated below) before starting a large one.

---

## 1. `form_fill_report.py` — form fills for a year (bulk export)

Pulls every **Fill Out Form** activity for one or more years with the Bulk Activity Extract
API, in monthly chunks, and writes **one CSV per year**. Every field a visitor submitted
becomes its own column (blank where a form did not have it). The same person appears once
per submission.

```bash
python form_fill_report.py --probe             # tiny pull: prints the columns it found, writes nothing
python form_fill_report.py                      # the current year, January 1 up to today
python form_fill_report.py --year 2025          # a specific year
python form_fill_report.py --year 2024 --year 2025
```

| Flag | Default | Purpose |
| --- | --- | --- |
| `--year YEAR` | current year | Year to pull. Repeat for several years. |
| `--out-dir DIR` | `output` | Where to write files. |
| `--probe` | off | Pull a tiny window, print the discovered columns and a sample row, write nothing. |
| `--today YYYY-MM-DD` | system date | Treat this as "today" (the current year is pulled up to this date). |

**Creates**

- `output/form_fills_<year>.csv` — one row per submission. Columns: `leadId`, `activityDate`,
  `formName`, the page-URL fields, then every submitted field alphabetically, then activity
  metadata (`Webpage ID`, `Client IP Address`, `User Agent`, …), `campaignId`, `marketoGUID`.
- `output/.work/form_fills_<year>.jsonl` and `.state.json` — resumable cache and checkpoint.

**Quota and resuming.** Marketo caps bulk exports at about **500 MB per day**. The run
checkpoints after every month; if the quota is hit (error `1029`) it stops cleanly with
progress saved. Re-run the **same command** after the quota resets and it skips months
already done. Form-fill data is text-only, so this is rarely reached.

## 2. `form_fills_month_get.py` — form fills for one month (GET only)

The same data as report 1 for a single calendar month, using only `GET` requests
(no export jobs). Use it when your API user can't create bulk exports, or for a quick
one-month pull.

```bash
python form_fills_month_get.py --year 2025 --month 7
python form_fills_month_get.py --year 2025 --month 7 --out-dir output
```

| Flag | Required | Purpose |
| --- | --- | --- |
| `--year YEAR` | yes | Four-digit year. |
| `--month MONTH` | yes | Month number, 1–12. The month runs from the 1st 00:00 UTC to the next month's 1st 00:00 UTC. |
| `--out-dir DIR` | no (`output`) | Where to write the file. |

**Creates** `output/form_fills_<YYYY>-<MM>.csv` (e.g. `form_fills_2025-07.csv`), one row per
submission. A month of about 1,000 submissions costs roughly 6 API calls.

Differences from report 1: `campaignId` is always blank (the REST endpoint doesn't return
it), and this report does **not** write the `output/.work` cache that reports 4, 5 and 7 need.

## 3. `combine_form_fills.py` — merge monthly files

Merges monthly files from report 2 into one file. Different months contain different form
fields, so the files have different columns; this takes the union of columns and leaves
blanks, so rows never misalign. It makes **no API calls**.

```bash
python combine_form_fills.py --months 2025-06 2025-07 2025-08
python combine_form_fills.py --months 2025-06 2025-07 --out-name summer.csv
```

| Flag | Required | Purpose |
| --- | --- | --- |
| `--months YYYY-MM …` | yes | Months to combine; each needs `output/form_fills_<YYYY>-<MM>.csv` already. |
| `--out-dir DIR` | no (`output`) | Where the monthly files are, and where to write. |
| `--out-name NAME` | no | Output filename. Default `form_fills_<first>_to_<last>.csv`. |

**Creates** `output/form_fills_<first>_to_<last>.csv` — all rows sorted by `activityDate`, plus a
`source_month` column.

---

### Reports 4–7 need a folder ID

Reports 4–7 look at the forms stored under one **Marketing Activities folder**, which you pass
as `--folder`. To find a folder's ID, select it in Marketing Activities and read the number
in the browser URL (for example `…#FO1234A1` → folder ID `1234`). The report includes every
subfolder and program beneath it.

Reports 4, 5 and 7 also use the **fill cache from report 1** to learn which pages forms were
submitted on, so run `form_fill_report.py` first for the years you want to include
(`--years`, default: the current year). Without the cache they still run, but the
"filled out on" and fill-count columns will be empty.

## 4. `form_pages_report.py` — pages per form

One row per form stored under the folder.

```bash
python form_pages_report.py --folder 1234
python form_pages_report.py --folder 1234 --years 2024 2025
```

| Flag | Required | Purpose |
| --- | --- | --- |
| `--folder ID` | yes | Marketing Activities folder ID (see above). |
| `--years Y …` | no (current year) | Years of cached fill data to include. |
| `--out-dir DIR` | no (`output`) | Output directory. |

**Creates** `output/form_pages_report.csv` — `form_id`, `form_name`, `stored_in`, `status`,
`fills_<years>`, `distinct_filled_pages`, `filled_out_on_pages` (where people actually submitted,
from activity), `lives_on_marketo_lps` (Marketo landing pages that embed the form).

## 5. `form_page_links_report.py` — every page per form

Like report 4 but **one row per form and page**, with links.

```bash
python form_page_links_report.py --folder 1234
python form_page_links_report.py --folder 1234 --years 2024 2025
```

Flags: `--folder` (required), `--years`, `--out-dir`, as in report 4.

**Creates** `output/form_page_links_report.csv` — `form_id`, `form_name`, `form_status`,
`relationship` (`Lives on (Marketo LP)` or `Filled out on`), `page_url`, `page_name`, `fills`,
`lp_id`, `lp_public_url`, `lp_status`.

## 6. `campaign_forms_report.py` — forms in a folder

Every distinct form that is **stored** in the folder subtree and/or **embedded** in a landing
page that belongs to a program in it. Uses no fill cache.

```bash
python campaign_forms_report.py --folder 1234
```

Flags: `--folder` (required), `--out-dir`.

**Creates** `output/campaign_forms_report.csv` — `form_id`, `form_name`, `status`, `sources`,
`stored_in`, `embedded_lp_count`, `embedded_landing_pages`.

> Limitation: Marketo's REST API does not expose smart-campaign trigger logic, so "forms
> referenced by a smart campaign's *Fills Out Form* trigger" cannot be read. This report covers
> the two sources the API does expose. Report 7 estimates the rest.

## 7. `campaign_form_map_report.py` — inferred program-to-form map

Because trigger logic is not available, this **infers** which form each program uses by matching
the program's name to form names (token overlap). Results are an approximation — every row
carries a confidence level.

```bash
python campaign_form_map_report.py --folder 1234
python campaign_form_map_report.py --folder 1234 --years 2024 2025
```

Flags: `--folder` (required), `--years`, `--out-dir`.

**Creates**

- `output/campaign_program_form_map.csv` — one row per program: `program_id`, `program_name`,
  `inferred_form_id`, `inferred_form_name`, `confidence`, `match_score`, `form_has_fills`.
- `output/campaign_forms_full.csv` — one row per distinct form, combining *stored*, *embedded* and
  *inferred* sources, with source flags and inference confidence.

## 8. `reply_list_report.py` — reply-list membership

Finds every static list whose name starts with a prefix, counts its members, and attributes it
to its parent program. Useful when a tool adds people to lists named like `Replied …`. Because
membership is only ever added to, the current count equals everyone ever added, and is not
limited by Marketo's activity-retention window.

```bash
python reply_list_report.py
python reply_list_report.py --prefix "Replied" --out-dir output
```

| Flag | Default | Purpose |
| --- | --- | --- |
| `--prefix TEXT` | `Replied` | List-name prefix to match (case-insensitive). |
| `--out-dir DIR` | `output` | Output directory. |

**Creates**

- `output/reply_lists_detail.csv` — one row per list: `program`, `list_name`, `list_id`,
  `member_count`, `workspace`, `createdAt`, `updatedAt`.
- `output/reply_lists_by_program.csv` — one row per program: `program`, `num_replied_lists`,
  `total_members`.

## Troubleshooting

| Message | Meaning / fix |
| --- | --- |
| `missing required config value(s)` | `.env` is missing or incomplete — see **Setup**. |
| `[auth] token request failed: invalid_client` | Wrong Client ID/Secret, or the service's API-only user is disabled. |
| `Access denied` / error `601`, `603` | The API role lacks a permission this report needs — see the permission list above. |
| Error `606` or `615` | Rate limit; the tools wait and retry automatically. |
| Error `1029` (report 1) | Daily bulk-export quota used. Re-run the same command later; it resumes. |

---

## License and disclaimer

This project is released under the [MIT License](LICENSE) and comes with **no warranty and no support**. It is an independent project, not affiliated with or endorsed by Adobe; Adobe and Marketo are trademarks of Adobe Inc.
