"""Fixed source locations and source limits for the AGB-register."""

from __future__ import annotations

BASE_URL = "https://www.vektis.nl"
FORM_URL = f"{BASE_URL}/agb-register/zoeken"
RESULTS_URL = f"{BASE_URL}/agb-register/zoeken/resultaten"

# Rows the source renders at most for one search; matches beyond it are invisible.
RENDER_CAP = 500

# An AGB-code is always eight digits, leading zeros included.
CODE_LENGTH = 8
