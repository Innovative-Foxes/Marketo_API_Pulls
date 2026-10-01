/*
 * Daily send throttle
 * FlowBoost webhook: "FB - Daily Draw"
 *
 * WHAT IT DOES
 *   Returns this person's slot number for TODAY. Marketo sends only slots
 *   1..N (your daily cap); everyone else stays in the queue and redraws
 *   tomorrow.
 *
 * HOW IT IS CALLED
 *   [02 Daily Draw batch, M-F]
 *        --Request Campaign-->  [03 Draw Handler trigger]
 *                                    --Call Webhook-->  here
 *   (Marketo cannot call a webhook from a batch campaign, hence the bridge.)
 *
 * WHY A DATE-KEYED COUNTER
 *   The counter name contains today's date, so a brand new counter starting at
 *   zero exists each morning. That gives us three properties for free:
 *     - unused capacity never banks up (a quiet Monday cannot make Tuesday
 *       send twice the cap)
 *     - nobody can be stranded in an already-released bucket, because the
 *       whole queue re-competes every business day
 *     - Fri -> Mon rollover needs no special handling: no batch runs on
 *       Sat/Sun, so no counter is ever created for those dates
 *
 * DO NOT replace this with a permanent global counter + fixed bucket number.
 * See README.md, "Why a daily draw", for the two ways that silently loses leads.
 *
 * WEBHOOK SETTINGS: see flowboost/WEBHOOK-CONFIG.md
 * Response mapping: response -> Throttle Daily Slot (Integer)
 */

/* Counter namespace. CHANGE "your-company" to a short slug unique to you: FlowBoost
 * counters are global to your API key, so a distinctive path keeps these counts
 * separate from any other counter you create. The lab harness rewrites this line
 * to an isolated test namespace, so keep it on one line and keep the exact
 * variable name. */
const COUNTER_PREFIX = "/your-company/daily-send-throttle/daily";

/* 14 days, so yesterday's date counters clean themselves up. */
const COUNTER_TTL = 60 * 60 * 24 * 14;

/* SET THIS to your Marketo instance's STANDARD-time UTC offset in hours
 * (Admin > Location > Default Timezone shows your zone). For example:
 *   -8 Pacific   -7 Mountain   -6 Central   -5 Eastern   0 UTC   +1 Central Europe
 *
 * We deliberately do arithmetic on a fixed offset rather than using
 * Intl.DateTimeFormat({ timeZone: ... }). Some Node builds ship without full
 * ICU timezone data, and on those a named time zone throws a RangeError - which
 * would surface as an opaque webhook failure.
 *
 * Using the STANDARD-time offset (not the daylight-saving one) puts the day
 * boundary at 00:00 standard time / 01:00 daylight time, so the key never runs
 * ahead of the local calendar date. Run the batch campaign well clear of
 * midnight (e.g. 08:00) and the date is unambiguous in both halves of the
 * year, so daylight saving is a non-issue. */
const OFFSET_HOURS = -8;

const drawDate = new Date(Date.now() + OFFSET_HOURS * 60 * 60 * 1000)
  .toISOString()
  .slice(0, 10); /* "YYYY-MM-DD" */

const counterName = `${COUNTER_PREFIX}/${drawDate}`;

FBCounter.autoAdd(counterName, { ttl: COUNTER_TTL })
  .then(entry => success(entry.entryIndex + 1)) /* entryIndex is 0-based; slot 1 reads better */
  .catch(failure);
