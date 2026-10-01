# MQL reason report

<!-- license-disclaimer -->
> ⚠️ **No warranty — use at your own risk.** This software is provided "as is", **without warranty of any kind**, express or implied, and the authors are not liable for any claim, damages or other liability arising from its use. You are responsible for reviewing the code and testing it before relying on it, for the API calls it makes against your Marketo quota, and for handling any personal data it exports in line with the law and your own policies. Run it against a sandbox or a small scope first. See [LICENSE](LICENSE) (MIT).

Finds every Marketo **smart campaign** whose flow stamps an **MQL reason** onto a lead
(by default the `Last MQL Reason` and `MQL Reason` fields), works out **which reason each
one actually writes**, and groups the result by reason — so you can see, for example, how many
campaigns stamp "Demo Request" versus "Webinar Registration", and which stamp nothing usable.

**Strictly read-only.** The API client has no method that can write to Marketo — every call
is a `GET`.

## Why it has to resolve tokens

The reason is almost never typed into the flow step. A *Change Data Value* step usually looks like:

```
Change Data Value
  Attribute Name: Last MQL Reason
  New Value:      {{my.Last MQL Reason}}        <- a program token
```

So the flow only says *that* a reason is stamped, not *which*. The value lives on the program,
or is inherited from a parent folder, and must be resolved per program before anything can be
grouped. The tool does this by merging two sources, because the program-level tokens endpoint
is not always complete (for some programs it returns inherited tokens, for others only the
program's own):

1. the program's own tokens, then
2. each parent folder's tokens, walking upward — the nearest definition wins, which is how
   Marketo itself resolves a `my` token.

The `token_defined_on` column records where each value was found (`program`, or
`folder: <name>`), which makes any surprising reason traceable.

## Setup

Requires **Python 3.9+**.

```bash
cd mql-reason-report
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
  - Read-Only Asset  (smart campaigns and their flows, programs, folders, program/folder tokens)

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

```bash
python mql_reason_report.py --limit 50      # smoke test: first 50 in-scope campaigns
python mql_reason_report.py                 # full run, default scope
```

| Flag | Default | Purpose |
| --- | --- | --- |
| `--scope S` | `active-plus` | Which campaigns to scan — see below. |
| `--field NAME` | `Last MQL Reason`, `MQL Reason` | Display name of a lead field that holds the MQL reason, **exactly as shown in Marketo**. Repeat for several. Flow steps match on the display name, not the API name. |
| `--limit N` | none | Only scan the first N in-scope campaigns. |
| `--stage S` | `all` | `campaigns`, `flows`, `report` or `all`. `report` rebuilds the CSVs from cache with **zero API calls**. |
| `--refresh` | off | Ignore caches and refetch from Marketo. |

### Choosing a scope — and what it costs

Reading a flow takes **one API call per campaign** (there is no bulk endpoint for flows), and
your daily quota is shared with every other integration on the instance. Pick the narrowest scope
that answers your question.

| `--scope` | Scans | Use it for |
| --- | --- | --- |
| `active` | campaigns with `isActive = true` | Only what is switched on right now. |
| `active-plus` *(default)* | active, plus *Recurring Run*, *Single Run* and *Requested* | Everything that can still fire. |
| `non-system` | every non-system campaign that belongs to a program | A historical view that includes inactive campaigns. |
| `all` | every smart campaign, including system ones | A complete audit. On a large instance this can use most of a day's quota — run it overnight. |

Total calls ≈ `campaigns ÷ 200` (listing) + one per campaign scanned + a handful per program that
stamps a reason. The first step prints the in-scope count so you can see the cost before the
scan starts; stop with Ctrl+C if it is too large — the scan is resumable.

> Marketo allows **50,000 REST API calls per day** and **100 calls per 20 seconds** on a standard subscription (higher limits can be purchased). Other integrations on your instance share this quota, so check how many calls a run will make (stated below) before starting a large one.

**Resumable.** The scan appends to `raw/flow_scan.jsonl` as it goes. Interrupt it and re-run: it picks
up where it stopped. Campaigns that failed are **not** marked done, so a re-run retries exactly
those. Changing `--field` triggers a re-scan automatically.

## What it creates

| File | Contents |
| --- | --- |
| `output/mql_reason_detail.csv` | One row per stamping step, sorted by reason so it groups on sight. |
| `output/mql_reason_summary.csv` | One row per distinct reason, with campaign and program counts. |
| `raw/smart_campaigns_all.json` | Cache: every smart campaign in the instance. |
| `raw/flow_scan.jsonl` | Cache: the scan result for each campaign. |
| `raw/program_tokens.json` | Cache: each program's metadata and resolved tokens. |

`output/` and `raw/` are created for you and are git-ignored.

**Detail columns:** `mql_reason`, `reason_field`, `value_kind`, `raw_value`, `program_name`,
`program_id`, `program_channel`, `program_folder`, `campaign_name`, `campaign_id`, `campaign_type`,
`campaign_status`, `campaign_is_active`, `workspace`, `token_defined_on`, `campaign_url`,
`program_url`, `campaign_updated_at`. A flow step with several if/then choices contributes one
row per choice, since each can stamp a different reason.

**Summary columns:** `mql_reason`, `reason_field`, `value_kind`, `campaign_count`,
`active_campaign_count`, `program_count`, `programs` (names, ` | ` separated). Sorted by campaign count.

### `value_kind` — how far to trust a row

| `value_kind` | Meaning |
| --- | --- |
| `program token` | `{{my.X}}` resolved to a real value — the normal case. |
| `literal` | A plain string typed directly into the flow step. |
| `unresolved token` | `{{my.X}}` where no such token exists on the program or any parent folder, or it exists but is empty. **The field is being stamped with unresolved token text or a blank** — worth fixing in Marketo by defining the token. |
| `dynamic token` | `{{lead.X}}`, `{{company.X}}` and similar — varies per lead, so there is no single static reason. |
| `empty` | The step sets nothing. |

## Caveats

- **Flow steps only.** This finds what *smart campaigns* stamp. The field can also be set by list
  imports, form fills, CRM sync or other integrations, and none of those appear here.
- **Scope is "can fire", not "ever fired".** The default scope skips inactive campaigns, so one that
  stamped a reason last year and was since deactivated will not appear. Use `--scope non-system`
  for the historical view.
- Reasons are often defined at **folder** level, so editing one folder token changes the reason for
  every program beneath it. `token_defined_on` shows where each value comes from.

## API endpoints used (all `GET`)

- `/identity/oauth/token` — access token
- `/rest/asset/v1/smartCampaigns.json` — list smart campaigns, paged
- `/rest/asset/v1/smartCampaign/{id}.json` — a campaign's flow (the only way to read flow steps)
- `/rest/asset/v1/program/{id}.json` — program name, channel, folder
- `/rest/asset/v1/folder/{id}.json` — folder name and parent, for the upward walk
- `/rest/asset/v1/folder/{id}/tokens.json?type=Program|Folder` — token values
- `/rest/v1/stats/usage.json` — optional, used by `python marketo_client.py` to show quota use

## Troubleshooting

| Message | Meaning / fix |
| --- | --- |
| `missing required config value(s)` | `.env` is missing or incomplete — see **Setup**. |
| `[auth] token request failed: invalid_client` | Wrong Client ID/Secret, or the service's API-only user is disabled. |
| `Access denied` / error `603` | The API role lacks **Read-Only Asset**. |
| `0 of N scanned campaigns stamp an MQL reason` | Your field's display name differs — pass it with `--field "<display name>"`. |

---

## License and disclaimer

This project is released under the [MIT License](LICENSE) and comes with **no warranty and no support**. It is an independent project, not affiliated with or endorsed by Adobe; Adobe and Marketo are trademarks of Adobe Inc.
