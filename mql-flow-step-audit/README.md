# MQL flow-step audit

<!-- license-disclaimer -->
> ⚠️ **No warranty — use at your own risk.** This software is provided "as is", **without warranty of any kind**, express or implied, and the authors are not liable for any claim, damages or other liability arising from its use. You are responsible for reviewing the code and testing it before relying on it, for the API calls it makes against your Marketo quota, and for handling any personal data it exports in line with the law and your own policies. Run it against a sandbox or a small scope first. See [LICENSE](LICENSE) (MIT).

Finds Marketo **smart campaigns that set a "Last MQL Date" field but never set the matching
"Last MQL Reason" field** — the campaigns responsible for leads that become MQLs with no
recorded reason. It reads each campaign's flow and looks for *Change Data Value* steps; nothing
is written to Marketo.

Related: [`../mql-reason-report`](../mql-reason-report) answers the opposite question — for the
campaigns that *do* stamp a reason, which reason is it.

## Setup

Requires **Node.js 18+**.

```bash
cd mql-flow-step-audit
npm install
cp .env.example .env               # then edit .env with your credentials
```

## Getting your Marketo API credentials

You need an **API-only user** and a **Custom service**. Everything below is in the
Marketo **Admin** area and requires an admin to do once. (Marketo calls the pair of
values a *Client ID* and *Client Secret*.)

1. **Role** — Admin → Users & Roles → Roles → **New Role**. Under **Access API**,
   tick the permissions this pull needs (all read-only):
  - Read-Only Asset  (smart campaigns, programs, folders)

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
npm start            # same as: node index.js
```

The audit reads one campaign flow per API call, so a big instance takes a while. It works in
**sessions**: each run scans for up to 8 minutes, saves its progress, and stops. **Re-run the same
command until it reports `=== DONE ===`** — it skips campaigns it has already scanned. The final
reports are only written once every in-scope campaign has been scanned.

### Settings (all optional, set in `.env`)

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCOPE` | `active` | `active` = campaigns that are switched on. `active-plus` = also *Recurring Run* / *Single Run* campaigns. `all` = every smart campaign, including inactive ones. |
| `MQL_DATE_DISPLAY_NAME` | `Last MQL Date` | Display name of the date field, exactly as shown in Marketo. |
| `MQL_REASON_DISPLAY_NAME` | `Last MQL Reason` | Display name of the reason field. |
| `SESSION_BUDGET_MS` | `480000` (8 min) | How long one run scans before saving and stopping. |
| `INTER_CALL_DELAY_MS` | `220` | Pause between API calls (about 4.5 calls/second, under Marketo's 100 calls per 20 seconds). |

> Marketo allows **50,000 REST API calls per day** and **100 calls per 20 seconds** on a standard subscription (higher limits can be purchased). Other integrations on your instance share this quota, so check how many calls a run will make (stated below) before starting a large one.
> Cost: one call per campaign in scope, plus one per 200 campaigns to list them, plus one per
> distinct parent program of a flagged campaign. `SCOPE=all` on a large instance can use most of a day's quota —
> start with the default.

A campaign is **flagged** ("suspect") when its flow has at least one step setting the date field and
**no** step setting the reason field.

## What it creates

Everything is written to **`output/`** in this folder (created for you, git-ignored).

| File | Contents |
| --- | --- |
| `output/suspect-campaigns.csv` / `.json` | One row per flagged campaign: `Campaign ID`, `Campaign Name`, `Campaign UI URL`, `Status`, `Type`, `Is Active`, `Workspace`, `Parent Program ID`, `Parent Program Name`, `Parent Program URL`, `Program Folder`, `Program Channel`, `Date Step Count`, `Reason Step Count`, `Total Flow Steps`, `Updated At`. |
| `output/suspect-programs.csv` / `.json` | The same, rolled up one row per program: `Program ID`, `Program Name`, `Program URL`, `Program Folder`, `Program Channel`, `# Suspect Campaigns`, `Suspect Campaign IDs`, `Suspect Campaign Names`. |
| `output/all-campaigns-summary.csv` | Every campaign scanned, for sanity-checking: `Campaign ID`, `Campaign Name`, `UI URL`, `Status`, `Date Steps`, `Reason Steps`, `Flag` (`SUSPECT` or blank), `Total Steps`. |
| `output/campaigns-list.json` | Cache of every smart campaign (delete it to refresh the list). |
| `output/results.jsonl` | Cache of each campaign's scan result — this is what lets runs resume. |

To start over from scratch, delete `output/`.

## Caveats

- **Flow steps only.** A field can also be set by list imports, forms, CRM sync or other integrations; those are invisible here.
- `Change Data Value` steps inside **choices** (if/then branches) are included.
- A campaign that cannot be fetched (deleted, or no permission) is recorded in `results.jsonl` with an error and skipped.

## API endpoints used (all `GET`)

- `/identity/oauth/token` — access token
- `/rest/asset/v1/smartCampaigns.json` — list smart campaigns, paged
- `/rest/asset/v1/smartCampaign/{id}.json` — a campaign's flow
- `/rest/asset/v1/program/{id}.json` — parent program details for flagged campaigns

## Troubleshooting

| Message | Meaning / fix |
| --- | --- |
| `Missing MARKETO_REST_BASE / MARKETO_CLIENT_ID / MARKETO_CLIENT_SECRET` | `.env` is missing or incomplete — see **Setup**. |
| `Marketo error 601/602` | Access token problem; check the Client ID/Secret and that the API-only user is active. |
| `Marketo error 603` | The API role lacks **Read-Only Asset**. |
| `Remaining: N/M. Re-run to continue.` | Normal — the session budget ended. Run it again. |
| Every campaign shows 0 date steps | The field's display name differs; set `MQL_DATE_DISPLAY_NAME` / `MQL_REASON_DISPLAY_NAME`. |

---

## License and disclaimer

This project is released under the [MIT License](LICENSE) and comes with **no warranty and no support**. It is an independent project, not affiliated with or endorsed by Adobe; Adobe and Marketo are trademarks of Adobe Inc.
