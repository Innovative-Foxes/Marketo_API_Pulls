/**
 * UTM Forwarding
 * ------------------------------------------------------------------
 * Persists inbound UTM (and related) query params in localStorage, then
 * re-appends them to the URL on later pageviews that arrive without them.
 * As long as the Marketo forms rendered have the UTM hidden fields set to
 * auto-fill from URL params, there is no additional code required for this
 * functionality to work.
 *
 * Behavior:
 *   - On a page whose URL carries tracking params: store them (last-touch).
 *   - On a page whose URL has none: restore the stored params onto the URL.
 *   - Storage set to a 30 day expiry.
 *
 * IMPORTANT - this script must be placed so it executes before Munchkin /
 * forms2.min.js / loadForm() scripts, otherwise the stored params will land
 * too late onto the URL to be captured by the hidden fields.
 *
 */
(function (root) {
  "use strict";

  const DEFAULTS = {
    expireDays: 30,
    storageKey: "last_utm_query",
    params: [
      "utm_source",
      "utm_medium",
      "utm_campaign",
      "utm_term",
      "utm_content",
      "utm_id",
      "gclid",
      "gbraid",
      "wbraid",
      "fbclid",
      "msclkid",
      "mkt_tok",
    ],
    marker: "utm_restored",
  };

  function isTrackingParam(name, params) {
    for (let i = 0; i < params.length; i++) {
      const p = params[i];
      if (p === "utm_" ? name.indexOf("utm_") === 0 : name === p) return true;
    }
    return false;
  }

  function extractTracking(rawQuery, params) {
    if (!rawQuery) return "";
    const pairs = rawQuery.split("&");
    const kept = [];
    for (let i = 0; i < pairs.length; i++) {
      const pair = pairs[i];
      if (!pair) continue;
      const name = pair.split("=")[0];
      if (isTrackingParam(decodeURIComponent(name), params)) kept.push(pair);
    }
    return kept.join("&");
  }

  function storageGet(key) {
    try {
      return root.localStorage.getItem(key);
    } catch (e) {
      return null;
    }
  }
  function storageSet(key, value) {
    try {
      root.localStorage.setItem(key, value);
      return true;
    } catch (e) {
      return false;
    }
  }
  function storageRemove(key) {
    try {
      root.localStorage.removeItem(key);
    } catch (e) {}
  }

  function run(userOpts) {
    const opts = {};
    let k;
    for (k in DEFAULTS) if (DEFAULTS.hasOwnProperty(k)) opts[k] = DEFAULTS[k];
    if (userOpts)
      for (k in userOpts) if (userOpts.hasOwnProperty(k)) opts[k] = userOpts[k];

    const now = new Date().getTime();
    const rawQuery = root.location.search.replace(/^\?/, "");
    const inbound = extractTracking(rawQuery, opts.params);

    if (inbound) {
      storageSet(opts.storageKey, JSON.stringify({ time: now, query: inbound }));
      return;
    }

    let restored;
    try {
      restored = JSON.parse(storageGet(opts.storageKey) || "null");
    } catch (e) {
      restored = null;
    }
    if (!restored || !restored.query) return;

    if (now - restored.time > opts.expireDays * 864e5) {
      storageRemove(opts.storageKey);
      return;
    }

    const link = document.createElement("a");
    link.href = root.location.href;

    const existing = link.search.replace(/^\?/, "");
    let addition = restored.query;
    if (opts.marker)
      addition += "&" + encodeURIComponent(opts.marker) + "=" + restored.time;

    link.search = existing ? existing + "&" + addition : addition;

    try {
      history.replaceState(history.state, "", link.href);
    } catch (e) {}
  }

  root.UTMForwarding = function (userOpts) {
    run(userOpts);
  };

  if (!root.__utmForwardingHasRun) {
    root.__utmForwardingHasRun = true;
    run(root.utmForwardingOptions);
  }
})(window);
