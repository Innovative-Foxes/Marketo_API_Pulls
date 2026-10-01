# FlowBoost webhook configuration (Marketo Admin)

Create both webhooks under **Admin → Integration → Webhooks → New Webhook**.
Admin-level permission is required — a standard marketing user cannot create webhooks.

Before you start, set the two things in the scripts that are yours to choose (see the comments at the top of each file):

- `COUNTER_PREFIX` / `COUNTER_NAME` — replace `your-company` with a short slug of your own, **the same in both files**.
- `OFFSET_HOURS` in `fb-daily-draw.js` — your Marketo instance's standard-time UTC offset (Admin → Location → Default Timezone).

## Shared settings (identical for both)

| Setting | Value |
|---|---|
| URL | `https://api.teknkl.com/flowboost$current/run?authoringEnv=pro` |
| Request Type | `POST` |
| Request Token Encoding | `JSON` (per the vendor setup email) |
| Response Type | `JSON` |

**Custom headers** (Webhook Actions → Set Custom Header):

| Header | Value |
|---|---|
| `Content-Type` | `application/javascript` |
| `X-Api-Key` | *FlowBoost **private** key, the whole string **including** its `com:` prefix* (also kept in `lab/.env` for testing) |

> `authoringEnv=pro` is **required**. FBCounter's promise API does not exist in the
> Standard authoring environment; without it you get a compile error, not a useful message.
>
> **Don't strip the `com:`.** FlowBoost issues the private key as `com:` + 32 characters, and it
> looks like a label but is part of the value. Stripping it produces an auth error that is easy to
> misread as "wrong key".
>
> Use the **private** key here. The public key (`pub:…`) exists for landing-page/browser use —
> never put the private key anywhere a browser can see it.

## Webhook 1 — `FB · Daily Draw`

| Setting | Value |
|---|---|
| Name | `FB · Daily Draw` |
| Template | paste the full contents of [`fb-daily-draw.js`](fb-daily-draw.js) |
| Description | `Daily send throttle. Returns this person's slot number for today. Called from "03 · Draw Handler" only.` |

**Response mapping:** `response` → `Throttle Daily Slot` (Integer)

## Webhook 2 — `FB · Assign Sequence`

| Setting | Value |
|---|---|
| Name | `FB · Assign Sequence` |
| Template | paste the full contents of [`fb-assign-sequence.js`](fb-assign-sequence.js) |
| Description | `Daily send throttle: queue arrival order. Audit/FIFO only - does not enforce the daily cap. Called from "01 - Enqueue" only.` |

**Response mapping:** `response` → `Throttle Send Sequence` (Integer)

## Gotchas

- **No whitespace in custom header values.** A stray leading/trailing space in the
  `Content-Type` or `X-Api-Key` value makes FlowBoost return **HTTP 400** "Could not parse request
  body into json" (it no longer sees `application/javascript`, so it tries to parse the script as
  JSON). Re-type the values; don't paste them with padding. The script itself (comments included)
  is not the problem.
- **Saving reshuffles the list.** Every time you save a webhook, Marketo refreshes and
  re-selects the *first* webhook in the list. Re-select yours before setting headers or
  mappings, or you will configure the wrong one.
- **Response mappings only apply on a 2xx.** A bad API key or a script error leaves the
  mapped field untouched. That is the behaviour you want (a failed call must not look like a slot),
  but it also means a silently wrong key looks like "nothing happened".
- **Neither script uses Marketo tokens.** That is deliberate: no `{{lead.*}}` token
  resolution to debug, and the payloads are testable verbatim via `lab/fb_lab.sh`.
- **The 30-second webhook timeout** is per call. These calls are sub-second.
- **Type header names by hand; never paste them.** A leading invisible character (tab or
  odd space, e.g. from a pasted email) in the header *name* is hidden in the grid view.
  Marketo then doesn't recognise your `Content-Type`, adds its own default after it, and
  FlowBoost JSON-parses the script: `400 Could not parse request body into json`. Auth still
  passes, so it looks like a body problem. To check: open Set Custom Header, double-click the
  name, press Cmd+Left then Shift+Right; the selection must cover the `C`/`X`, not a blank.
