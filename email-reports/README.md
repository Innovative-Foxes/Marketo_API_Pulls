# Email reports

<!-- license-disclaimer -->
> ⚠️ **No warranty — use at your own risk.** This software is provided "as is", **without warranty of any kind**, express or implied, and the authors are not liable for any claim, damages or other liability arising from its use. You are responsible for reviewing the code and testing it before relying on it, for the API calls it makes against your Marketo quota, and for handling any personal data it exports in line with the law and your own policies. Run it against a sandbox or a small scope first. See [LICENSE](LICENSE) (MIT).

Three read-only reports about the **emails** in your Marketo instance: which were sent and how
many times, how your emails split between operational and non-operational, and which emails use
which **From address**. Nothing is changed in Marketo.

| Script | Answers |
| --- | --- |
| [`sent_emails_by_id.py`](#1-sent_emails_by_idpy--emails-sent-this-year-with-asset-ids) | Which emails were actually sent this year, how many times, keyed by asset ID |
| [`marketo_email_report.py`](#2-marketo_email_reportpy--operational-vs-non-operational-emails) | Sent / delivered per email, split into operational and non-operational |
| [`from_address_audit.py`](#3-from_address_auditpy--from-address-audit) | Every email's From address, classified against a sending domain you choose |

## Setup

Requires **Python 3.9+**.

```bash
cd email-reports
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
  - Read-Only Asset     (list email assets: name, From address, operational flag)
  - Read-Only Activity  (Send Email / Email Delivered activity — sent_emails_by_id.py only)

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
5. **`MARKETO_APP_BASE`** *(required for this pull)*: the host in your browser's address bar
   while logged in to Marketo, e.g. `https://app-ab12.marketo.com` (no path).

Put the values in this folder's `.env` (see **Setup**). `.env` is git-ignored, so your
secret is never committed.

## Where output goes

All reports write to **`output/`** in this folder by default (created for you, git-ignored). Change it
with `--out-dir`.

> Marketo allows **50,000 REST API calls per day** and **100 calls per 20 seconds** on a standard subscription (higher limits can be purchased). Other integrations on your instance share this quota, so check how many calls a run will make (stated below) before starting a large one.

---

## 1. `sent_emails_by_id.py` — emails sent this year, with asset IDs

Uses the **Bulk Activity Extract** API to pull every *Send Email* activity for a year, reduces them to one
row per email asset, and enriches each with its From address, domain and Marketo link. Because every send
record carries the email's asset ID, there is no name-matching guesswork.

```bash
python sent_emails_by_id.py                          # the current year, January 1 to today
python sent_emails_by_id.py --year 2025
python sent_emails_by_id.py --year 2025 --activity delivered
```

| Flag | Default | Purpose |
| --- | --- | --- |
| `--year YEAR` | current year | Year to extract. |
| `--activity {sent,delivered}` | `sent` | `sent` = *Send Email* activity (type 6); `delivered` = *Email Delivered* (type 7). |
| `--out-dir DIR` | `output` | Where to write files. |

**Creates**

- `output/emails_sent_<year>.csv` — `email_id`, `email_name`, `program`, `sent_count`, `operational`,
  `fromEmail`, `domain`, `url`.
- `output/emails_sent_<year>.state.json` — checkpoint used for resuming.

**Quota and resuming.** A full year of sends can exceed Marketo's bulk-export limit (about **500 MB per day**),
so the year is pulled one month at a time, progress is saved after every month, and the file is usable even if
a run stops early. If the quota is hit (error `1029`), re-run the **same command** after it resets and it continues
from the first unfinished month. The script creates export *jobs* in Marketo (a `POST`), but reads and changes
no data.

## 2. `marketo_email_report.py` — operational vs non-operational emails

Joins your email assets with Marketo's **Email Performance Report** (which you export from the Marketo UI) to
produce sent and delivered numbers per email, split by the asset's *operational* flag.

First export the performance data: in Marketo open **Analytics → Email Performance**, set the date range you
want, and export it to CSV (or `.xlsx`). The file needs a name column (`Email Name`, `Email` or `Name`) and `Sent`
and `Delivered` columns.

```bash
python marketo_email_report.py --epr email_performance.csv
python marketo_email_report.py --epr email_performance.csv --year 2025 --out-dir output
python marketo_email_report.py            # metadata only: every email asset, no performance numbers
```

| Flag | Default | Purpose |
| --- | --- | --- |
| `--epr FILE` | none | The Email Performance Report export. Omit it for metadata-only mode. |
| `--year YEAR` | current year | Used only in the output filenames. |
| `--out-dir DIR` | `output` | Where to write files. |
| `--url-suffix S` | `A1` | Deep-link suffix after `#EM<id>`. Use `A1LA1` for program-local emails. |

**Creates** (`<year>` is `--year`)

- `output/operational_emails_<year>.csv` and `output/non_operational_emails_<year>.csv` — with `--epr`:
  `email_name`, `sent`, `delivered`, `url`, `fromEmail`, `domain`. Only emails that appear in the performance
  export are written. Without `--epr`: `email_name`, `url`, `fromEmail`, `domain` for every asset.
- `output/unresolved_emails_<year>.csv` — only if some performance rows couldn't be matched to an asset
  (`label`, `sent`, `delivered`, `reason`). The run summary shows what share of send volume was resolved.

Matching is by email name; when several assets share a name the script uses the program/folder in the
report's label, and falls back to the shared *operational* flag if all candidates agree.

## 3. `from_address_audit.py` — From address audit

Reads every email asset and classifies it by its **From address** against a sending domain you give it — for
example before moving all your emails onto one authenticated sending domain. It makes **no** changes.

```bash
python from_address_audit.py --target-domain mail.example.com
python from_address_audit.py --target-domain mail.example.com --year 2025 --out-dir output
```

| Flag | Required | Purpose |
| --- | --- | --- |
| `--target-domain DOMAIN` | yes | The sending domain you consider correct, e.g. `mail.example.com`. |
| `--year YEAR` | no (current year) | Used only in the output filenames. |
| `--out-dir DIR` | no (`output`) | Where to write files. |

Each email gets a `status`:

| `status` | Meaning |
| --- | --- |
| `token` | From address uses a `{{token}}` — leave as-is. |
| `target` | Already on the target domain — nothing to do. |
| `empty` | From address blank — flagged (it would fail approval). |
| `candidate` | A literal From address on some other domain — would need updating. |

**Creates**

- `output/from_address_audit_<year>.csv` — every email: `id`, `email_name`, `operational`, `status`,
  `current_fromEmail`, `current_domain`, `folder`, `is_local`, `url`.
- `output/from_address_candidates_<year>.csv` — the same columns, only for `candidate` emails.

It also prints a breakdown of the candidate emails by domain.

## API endpoints used

- `GET /identity/oauth/token` — access token
- `GET /rest/asset/v1/emails.json` — all email assets, paged
- `POST /bulk/v1/activities/export/create.json`, `.../{id}/enqueue.json`, `GET .../status.json`,
  `GET .../file.json` — Bulk Activity Extract (report 1 only; creates export jobs and reads activity data only)

## Troubleshooting

| Message | Meaning / fix |
| --- | --- |
| `missing required config value(s): MARKETO_APP_BASE` | These reports build Marketo links, so `MARKETO_APP_BASE` is required — see **Setup**. |
| `[auth] token request failed: invalid_client` | Wrong Client ID/Secret, or the service's API-only user is disabled. |
| `Access denied` / error `603` | The API role lacks **Read-Only Asset** (and **Read-Only Activity** for report 1). |
| `EPR file is missing a 'Sent' column` | The performance export needs columns named `Sent` and `Delivered` plus an email-name column. |
| Error `1029` (report 1) | Daily bulk-export quota used. Re-run the same command later; it resumes. |

---

## License and disclaimer

This project is released under the [MIT License](LICENSE) and comes with **no warranty and no support**. It is an independent project, not affiliated with or endorsed by Adobe; Adobe and Marketo are trademarks of Adobe Inc.
