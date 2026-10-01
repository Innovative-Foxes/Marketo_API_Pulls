# Marketo build checklist — daily send throttle

Companion to [`../README.md`](../README.md). This is the click-by-click sheet, plus somewhere to
record the ids and names you discover along the way. **Fill in the blanks as you go** — the ids are
needed by `verify/queue_report.py` and by anyone picking this up later.

Throughout, `[N]` means your **daily cap** (the example uses 50), and *audience criteria* means the
fields/values that decide who is allowed into the queue (for example `Lead Segment is Priority`).

---

## Discovered facts — fill these in first (README step 0)

| Thing | Value |
|---|---|
| Instance REST host | *(in `verify/.env` as `MARKETO_REST_BASE`)* |
| Instance timezone (Admin → Location) | `______________` → set `OFFSET_HOURS` to its **standard-time** UTC offset |
| Audience field 1 | field `______________` is `______________` · type `________` |
| Audience field 2 *(if any)* | field `______________` is `______________` · type `________` |
| How the audience flags arrive | `______________` *(sync from CRM, form, import, manual…)* |
| Program this lives in | `________________` |
| Drip entry email (or engagement program) | `________________` |
| Cap scope | **[N] new people entering the drip per day.** Follow-ups not counted *(confirm this with the business owner)* |

---

## 1 · Custom fields

**Admin → Field Management → New Custom Field.** Note the REST name Marketo generates for each —
it is usually the display name camel-cased, but confirm rather than assume.

| Display name | Type | Purpose | REST name (record it) |
|---|---|---|---|
| `Throttle Queue Status` | String | `Queued` / `Sent` / `Suppressed` | `________________` |
| `Throttle Queue Date` | Datetime | when they entered the queue | `________________` |
| `Throttle Send Sequence` | Integer | global arrival order (FIFO + audit) | `________________` |
| `Throttle Daily Slot` | Integer | slot from the webhook, overwritten daily | `________________` |

- [ ] All created
- [ ] REST names recorded above

---

## 2 · Static lists

Create inside the program so they travel with it.

| List | Purpose | Id (record it) |
|---|---|---|
| `Throttle - Send Queue` | everyone waiting. **Member count = your queue-depth dashboard.** | `________` |
| `Throttle - Sent` | audit trail + duplicate guard | `________` |

- [ ] Both created, ids recorded

---

## 3 · Webhooks

See [`../flowboost/WEBHOOK-CONFIG.md`](../flowboost/WEBHOOK-CONFIG.md) for the exact settings.

- [ ] `FB · Daily Draw` — mapping `response` → `Throttle Daily Slot`
- [ ] `FB · Assign Sequence` — mapping `response` → `Throttle Send Sequence`
- [ ] Both URLs contain `?authoringEnv=pro`
- [ ] Both have `Content-Type: application/javascript` + `X-Api-Key` headers

---

## 4 · `01-Enqueue` — trigger campaign

### Smart list

**Triggers** — set the trigger group to **ANY**:

- [ ] `Data Value Changes` → one trigger per audience field, New Value = the audience value
- [ ] `Person is Created` *(catches records that arrive already flagged)*
- [ ] `Campaign is Requested` *(lets a one-off backfill batch reuse this flow at go-live)*

**Filters — set to ALL:**

- [ ] each audience field `is` its audience value
- [ ] `Throttle Queue Status` **is empty**
- [ ] `Member of List` `Throttle - Sent` — **not** member
- [ ] `Unsubscribed` = `false`
- [ ] `Marketing Suspended` = `false`
- [ ] `Email Invalid` = `false`
- [ ] `Email Address` **is not empty**

### Flow

1. [ ] `Call Webhook` → `FB · Assign Sequence`
2. [ ] `Change Data Value` → `Throttle Queue Status` = `Queued`
3. [ ] `Change Data Value` → `Throttle Queue Date` = `{{system.dateTime}}`
4. [ ] `Add to List` → `Throttle - Send Queue`

### Schedule

- [ ] Qualification rule: **once per person**
- [ ] Activated

---

## 5 · `03-Draw Handler` — trigger campaign

Build before `02`, which has to point at it.

### Smart list

- [ ] `Campaign is Requested` — Source: **`Marketo Flow Action`** *(= requested by another campaign's Request Campaign step)*

### Flow

0. [ ] `Change Data Value` → `Throttle Daily Slot` = `NULL`, clearing yesterday's slot
1. [ ] `Call Webhook` → `FB · Daily Draw`
2. [ ] `Send Email` with a **choice**:
       - **Choice 1:** if `Throttle Daily Slot` `is between` `1` and `[N]` → the drip entry email
       - **Default Choice:** `-- Do Nothing --`

   > 🔴 **Add Choice moves the step's existing value into the Default Choice.** Pick the email
   > *inside Choice 1*, and make sure the default says Do Nothing. Inverted, it skips the winners
   > and sends to everyone else, with no cap.

**Nothing else in this flow.** No other `Change Data Value`, no list steps.

> **Why clear the slot first:** if the webhook fails, Marketo keeps the previous value. Someone
> who drew slot 12 yesterday but whose email was blocked would be sent today outside the counter.
> Cleared first, a failed webhook leaves the slot empty, so the choice step sends nothing.

> 🔴 **Don't add bookkeeping here.** A person blocked by communication limits skips `Send Email` but
> *"will still go through the other steps as normal"* (Adobe KB). Any Sent/remove-from-queue step in
> this flow drops them from the queue **unsent, silently**. Bookkeeping lives in `04-Mark Sent`.

> **The cap lives here, in one place:** the `1 and [N]` bounds on the Send Email choice. To change
> the daily cap, edit that single choice, not the script. Marketo enforces the cap.

> For a multi-step nurture, replace step 2 with `Add to Engagement Program` / `Change Program Status`,
> same choice gating.

### Schedule

- [ ] 🔴 Qualification rule: **EVERY TIME** — see README step 5. Get this wrong and
      every lead that loses a draw is frozen forever.
- [ ] Activated

---

## 6 · `02-Daily Draw` — batch campaign

### Smart list — ALL

- [ ] `Member of List` = `Throttle - Send Queue`
- [ ] `Throttle Queue Status` = `Queued`
- [ ] `Unsubscribed` = `false`
- [ ] `Marketing Suspended` = `false`
- [ ] `Email Invalid` = `false`
- [ ] `Email Address` **is not empty**
- [ ] `Was Sent Email` **NOT** = the drip entry email

> The suppression filters are repeated from `01` on purpose. Someone can unsubscribe *while queued*,
> and if they reach the draw they consume one of the slots and send nothing.
>
> **`Was Sent Email` closes the delivery-lag gap.** Bookkeeping fires on *Email is Delivered*,
> so someone sent at 08:05 whose delivery confirmation hasn't landed by tomorrow's draw is still
> `Queued`. Without this filter they'd be redrawn and **emailed twice**. It's safe for
> comm-limited people: a blocked send logs no Send activity, so they stay eligible.

### Flow

1. [ ] `Request Campaign` → `03-Draw Handler`

> Not `Call Webhook`. Marketo will refuse: *"Cannot call webhook in batch campaign."*

### Schedule

- [ ] Qualification rule: **every time** *(the batch default is "once", which would permanently exclude anyone who loses a draw)*
- [ ] Recurring — **Mon, Tue, Wed, Thu, Fri** only
- [ ] 08:00 instance time *(any time works; keep it well clear of midnight so the date key is unambiguous)*
- [ ] Approved / scheduled

---

## 6b · `04-Mark Sent` — trigger campaign

Bookkeeping, only for people whose email was actually delivered.

### Smart list

- [ ] Trigger: `Email is Delivered` → Email = the drip entry email
- [ ] Filter: `Member of List` = `Throttle - Send Queue` *(ignores people who get the same email from another campaign)*

*(There is no "Email is Sent" trigger in Marketo. Delivered is the closest confirmation.)*

### Flow

1. [ ] `Change Data Value` → `Throttle Queue Status` = `Sent`
2. [ ] `Remove from List` → `Throttle - Send Queue`
3. [ ] `Add to List` → `Throttle - Sent`

### Schedule

- [ ] Qualification rule: **once per person**
- [ ] Activated

A comm-limited or undelivered person never fires this trigger, stays `Queued`, and redraws the next
business day. That day's draw sends [N]−1 instead of [N]: under the cap, never over.

---

## 7 · Optional — tiered oldest-first draw

Only if lead freshness matters. Same webhook, three campaigns sharing the counter.

| Campaign | Extra smart-list filter | Time | Built |
|---|---|---|---|
| `02a · Draw (aged 3d+)` | `Throttle Queue Date` `before` `3 days ago` | 08:00 | [ ] |
| `02b · Draw (aged 1–3d)` | `Throttle Queue Date` `before` `1 day ago` | 08:15 | [ ] |
| `02c · Draw (today's)` | *(none)* | 08:30 | [ ] |

---

## 8 · Optional — queue-depth alert

Recommended. This is how anyone finds out if the draw silently stops running.

- [ ] Batch campaign, daily: smart list `Member of Throttle - Send Queue` + `Throttle Queue Date` before
      `N days ago` → flow `Send Alert Email` to the program owner
- [ ] Agree N with the business owner (7 days is a reasonable default)

---

## Final sign-off

- [ ] All fields and lists created, ids recorded in this file
- [ ] Both webhooks created and mapped
- [ ] All four campaigns built (`01`, `02`, `03`, `04`)
- [ ] `01` qualification = **once**
- [ ] `03` qualification = **every time** 🔴
- [ ] `03` flow = webhook + Send Email choice **only**, no bookkeeping
- [ ] `04` qualification = **once**
- [ ] `02` schedule = **M–F**
- [ ] Tests 1–13 pass (README step 7)
- [ ] Test 14 (live smoke test) passes
- [ ] Handover walkthrough done
