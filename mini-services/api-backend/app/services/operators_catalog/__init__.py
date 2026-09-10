"""The curated operator shelf (task #3 split from app/services/operators.py).

Every pre-wired operator (Meeting, Sales, Clinic, Support, Operations, HR,
Finance, Procurement, Logistics) is pure declarative data - the exact
topology install_operator() builds - moved verbatim into its own module
here, one per vertical, instead of nine ~150-200 line dict literals back
to back in one 2500-line file. CATALOG preserves the original order so
OPERATORS in operators.py is unchanged.
"""

from __future__ import annotations

from . import meeting
from . import sales
from . import clinic
from . import support
from . import operations
from . import hr
from . import finance
from . import procurement
from . import logistics

CATALOG: list[dict] = [
    meeting.OPERATOR,
    sales.OPERATOR,
    clinic.OPERATOR,
    support.OPERATOR,
    operations.OPERATOR,
    hr.OPERATOR,
    finance.OPERATOR,
    procurement.OPERATOR,
    logistics.OPERATOR,
]
