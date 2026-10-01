# Folder programs export

<!-- license-disclaimer -->
> ⚠️ **No warranty — use at your own risk.** This software is provided "as is", **without warranty of any kind**, express or implied, and the authors are not liable for any claim, damages or other liability arising from its use. You are responsible for reviewing the code and testing it before relying on it, for the API calls it makes against your Marketo quota, and for handling any personal data it exports in line with the law and your own policies. Run it against a sandbox or a small scope first. See [LICENSE](LICENSE) (MIT).

Exports **every Marketo program under a named folder** — the whole subtree — to JSON, CSV, plain
text and a multi-tab **Excel workbook**. Handy for inventories, audits, or handing a team a clean
list of what lives in their part of Marketing Activities. Read-only; nothing is written to Marketo.

## Setup

Requires **Node.js 18+**.

```bash
cd folder-programs-export
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

Give it the **name of the folder** as it appears in Marketing Activities:

```bash
node index.js --root "Marketing Programs"
# or set ROOT_FOLDER_NAME in .env and run:
npm start
```

| Option | Required | Purpose |
| --- | --- | --- |
| `--root "Folder Name"` | yes (or `ROOT_FOLDER_NAME` in `.env`) | The folder to export. The command-line value wins if both are set. |

The folder is looked up by name. If several folders share the name (for example across workspaces), the
one under **Marketing Activities** is used.

**Cost:** one call to find the folder, one per 200 folders to read the tree, and one per 200 programs
to list programs. Typically a few dozen calls. It makes about 4–5 calls per second.

> Marketo allows **50,000 REST API calls per day** and **100 calls per 20 seconds** on a standard subscription (higher limits can be purchased). Other integrations on your instance share this quota, so check how many calls a run will make (stated below) before starting a large one.

## What it creates

Written to **`output/`** in this folder (created for you, git-ignored):

| File | Contents |
| --- | --- |
| `output/programs.csv` | One row per program: `Folder Path`, `Folder ID`, `Program ID`, `Program Name`, `Program Type`, `Channel`, `Status`, `Created At`, `Updated At`, `URL`. A folder with no programs gets one `(no programs)` row. |
| `output/programs.json` | The same data grouped by folder. |
| `output/programs.txt` | A readable outline: each folder with its program names. |
| `output/programs.xlsx` | Excel workbook — see below. |

### The Excel workbook

- **Tab 1**, named after the root folder: programs that sit directly in the root folder itself
  (not inside any of its subfolders).
- **One tab per direct child folder** of the root, covering that child's whole subtree — so a root with
  children `2025`, `2026` and `Evergreen` gives four tabs.

Each tab has a **Total Programs** count, then the columns `Program Name`, `Folder Location`
(relative to the tab's folder), `Channel`, `Status`, `Marketo URL` (clickable), with a filter on the header.
Tab names are shortened to Excel's 31-character limit and made unique.

## Notes

- Marketo's program listing can return programs outside the requested folder (sometimes the whole
  workspace), so the export keeps only programs whose folder is inside the tree and logs how many it dropped.
- Program internals (assets, smart campaigns) are not included — only the programs themselves.

## API endpoints used (all `GET`)

- `/identity/oauth/token` — access token
- `/rest/asset/v1/folder/byName.json` — find the root folder
- `/rest/asset/v1/folders.json` — the folder tree, paged
- `/rest/asset/v1/programs.json` — programs, paged

## Troubleshooting

| Message | Meaning / fix |
| --- | --- |
| `Missing MARKETO_REST_BASE / MARKETO_CLIENT_ID / MARKETO_CLIENT_SECRET` | `.env` is missing or incomplete — see **Setup**. |
| `No root folder given` | Pass `--root "Folder Name"` or set `ROOT_FOLDER_NAME` in `.env`. |
| `Folder "…" not found` | The name must match exactly (case and spacing). Check it in Marketing Activities. |
| `Marketo API error 603` | The API role lacks **Read-Only Asset**. |

---

## License and disclaimer

This project is released under the [MIT License](LICENSE) and comes with **no warranty and no support**. It is an independent project, not affiliated with or endorsed by Adobe; Adobe and Marketo are trademarks of Adobe Inc.
