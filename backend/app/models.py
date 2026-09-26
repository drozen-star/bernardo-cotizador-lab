"""Import side-effect module that registers every ORM model on ``Base.metadata``.

Both ``Base.metadata.create_all`` (first-boot convenience) and Alembic autogenerate
need the complete table set. Importing models from many feature slices means any
one of them could be missed by accident, so the registry is centralised here and
``app.main`` / ``alembic/env.py`` import *this* module rather than each slice.
"""

from app.features.auth.model import User  # noqa: F401
from app.features.comparison.model import Approval  # noqa: F401
from app.features.comparison.model import Comparison  # noqa: F401
from app.features.followup.model import FollowUp  # noqa: F401
from app.features.invitation.model import Invitation  # noqa: F401
from app.features.quote.model import SupplierQuote  # noqa: F401
from app.features.rfq.batch_model import RFQBatch  # noqa: F401  (lab L1)
from app.features.rfq.model import RFQ  # noqa: F401
from app.features.supplier.model import Supplier  # noqa: F401

__all__ = [
    "Approval",
    "Comparison",
    "FollowUp",
    "Invitation",
    "RFQ",
    "RFQBatch",
    "Supplier",
    "SupplierQuote",
    "User",
]
