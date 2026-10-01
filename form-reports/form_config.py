"""Optional, instance-specific settings shared by the form reports.

Everything here is read lazily (at call time) from environment variables, so the
values you put in `.env` -- which `auth.load_config()` loads at startup -- are
picked up. All of it is optional; the defaults work for any Marketo instance.

    FORM_PAGE_URL_FIELDS     Comma-separated names of YOUR hidden form fields
                             that hold the page a form was filled out on (for
                             example a field your forms populate with
                             document.location). Tried before the built-in
                             "Webpage URL" / "Referrer URL" attributes.
    ADOBE_EXPERIENCE_TENANT  Your Adobe Experience Cloud tenant slug (the part
                             after "@" in experience.adobe.com/#/@<tenant>/...).
                             When set, landing-page links point at the Marketo
                             Engage UI inside Adobe Experience Cloud.
"""

from __future__ import annotations

import os

# Marketo's built-in activity attributes, always consulted after custom fields.
BUILTIN_PAGE_FIELDS = ["Webpage URL", "Referrer URL"]

# Activity metadata + bookkeeping ids pushed to the back of every report.
BACK_COLUMNS = [
    "Webpage ID",
    "Client IP Address",
    "User Agent",
    "Query Parameters",
    "CAPTCHA Normalized Score",
    "CAPTCHA Provider",
    "CAPTCHA Raw Score",
    "campaignId",
    "marketoGUID",
]


def page_url_fields() -> list[str]:
    """Fields holding the page a form was filled out on, best candidate first."""
    custom = [f.strip() for f in os.getenv("FORM_PAGE_URL_FIELDS", "").split(",") if f.strip()]
    return custom + [f for f in BUILTIN_PAGE_FIELDS if f not in custom]


def order_columns(keys) -> list[str]:
    """Stable, human-friendly column order for form-fill CSVs.

    leadId, activityDate, formName and the page-URL fields lead; activity
    metadata and ids trail; every submitted form field sits between them in
    alphabetical order. Nothing is dropped: any field seen in the data appears.
    """
    keys = set(keys)
    front = [c for c in ["leadId", "activityDate", "formName"] + page_url_fields() if c in keys]
    back = [c for c in BACK_COLUMNS if c in keys]
    placed = set(front) | set(back)
    middle = sorted(k for k in keys if k not in placed)
    return front + middle + back


def lp_link(so_id, lp_id, fallback: str = "") -> str:
    """Link to a Marketo landing page.

    Uses the Adobe Experience Cloud deep link when ADOBE_EXPERIENCE_TENANT is
    set, otherwise `fallback` (typically the page's public URL), otherwise "".
    """
    tenant = os.getenv("ADOBE_EXPERIENCE_TENANT", "").strip()
    if tenant:
        return (f"https://experience.adobe.com/#/@{tenant}/so:{so_id}"
                f"/marketo-engage/landingpage/{lp_id}")
    return fallback or ""
