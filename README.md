# BoneHub Dataset Quality Check — 3D Slicer extension

Editor client for the [BoneHub dataset quality-check
server](https://github.com/BoneHub/qc-server), the human half of
a client–server setup for quality check of segmentations in the
[BoneHub Dataset](https://github.com/BoneHub/BoneHub-Dataset).

The server runs the quality check in stages. Reviewers judge each subject first, label by
label, on the server's review page in the browser. A subject with a label they rejected —
it needs correction, or should not be there — or a bone they reported missing goes to the
editors, and so does a subject that has no segmentation yet. This extension is the editors'
client: it leases such a subject, loads its segmentation into 3D Slicer with every segment
named and coloured as its BoneHub label, together with its image when the server sends one,
shows why the subject came, and uploads the correction. A subject sent with its image but
no segmentation is segmented from scratch.

Nothing reaches the dataset until the server's administrator approves the subject. A
correction waits on the server: the labels it changes go back to a reviewer, unless the
server is set to take an editor's word for them, and only the administrator's approval
writes the accepted labels and the corrected segmentation into the dataset. An editor who
cannot correct a subject rejects it, which sends it to the administrator with a comment; the
dataset is left untouched.

The server gives each account one role or both: an **editor** corrects segmentations, here
in 3D Slicer; a **reviewer** judges them in the browser, on the server's review page. This
extension works as an editor. The key of an account that is a reviewer only is refused when
connecting, with the address of the review page to use instead; an account with both roles
can work in either.

Segmentations travel in the dataset's own format, BoneHub data schema 0.3's `.seg.nrrd`,
which 3D Slicer opens natively. The extension works with a quality-check server of version
0.4, serving schema 0.3, only: it checks both when connecting, and a server of another
version is refused with a message saying which side to update.

## Requirements

- 3D Slicer 5.6 or newer (developed and tested against 5.12)
- A server URL and an API key from your administrator, for an account with the editor role
  (whatever it is sent of each subject, see [below](#what-the-server-sends))
- A quality-check server of version 0.4 (BoneHub data schema 0.3)

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
   machine. Connecting checks that the key is an editor's and that the server is of version
   0.4, and fetches the BoneHub label map from the server, so the extension never needs
   updating when the dataset gains labels. The status line also says whether the labels you
   correct go back to a reviewer, or are accepted when you tick them (see
   [below](#what-happens-to-your-correction)).
2. **Subject** — *Get next subject* leases a subject waiting for an editor and loads it into
   an empty scene: the scene is closed first, so nothing of the last subject — nor anything
   else you loaded — is left in it. The slice views are centred on the segmentation, so you
   land on the bones rather than on an empty corner of the volume. The box under the lease
   says [why the subject came to you](#why-a-subject-comes-to-you). When no subject waits for
   an editor, the extension says there is nothing to correct; try again later.
   *Reload subject in hand* picks up a subject you already hold as an editor, for instance
   after restarting Slicer.
3. **Correction** — *Correct in Segment Editor* switches to the Segment Editor with the image
   and segmentation already selected. Use any of its tools. A segment's name is its
   label, so to fix a bone labelled on the wrong side, rename its segment (`FEMUR_LEFT` to
   `FEMUR_RIGHT`). To add a bone, pick its label in the drop-down here and press *Add
   segment*, then paint it. A bone a reviewer reported missing is picked in the drop-down
   for you, and clicking a label the segmentation lacks in the labels table picks that one.
   A subject sent without a segmentation starts empty, and every bone is added this way.
   Only names from that list exist in BoneHub; anything else is refused.
4. **Submit** — *Confirm and submit* uploads your correction, once you have confirmed what it
   will do; see [below](#what-happens-to-your-correction). *Reject* is for a subject you
   cannot correct — the image is unusable, say: it goes to the administrator with your
   comment, and nothing in the dataset changes. The comment is kept in the subject's history,
   where reviewers, the administrator and later editors read it.

`Refresh` re-reads the segments after an edit; returning to the module from the Segment
Editor does the same automatically.

### Why a subject comes to you

The box in the *Subject* section lists why the server handed you the subject:

- each label a reviewer rejected, with the reason — it needs correction, or should not be
  there — and who rejected it;
- each bone a reviewer reported missing;
- the administrator's word, when they sent the subject back to the editors, and a reviewer's
  rejection of the subject as a whole;
- that the subject has no segmentation yet, when that is so.

When the segmentation you were sent is an earlier editor's correction rather than the
dataset's own, the box says whose it is: that correction waits on the server, and the dataset
keeps its own segmentation until the administrator approves the subject. Below that comes the
subject's history — every review, correction and step of the administrator so far — with the
comments.

The labels table says the same label by label, in its *Quality check* column:

| Quality check | Meaning |
| --- | --- |
| rejected: needs correction (rita) | rita rejected the label: correct it |
| rejected: should not be there (rita) | rita rejected the label: delete its segment, unless you disagree |
| missing (rita) | rita reported the bone missing: add it |
| accepted (rita) | rita accepted the label; it keeps that verdict unless you change it |
| to review | waits for a reviewer: nobody has reviewed it yet, or an editor corrected it |
| to review: removed by eddie | eddie took it out of the segmentation, and a reviewer has yet to agree |
| kept | not under review, as the dataset has it reviewed already |
| removed | not in the segmentation, and nobody needs to look at it again |
| added | not in the segmentation you were sent: you added it |

Rejected labels are tinted red and missing ones amber; each row's tooltips say more. The
*Dataset* column shows the label's status in `Subject_info_XXX.json` today — `0` not
available, `1` not reviewed, `2` reviewed — which only the administrator's approval changes,
and *Painted* whether your segmentation has the label.

### What happens to your correction

The server compares your upload with the segmentation it replaces, voxel by voxel. What
becomes of each label then depends on the server's `edits_need_review` setting, which the
status line reports when you connect:

| Label | Corrections go back to a reviewer (the default) | The server takes an editor's word |
| --- | --- | --- |
| Changed or added by you, or rejected by a reviewer | goes back to a reviewer | accepted if you tick it, else goes back to a reviewer |
| Left as it was | keeps its verdict | keeps its verdict, ticked or not |
| Taken out, as a reviewer asked (*should not be there*) | removed | removed |
| Taken out, although nobody asked | removed once a reviewer agrees | removed |
| Reported missing, and not added | goes back to a reviewer, who sees you left it out | stays out |

The labels table has tick boxes only when the server takes an editor's word. Otherwise there
is nothing to tick, and the upload vouches for no label. Before uploading, the extension asks
you to confirm, and lists what can be told without the server's comparison: the labels you
took out, the missing bones still not there, the bones a reviewer said should not be there
that still are, and any segment left empty. After the upload, the status line gives the
server's word on where the subject went, with the labels it counts as changed, accepted,
taken out and waiting for a reviewer.

Whatever the setting, the correction waits on the server, and nothing reaches the dataset
before the administrator approves the subject. Approving sets the accepted labels to `2`
("available, reviewed and corrected") in `Subject_info_XXX.json` — and, by default, the
labels no longer in the segmentation to `0` — and moves the correction into the dataset.

### What the server sends

The server's administrator decides, per account, whether it is sent each subject's image,
its segmentation, or both. Either one is enough to work on.

| The server sends | What happens |
| --- | --- |
| The image and the segmentation | Both are loaded. The segmentation is written back on the image's voxel grid |
| The segmentation only | The segmentation is loaded over a blank volume on its own voxel grid, which is the image's on the server, and written back on that grid. Painting, erasing, scissors, islands, smoothing, margins and the logical operators all work. Tools that follow image intensity, such as *Threshold* or *Grow from seeds*, have nothing to follow |
| The image only | The image is loaded with an empty segmentation. Add each bone's label with *Add segment* and paint it; the segmentation is written back on the image's voxel grid |

The segmentation sent is the one under review: an earlier editor's correction, when the
subject has been corrected before, or else the dataset's own.

The image only is what an account sent the image only always gets, and what any other
account gets for a subject that has no segmentation yet. The server hands an account sent
the image only nothing but such subjects, as it will not let the account replace a
segmentation it has not seen. Should the server have a segmentation of a subject such an
account holds all the same, the extension warns on loading, because the upload would be
refused; reject such a subject with a comment, which sends it to the administrator.

A subject sent neither file is not loaded. The correction section stays locked and the
comment is filled in for you: reject the subject, which sends it to the administrator.
Releasing it puts it back in the queue, and you may be given it again.

Without the image there is only the segmentation's own grid to write back on. If the
server reports that the stored segmentation is not on its image's grid, the extension
warns you on loading, because the upload would be refused; reject such a subject with a
comment.

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
than left to chance; the server then keeps the upload, in canonical form, until the
administrator approves the subject.

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
│   ├── labels.py                   the BoneHub label map, label statuses, reasons to reject a label, and each label's colour
│   ├── segmentation.py             writing the corrected segmentation as a BoneHub .seg.nrrd
│   └── session.py                  connection, lease, downloads, verdict
└── Resources/
    ├── Icons/BoneHubQualityCheck.png
    └── UI/BoneHubQualityCheck.ui
```

Nothing in `BoneHubQualityCheckLib` imports Slicer, so the server-facing half can be run and
tested on its own. `client.py` is a verbatim copy of
`qc_server/client.py`; change it there and copy it across, so both sides
stay on one definition of the API.

## Tests

There are two suites, both registered as ctest targets when the extension is built.

| Suite | What it covers |
| --- | --- |
| `BoneHubQualityCheck.py` | The label colours, and the label map with its statuses and reasons to reject a label; that an account can connect whatever it is sent of each subject, and that only a server of version 0.4 is accepted; how the quality check of a subject is put into words — each label's state, the history, the requests, whose correction the segmentation is, and the server's word on an upload; and the round trip of a BoneHub `.seg.nrrd` through the scene: that labels, voxels and the voxel grid come back unchanged (on an oblique image), that renaming a segment relabels it, that a missing bone can be added, that a segmentation edited without its image is written back on the image's grid, that a subject sent without its segmentation is painted from scratch and written back on the image's grid, that a subject sent neither file is not loaded, that every subject starts from an empty scene, and that a segment which is not a BoneHub label is refused |
| `Testing/Python/BoneHubQualityCheckModuleTest.py` | The panel: that the `.ui` file still carries every widget the code uses, that the sections stay locked until there is something to do, that the key stays masked, that the editor sees why a subject came to them — the labels rejected and missing, which stand out, the administrator's requests, the history with its comments, and an earlier editor's correction — that a missing bone is picked for *Add segment*, that a subject without a segmentation opens for painting from scratch (with a warning for an account sent images only when the server has a segmentation it was not sent), that a subject sent neither file can only be rejected, that the Segment Editor can edit a subject without its image, that tick boxes appear only when the server takes an editor's word and behave across a refresh, what confirming asks, sends and reports under either setting, that rejecting sends the subject to the administrator, and that an empty queue speaks of correcting |

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
would corrupt the dataset silently once approved.

## License

See [LICENSE](LICENSE).
