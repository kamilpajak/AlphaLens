from .types import FormType

WATCHDOG_DEFAULTS = {
    # Required — SEC mandates real contact info. Override before use.
    "user_agent": None,
    "edgar_base_url": "https://www.sec.gov/cgi-bin/browse-edgar",
    "edgar_recent_count": 40,
    "rate_limit_seconds": 0.15,
    "fetch_form4_details": False,
    "fetch_8k_details": False,
    "form_filter": [
        FormType.FORM_8K,
        FormType.FORM_4,
        FormType.FORM_13D,
        FormType.FORM_13G,
        FormType.FORM_13D_A,
        FormType.FORM_13G_A,
    ],
}
