# BoneHub Dataset Quality Check — 3D Slicer extension

Reviewer client for the [BoneHub dataset quality-check
server](https://github.com/BoneHub/bonehub_dataset_quality_check_server), the human half of
a client–server setup for quality check of segmentations in the
[BoneHub Dataset](https://github.com/BoneHub/BoneHub-Dataset).

The server hands out subjects one at a time. This extension leases one, loads its image and
segmentation into 3D Slicer with every segment named and coloured as its BoneHub label, and
sends the reviewed segmentation back. Confirming replaces the segmentation in the dataset
and sets the labels you vouch for in `Subject_info_XXX.json` to status `2` ("available,
reviewed and corrected"), whatever status they had. Rejecting leaves the dataset untouched
and is recorded only in the audit trail.

Segmentations travel in the dataset's own format, BoneHub data schema 0.3's `.seg.nrrd`,
which 3D Slicer opens natively. The extension works with a server of schema 0.3 only: it
checks when connecting, and a server of another version is refused with a message saying
which side to update.

## Requirements

- 3D Slicer 5.6 or newer (developed and tested against 5.12)
- A server URL and a reviewer API key from your administrator
- A quality-check server of version 0.2 or newer (BoneHub data schema 0.3)

No Python packages are installed into Slicer: the client is written against the standard
library, and writing segmentations uses the numpy and SimpleITK that ship with Slicer.

## Installation

Until the extension is in the Extensions Manager, install it from source:

1. Clone this repository.
2. In Slicer, open **Edit → Application Settings → Modules**.
3. Drag the `BoneHubQualityCheck` folder into **Additional module paths**.
4. Restart Slicer. The module appears under **Segmentation → BoneHub Quality Check**.

## Reviewing a subject

1. **Server** — enter the URL and your API key and press *Connect*. The key is masked; tick
   *Remember* to keep it in this computer's Slicer settings, or untick it on a shared
   machine. Connecting also fetches the BoneHub label map from the server, so the extension
   never needs updating when the dataset gains labels.
2. **Subject** — *Get next subject* leases one and loads it. The slice views are centred on
   the segmentation, so you land on the bones rather than on an empty corner of the volume.
   *Reload subject in hand* picks up a subject you already hold, for instance after
   restarting Slicer.
3. **Review** — *Correct in Segment Editor* switches to the Segment Editor with the image
   and segmentation already selected. Use any of its tools. A segment's name is its
   label, so to fix a bone labelled on the wrong side, rename its segment (`FEMUR_LEFT` to
   `FEMUR_RIGHT`). To add a bone the automatic segmentation missed, pick its label in the
   drop-down here and press *Add segment*, then paint it. Only names from that list exist
   in BoneHub; anything else is refused.
4. **Submit** — tick the labels you vouch for. *Confirm and submit* uploads the
   segmentation and marks the ticked labels reviewed; labels left unticked keep the status
   they already have. *Reject* sends the verdict without changing anything in the dataset.

`Refresh` re-reads the segments after an edit; returning to the module from the Segment
Editor does the same automatically.

### What happens to the labels you tick

The *Dataset* column shows each label's status in `Subject_info_XXX.json` today: `0` not
available, `1` not reviewed, `2` reviewed.

| Situation | Result in `Subject_info_XXX.json` |
| --- | --- |
| Ticked, and painted in the segmentation | set to `2`, reviewed and corrected |
| Painted but not ticked | keeps its current status; a label you introduced is recorded as `1`, not reviewed |
| In the dataset, but no longer painted | set to `0`, not available, if the server is configured for it |
| Rejected subject | nothing changes |

## How the segmentation survives the round trip

The subject's segmentation arrives as a BoneHub `.seg.nrrd`, which Slicer loads with one
segment per label, named after it and coloured as the dataset colours it. The file header
also tags each segment with its label value; the dataset's reader trusts that tag first,
so a segment whose tag and name disagree is renamed after its tag on loading. From then on
the segment's name is the only record of its label, and the tags are dropped so that they
cannot contradict a rename.

On submission the segments are written back in the same format, onto the image's own
voxel grid: one segment number per label, in ascending label value, as the dataset's own
files are numbered. A BoneHub mask holds one label per voxel, so where two segments
overlap the higher label value wins, the same way every time. The server refuses an upload
whose voxel grid does not match the image, so this is checked by the test suite rather
than left to chance; the server then stores the upload in canonical form.

A segment that is not named after a BoneHub label is caught before the upload, with a
message naming it, rather than being refused by the server after the wait.

## Advanced settings

| Setting | What it does |
| --- | --- |
| Working folder | Where images and segmentations are downloaded. Defaults to a folder in the system temporary directory |
| Network timeout | Raise it for large images over a slow link |
| Keep downloaded files after submitting | Leaves the files in the working folder instead of deleting them |
| Get the next subject after submitting | Goes straight on to the next subject |

## Layout

```
BoneHubQualityCheck/
├── BoneHubQualityCheck.py          the module: panel, scene logic and the self-test
├── BoneHubQualityCheckLib/
│   ├── client.py                   REST client, vendored from the server repository
│   ├── labels.py                   the BoneHub label map, label statuses, and each label's colour
│   ├── segmentation.py             writing the reviewed segmentation as a BoneHub .seg.nrrd
│   └── session.py                  connection, lease, downloads, verdict
└── Resources/
    ├── Icons/BoneHubQualityCheck.png
    └── UI/BoneHubQualityCheck.ui
```

Nothing in `BoneHubQualityCheckLib` imports Slicer, so the server-facing half can be run and
tested on its own. `client.py` is a verbatim copy of
`bonehub_quality_check_server/client.py`; change it there and copy it across, so both sides
stay on one definition of the API.

## Tests

There are two suites, both registered as ctest targets when the extension is built.

| Suite | What it covers |
| --- | --- |
| `BoneHubQualityCheck.py` | The label colours and label map, and the round trip of a BoneHub `.seg.nrrd` through the scene: that labels, voxels and the voxel grid come back unchanged (on an oblique image), that renaming a segment relabels it, that a segmentation can be started from scratch, and that a segment which is not a BoneHub label is refused |
| `Testing/Python/BoneHubQualityCheckModuleTest.py` | The panel: that the `.ui` file still carries every widget the code uses, that the sections stay locked until there is something to do, that the key stays masked, and that the tick boxes behave across a refresh |

The first also runs from **Reload and Test** in the module's *Advanced* section. Running
either against a source checkout, with `MODULE` standing for the path to the
`BoneHubQualityCheck` folder:

```bash
# the logic, no window needed
Slicer --no-main-window --testing --additional-module-paths "$MODULE" \
       --python-code "import BoneHubQualityCheck as m; m.BoneHubQualityCheckTest().runTest()"

# the panel, which needs a main window. Its folder goes on sys.path, not on the module
# path: it is a test, not a module for Slicer to load.
Slicer --testing --additional-module-paths "$MODULE" \
       --python-code "import sys; sys.path.append(r'$MODULE/Testing/Python'); \
                      import BoneHubQualityCheckModuleTest as t; t.BoneHubQualityCheckModuleTest().runTest()"
```

The round-trip test is the one that matters most: the server refuses an upload whose voxel
grid does not match the image, and a segmentation written back with the wrong label values
would corrupt the dataset silently.

## License

See [LICENSE](LICENSE).
