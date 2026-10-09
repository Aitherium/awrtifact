"""Where a split artifact's parts can live besides GitHub releases.

GitHub releases (`gh.py`, `upload.py`, `fetch.py`) stay the default and the
reversible fallback. `strata` keeps the parts in an AitherStrata pool, content
addressed by each part's sha256.
"""

from .strata import StrataBackendError, StrataPartStore, from_env

__all__ = ["StrataBackendError", "StrataPartStore", "from_env"]
