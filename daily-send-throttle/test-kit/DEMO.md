# End-to-end demo: the daily throttle in test mode

Shows, with no real email sent: leads queue when flagged, **exactly N are drawn per "day"** (the examples use
N = 50), the overflow waits and goes first the next day, and nothing is lost.

Run it in a `TEST` folder inside the program, using the test-mode webhook script so test draws can never
use real production slots.

## What "goes through" means in test mode

`03-Draw Handler` adds drawn leads to a list **`Throttle - TEST Drawn`** *instead of* sending email.
`02-Daily Draw` excludes anyone already on that list (a test-only filter), so a drawn lead is not drawn again.
At go-live that filter is removed and `04-Mark Sent` does the job for real.

## Before you present (10 min)

1. Confirm active: `01-Enqueue`, `03-Draw Handler`. Inactive: `02-Daily Draw` (you run it manually),
   `04-Mark Sent`.
2. Paste [`../flowboost/fb-daily-draw.TEST.js`](../flowboost/fb-daily-draw.TEST.js) into the `FB · Daily Draw`
   webhook. Check its `OFFSET_HOURS` is your instance's offset and the prefix contains `/TEST/`.
3. Clear old test data: `Throttle - Send Queue`, `Throttle - Sent`, `Throttle - TEST Drawn` all at **0** members.
4. Import **one** lead first and confirm it becomes `Queued` with `Throttle Send Sequence` filled. That proves
   `01-Enqueue` and the `FB · Assign Sequence` webhook.
5. Check that lead is in `Marketable Persons`, or `02` will never pick it up.
6. Open in tabs: the three lists (member counts), `02-Daily Draw` → Schedule (**Run Once**), and
   Admin → Webhooks → `FB · Daily Draw`.

## Generate the test files

```bash
python3 make_test_leads.py --mailbox you --domain example.com --count 1  --start 901 \
    --audience "Your Field=Your Value" --out demo_single.csv
python3 make_test_leads.py --mailbox you --domain example.com --count 50 --start 1   \
    --audience "Your Field=Your Value" --out demo1_50_leads.csv
python3 make_test_leads.py --mailbox you --domain example.com --count 20 --start 101 \
    --audience "Your Field=Your Value" --out demo2_20_leads.csv
```

Use `--mailbox`/`--domain` for an inbox you control, and one `--audience Field=Value` per field in your queue's
audience criteria. Never reuse an address: pick fresh `--start` numbers for every batch. (The files are
git-ignored — they contain addresses.)

## Scene 1: the cap (N+1 in, N out)

1. Import `demo1_50_leads.csv` (Database → Import List, dedupe on email).
2. After a minute or two: `Throttle - Send Queue` = **51** (50 + the check lead), all `Queued`, sequence numbers 1..51.
3. `02-Daily Draw` → Schedule → **Run Once**.
4. Result: `Throttle - TEST Drawn` = **50**. Exactly one lead is still `Queued`, with `Throttle Daily Slot` = **51**.
   Say: *"The 51st wasn't lost. It just didn't get a slot today."*

## Scene 2: next day, carry-forward

1. Import `demo2_20_leads.csv`. The queue is now 21 (the leftover + 20 new).
2. Simulate tomorrow: `FB · Daily Draw` → Edit → set `OFFSET_HOURS` to your offset **+24** (for `-8`, use `16`), save.
   (Safe: this is the test-namespace script.)
3. **Run Once** `02-Daily Draw` again.
4. Result: `Throttle - TEST Drawn` = **71**; nothing left undrawn. The counter restarted for the new day.
   Say: *"Unused capacity doesn't bank, and the leftover went first."*
5. **Set `OFFSET_HOURS` back to your real offset.**

## Scene 3: it's guarded (pick any)

- Unsubscribe a queued lead before **Run Once**: it is skipped and uses no slot.
- Change a queued lead's audience field to something else: if you built the optional dequeue campaign, it is removed.
- Add a lead **without** the `+throttletest` marker: ignored by `01-Enqueue` if you added the test-mode safety filter.

## Where the evidence is

- List member counts: `Throttle - Send Queue`, `Throttle - TEST Drawn`.
- Person → Activity Log: Data Value Changes (`Throttle Queue Status`, `Throttle Daily Slot`), Call Webhook, Added to List.
- Campaign → `02-Daily Draw` → Results / Run History.
- Or from the command line: `python verify/queue_report.py --queue-list-id <id>` (see the main README).

## Optional finale: real email

Switch `03`'s Choice 1 from *Add to List* to *Send Email* = your drip entry email, activate `04-Mark Sent`, and
draw 3 test leads. The emails arrive in your inbox; `04` marks them Sent. Check Choice 1 (slot 1–N) holds the
email and the default is *Do Nothing*.

## Reset after the demo

Remove the test leads from `Throttle - Send Queue` / `Throttle - Sent` / `Throttle - TEST Drawn` and clear
`Throttle Queue Status`, or just use fresh `--start` numbers next time. **Before go-live:** paste the
production `fb-daily-draw.js` back into the webhook, remove the `Not member of Throttle - TEST Drawn` filter
from `02`, switch `03` back to *Send Email*, and delete any test-only safety filters.
