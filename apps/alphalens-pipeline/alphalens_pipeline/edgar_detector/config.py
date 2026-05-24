from .types import FormType

DETECTOR_DEFAULTS = {
    "edgar_base_url": "https://www.sec.gov/cgi-bin/browse-edgar",
    "edgar_recent_count": 40,
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
