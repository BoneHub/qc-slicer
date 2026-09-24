# BoneHub Dataset Quality Check — 3D Slicer extension

Editor client for the [BoneHub dataset quality-check
server](https://github.com/BoneHub/bonehub_dataset_quality_check_server), the human half of
a client–server setup for quality check of segmentations in the
[BoneHub Dataset](https://github.com/BoneHub/BoneHub-Dataset).

The server hands out subjects one at a time. This extension leases one, loads its
segmentation into 3D Slicer with every segment named and coloured as its BoneHub label,
together with its image when the server sends one, and sends the corrected segmentation
back. Confirming replaces the segmentation in the dataset
and sets the labels you vouch for in `Subject_info_XXX.json` to status `2` ("available,
reviewed and corrected"), whatever status they had. Rejecting leaves the dataset untouched
and is recorded only in the audit trail.

The server gives each account one role or both: an **editor** corrects segmentations, here
in 3D Slicer; a **reviewer** checks them in the browser, on the server's review page, and
confirms or rejects them as they are. This extension works as an editor. The key of an
account that is a reviewer only is refused when connecting, with the address of the review
page to use instead; an account with both roles can work in either.

Segmentations travel in the dataset's own format, BoneHub data schema 0.3's `.seg.nrrd`,
which 3D Slicer opens natively. The extension works with a server of schema 0.3 only: it
checks when connecting, and a server of another version is refused with a message saying
which side to update.

## Requirements

- 3D Slicer 5.6 or newer (developed and tested against 5.12)
- A server URL and an API key from your administrator, for an account with the editor role
  that is sent segmentations, with or without their images (see [below](#what-the-server-sends))
- A quality-check server of version 0.3 or newer (BoneHub data schema 0.3), which knows
  editors and reviewers

No Python packages are installed into Slicer: the client is written against the standard
library, and writing segmentations uses the numpy and SimpleITK that ship with Slicer.

## Installation

Until the extension is in the Extensions Manager, install it from source:

1. Clone this repository.
2. In Slicer, open **Edit → Application Settings → Modules**.
3. Drag the `BoneHubQualityCheck` folder into **Additional module paths**.
4. Restart Slicer. The module appears under **Segmentation → BoneHub Quality Check**.

## Correcting a subject

1. **Server** — enter the URL and your API key and press *Connect*. The key is masked; tick
   *Remember* to keep it in this computer's Slicer settings, or untick it on a shared
   machine. Connecting checks that the key is an editor's, and also fetches the BoneHub label
   map from the server, so the extension never needs updating when the dataset gains labels.
2. **Subject** — *Get next subject* leases one and loads it into an empty scene: the scene
   is closed first, so nothing of the last subject — nor anything else you loaded — is left
   in it. The slice views are centred on the segmentation, so you land on the bones rather
   than on an empty corner of the volume.
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

### What the server sends

The server's administrator decides, per account, whether it is sent each subject's image,
its segmentation, or both. The segmentation is what you correct, so the extension needs
it; the image is optional.

| The server sends | What happens |
| --- | --- |
| The image and the segmentation | Both are loaded. The segmentation is written back on the image's voxel grid |
| The segmentation only | The segmentation is loaded over a blank volume on its own voxel grid, which is the image's on the server, and written back on that grid. Painting, erasing, scissors, islands, smoothing, margins and the logical operators all work. Tools that follow image intensity, such as *Threshold* or *Grow from seeds*, have nothing to follow |
| The image only | The account is refused when connecting, since it would have nothing to correct. Ask your administrator to send it the segmentation too |

A subject that has no segmentation is not loaded. The review section stays locked and the
comment is filled in for you: reject the subject, so that it is not handed to you again.
Releasing it puts it back in the queue, and you may be given it again.

Without the image there is only the segmentation's own grid to write back on. If the
server reports that the stored segmentation is not on its image's grid, the extension
warns you on loading, because a confirmation would be refused; reject such a subject with
a comment.

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
voxel grid (without the image, the grid the segmentation arrived on, read from its file
header): one segment number per label, in ascending label value, as the dataset's own
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
| `BoneHubQualityCheck.py` | The label colours and label map; that an account sent images only is refused; and the round trip of a BoneHub `.seg.nrrd` through the scene: that labels, voxels and the voxel grid come back unchanged (on an oblique image), that renaming a segment relabels it, that a missing bone can be added, that a segmentation edited without its image is written back on the image's grid, that a subject is not loaded without its segmentation, that every subject starts from an empty scene, and that a segment which is not a BoneHub label is refused |
| `Testing/Python/BoneHubQualityCheckModuleTest.py` | The panel: that the `.ui` file still carries every widget the code uses, that the sections stay locked until there is something to do, that the key stays masked, that a subject without a segmentation can only be rejected, that the Segment Editor can edit a subject without its image, and that the tick boxes behave across a refresh |

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
