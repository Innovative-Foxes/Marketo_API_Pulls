# UTM first-touch / last-touch capture

<!-- license-disclaimer -->
> ⚠️ **No warranty — use at your own risk.** This software is provided "as is", **without warranty of any kind**, express or implied, and the authors are not liable for any claim, damages or other liability arising from its use. You are responsible for reviewing the code and testing it before relying on it. **These scripts run on your live website and set cookies or browser storage.** Test them before deploying, and make sure your use meets your consent and privacy obligations. See [LICENSE](LICENSE) (MIT).

A browser script that records **where a visitor first came from** (first touch, written once) and **where they most
recently came from** (last touch), as cookies, and copies both into hidden fields on your Marketo forms. Every form
submission then carries its first-touch and last-touch campaign data.

**No API keys, no Marketo credentials, no build step.** It runs entirely in the visitor's browser.

It uses the same cookie names and form field names as the widely used `ppcUrlCookie` snippet, so it can replace that
snippet without reconfiguring forms.

## Install

1. Add the script to your pages **before** your Marketo form loads, or at least before the form is submitted:

   ```html
   <script src="/path/to/utm-first-touch.js"></script>
   ```

   Or paste its contents into a `<script>` tag or your tag manager.

2. Add these **hidden fields** to your Marketo forms (and create matching lead fields in Marketo). The script fills
   any `<input>` whose `name` matches, ignoring case, on every form on the page:

   | Hidden form field | Filled with |
   | --- | --- |
   | `utm_campaignID` | last-touch `utm_campaignID` |
   | `utm_source__c`, `utm_medium__c`, `utm_campaign__c`, `utm_term__c`, `utm_content__c` | last-touch `utm_source`, `utm_medium`, `utm_campaign`, `utm_term`, `utm_content` |
   | `ft_utm_campaignID` | first-touch `utm_campaignID` |
   | `ft_utm_source`, `ft_utm_medium`, `ft_utm_campaign`, `ft_utm_term`, `ft_utm_content` | first-touch values |
   | `ft_captured_date` *(optional)* | timestamp of the first-touch visit — lets you tell "first touch had no term" from "no first touch recorded" |

   Fields the page does not have are skipped silently. If your lead fields use different names, change the second
   column of the `FORM_FIELDS` list near the top of the script.

## How it behaves

- **First touch** is written once, as one set, on the visitor's first *tagged* visit (one carrying at least one tracked
  parameter), and never changed afterward.
- **Last touch** is replaced as a whole set on every tagged visit; an untagged visit leaves it alone.
- Parameters missing from a URL are left empty — the script **never writes the text `null`**, and any cookie already
  holding `null` is treated as empty and repaired on the next tagged visit.
- Parameter names are matched ignoring case (`utm_CampaignID` = `utm_campaignid`).
- Cookies last **365 days**. Forms rendered later by Marketo are filled when `MktoForms2.whenReady` fires.

Cookie names written: `utmCampaignId`, `utmSource`, `utmMedium`, `utmCampaign`, `utmKeyword`, `utmContent`
(last touch), `ftUtmCampaignId`, `ftUtmSource`, `ftUtmMedium`, `ftUtmCampaign`, `ftUtmKeyword`, `ftUtmContent`
(first touch) and `ftCaptured`.

## Options

Set `window.utmFirstTouchOptions` **before** the script loads. All keys are optional:

```html
<script>
  window.utmFirstTouchOptions = {
    cookieDomains: ["example.com"], // share cookies across subdomains/sites
    cookieDays: 365,                // cookie lifetime in days
    useLatestValues: true           // false = freeze last touch after the first write
  };
</script>
<script src="/path/to/utm-first-touch.js"></script>
```

- **`cookieDomains`** — if the current hostname contains one of these, the cookie is scoped to that parent domain, so
  `www.example.com` and `go.example.com` share it. List every parent domain you run (for several sites, list them all).
  With no match — or no option — the cookie is scoped to the current hostname only.

## Debugging

The script exposes `window.utmTracking`:

```js
utmTracking.read()          // every cookie value it manages
utmTracking.capture()       // re-run capture for the current URL
utmTracking.populateForms() // re-fill forms, e.g. after one renders late
```

## Run the tests

No dependencies. Needs Node.js 18+:

```bash
cd utm-first-touch
node utm-first-touch.test.js     # prints PASS/FAIL per check; "33 passed, 0 failed" when healthy
```

## Files

| File | Purpose |
| --- | --- |
| `utm-first-touch.js` | The script — deploy this. |
| `utm-first-touch.test.js` | Test suite for it — do not deploy. |

## Notes

- For a lighter last-touch-only approach that keeps parameters on the URL instead of in cookies, see
  [`../utm-forwarding`](../utm-forwarding).
- Cookies are set by JavaScript: check your cookie/consent policy and load the script according to your consent rules.

---

## License and disclaimer

This project is released under the [MIT License](LICENSE) and comes with **no warranty and no support**. It is an independent project, not affiliated with or endorsed by Adobe; Adobe and Marketo are trademarks of Adobe Inc.
