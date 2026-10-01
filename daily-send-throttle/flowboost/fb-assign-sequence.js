/*
 * Daily send throttle
 * FlowBoost webhook: "FB - Assign Sequence"
 *
 * WHAT IT DOES
 *   Stamps a permanent, ever-increasing arrival number on each person as they
 *   enter the queue. Purely for FIFO ordering and auditing - it is NOT what
 *   enforces the daily cap (fb-daily-draw.js does that).
 *
 * HOW IT IS CALLED
 *   [01 Enqueue trigger] --Call Webhook--> here, once per person
 *
 * Bump the trailing version segment ONLY to deliberately restart numbering
 * from 1. Changing it mid-flight makes the FIFO ordering meaningless.
 *
 * WEBHOOK SETTINGS: see flowboost/WEBHOOK-CONFIG.md
 * Response mapping: response -> Throttle Send Sequence (Integer)
 */

/* CHANGE "your-company" to the same slug you used in fb-daily-draw.js. */
const COUNTER_NAME =
  "/your-company/daily-send-throttle/arrival-sequence/v1";

FBCounter.autoAdd(COUNTER_NAME)
  .then(entry => success(entry.entryIndex + 1))
  .catch(failure);
