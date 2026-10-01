# Throttle verification scripts

<!-- license-disclaimer -->
> ⚠️ **No warranty — use at your own risk.** This software is provided "as is", **without warranty of any kind**, express or implied, and the authors are not liable for any claim, damages or other liability arising from its use. You are responsible for reviewing the code and testing it before relying on it, for the API calls it makes against your Marketo quota, and for handling any personal data it exports in line with the law and your own policies. Run it against a sandbox or a small scope first. See [LICENSE](../LICENSE) (MIT).

Two **read-only** scripts that make building and testing the [daily send throttle](../README.md) faster by
reading your Marketo instance instead of clicking through it. They are optional — the throttle itself is
built entirely in the Marketo UI — but they save time. Nothing is ever written to Marketo.

| Script | Run it | To learn |
| --- | --- | --- |
| [`discover.py`](#1-discoverpy--inventory-before-you-build) | before building | The real REST names of your fields, and whether lists/campaigns for a throttle already exist |
| [`queue_report.py`](#2-queue_reportpy--queue-snapshot-while-testing) | while testing and after go-live | How many people are queued, drawn and sent, and whether anything is stuck |

## Setup

Requires **Python 3.9+**.

```bash
cd daily-send-throttle/verify
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
  - Read-Only Person  (field definitions, static-list members)
  - Read-Only Asset   (static lists, campaigns)

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

> Marketo allows **50,000 REST API calls per day** and **100 calls per 20 seconds** on a standard subscription (higher limits can be purchased). Other integrations on your instance share this quota, so check how many calls a run will make (stated below) before starting a large one.
> `discover.py` costs a handful of calls; `queue_report.py` costs about one call per 300 members of each list you read.

---

## 1. `discover.py` — inventory before you build

Answers three questions that otherwise become build-time surprises: the **REST names** of your fields (the display
name in the Marketo UI is almost never the REST name), whether any throttle-style fields already exist, and what your
static lists and campaigns look like.

```bash
python discover.py                            # throttle-style fields, lists and campaigns
python discover.py --match "priority|segment" # also include names matching your audience terms
python discover.py --all-fields               # also dump every lead field to a file
```

| Flag | Purpose |
| --- | --- |
| `--match REGEX` | Extra case-insensitive regular expression of names to include, matched against fields, lists and campaigns. Use part of your audience field names, e.g. `"priority\|segment"`. |
| `--all-fields` | Also write every lead field to `output/fields.json`. |

By default it matches names containing `queue`, `bucket`, `slot`, `sequence`, `throttl`, `drip`, `daily cap`, `send date`
(fields), and `queue`, `throttl`, `drip`, `sent` (lists and campaigns) — things that would collide with a throttle build.

**Output.** It prints three sections to the terminal — **FIELDS** (REST name, display name, type), **STATIC LISTS** (id,
name) and **CAMPAIGNS** (id, name). Copy the REST names and list ids into
[`../marketo/BUILD-CHECKLIST.md`](../marketo/BUILD-CHECKLIST.md). With `--all-fields` it creates **`output/fields.json`**
in this folder (git-ignored).

## 2. `queue_report.py` — queue snapshot while testing

After a draw has run, shows exactly how many people were released, how many are still waiting, how long the oldest has
waited, and flags configuration mistakes — without clicking through Marketo.

```bash
python queue_report.py --queue-list-id 1234
python queue_report.py --queue-list-id 1234 --sent-list-id 1235
python queue_report.py --queue-list-id 1234 --sent-list-id 1235 --daily-cap 100 --csv output/queue_snapshot.csv
```

Get the list ids from `discover.py`, or from the number in a static list's Marketo URL.

| Flag | Required | Purpose |
| --- | --- | --- |
| `--queue-list-id ID` | yes | Id of your `Throttle - Send Queue` static list. |
| `--sent-list-id ID` | no | Id of your `Throttle - Sent` list; enables the "in both lists" check. |
| `--daily-cap N` | no (50) | Your daily cap, used to flag people who hold a released slot but are still queued. |
| `--status-field`, `--date-field`, `--slot-field`, `--seq-field` | no | REST names of the four throttle fields. Defaults: `throttleQueueStatus`, `throttleQueueDate`, `throttleDailySlot`, `throttleSendSequence`. Override if `discover.py` shows different names. |
| `--csv PATH` | no | Also write the raw queue rows to this file. |

**Output.** Printed to the terminal: queue size, a count per `Throttle Queue Status`, the oldest and newest queue date,
and how many queued people carry a slot number. It warns you when:

- **people still queued hold a slot 1–N** — they should have been sent and removed; check the choice steps in `03 - Draw Handler`;
- **people are in both the queue and the sent list** — the *Remove from List* step isn't firing.

With `--csv` it also creates the file you name (for example `output/queue_snapshot.csv`, git-ignored) with the columns
`id`, `email`, and the four throttle fields.

## Troubleshooting

| Message | Meaning / fix |
| --- | --- |
| `missing required config value(s)` | `.env` is missing or incomplete — see **Setup**. |
| `603 Access denied` on every endpoint, but a token *is* issued | The credentials are valid but the API user's role lacks the permissions above. `discover.py` prints the API user and the exact things to check. Role *names* are arbitrary — "API Admin" does not necessarily have any *Access API* box ticked. Also check the role is granted in the workspace that holds the assets. |
| Role was just fixed but still `603` | Marketo may hand back an existing valid token minted before the change. Wait for it to expire (`expires_in`), then re-run. |
| `Marketo rejected a field name` | The default REST names don't match your instance. Run `discover.py`, then pass the real names with `--status-field` etc. |

---

## License and disclaimer

This project is released under the [MIT License](../LICENSE) and comes with **no warranty and no support**. It is an independent project, not affiliated with or endorsed by Adobe; Adobe and Marketo are trademarks of Adobe Inc.
