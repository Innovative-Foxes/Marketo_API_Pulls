# Daily send throttle

<!-- license-disclaimer -->
> ⚠️ **No warranty — use at your own risk.** This software is provided "as is", **without warranty of any kind**, express or implied, and the authors are not liable for any claim, damages or other liability arising from its use. You are responsible for reviewing the code and testing it before relying on it. **This guide changes how email is sent from your Marketo instance** — a mistake can send unintended email to real people, or silently stop sending. Build and test it in a sandbox or with test data before going live. Third-party services it uses (such as FlowBoost) are governed by their own terms. See [LICENSE](LICENSE) (MIT).

Let people enter a Marketo nurture or drip **as fast as they qualify, but send to no more than N of them per
business day** (the examples use **N = 50**, Monday–Friday), carrying the overflow forward to the next day.

Marketo has no native way to do this (see [Why Marketo can't do this natively](#why-marketo-cant-do-this-natively)).
This project builds it with Marketo smart campaigns plus a small [FlowBoost](https://flowboo.st/) webhook that keeps a
**date-keyed counter**. It is a *build guide*, not a report: you create the campaigns in your Marketo instance by
following the runbook below. Two read-only scripts in [`verify/`](verify/README.md) are the only parts that call the API.

> **Read this first, then follow the runbook top to bottom.** If a third-party service is off the table, jump to
> [Alternatives](#alternatives).

| | |
|---|---|
| **Requirement** | Anyone matching your *audience criteria* enters the drip, but **no more than N emails go out per day, Mon–Fri**, with the remainder carried forward |
| **Approach** | Marketo static-list queue + FlowBoost date-keyed counter (a "daily draw") |
| **Effort** | Roughly 9–12 hours end to end, about half of it testing |
| **You need** | Marketo admin access (to create fields, webhooks, campaigns) and a FlowBoost private API key |

---

## What you are building

```
CRM / form / import
      │  something marks a person as matching your audience criteria
      ▼
[01-Enqueue]  (trigger)  ──►  static list "Throttle - Send Queue"  + FIFO sequence stamp
      │
      │   ...person waits in the queue, however many days that takes...
      ▼
[02-Daily Draw]  (batch, M–F 08:00)
      │  flow step: Request Campaign          ◄── webhooks cannot run in a batch
      ▼                                            campaign; this bridge is mandatory
[03-Draw Handler]  (trigger)
      │  flow step: Call Webhook → FB · Daily Draw
      │             increments a counter keyed to TODAY'S DATE,
      │             returns this person's slot number for today
      ▼
   ┌──────────────────────────┬──────────────────────────┐
   │ slot 1–N                 │ slot N+1 and above       │
   ▼                          ▼                          │
 Send email                 do nothing —                 │
 (04-Mark Sent then         stays Queued,  ──────────────┘
  records it)               redraws tomorrow
```

**The one idea that makes this work:** the counter name contains today's date, so a fresh counter starting at zero
exists each morning and the *entire* queue re-competes for the N slots every business day. That prevents both failure
modes below. Do not "simplify" it back to a permanent global counter with a fixed bucket per person.

## Why a daily draw

The obvious design assigns each person a **permanent, global sequence number** on arrival and derives a bucket from it:
people 1–50 are Bucket 1, 51–100 Bucket 2, and so on; Bucket 1 releases Monday, Bucket 2 Tuesday. That is correct
**only if every person is enumerated before any bucket is released.** A continuously growing queue breaks it twice.

**Failure A — the orphaned partial bucket (guaranteed to happen).**

| Day | New qualifiers | Sequence | What happens |
|---|---|---|---|
| Mon | 30 | 1–30 → Bucket 1 | Bucket 1 releases: 30 sends. Bucket 1 is now "spent". |
| Tue | 40 | 31–70 → seq 31–50 land in **Bucket 1**, 51–70 in Bucket 2 | Bucket 2 releases: 20 sends. **The 20 people in seq 31–50 are orphaned forever**, and nothing reports it. |

**Failure B — "release everything up to today's bucket" breaks the cap.** Patching A by releasing `bucket <= today's index`
lets unused capacity bank up:

| Day | New qualifiers | What happens |
|---|---|---|
| Mon | 0 | 0 sends; index advances to 1. |
| Tue | 200 | Index is 2, so buckets 1 **and** 2 release: **100 sends**. Cap violated. |

**Root cause:** a fixed bucket assigned at arrival time is the wrong primitive when the arrival rate is unknown. The right
primitive is a **daily draw**: capacity is counted *per calendar day at release time*, and everyone still waiting
re-competes the next business day. Properties you get for free: unused capacity never banks, nobody is stranded, and
Friday→Monday needs no special handling (no batch runs on the weekend, so no weekend counter is ever created).

## Why Marketo can't do this natively

Marketo has no "send the first N who qualify today and hold the rest." Every native mechanism is *per-person* or
*proportional*, never *absolute*:

| Approach | Why it doesn't work |
|---|---|
| **Communication limits** | Caps emails *one person* receives. Does nothing to cap total campaign volume. |
| **Smart campaign person limit** (Schedule tab) | A safety brake, not a throttle: it **blocks the entire run** when too many qualify. It sends 0, not N. |
| **Random sample** | Percentage-based. 25% of 200 is 50, but 25% of 40 is 10 and 25% of 800 is 200. No percentage yields a fixed N, and skipped people aren't tracked. |
| **Alphabetical / last-name splitting** | Spreads a *known* list across days with uneven buckets; can't handle a growing audience. |
| **Wait-and-re-request loop** | Fragile: people in a wait step are flushed when the campaign is edited, and nothing counts to N. |
| **Self-Service Flow Steps** (LaunchPoint) | The enterprise-grade route (batches of 1,000), but needs a hosted LaunchPoint service. |

**The platform constraint that shapes the design.** Marketo documents *"Use the Call Webhook flow step only in Trigger
campaigns"*; in a batch campaign it fails with `Cannot call webhook in batch campaign`. So the batch campaign cannot call
the counter directly. The standard workaround is the two-campaign bridge above: a scheduled batch campaign uses *Request
Campaign* to hand each person to a trigger campaign (`Campaign is Requested`, source `Marketo Flow Action`), which calls
the webhook. This is not optional.

## Alternatives

- **A scheduled script against the Marketo REST API (no third party, exact FIFO).** A cron job reads the queue list
  (`GET /rest/v1/list/{id}/leads.json`), sorts by queue date, takes the oldest N, and requests the `03-Draw Handler`-style
  trigger campaign for them (`POST /rest/v1/campaigns/{id}/trigger.json`, max 100 leads per call). Exact ordering and
  easy holiday calendars, but you must host and monitor it and someone has to maintain code.
- **Count outside Marketo.** If your leads originate in a CRM, a scheduled CRM flow can pick the oldest N un-released
  records each weekday and stamp a date field that Marketo triggers on. Exact and third-party-free, but it needs your CRM admin.
- **Marketo-only approximations.** A recurring batch that releases a random percentage can approximate the cap with no
  third party but is not exact and needs re-tuning when volume changes; a manual daily release is exact but costs minutes
  every business day.

Everything below builds the FlowBoost version.

---

## What's in this folder

| Path | What it is |
| --- | --- |
| [`flowboost/`](flowboost/) | The two webhook scripts to paste into Marketo, plus [`WEBHOOK-CONFIG.md`](flowboost/WEBHOOK-CONFIG.md) with exact webhook settings. |
| [`marketo/BUILD-CHECKLIST.md`](marketo/BUILD-CHECKLIST.md) | Click-by-click build sheet with blanks to record your field names and ids. |
| [`lab/`](lab/) | `fb_lab.sh` — test the webhook scripts against FlowBoost **without touching Marketo**. |
| [`test-kit/`](test-kit/) | `make_test_leads.py` to generate importable test leads, and [`DEMO.md`](test-kit/DEMO.md), a scripted end-to-end demo. |
| [`verify/`](verify/README.md) | Two read-only API scripts: `discover.py` (inventory) and `queue_report.py` (queue snapshot). |

---

## Step 0 — Prerequisites

### 0.1 · A FlowBoost account and its private API key

FlowBoost is a third-party Marketo webhook service at <https://flowboo.st/>. Its free Community plan allows 50,000 calls per
month, which is enough: the draw calls the webhook once per *queued* person per business day, so a 200-lead drop draining over
four days is about 500 calls, and a perpetually two-week-deep queue is roughly 10,000 per month. Check the site for current plans.

**What you need from the account: one string, the _private_ API key.**

| Credential | Needed? | Where it goes |
|---|---|---|
| **Private** API key | **Yes** | the `X-Api-Key` custom header on both Marketo webhooks, and `lab/.env` for testing |
| **Public** API key | No | browser/landing-page use only — this build has no landing-page component |
| FlowBoost account login | No | only to change plans or regenerate keys |

**Where to get the key:** sign up at <https://flowboo.st/>; the keys arrive in the signup email and are shown in your dashboard.
The private key looks like `com:` followed by 32 characters — **the `com:` prefix is part of the value** — and the public key
starts `pub:`. Treat the private key as a secret: Marketo webhook headers are visible only to admins, and `lab/.env` is
git-ignored. Decide who owns the account (your company vs. an agency) before you build, since the throttle stops working if the
account lapses.

- [ ] Account created, **private** key stored in `lab/.env`

### 0.2 · Marketo admin access

Creating fields, webhooks and campaigns needs a Marketo admin. The `verify/` scripts also need an API user — see
[`verify/README.md`](verify/README.md) for exactly how to create one. **They are optional and do not block the build.**

### 0.3 · Decide the scope in writing

When someone says "50 sends per day," do they mean **50 people entering the drip per day**, or **50 total emails per day across the
whole program including follow-ups?** This runbook throttles **entry into the drip**; once someone is in a multi-day nurture, their
later emails are additional sends on top of that day's N. If the business means 50 total emails, the design needs a second throttle
layer and the effort rises materially. Get the answer before building.

- [ ] Scope confirmed: **N new people entering the drip per day; follow-ups not counted**

### 0.4 · Confirm how your audience is identified

Decide exactly which field(s) and value(s) put someone in the queue (this document calls them your *audience criteria*), and record
their **REST names** — the display name in the UI is almost never the REST name. Run `python verify/discover.py` or check
**Admin → Field Management**. Note each field's type: a multi-select needs different matching than a string.
Record the answers in [`marketo/BUILD-CHECKLIST.md`](marketo/BUILD-CHECKLIST.md).

### 0.5 · Confirm your instance timezone

**Admin → Location → Default Timezone.** The draw script uses a fixed UTC offset for the date key. Set `OFFSET_HOURS` in
[`flowboost/fb-daily-draw.js`](flowboost/fb-daily-draw.js) to your zone's **standard-time** offset (`-8` Pacific, `-7` Mountain,
`-6` Central, `-5` Eastern, `0` UTC). The comment block in that file explains why a fixed offset beats `Intl`.

- [ ] `OFFSET_HOURS` set; `COUNTER_PREFIX` and `COUNTER_NAME` slug (`your-company`) changed to your own, **identical in both files**

---

## Step 1 — Create the fields and lists

Full detail and a place to record ids: [`marketo/BUILD-CHECKLIST.md`](marketo/BUILD-CHECKLIST.md).

**Fields** (Admin → Field Management → New Custom Field):

- [ ] `Throttle Queue Status` — String — `Queued` / `Sent` / `Suppressed`
- [ ] `Throttle Queue Date` — Datetime — when they entered the queue
- [ ] `Throttle Send Sequence` — Integer — global arrival order (FIFO + audit)
- [ ] `Throttle Daily Slot` — Integer — slot returned by the webhook, overwritten daily

**Static lists** (inside the program):

- [ ] `Throttle - Send Queue` — everyone waiting. *Its member count is your queue-depth dashboard.*
- [ ] `Throttle - Sent` — audit trail and duplicate guard

---

## Step 2 — Prove the script in the lab, before touching Marketo

The fastest loop available: no leads, no campaigns, no waiting on batch runs. Don't skip it — debugging FlowBoost through
Marketo's webhook log is painful by comparison.

```bash
cd lab && cp .env.example .env    # then paste your FlowBoost PRIVATE key into lab/.env
```

### 2.1 · One call

```bash
./lab/fb_lab.sh
```

Expect `{"response":1}`. Each lab run rewrites the counter namespace to an isolated `LAB/<timestamp>` path, so runs never pollute each
other or the production counter.

- [ ] Returns `{"response":1}`

### 2.2 · Prove the counter increments and the cap boundary is exact

```bash
./lab/fb_lab.sh -n 51
```

- [ ] Calls 1–51 return slots 1–51, strictly in order, no gaps or repeats
- [ ] Slot 50 and slot 51 are distinct — this is the boundary Marketo's choice step keys on

### 2.3 · Prove date-keying works

```bash
./lab/fb_lab.sh -n 3 -f ../flowboost/fb-assign-sequence.js   # the other webhook
```

- [ ] Returns 1, 2, 3 from its own independent counter

To prove the date boundary, temporarily change `OFFSET_HOURS` to a value that rolls the date (for example, add 24 to it), run again,
and confirm you get slot 1 from a fresh counter. **Revert it afterwards.**

- [ ] A different date key yields a fresh counter starting at 1

### 2.4 · Resolve the TTL question

`FBCounter.create(path, true, { ttl })` is documented by the vendor. `autoAdd`'s options argument is **not** explicitly documented, so
verify it here rather than in production.

If `autoAdd` rejects the options object, either drop the TTL (counters are tiny — prune occasionally) or fall back to:

```javascript
FBCounter.create(counterName, true, { ttl: COUNTER_TTL })
  .then(() => FBCounter.count(counterName))
  .then(success)
  .catch(failure);
```

⚠️ `create` + `count` is **not atomic** the way `autoAdd` is — two genuinely simultaneous calls can receive the same slot number, which
would let N+1 people send. Prefer `autoAdd`; if you must use the fallback, record the risk in your handover notes.

- [ ] `autoAdd` accepts `{ ttl }` (every call in 2.1–2.3 passed the options object and succeeded)

---

## Step 3 — Create the webhooks in Marketo

Follow [`flowboost/WEBHOOK-CONFIG.md`](flowboost/WEBHOOK-CONFIG.md) exactly. Needs Admin permission.

- [ ] `FB · Daily Draw` created, template = [`flowboost/fb-daily-draw.js`](flowboost/fb-daily-draw.js)
- [ ] `FB · Assign Sequence` created, template = [`flowboost/fb-assign-sequence.js`](flowboost/fb-assign-sequence.js)
- [ ] Both URLs end in `?authoringEnv=pro` — **without this, FBCounter does not exist**
- [ ] Both have the `Content-Type: application/javascript` and `X-Api-Key` custom headers
- [ ] Response mappings: `response` → `Throttle Daily Slot` / `Throttle Send Sequence`

> Every save re-selects the *first* webhook in the list. Re-select yours before setting headers or mappings, or you will configure the
> wrong webhook and not notice.

---

## Step 4 — `01-Enqueue` (trigger campaign)

**Smart list, triggers (ANY):**

- `Data Value Changes` → one trigger per audience field, New Value = the audience value
- `Person is Created`
- `Campaign is Requested` *(for an optional go-live backfill)*

**Filters — all must be true:**

- each audience field `is` its audience value
- `Throttle Queue Status` **is empty**  ← the real duplicate guard
- Not member of `Throttle - Sent`
- `Unsubscribed = false`, `Marketing Suspended = false`, `Email Invalid = false`, `Email Address is not empty`

> **Suppression has to happen here *and* again at draw time (step 6).** An unsubscribed person who reaches the draw consumes one of the
> N slots and sends nothing — silent under-delivery. Filter them out in both places.

**Flow:**

1. `Call Webhook` → `FB · Assign Sequence`
2. `Change Data Value` → `Throttle Queue Status` = `Queued`
3. `Change Data Value` → `Throttle Queue Date` = `{{system.dateTime}}`
4. `Add to List` → `Throttle - Send Queue`

**Schedule → Qualification rule: once per person.** ("Every time" would re-queue anyone whose flags get touched again.)

- [ ] Built, smart list previewed against real data, qualification rule set to **once**

---

## Step 5 — `03-Draw Handler` (trigger campaign)

Build this *before* step 6 — the batch campaign needs to point at it.

**Smart list:** `Campaign is Requested` — Source: **`Marketo Flow Action`** *(= requested by another campaign's Request Campaign step)*

**Flow:**

0. `Change Data Value` → `Throttle Daily Slot` = `NULL`, clearing yesterday's slot
1. `Call Webhook` → `FB · Daily Draw`
2. `Choice` → **Send Email**:
   - if `Throttle Daily Slot` **is between 1 and N** → `Send Email` (the drip entry email)
   - default → *do nothing*

> **Why clear the slot first:** if the webhook fails, Marketo keeps the previous value. Someone who drew slot 12 yesterday but whose email
> was blocked would be sent today outside the counter. Cleared first, a failed webhook leaves the slot empty, so the choice sends nothing.

> ⚠️ **Do not put bookkeeping in this flow.** A person blocked by communication limits skips `Send Email` but *"will still go through
> the other steps as normal"* ([Adobe KB](https://experienceleaguecommunities.adobe.com/adobe-marketo-engage-knowledge-base-28/lead-skips-the-send-email-flow-step-but-runs-through-the-other-flow-steps-245382)).
> A `Sent` / remove-from-queue step here would drop comm-limited people from the queue **unsent, silently**. Bookkeeping lives in a
> separate trigger:
>
> **`04-Mark Sent`** — trigger `Email is Delivered` → Email = the drip entry email; filter member of `Throttle - Send Queue`.
> Flow: `Throttle Queue Status` = `Sent` → `Remove from List` `Throttle - Send Queue` → `Add to List` `Throttle - Sent`.
> Qualification: once per person.
>
> A blocked person then stays `Queued` and redraws tomorrow; having consumed a slot today, that day sends N−1. Under the cap, never over.

> ### 🔴 Schedule → Qualification rule: **EVERY TIME**
>
> The single most important setting in the build. This campaign must run for the same person on multiple consecutive days. At "once",
> **everyone who loses a draw is frozen permanently** and never sends. Test 10 exists to catch this.

For a multi-step nurture, step 2 becomes `Add to Engagement Program` / `Change Program Status` rather than a bare `Send Email`; the
choice gating is the same.

- [ ] Built, **qualification rule = every time**, choice step gated on `1–N`
- [ ] `04-Mark Sent` built and activated, qualification **once**

---

## Step 6 — `02-Daily Draw` (batch campaign)

**Smart list:**

- Member of list `Throttle - Send Queue`
- `Throttle Queue Status = Queued`
- Suppression filters repeated (unsubscribed / suspended / invalid / blank email) — state changes while people wait
- `Was Sent Email` **NOT** = the drip entry email

> **`Was Sent Email` closes the delivery-lag gap.** Bookkeeping fires on *Email is Delivered*, so someone sent at 08:05 whose delivery
> confirmation hasn't landed by tomorrow's draw is still `Queued`. Without this filter they'd be redrawn and **emailed twice**. It's safe
> for comm-limited people: a blocked send logs no Send activity, so they stay eligible.

**Flow:** `Request Campaign` → `03-Draw Handler` (never `Call Webhook` — Marketo refuses in a batch campaign).

**Qualification:** **every time.** The batch default is *once*, which would permanently exclude anyone who loses a draw.

**Schedule:** Recurring, **Mon–Fri only**, 08:00 instance time. Nothing runs Sat/Sun, which is what makes the Friday→Monday rollover work.

- [ ] Built and scheduled M–F
- [ ] Holidays decided — a recurring campaign will happily run on a public holiday

### Optional — strict oldest-first ordering

Marketo doesn't guarantee batch processing order, so "the N that go today" isn't strictly oldest-first. **The cap is never violated
either way** — only fairness. If freshness matters, split into three staggered campaigns sharing the same counter:

| Campaign | Extra filter | Runs |
|---|---|---|
| `02a · Draw (aged 3d+)` | `Throttle Queue Date` before `3 days ago` | 08:00 |
| `02b · Draw (aged 1–3d)` | `Throttle Queue Date` before `1 day ago` | 08:15 |
| `02c · Draw (today's)` | *(none)* | 08:30 |

Older tiers consume the slots first; later tiers find the counter exhausted and hold. Same webhook, no script change.

---

## Step 7 — Test

**Use a sandbox, or a test segment on a fake domain.** For tests 4–10, swap the `Send Email` step for `Add to List` so you count list
membership instead of firing real mail — then switch it back for test 14. [`test-kit/DEMO.md`](test-kit/DEMO.md) walks through a
scripted version, and `python verify/queue_report.py --queue-list-id <id> --sent-list-id <id>` gives you the counts without clicking
through Marketo.

| # | Scenario | Expected |
|---|---|---|
| 1 | Lab: 3 calls to `FB · Daily Draw` | slots 1, 2, 3 |
| 2 | Lab: different date key | independent counter, starts at 1 |
| 3 | One test lead through `01-Enqueue` | `Queued`, on the queue list, `Throttle Send Sequence` populated |
| 4 | 10 leads, run the draw | all 10 send, slots 1–10, queue empties |
| 5 | **N−1 / N / N+1 leads** | N−1→N−1 send; N→N send; **N+1→N send, exactly 1 stays `Queued`** |
| 6 | **200 leads in one drop** (N = 50) | day 1: 50 send / 150 queued; days 2–4: 50/50/50; **200 sent, zero lost** |
| 7 | **Regression — Failure A**: 30 leads day 1, 40 more day 2 | day 1: 30 send. day 2: 40 send. **Nobody orphaned.** |
| 8 | **Regression — Failure B**: 0 leads day 1, 200 day 2 | day 2: exactly **N**, not 2N. **No banked capacity.** |
| 9 | **Fri → Mon rollover** | 100 queued Friday → 50 Fri, 0 Sat, 0 Sun, 50 Mon |
| 10 | **Redraw persistence** | a lead that loses Monday's draw wins a later day ← catches a "once" qualification rule on `03` |
| 11 | **Suppression** | unsubscribe a queued lead → excluded from the draw **and does not consume a slot** |
| 12 | **Duplicate guard** | re-flag an already-`Sent` lead → not re-queued, not re-sent |
| 13 | **Webhook failure** | break the API key → webhook 4xx, no send fires, leads stay `Queued`; restore the key → next day's draw recovers them |
| 14 | **Live smoke test** | 3 real leads end-to-end, real email, before handover |

**Tests 7, 8 and 10 are the point of the exercise** — they distinguish the daily-draw design from the naive one. Don't skip them.

To simulate consecutive days without waiting, run `02-Daily Draw` manually and temporarily shift `OFFSET_HOURS` in the webhook template
to move the date key. Revert it before go-live.

- [ ] Tests 1–13 pass in sandbox
- [ ] Test 14 passes in production

---

## Step 8 — Go live

- [ ] `OFFSET_HOURS` back to your real standard-time offset if you shifted it for multi-day testing
- [ ] Webhook templates pasted from `flowboost/*.js`, **never** from a lab-rewritten or `.TEST` copy
- [ ] `Send Email` steps restored in `03-Draw Handler` (not `Add to List`); any test-only list filters removed from `02`
- [ ] Real drip email approved and live
- [ ] `01-Enqueue`, `03-Draw Handler` and `04-Mark Sent` activated; `02-Daily Draw` scheduled M–F
- [ ] Queue-depth alert built *(recommended)* — a batch campaign on `Throttle Queue Date` older than N days → `Send Alert Email` to the program
      owner. This is how anyone notices if the draw silently stops.
- [ ] Watch the first three business days of real sends before calling it done

## Step 9 — Handover

- [ ] Walk the owners through the four campaigns and the two webhooks
- [ ] Tell them: **queue depth is visible any time** as the member count of `Throttle - Send Queue`
- [ ] Document that the cap changes in **one place** — the `1 and N` bounds in the `03 · Draw Handler` choice step. Marketo enforces the cap; the script
      only hands out slot numbers.
- [ ] Record the FlowBoost dependency and who owns the account and key

---

## What this project creates

This project builds things in **Marketo** (fields, lists, campaigns, webhooks) and does not itself generate reports, except:

| Where | What | Created by |
| --- | --- | --- |
| `verify/output/fields.json` | every lead field (only with `--all-fields`) | `verify/discover.py` |
| the path you pass to `--csv` | a snapshot of the queue | `verify/queue_report.py` |
| `test-kit/*.csv` | importable test leads | `test-kit/make_test_leads.py` |

All are git-ignored. API keys go in `lab/.env` (FlowBoost) and `verify/.env` (Marketo) — copy the matching `.env.example` in each folder.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Illegal return statement` / `FBCounter is not defined` | `authoringEnv=pro` missing from the webhook URL |
| Webhook 401/403 | wrong or missing `X-Api-Key`; check you used the **private** key, `com:` prefix included |
| Webhook 400 "Could not parse request body into json" | stray whitespace or an invisible character in a custom header name or value — see [`WEBHOOK-CONFIG.md`](flowboost/WEBHOOK-CONFIG.md) |
| `Cannot call webhook in batch campaign` | you put `Call Webhook` in `02` instead of `Request Campaign` → `03` |
| `Throttle Daily Slot` never populates | response mapping missing, or the webhook is returning non-2xx (mappings only apply on 2xx) |
| Everyone sends, cap ignored | choice step in `03` not gated on `1 and N`, or the email sits in the Default Choice |
| Leads queue up and never send | `03-Draw Handler` qualification rule is "once" instead of **every time** |
| Fewer than N send despite a deep queue | suppressed people are consuming slots — add the suppression filters to `02`'s smart list |

---

## License and disclaimer

This project is released under the [MIT License](LICENSE) and comes with **no warranty and no support**. It is an independent project, not affiliated with or endorsed by Adobe; Adobe and Marketo are trademarks of Adobe Inc.
