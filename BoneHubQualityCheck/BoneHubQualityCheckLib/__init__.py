"""Support code for the BoneHub Quality Check module.

Nothing in this package imports 3D Slicer, so it can be tested on its own:

- :mod:`client`       the dependency-free REST client, vendored from the server repository
- :mod:`labels`       the BoneHub label map, label statuses, reasons to reject a label, and the
                       colour each label is drawn in
- :mod:`segmentation` writing the corrected segmentation in BoneHub's ``.seg.nrrd`` format
- :mod:`session`      the editor's session: connection, lease, downloads, verdict
"""

from .client import BoneHubQCClient, QCClientError
from .labels import LABEL_STATUS_VALUES, REJECT_REASONS, LabelMap, label_color, status_text
from .segmentation import SEGMENTATION_SUFFIX, write_segmentation
from .session import BoneHubQCSession

__all__ = [
    "LABEL_STATUS_VALUES",
    "REJECT_REASONS",
    "SEGMENTATION_SUFFIX",
    "BoneHubQCClient",
    "BoneHubQCSession",
    "LabelMap",
    "QCClientError",
    "label_color",
    "status_text",
    "write_segmentation",
]
