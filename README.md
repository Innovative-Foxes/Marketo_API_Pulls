# Marketo API Pulls

<!-- license-disclaimer -->
> ⚠️ **No warranty — use at your own risk.** This software is provided "as is", **without warranty of any kind**, express or implied, and the authors are not liable for any claim, damages or other liability arising from its use. You are responsible for reviewing the code and testing it before relying on it, for the API calls it makes against your Marketo quota, and for handling any personal data it exports in line with the law and your own policies. Several folders also change nothing in Marketo but one ([`daily-send-throttle`](daily-send-throttle/README.md)) is a build guide that affects live email sending. See [LICENSE](LICENSE) (MIT).

A toolkit of **read-only** scripts that pull data out of Adobe Marketo Engage through its REST APIs and turn it into clean
CSVs, plus a build guide and two browser scripts for related marketing-ops work. Each folder is a self-contained "pull" with
its own README, its own credentials file and its own output folder — clone the repo, set up the pull you need, and run it.

> **Nothing here writes to your Marketo instance.** Every script reads data (the bulk-extract tools create short-lived export
> jobs, which change no data). Even so, use a dedicated **API-only user with read-only permissions** — each pull's README lists
> the exact permissions it needs.

## The pulls

| Folder | What it gives you | Runs on |
| --- | --- | --- |
| [`form-reports`](form-reports/README.md) | Every form submission (per year or month), which pages each form lives on, forms in a folder, inferred program→form map, reply-list membership | Python |
| [`program-members-report`](program-members-report/README.md) | Every member of every program created in a date window, with their program status, plus a per-program summary | Python |
| [`mql-reason-report`](mql-reason-report/README.md) | Which campaigns stamp an MQL reason, and which reason each one writes, grouped by reason | Python |
| [`mql-flow-step-audit`](mql-flow-step-audit/README.md) | Campaigns that set a "Last MQL Date" without setting the matching reason | Node.js |
| [`email-reports`](email-reports/README.md) | Emails sent per year (with asset IDs), sent/delivered split by operational flag, From-address audit | Python |
| [`folder-programs-export`](folder-programs-export/README.md) | Every program under a folder tree, as JSON, CSV, text and an Excel workbook | Node.js |
| [`daily-send-throttle`](daily-send-throttle/README.md) | A build guide for capping nurture sends at N per day, plus read-only `verify/` pulls | Marketo + Python |
| [`utm-forwarding`](utm-forwarding/README.md) | Browser script that re-applies a visitor's UTM parameters on later pageviews | Browser JS |
| [`utm-first-touch`](utm-first-touch/README.md) | Browser script that stores first- and last-touch UTMs in cookies and fills form fields | Browser JS |

The two UTM folders are website scripts, not API pulls — they need no credentials.

## Quick start

Every pull works the same way. Using `program-members-report` as the example:

```bash
git clone <this-repo-url> marketo-api-pulls
cd marketo-api-pulls/program-members-report

# Python pulls
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env              # then edit .env with your Marketo credentials
python auth.py                    # prints an access token if the credentials work

python program_members_report.py --start 2025-01-01 --end 2025-12-31
```

For the Node.js pulls (`mql-flow-step-audit`, `folder-programs-export`) install with `npm install` instead of `pip`, and run
them with `npm start` / `node index.js`. Each README gives the exact commands and flags.

**Requirements:** Python 3.9+ for the Python pulls, Node.js 18+ for the Node pulls.

## Getting your Marketo API credentials

Every pull needs the same three values, kept in that pull's `.env`:

| `.env` key | Where it comes from |
| --- | --- |
| `MARKETO_CLIENT_ID` | the **Client ID** of a LaunchPoint Custom service |
| `MARKETO_CLIENT_SECRET` | the **Client Secret** of that service |
| `MARKETO_REST_BASE` | your **REST endpoint host**, e.g. `https://123-ABC-456.mktorest.com` (no trailing `/rest`) |
| `MARKETO_APP_BASE` *(optional)* | the host in your browser while logged in to Marketo, e.g. `https://app-ab12.marketo.com`; only used to build clickable links |

To create them (a Marketo admin does this once — it's all in the **Admin** area):

1. **Role** — Admin → Users & Roles → Roles → **New Role**. Under **Access API**, tick the read-only permissions the pull needs
   (its README lists them: typically *Read-Only Asset*, *Read-Only Person* and/or *Read-Only Activity*).
2. **User** — Admin → Users & Roles → Users → **Invite New User**, tick **API Only**, assign the role.
3. **Service** — Admin → Integration → **LaunchPoint** → **New** → **New Service**, type **Custom**, API Only User = that user.
   Open it and choose **View Details** to see the Client ID and Client Secret.
4. **Endpoint** — Admin → Integration → **Web Services** → *REST API* → **Endpoint**.

One API user and service can be shared by all the pulls, but each pull has its **own `.env`** so it can be used on its own.

## How the pulls are organised

```
marketo-api-pulls/
├── README.md                      this file
├── <pull>/                        one folder per pull
│   ├── README.md                  exactly how to use it, what it creates
│   ├── .env.example               credential template  →  copy to .env (git-ignored)
│   ├── requirements.txt           or package.json for Node
│   ├── <scripts>
│   ├── output/                    reports it writes (created on first run, git-ignored)
│   └── raw/                       cached API responses (where used, git-ignored)
```

- **`output/`** is where each pull writes its reports. **`raw/`** (or a `.work/` folder) holds cached API responses so an
  interrupted run resumes and a re-run is cheap. Both are created automatically and are git-ignored.
- Secrets never leave your machine: `.env` files, outputs and caches are all git-ignored. Only `.env.example` templates are committed.
- The Python pulls each carry the same small `auth.py`, so any folder can be copied out and used on its own.

## API quota and etiquette

Marketo allows **50,000 REST API calls per day** and **100 calls per 20 seconds** on a standard subscription, shared by every
integration on the instance. The pulls throttle themselves to about 4 calls per second, retry on rate-limit errors, and state
roughly how many calls a run will make. For big jobs — especially anything that scans every campaign — check the README's cost
note and consider running overnight. Bulk activity exports have a separate daily limit (about 500 MB); the tools that use them
resume from where they stopped.

## A note on personal data

Some pulls export **personal data**: `program-members-report` writes lead email addresses, and `form-reports` contains
everything visitors typed into forms plus IP addresses and user agents. Treat the `output/` and `raw/` folders as personal data:
keep them out of version control (the `.gitignore` does this), and share files over a channel your organisation approves.

---

## License and disclaimer

This project is released under the [MIT License](LICENSE) and comes with **no warranty and no support**. It is an independent project, not affiliated with or endorsed by Adobe; Adobe and Marketo are trademarks of Adobe Inc.
