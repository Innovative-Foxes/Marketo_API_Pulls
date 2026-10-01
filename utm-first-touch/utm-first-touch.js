/**
 * UTM first-touch / last-touch capture
 * ---------------------------------------------------------------------------
 * Stores the visitor's first-touch UTM values (written once) and last-touch UTM
 * values (the most recent tagged visit) in cookies, and copies both into the
 * hidden fields of Marketo forms. The cookie names and form field names match
 * the widely used "ppcUrlCookie" snippet, so this can replace it without
 * reconfiguring your forms.
 *
 * Design notes:
 *
 * 1. Never writes the literal string "null".
 *    A parameter missing from the URL is left empty, not stamped with a
 *    placeholder. Snippets that write "null" poison the first-touch record: the
 *    guard treats any non-empty value as "already recorded", so a cookie holding
 *    "null" is considered filled and can never be corrected.
 *
 * 2. Existing "null" values self-heal.
 *    Any cookie already holding "null" is treated as empty, so visitors carrying
 *    a poisoned cookie are repaired on their next tagged visit instead of
 *    staying wrong until the cookie expires.
 *
 * 3. Last touch is a snapshot, not a merge.
 *    A visit carrying any tracking parameter replaces the whole last-touch set;
 *    a visit carrying none leaves it alone. (Falling back to the old cookie for
 *    each missing parameter would blend every visit together.)
 *
 * 4. Query-string matching is case-insensitive.
 *    utm_CampaignID and utm_campaignid are both captured.
 *
 * 5. Form field matching is case-insensitive, and fills every match.
 *    Some builds name a field fT_utm_* and others ft_utm_*, and
 *    getElementsByName is case-sensitive, so one spelling would silently fill
 *    nothing. Lookup ignores case and populates all matching inputs, which also
 *    fixes pages that carry more than one form.
 *
 * 6. No global leak.
 *
 * Options (set BEFORE this script loads; all optional):
 *
 *   <script>
 *     window.utmFirstTouchOptions = {
 *       cookieDomains: ["example.com"], // share cookies across subdomains/sites
 *       cookieDays: 365,                // cookie lifetime
 *       useLatestValues: true           // false = freeze last-touch after first write
 *     };
 *   </script>
 *
 * cookieDomains: when the current hostname contains one of these, the cookie is
 * scoped to it (so www.example.com and go.example.com share cookies). If none
 * match, the cookie is scoped to the current hostname.
 */
(function () {
  "use strict";

  var OPTS = (typeof window !== "undefined" && window.utmFirstTouchOptions) || {};

  // false = freeze last-touch values after the first time they are recorded.
  var USE_LATEST_VALUES = OPTS.useLatestValues !== false;

  var COOKIE_DAYS = OPTS.cookieDays || 365;

  /**
   * Records that a first-touch set has been captured, holding the timestamp of
   * that visit. This is what makes "this parameter was absent on the
   * first-touch visit" distinguishable from "no first touch was ever
   * recorded" — the two are otherwise identical, since both leave the cookie
   * empty. Storing an explicit marker keeps the absent parameters genuinely
   * empty rather than stamping a placeholder string into them.
   */
  var FT_MARKER = "ftCaptured";

  // Cookie name : query-string parameter
  var LAST_TOUCH = [
    ["utmCampaignId", "utm_campaignID"],
    ["utmSource", "utm_source"],
    ["utmMedium", "utm_medium"],
    ["utmCampaign", "utm_campaign"],
    ["utmKeyword", "utm_term"],
    ["utmContent", "utm_content"],
  ];

  var FIRST_TOUCH = [
    ["ftUtmCampaignId", "utm_campaignID"],
    ["ftUtmSource", "utm_source"],
    ["ftUtmMedium", "utm_medium"],
    ["ftUtmCampaign", "utm_campaign"],
    ["ftUtmKeyword", "utm_term"],
    ["ftUtmContent", "utm_content"],
  ];

  // Cookie name : form field name
  var FORM_FIELDS = [
    ["utmCampaignId", "utm_campaignID"],
    ["utmSource", "utm_source__c"],
    ["utmMedium", "utm_medium__c"],
    ["utmCampaign", "utm_campaign__c"],
    ["utmKeyword", "utm_term__c"],
    ["utmContent", "utm_content__c"],
    ["ftUtmCampaignId", "ft_utm_campaignID"],
    ["ftUtmSource", "ft_utm_source"],
    ["ftUtmMedium", "ft_utm_medium"],
    ["ftUtmCampaign", "ft_utm_campaign"],
    ["ftUtmKeyword", "ft_utm_term"],
    ["ftUtmContent", "ft_utm_content"],
    // Requires a matching field in Marketo / Pardot. Until that field exists
    // this line is a harmless no-op, since unmatched names are skipped. Once
    // it does exist, "ft_captured_date is set AND ft_utm_term is empty" reads
    // unambiguously as "first touch was captured and carried no term".
    [FT_MARKER, "ft_captured_date"],
  ];

  // Parent domains to scope cookies to, from options. Empty = current hostname.
  var COOKIE_DOMAINS = OPTS.cookieDomains || [];

  /**
   * A value counts as absent if it is unset, blank, or one of the sentinel
   * strings previous versions of this script wrote into cookies. Treating
   * "null" as absent is what allows poisoned records to repair themselves.
   */
  function isBlank(value) {
    return (
      value === null ||
      value === undefined ||
      value === "" ||
      value === "undefined" ||
      value === "null"
    );
  }

  function decode(value) {
    try {
      return decodeURIComponent(value);
    } catch (e) {
      // Values written by the previous script used escape(), which is not
      // always valid percent-encoding.
      try {
        return unescape(value);
      } catch (e2) {
        return value;
      }
    }
  }

  function getCookie(name) {
    var parts = document.cookie.split(";");
    for (var i = 0; i < parts.length; i++) {
      var pair = parts[i];
      var eq = pair.indexOf("=");
      if (eq === -1) continue;
      if (pair.slice(0, eq).replace(/^\s+|\s+$/g, "") === name) {
        return decode(pair.slice(eq + 1));
      }
    }
    return null;
  }

  function cookieDomain() {
    var host = window.location.hostname.toLowerCase();
    for (var i = 0; i < COOKIE_DOMAINS.length; i++) {
      if (host.indexOf(COOKIE_DOMAINS[i]) !== -1) return COOKIE_DOMAINS[i];
    }
    return host;
  }

  function setCookie(name, value, days) {
    var expires = new Date();
    expires.setDate(expires.getDate() + days);
    document.cookie =
      name +
      "=" +
      encodeURIComponent(value) +
      "; domain=" +
      cookieDomain() +
      "; path=/; expires=" +
      expires.toUTCString();
  }

  function clearCookie(name) {
    document.cookie =
      name +
      "=; domain=" +
      cookieDomain() +
      "; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT";
  }

  /** Reads a query-string parameter, ignoring case on the parameter name. */
  function getParam(wanted) {
    var query = window.location.search.substring(1);
    if (!query) return null;
    var target = wanted.toLowerCase();
    var pairs = query.split("&");
    for (var i = 0; i < pairs.length; i++) {
      if (!pairs[i]) continue;
      var eq = pairs[i].indexOf("=");
      var name = eq === -1 ? pairs[i] : pairs[i].slice(0, eq);
      if (name.toLowerCase() !== target) continue;
      var raw = eq === -1 ? "" : pairs[i].slice(eq + 1);
      // Strip a trailing fragment if this was the last parameter.
      return decode(raw.split("#")[0]);
    }
    return null;
  }

  /** True if this URL carries at least one of the tracked parameters. */
  function urlHasTracking(pairs) {
    for (var i = 0; i < pairs.length; i++) {
      if (!isBlank(getParam(pairs[i][1]))) return true;
    }
    return false;
  }

  /** True if any cookie in the set holds a real value. */
  function anyRecorded(pairs) {
    for (var i = 0; i < pairs.length; i++) {
      if (!isBlank(getCookie(pairs[i][0]))) return true;
    }
    return false;
  }

  /**
   * True once a first-touch set has been captured for this visitor: either the
   * marker cookie exists, or a first-touch cookie already holds a real value
   * (a visitor recorded before the marker was introduced). Blank and "null"
   * values never count, which is what lets poisoned records be rewritten.
   */
  function firstTouchRecorded() {
    return !isBlank(getCookie(FT_MARKER)) || anyRecorded(FIRST_TOUCH);
  }

  /**
   * Writes a whole set from the current URL. Parameters absent from the URL
   * are cleared rather than being backfilled from the previous visit or
   * stamped with "null", so the stored set is a faithful snapshot.
   */
  function writeSet(pairs) {
    for (var i = 0; i < pairs.length; i++) {
      var value = getParam(pairs[i][1]);
      if (isBlank(value)) {
        clearCookie(pairs[i][0]);
      } else {
        setCookie(pairs[i][0], value, COOKIE_DAYS);
      }
    }
  }

  function capture() {
    if (!urlHasTracking(LAST_TOUCH)) return; // Untagged pageview, nothing to record.

    // First touch is written once, as one atomic set, on the first tagged
    // visit. It is never touched again, so a parameter that visit did not
    // carry stays empty rather than being backfilled from a later visit with
    // different campaign values. The stored set always reflects one visit.
    if (!firstTouchRecorded()) {
      writeSet(FIRST_TOUCH);
      setCookie(FT_MARKER, new Date().toISOString(), COOKIE_DAYS);
    }

    // With USE_LATEST_VALUES off, last touch behaves like first touch and is
    // only written while it has never held a real value.
    if (USE_LATEST_VALUES || !anyRecorded(LAST_TOUCH)) writeSet(LAST_TOUCH);
  }

  /** Populates every field whose name matches, ignoring case. */
  function fillField(fieldName, value) {
    var target = fieldName.toLowerCase();
    var inputs = document.getElementsByTagName("input");
    var filled = 0;
    for (var i = 0; i < inputs.length; i++) {
      var name = inputs[i].getAttribute("name");
      if (name && name.toLowerCase() === target) {
        inputs[i].value = value;
        filled++;
      }
    }
    return filled;
  }

  function populateForms() {
    for (var i = 0; i < FORM_FIELDS.length; i++) {
      var value = getCookie(FORM_FIELDS[i][0]);
      if (isBlank(value)) continue; // Never push "null" or blanks into a form.
      try {
        fillField(FORM_FIELDS[i][1], value);
      } catch (e) {
        // A single unmatched field should not stop the rest.
      }
    }
  }

  capture();

  // Populate immediately for forms already in the DOM, and again once Marketo
  // has rendered, since Marketo forms are injected after this script runs.
  populateForms();

  if (typeof MktoForms2 !== "undefined" && MktoForms2.whenReady) {
    MktoForms2.whenReady(function () {
      populateForms();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", populateForms);
  }

  // Exposed for debugging and for manual re-population after a form renders.
  window.utmTracking = {
    capture: capture,
    populateForms: populateForms,
    read: function () {
      var out = {};
      var all = LAST_TOUCH.concat(FIRST_TOUCH);
      for (var i = 0; i < all.length; i++) out[all[i][0]] = getCookie(all[i][0]);
      return out;
    },
  };
})();
