# UTM forwarding

<!-- license-disclaimer -->
> ⚠️ **No warranty — use at your own risk.** This software is provided "as is", **without warranty of any kind**, express or implied, and the authors are not liable for any claim, damages or other liability arising from its use. You are responsible for reviewing the code and testing it before relying on it. **These scripts run on your live website and set cookies or browser storage.** Test them before deploying, and make sure your use meets your consent and privacy obligations. See [LICENSE](LICENSE) (MIT).

A small browser script that **keeps a visitor's campaign tracking parameters alive across pages**.
When someone lands from an ad or email with `utm_*` / click-id parameters, the script remembers them;
on later pageviews that arrive *without* them it puts them back on the URL. Marketo forms whose hidden
fields auto-fill from URL parameters then capture the campaign even when the form is on a page the visitor
reached by clicking around your site.

**No API keys, no Marketo credentials, no build step.** It runs entirely in the visitor's browser.

## What it does

- **Page whose URL carries tracking parameters** → stores them in `localStorage` (last touch wins).
- **Page whose URL has none** → restores the stored parameters onto the URL with `history.replaceState`
  (no reload, no extra history entry) and adds a `utm_restored=<timestamp>` marker so you can tell restored
  values from real ones.
- Stored values expire after **30 days**.
- Parameters handled: `utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content`, `utm_id`,
  `gclid`, `gbraid`, `wbraid`, `fbclid`, `msclkid`, `mkt_tok`. Everything else in the URL is ignored.

## Install

Add the script to every page that has (or links to pages with) a Marketo form, **above** Munchkin, `forms2.min.js`
and any `loadForm()` call. It must run first, otherwise the restored parameters land on the URL after the form has
already read it.

```html
<script src="/path/to/utm-forwarding.js"></script>
<!-- Munchkin / forms2.min.js / MktoForms2.loadForm(...) go AFTER this -->
```

Or paste the file's contents into a `<script>` tag (or your tag manager, set to fire first). The script runs itself —
nothing else to call.

Then make sure each Marketo form has **hidden fields set to auto-fill from the URL parameters** (in Marketo's form
editor: field → *Hidden* → *Prefill from URL parameter*, with the parameter name e.g. `utm_source`).

## Options

Set `window.utmForwardingOptions` **before** the script loads. All keys are optional:

```html
<script>
  window.utmForwardingOptions = {
    expireDays: 30,                 // how long stored values live
    storageKey: "last_utm_query",   // localStorage key
    marker: "utm_restored",         // parameter added to restored URLs ("" to disable)
    params: ["utm_source", "utm_medium", "utm_campaign", "gclid"] // replaces the default list
  };
</script>
<script src="/path/to/utm-forwarding.js"></script>
```

## Try it locally

[`utm-forwarding.test.html`](utm-forwarding.test.html) is a test harness that shows the URL, the tracking parameters
on it, and what is stored. Serve this folder and open it:

```bash
cd utm-forwarding
python3 -m http.server 8000
# open http://localhost:8000/utm-forwarding.test.html
```

1. Click **① Arrive WITH UTMs** — the script stores the UTMs and ignores the unrelated `foo` parameter.
2. Click **② Land on a CLEAN page** — the script puts the stored UTMs back onto the clean URL.
3. **Reset storage** clears it.

## Files

| File | Purpose |
| --- | --- |
| `utm-forwarding.js` | The script — deploy this. |
| `utm-forwarding.test.html` | Manual test harness — do not deploy. |

## Notes

- Only **last touch** is kept here. For first-touch *and* last-touch values stored in cookies, see
  [`../utm-first-touch`](../utm-first-touch).
- If `localStorage` is unavailable (private browsing, blocked storage) the script does nothing and never throws.
- Check your cookie/consent policy: this stores marketing parameters on the visitor's device.

---

## License and disclaimer

This project is released under the [MIT License](LICENSE) and comes with **no warranty and no support**. It is an independent project, not affiliated with or endorsed by Adobe; Adobe and Marketo are trademarks of Adobe Inc.
