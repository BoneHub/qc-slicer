"""The BoneHub label map as the client sees it, plus the colour each label is drawn in.

The label map itself is never hard-coded here: it is fetched from the server's
``/api/v1/labels`` endpoint, so a dataset that gains labels does not need a new release of
the extension. This module only wraps that payload and decides how a label is shown.

Label values follow BoneHub data schema 0.3: nine digits built from a bone's structure,
part, tissue and side (see ``bonehub_data_schema/labelmap.py``). A label's status in
``Subject_info_XXX.json`` is 0, 1 or 2 (see :data:`LABEL_STATUS_VALUES`); the server sets it
when its administrator approves a subject. A reviewer who rejects a label gives one of the
reasons in :data:`REJECT_REASONS`.
"""

from __future__ import annotations

import colorsys

#: The BoneHub data schema this extension is written for, as major.minor. Label statuses
#: and the segmentation file format are those of this version.
SCHEMA_VERSION = "0.3"

#: Meaning of the label statuses in ``Subject_info_XXX.json``. Served by the server as
#: well; this copy is the fallback for a status the server did not describe.
LABEL_STATUS_VALUES: dict[int, str] = {
    0: "not available",
    1: "available, not reviewed or corrected",
    2: "available, reviewed and corrected (if necessary)",
}

#: Short forms of the above. The module's panel is a narrow dock, so a table cell has room
#: for a word; the full wording from :data:`LABEL_STATUS_VALUES` goes in the tooltip.
LABEL_STATUS_SHORT: dict[int, str] = {
    0: "absent",
    1: "unreviewed",
    2: "reviewed",
}

#: Why a reviewer rejected a label: its segmentation needs correcting, the bone should not be
#: segmented at all, or it should be and is not. Served by the server as well; this copy is
#: the fallback for a reason the server did not describe.
REJECT_REASONS: dict[str, str] = {
    "quality": "needs correction",
    "absent": "should not be there",
    "missing": "is missing",
}


def label_color(value: int) -> tuple[float, float, float]:
    """The RGB colour in 0..1 that BoneHub's segmentation files give a label value.

    A copy of ``_segment_color`` in ``bonehub_data_schema/segmentation_file.py``: the hue
    comes from the body region and the shade from the finer fields. A segment loaded from
    a dataset file already carries this colour in the file header; drawing the segments
    added here the same way keeps a bone one colour however it reached the scene.
    """
    value = int(value)
    region = value // 100_000_000
    structure = value // 100_000
    tissue = (value // 10) % 100
    hue = ((region / 10.0) + (structure % 100) * 0.013) % 1.0
    saturation = 0.45 + ((structure // 100) % 5) * 0.09
    lightness = min(0.42 + tissue * 0.09, 0.78)
    return colorsys.hls_to_rgb(hue, lightness, saturation)


def status_text(value, short: bool = True) -> str:
    """Describe a ``Subject_info`` label status, falling back to the bare number."""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return str(value)
    if short:
        description = LABEL_STATUS_SHORT.get(value)
        return f"{value} {description}" if description else str(value)
    description = LABEL_STATUS_VALUES.get(value)
    return f"{value} - {description}" if description else str(value)


def schema_is_supported(version) -> bool:
    """True if a server's schema version has the major.minor this extension is written for."""
    return isinstance(version, str) and version.split(".")[:2] == SCHEMA_VERSION.split(".")


class LabelMap:
    """The BoneHub label names and their values, the label statuses and the reasons to reject
    a label, as served by ``/api/v1/labels``."""

    def __init__(self, name_to_value: dict | None = None, statuses: dict | None = None, reasons: dict | None = None):
        # Background is never a segment, so it is left out even if a server sends it.
        self.name_to_value: dict[str, int] = {
            str(k): int(v) for k, v in (name_to_value or {}).items() if int(v) != 0
        }
        self.value_to_name: dict[int, str] = {v: k for k, v in self.name_to_value.items()}
        self.statuses: dict[int, str] = dict(LABEL_STATUS_VALUES)
        for key, text in (statuses or {}).items():
            try:
                self.statuses[int(key)] = str(text)
            except (TypeError, ValueError):
                continue
        self.reasons: dict[str, str] = {**REJECT_REASONS, **{str(k): str(v) for k, v in (reasons or {}).items()}}

    @classmethod
    def from_payload(cls, payload: dict) -> "LabelMap":
        """Build from the body of ``GET /api/v1/labels``."""
        payload = payload or {}
        return cls(
            payload.get("label_name_to_value"), payload.get("label_status_values"), payload.get("reject_reasons")
        )

    def __bool__(self) -> bool:
        return bool(self.name_to_value)

    def __len__(self) -> int:
        return len(self.name_to_value)

    def value_of(self, name: str):
        """The BoneLabelMap value of a label name, or None when the name is not a BoneHub label."""
        return self.name_to_value.get(name)

    def name_of(self, value: int):
        """The label name of a BoneLabelMap value, or None when the value is not a BoneHub label."""
        try:
            return self.value_to_name.get(int(value))
        except (TypeError, ValueError):
            return None

    def sorted_names(self) -> list:
        """Every label name, ordered by value, which is anatomical order."""
        return [name for name, _ in sorted(self.name_to_value.items(), key=lambda item: item[1])]

    def describe(self, status) -> str:
        """The server's own wording for a label status, for a tooltip."""
        try:
            return self.statuses.get(int(status), str(status))
        except (TypeError, ValueError):
            return str(status)

    def reason_text(self, reason) -> str:
        """The server's own wording for a reason to reject a label: 'needs correction' for 'quality'."""
        return self.reasons.get(str(reason), str(reason))

    def color_of(self, name: str) -> tuple[float, float, float]:
        value = self.name_to_value.get(name)
        return label_color(value) if value is not None else (0.5, 0.5, 0.5)
