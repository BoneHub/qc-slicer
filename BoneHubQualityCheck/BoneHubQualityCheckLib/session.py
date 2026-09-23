"""The reviewer's session with the quality-check server: connection, lease, files, verdict.

Everything here is plain Python with no 3D Slicer imports, so it can be exercised outside
the application. The scene side of a review -- volumes, segmentations, the segment editor
-- lives in the module's logic class instead.

A session owns one working folder per assignment::

    <workspace>/<subject_key>/
        <subject_key>.nii.gz                 the image, as downloaded
        <subject_key>_segmentation.seg.nrrd  the segmentation on the server, if any
        <subject_key>_reviewed.seg.nrrd      what the reviewer sends back
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .client import BoneHubQCClient, QCClientError
from .labels import SCHEMA_VERSION, LabelMap, schema_is_supported
from .segmentation import SEGMENTATION_SUFFIX

#: Downloads and the upload share one timeout; a whole-body CT over a slow link is slow.
DEFAULT_TIMEOUT = 300


class BoneHubQCSession:
    """One reviewer, one server, at most one subject in hand at a time."""

    def __init__(self, workspace: Path | None = None, timeout: int = DEFAULT_TIMEOUT):
        self.workspace = Path(workspace) if workspace else Path(tempfile.gettempdir()) / "BoneHubQualityCheck"
        self.timeout = timeout

        self.client: BoneHubQCClient | None = None
        self.server_info: dict = {}
        self.labels = LabelMap()
        self.handout: dict = {}
        self.image_path: Path | None = None
        self.segmentation_path: Path | None = None
        #: Working folder of the subject that was submitted or released last, kept so its
        #: downloads can still be cleaned up after the lease is gone.
        self.finished_folder: Path | None = None

    # ------------------------------------------------------------- connection
    @property
    def connected(self) -> bool:
        return self.client is not None and bool(self.server_info)

    @property
    def user_name(self) -> str:
        return self.server_info.get("user", "")

    def connect(self, base_url: str, api_key: str) -> dict:
        """Check the server, the key and the data schema, and fetch the label map.

        Raises ``QCClientError`` when the server is unreachable, the key is refused, or the
        server serves another version of the BoneHub data schema. A connection that was
        already working is left in place, so a failed reconnect never strands a reviewer
        holding a subject they can no longer submit.
        """
        base_url = (base_url or "").strip()
        api_key = (api_key or "").strip()
        if not base_url:
            raise QCClientError("Enter the server URL, for example http://localhost:8000.")
        if not api_key:
            raise QCClientError("Enter the API key your administrator gave you.")

        client = BoneHubQCClient(base_url, api_key, timeout=self.timeout)
        info = client.ping()
        version = info.get("schema_version")
        if not schema_is_supported(version):
            raise QCClientError(
                f"The server serves BoneHub data schema {version or 'older than 0.3'}, but this extension is "
                f"written for schema {SCHEMA_VERSION}, whose segmentations are {SEGMENTATION_SUFFIX} files. "
                "Update whichever of the two is older."
            )
        # Every segment is checked against the label map, so a server that cannot send it
        # leaves nothing to review with.
        labels = LabelMap.from_payload(client.labels())
        if not labels:
            raise QCClientError("The server sent an empty BoneHub label map.")

        self.client = client
        self.server_info = info
        self.labels = labels
        return info

    def disconnect(self) -> None:
        self.client = None
        self.server_info = {}
        self.labels = LabelMap()
        self.clear_subject()

    def _require_client(self) -> BoneHubQCClient:
        if self.client is None:
            raise QCClientError("Connect to the server first.")
        return self.client

    # --------------------------------------------------------------- subjects
    @property
    def has_subject(self) -> bool:
        return bool(self.handout)

    @property
    def assignment_id(self) -> str:
        return self.handout.get("assignment_id", "")

    @property
    def subject_key(self) -> str:
        return self.handout.get("subject_key", "")

    def clear_subject(self) -> None:
        self.handout = {}
        self.image_path = None
        self.segmentation_path = None

    def next_subject(self) -> dict:
        """Lease the next subject and remember the handout. Raises 404 on an empty queue."""
        self.clear_subject()
        self.handout = self._require_client().next_subject()
        return self.handout

    def open_assignments(self) -> list:
        return self._require_client().my_assignments()

    def reload_assignment(self, assignment_id: str) -> dict:
        """Fetch the handout of a subject this reviewer already holds."""
        self.clear_subject()
        self.handout = self._require_client().assignment(assignment_id)
        return self.handout

    def extend_lease(self) -> dict:
        assignment = self._require_client().extend(self._require_assignment())
        self.handout["expires_at"] = assignment.get("expires_at", self.handout.get("expires_at"))
        return assignment

    def release_subject(self) -> dict:
        assignment = self._require_client().release(self._require_assignment())
        self.finished_folder = self.workspace / self.subject_key if self.subject_key else None
        self.clear_subject()
        return assignment

    def _require_assignment(self) -> str:
        if not self.handout:
            raise QCClientError("No subject is in hand. Ask for the next subject first.")
        return self.handout["assignment_id"]

    # ------------------------------------------------------------------ files
    def subject_folder(self) -> Path:
        folder = self.workspace / (self.subject_key or "unassigned")
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def reviewed_path(self) -> Path:
        return self.subject_folder() / f"{self.subject_key}_reviewed{SEGMENTATION_SUFFIX}"

    def download_image(self) -> Path:
        destination = self.subject_folder() / f"{self.subject_key}.nii.gz"
        self.image_path = self._require_client().download_image(self._require_assignment(), destination)
        return self.image_path

    def download_segmentation(self):
        """Download the stored segmentation, or return None when the subject has none."""
        if not self.handout.get("has_segmentation"):
            self.segmentation_path = None
            return None
        destination = self.subject_folder() / f"{self.subject_key}_segmentation{SEGMENTATION_SUFFIX}"
        self.segmentation_path = self._require_client().download_segmentation(self._require_assignment(), destination)
        return self.segmentation_path

    def discard_files(self, folder: Path | None = None) -> None:
        """Remove a subject's working folder. Safe to call when it was never created.

        Defaults to the subject in hand; pass ``finished_folder`` to clean up after a
        submission, which has already let go of the subject.
        """
        folder = folder or (self.workspace / self.subject_key if self.subject_key else None)
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)

    # ----------------------------------------------------------- the verdict
    def submit(
        self,
        quality_check_confirmed: bool,
        segmentation_path: Path | None = None,
        confirmed_labels: list | None = None,
        comment: str | None = None,
    ) -> dict:
        """Send the verdict and let go of the subject.

        The lease is only cleared when the server accepted the submission, so a rejected
        upload leaves the reviewer holding the subject and able to try again.
        """
        result = self._require_client().submit(
            self._require_assignment(),
            quality_check_confirmed=quality_check_confirmed,
            segmentation_path=segmentation_path,
            confirmed_labels=confirmed_labels,
            comment=(comment or None),
        )
        self.finished_folder = self.workspace / self.subject_key if self.subject_key else None
        self.clear_subject()
        return result
