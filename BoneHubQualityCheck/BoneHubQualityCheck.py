"""Editor client for the BoneHub dataset quality-check server.

The module walks an editor through one subject at a time:

1. connect to the server with the editor's API key,
2. lease the next subject waiting for an editor -- one a reviewer sent back, with a label
   rejected or a bone reported missing, or one without any segmentation -- and load what
   the server sends of it, its image, its segmentation or both, into the scene,
3. show why it came: the labels rejected and why, the bones reported missing, the
   administrator's requests and the subject's history,
4. correct the segmentation in the Segment Editor, or paint one from scratch,
5. confirm, which uploads the correction, or reject, which sends the subject to the
   administrator. Either waits on the server: the labels a correction changes go back to a
   reviewer, unless the server takes the editor's word for them, and nothing reaches the
   dataset before the administrator approves the subject.

The server gives each account the role of reviewer, editor, or both, and this module works as
an editor. An account that is a reviewer only is refused when connecting and pointed to the
server's review page, where reviewers work.

Either file is enough to work on. Without the segmentation, the editor starts from an empty
one on the image, adds the labels and paints them. Without the image, the segmentation is
edited and written back on its own voxel grid, which is the image's on the server. Only a
subject that comes with neither is not loaded, and is offered for rejection. The segmentation
sent is the one under review: an earlier editor's correction waiting on the server, or the
dataset's own.

Segmentations travel in the BoneHub dataset's own format, ``.seg.nrrd``, which 3D Slicer
opens natively with one segment per label. In the scene a segment's name is its label, so
renaming a segment in the Segment Editor relabels it. See ``BoneHubQualityCheckLib`` for the
server-facing half, which has no dependency on Slicer.
"""

import json
import logging
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import ctk
import numpy as np
import qt
import slicer
import vtk
from slicer.i18n import tr as _
from slicer.i18n import translate
from slicer.ScriptedLoadableModule import (
    ScriptedLoadableModule,
    ScriptedLoadableModuleLogic,
    ScriptedLoadableModuleTest,
    ScriptedLoadableModuleWidget,
)
from slicer.util import VTKObservationMixin

from BoneHubQualityCheckLib import QCClientError
from BoneHubQualityCheckLib.labels import (
    LABEL_STATUS_VALUES,
    REJECT_REASONS,
    LabelMap,
    label_color,
    schema_is_supported,
    status_text,
)
from BoneHubQualityCheckLib.segmentation import (
    LABEL_TAG,
    SEGMENTATION_SUFFIX,
    VALUE_TAG,
    geometry_from_ijk_to_ras,
    ijk_to_ras_from_geometry,
    read_voxel_grid,
    segment_number_dtype,
    write_segmentation,
)
from BoneHubQualityCheckLib.session import DEFAULT_TIMEOUT, BoneHubQCSession

#: Root of this module's entries in the application settings.
SETTINGS_PREFIX = "BoneHubQualityCheck/"


#
# BoneHubQualityCheck
#


class BoneHubQualityCheck(ScriptedLoadableModule):
    """Module metadata, shown in the module selector and the help panel."""

    def __init__(self, parent):
        ScriptedLoadableModule.__init__(self, parent)
        self.parent.title = _("BoneHub Quality Check")
        self.parent.categories = [translate("qSlicerAbstractCoreModule", "Segmentation")]
        self.parent.dependencies = ["Segmentations", "SegmentEditor"]
        self.parent.contributors = ["Hamid Alavi (University of Twente)"]
        self.parent.helpText = _("""
Editor client for the <a href="https://github.com/BoneHub/bonehub_dataset_quality_check_server">BoneHub
dataset quality-check server</a>. Enter the server URL and the API key your administrator
gave you, ask for the next subject, correct its segmentation in the Segment Editor, and
upload the correction.
<p>The key must belong to an account with the editor role. Reviewers work in the browser
instead, on the server's review page, and the key of an account that is a reviewer only is
refused here.
<p>Reviewers judge each subject first. You are handed the subjects they send back, with a
label rejected (it needs correction, or should not be there) or a bone reported missing, and
the subjects that have no segmentation yet. The <i>Subject</i> section says why a subject came
to you, with the administrator's requests and the history of its quality check, and the labels
table marks the rejected and missing labels. A missing bone is picked for <i>Add segment</i>
already.
<p>The segmentation you are sent is the one under review, which may be an earlier editor's
correction waiting on the server; the <i>Subject</i> section says so. A subject sent without a
segmentation starts from an empty one on the image: add each label with <i>Add segment</i> and
paint it. One sent without its image is edited on the segmentation's own voxel grid, over a
blank volume.
<p><i>Confirm and submit</i> uploads your correction, which waits on the server. The labels you
changed or added, and those a reviewer rejected, go back to a reviewer, unless the server
takes your word for them: then the ones you tick are accepted. Connecting says which. The
labels you left alone keep their verdicts. Nothing reaches the dataset before the
administrator approves the subject.
<p><i>Reject</i> is for a subject you cannot correct: it goes to the administrator with your
comment, and nothing in the dataset changes.
<p>A segment's name is its BoneHub label: rename a segment to relabel it.
<p>Each subject is loaded into an empty scene. The scene is closed when you leave a subject,
so anything else you loaded into it goes as well.
""")
        self.parent.acknowledgementText = _("""
Developed for the BoneHub Dataset at the Department of Biomechanical Engineering,
University of Twente.
""")


#
# BoneHubQualityCheckWidget
#


class BoneHubQualityCheckWidget(ScriptedLoadableModuleWidget, VTKObservationMixin):
    """The editor's panel."""

    def __init__(self, parent=None):
        ScriptedLoadableModuleWidget.__init__(self, parent)
        VTKObservationMixin.__init__(self)
        self.logic = None

    # ------------------------------------------------------------------ setup
    def setup(self):
        ScriptedLoadableModuleWidget.setup(self)

        uiWidget = slicer.util.loadUI(self.resourcePath("UI/BoneHubQualityCheck.ui"))
        self.layout.addWidget(uiWidget)
        self.ui = slicer.util.childWidgetVariables(uiWidget)
        uiWidget.setMRMLScene(slicer.mrmlScene)

        self.logic = BoneHubQualityCheckLogic()

        self.ui.workspacePathLineEdit.filters = ctk.ctkPathLineEdit.Dirs | ctk.ctkPathLineEdit.Writable
        # The panel is a narrow dock. The label names are kept whole, and the quality check
        # takes the room that is left, wrapping onto as many lines as it needs there: rows are
        # fitted to it again whenever the columns change width.
        table = self.ui.labelsTableWidget
        table.setHorizontalHeaderLabels([_("Label"), _("Quality check"), _("Dataset"), _("Painted")])
        header = table.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, qt.QHeaderView.Stretch)
        for column in (2, 3):
            header.setSectionResizeMode(column, qt.QHeaderView.ResizeToContents)
        table.wordWrap = True
        # Fitted once the columns have settled: while a section is being resized, the header
        # still reports its old width. The timer belongs to the table, so it goes with it.
        rowFit = qt.QTimer(table)
        rowFit.setSingleShot(True)
        rowFit.setInterval(0)
        rowFit.timeout.connect(table.resizeRowsToContents)
        header.sectionResized.connect(lambda *args: rowFit.start())
        for column, tip in enumerate(
            (
                _("BoneHub label name. A segment's name is its label."),
                _("What the quality check has made of the label so far, and who said so: rejected, because it "
                  "needs correction or should not be there; missing; accepted; to review; kept, as the dataset has "
                  "it reviewed already; or removed."),
                _("The label's status in Subject_info today: 0 not available, 1 not reviewed, 2 reviewed. It "
                  "changes only when the administrator approves the subject."),
                _("Whether this label has a segment in the segmentation you are correcting."),
            )
        ):
            self.ui.labelsTableWidget.horizontalHeaderItem(column).setToolTip(tip)
        # Lists in the narrow Subject section, indented by a little rather than by Qt's 40 pixels.
        self.ui.caseTextBrowser.document.setIndentWidth(14)

        # Connections
        self.ui.connectButton.clicked.connect(self.onConnect)
        self.ui.showKeyCheckBox.toggled.connect(self.onShowKeyToggled)
        self.ui.serverUrlLineEdit.returnPressed.connect(self.onConnect)
        self.ui.apiKeyLineEdit.returnPressed.connect(self.onConnect)

        self.ui.nextSubjectButton.clicked.connect(self.onNextSubject)
        self.ui.reloadSubjectButton.clicked.connect(self.onReloadSubject)
        self.ui.extendLeaseButton.clicked.connect(self.onExtendLease)
        self.ui.releaseSubjectButton.clicked.connect(self.onReleaseSubject)

        self.ui.refreshLabelsButton.clicked.connect(self.updateLabelsTable)
        self.ui.labelsTableWidget.cellClicked.connect(self.onLabelCellClicked)
        self.ui.selectAllLabelsButton.clicked.connect(lambda: self.setAllLabelsChecked(True))
        self.ui.selectNoLabelsButton.clicked.connect(lambda: self.setAllLabelsChecked(False))
        self.ui.addSegmentButton.clicked.connect(self.onAddSegment)
        self.ui.segmentEditorButton.clicked.connect(self.onOpenSegmentEditor)

        self.ui.confirmButton.clicked.connect(self.onConfirm)
        self.ui.rejectButton.clicked.connect(self.onReject)

        self.ui.openWorkspaceButton.clicked.connect(self.onOpenWorkspace)

        self.loadSettings()
        self.updateGuiFromSession()

    def cleanup(self):
        self.saveSettings()
        self.removeObservers()

    def enter(self):
        self.updateLabelsTable()

    # --------------------------------------------------------------- settings
    def loadSettings(self):
        settings = qt.QSettings()
        self.ui.serverUrlLineEdit.text = settings.value(SETTINGS_PREFIX + "serverUrl", "http://localhost:8000")
        remember = _toBool(settings.value(SETTINGS_PREFIX + "rememberKey", True))
        self.ui.rememberKeyCheckBox.checked = remember
        self.ui.apiKeyLineEdit.text = settings.value(SETTINGS_PREFIX + "apiKey", "") if remember else ""
        self.ui.workspacePathLineEdit.currentPath = settings.value(
            SETTINGS_PREFIX + "workspace", str(self.logic.session.workspace)
        )
        self.ui.timeoutSpinBox.value = int(settings.value(SETTINGS_PREFIX + "timeout", DEFAULT_TIMEOUT))
        self.ui.keepFilesCheckBox.checked = _toBool(settings.value(SETTINGS_PREFIX + "keepFiles", False))
        self.ui.autoNextCheckBox.checked = _toBool(settings.value(SETTINGS_PREFIX + "autoNext", False))

    def saveSettings(self):
        settings = qt.QSettings()
        settings.setValue(SETTINGS_PREFIX + "serverUrl", self.ui.serverUrlLineEdit.text)
        settings.setValue(SETTINGS_PREFIX + "rememberKey", self.ui.rememberKeyCheckBox.checked)
        settings.setValue(SETTINGS_PREFIX + "apiKey", self.ui.apiKeyLineEdit.text if self.ui.rememberKeyCheckBox.checked else "")
        settings.setValue(SETTINGS_PREFIX + "workspace", self.ui.workspacePathLineEdit.currentPath)
        settings.setValue(SETTINGS_PREFIX + "timeout", self.ui.timeoutSpinBox.value)
        settings.setValue(SETTINGS_PREFIX + "keepFiles", self.ui.keepFilesCheckBox.checked)
        settings.setValue(SETTINGS_PREFIX + "autoNext", self.ui.autoNextCheckBox.checked)

    def applySettingsToSession(self):
        session = self.logic.session
        workspace = self.ui.workspacePathLineEdit.currentPath
        if workspace:
            session.workspace = Path(workspace)
        session.timeout = int(self.ui.timeoutSpinBox.value)
        if session.client is not None:
            session.client.timeout = session.timeout

    # ------------------------------------------------------------ connection
    def onShowKeyToggled(self, shown):
        self.ui.apiKeyLineEdit.setEchoMode(qt.QLineEdit.Normal if shown else qt.QLineEdit.Password)

    def onConnect(self):
        self.applySettingsToSession()
        session = self.logic.session
        try:
            info = self.runWithProgress(
                _("Contacting the server..."),
                lambda: session.connect(self.ui.serverUrlLineEdit.text, self.ui.apiKeyLineEdit.text),
            )
        except QCClientError as error:
            self.setStatus(self.ui.connectionStatusLabel, str(error), ok=False)
            slicer.util.errorDisplay(str(error), windowTitle=_("Could not connect"))
            self.updateGuiFromSession()
            return

        self.saveSettings()
        datasets = info.get("allowed_dataset_ids")
        scope = _("datasets {ids}").format(ids=", ".join(str(i) for i in datasets)) if datasets else _("all datasets")
        status = _("Connected as '{user}', editor ({scope}). {labels} labels known, data schema {schema}.").format(
            user=info.get("user", "?"), scope=scope, labels=len(session.labels), schema=info.get("schema_version")
        )
        if info.get("data_access") == "segmentation":
            status += " " + _("This account is sent segmentations without their images.")
        elif info.get("data_access") == "image":
            status += " " + _("This account is sent images without their segmentations, so it segments from scratch.")
        if session.edits_need_review:
            status += " " + _("The labels you correct go back to a reviewer.")
        else:
            status += " " + _("The labels you correct and tick are accepted on your word, without another review.")
        self.setStatus(self.ui.connectionStatusLabel, status, ok=True)
        self.populateAddLabelComboBox()
        self.ui.subjectCollapsibleButton.collapsed = False
        self.updateGuiFromSession()

    # -------------------------------------------------------------- subjects
    def onNextSubject(self):
        if not self.confirmDiscardingCurrentSubject():
            return
        self.applySettingsToSession()
        session = self.logic.session
        self.clearReview()

        try:
            handout = self.runWithProgress(_("Asking the server for the next subject..."), session.next_subject)
        except QCClientError as error:
            if error.status_code == 404:
                slicer.util.infoDisplay(
                    _("There is nothing for you to correct right now.\n\nA subject comes to the editors when a "
                      "reviewer rejects one of its labels or reports a bone missing, or when it has no "
                      "segmentation yet. Try again later.\n\n{detail}").format(detail=str(error)),
                    windowTitle=_("Nothing to correct"),
                )
            else:
                slicer.util.errorDisplay(str(error), windowTitle=_("Could not lease a subject"))
            self.updateGuiFromSession()
            return

        self.downloadAndLoad(handout)

    def onReloadSubject(self):
        """Pick up a subject this editor already holds, after a restart or a lost scene."""
        self.applySettingsToSession()
        session = self.logic.session
        try:
            assignments = self.runWithProgress(_("Looking up your open subjects..."), session.open_assignments)
        except QCClientError as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Could not list your subjects"))
            return
        if not assignments:
            text = _("You are not holding any subject to correct. Use 'Get next subject'.")
            if "reviewer" in (session.server_info.get("roles") or []):
                text += "\n\n" + _("Subjects you hold as a reviewer, on the review page, are not listed here.")
            slicer.util.infoDisplay(text, windowTitle=_("Nothing in hand"))
            return

        assignment = assignments[0]
        if len(assignments) > 1:
            choices = [f"{a.get('subject_key')} (expires {a.get('expires_at')})" for a in assignments]
            chosen = qt.QInputDialog.getItem(
                slicer.util.mainWindow(), _("Subjects in hand"), _("Reload which subject?"), choices, 0, False
            )
            if not chosen:
                return
            assignment = assignments[choices.index(chosen)]

        if not self.confirmDiscardingCurrentSubject():
            return
        self.clearReview()
        try:
            handout = self.runWithProgress(
                _("Fetching the subject..."), lambda: session.reload_assignment(assignment["assignment_id"])
            )
        except QCClientError as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Could not fetch the subject"))
            return
        self.downloadAndLoad(handout)

    def downloadAndLoad(self, handout):
        """Fetch the subject's files, then build the scene from them.

        Either file is enough. Without the segmentation, the editor paints one from scratch
        on the image; without the image, the segmentation is edited on its own voxel grid. A
        subject that comes with neither is not loaded, and all that is left to do with it is
        to reject it, which sends it to the administrator.
        """
        session = self.logic.session
        key = handout.get("subject_key", "")
        # Whatever happens next, the last subject's segmentation is not this one's, and must
        # not be what confirming this one uploads.
        self.clearReview()
        self.setStatus(self.ui.submitStatusLabel, "")

        if not handout.get("has_segmentation") and not handout.get("has_image"):
            self.ui.commentTextEdit.plainText = _("Neither the image nor the segmentation was sent, so there was "
                                                  "nothing to work on.")
            self.ui.submitCollapsibleButton.collapsed = False
            self.updateGuiFromSession()
            slicer.util.warningDisplay(
                _("The server sent neither the image nor the segmentation of {key}, so there is nothing here "
                  "to work on.\n\nReject the subject: that sends it to the administrator, with the comment filled "
                  "in for you. Releasing it puts it back in the queue, and you may be given it again.").format(
                    key=key
                ),
                windowTitle=_("Nothing sent"),
            )
            return

        def fetch():
            segmentationPath = session.download_segmentation()
            imagePath = session.download_image() if handout.get("has_image") else None
            return segmentationPath, imagePath

        try:
            segmentationPath, imagePath = self.runWithProgress(_("Downloading {key}...").format(key=key), fetch)
        except QCClientError as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Download failed"))
            self.updateGuiFromSession()
            return

        try:
            slicer.app.setOverrideCursor(qt.Qt.WaitCursor)
            self.logic.loadSubject(handout, segmentationPath, imagePath)
        except Exception as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Could not load the subject"))
        finally:
            slicer.app.restoreOverrideCursor()

        self.ui.reviewCollapsibleButton.collapsed = False
        self.ui.submitCollapsibleButton.collapsed = False
        self.updateGuiFromSession()
        self.pickNextMissingLabel()

        # With the image, the upload is written on the image's grid whatever grid the stored
        # segmentation is on. Without it, the segmentation's own grid is all there is.
        issue = handout.get("stored_segmentation_issue")
        if imagePath is None and issue and self.logic.segmentationNode is not None:
            slicer.util.warningDisplay(
                _("The server reports a problem with the segmentation of {key}:\n\n{issue}\n\nWithout the "
                  "image, 3D Slicer can only write the segmentation back on its own voxel grid, so the server "
                  "will refuse your upload. Reject the subject with a comment instead, which sends it to the "
                  "administrator.").format(key=key, issue=issue),
                windowTitle=_("Segmentation off the image's grid"),
            )

        # An account sent the image only is not sent the segmentation even when the server has
        # one, and the server will not let it replace a segmentation it has not seen. The
        # handout still says where the segmentation under review comes from, so the editor
        # hears it before painting.
        if segmentationPath is None and handout.get("segmentation_source") and self.logic.segmentationNode is not None:
            slicer.util.warningDisplay(
                _("The server has a segmentation of {key}, but this account is not sent segmentations. The "
                  "server will not let you replace a segmentation you have not seen, so it will refuse your "
                  "upload. Reject the subject with a comment instead, which sends it to the "
                  "administrator.").format(key=key),
                windowTitle=_("Segmentation not sent"),
            )

    def onExtendLease(self):
        try:
            self.runWithProgress(_("Extending the lease..."), self.logic.session.extend_lease)
        except QCClientError as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Could not extend the lease"))
        self.updateGuiFromSession()

    def onReleaseSubject(self):
        if not slicer.util.confirmYesNoDisplay(
            _("Give {key} back without uploading anything? Your corrections are lost, and the subject goes "
              "back to the queue for an editor.").format(key=self.logic.session.subject_key),
            windowTitle=_("Release subject"),
        ):
            return
        try:
            self.runWithProgress(_("Releasing the subject..."), self.logic.session.release_subject)
        except QCClientError as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Could not release the subject"))
            return
        self.finishSubject(_("Subject released. It is back in the queue, waiting for an editor."), ok=True)

    def confirmDiscardingCurrentSubject(self):
        """Warn before walking away from a subject that is still leased."""
        session = self.logic.session
        if not session.has_subject:
            return True
        return slicer.util.confirmYesNoDisplay(
            _("You are still holding {key}, and your corrections to it have not been uploaded. "
              "They will be discarded. The subject stays leased to you, and if you are already "
              "at your limit the server will simply hand {key} back. Continue?").format(
                key=session.subject_key
            ),
            windowTitle=_("Subject still in hand"),
        )

    def clearReview(self):
        """Empty the scene and the panel of the last subject.

        Called before the server is asked for another subject, as well as before one is
        loaded: by then the session has let go of the last subject, so a failed request must
        not leave it on screen either.
        """
        self.logic.clearScene()
        self.ui.labelsTableWidget.setRowCount(0)  # so the new subject does not inherit ticks
        self.ui.commentTextEdit.plainText = ""

    # ---------------------------------------------------------------- review
    def populateAddLabelComboBox(self):
        self.ui.addLabelComboBox.clear()
        for name in self.logic.session.labels.sorted_names():
            self.ui.addLabelComboBox.addItem(name)

    def updateLabelsTable(self):
        """Show every label of this subject: what the quality check has made of it, what the
        dataset says, and what is in the scene.

        The labels a reviewer rejected or reported missing stand out. A label in the
        segmentation has a tick box only when a tick counts, which is when the server takes
        an editor's word for a correction. Ticks the editor already made are kept, so
        refreshing after an edit in the Segment Editor does not undo their choices.
        """
        table = self.ui.labelsTableWidget
        session = self.logic.session
        ticks = not session.edits_need_review
        # A label already in the table keeps whatever the editor did with it; a label that
        # has just appeared -- one they added and painted -- starts ticked, like the labels
        # the subject arrived with.
        previouslyChecked = self.checkedLabels() if table.rowCount else None
        previouslyListed = self._checkableLabels() if table.rowCount else set()

        inSegmentation = self.logic.segmentLabelValues()
        inDataset = dict(session.handout.get("segmentation_labels") or {})
        inCase = _caseLabels(session.handout)
        darkTheme = table.palette.color(qt.QPalette.Base).lightness() < 128

        def sortKey(name):
            value = inSegmentation.get(name)
            if value is None:
                value = session.labels.value_of(name)
            return (value is None, value or 0, name)

        names = sorted(set(inDataset) | set(inCase) | set(inSegmentation), key=sortKey)
        table.setRowCount(len(names))
        for row, name in enumerate(names):
            present = name in inSegmentation
            value = inSegmentation.get(name) if present else session.labels.value_of(name)

            item = qt.QTableWidgetItem(name)
            if present and ticks:
                item.setFlags(qt.Qt.ItemIsUserCheckable | qt.Qt.ItemIsEnabled | qt.Qt.ItemIsSelectable)
                known = previouslyChecked is not None and name in previouslyListed
                ticked = name in previouslyChecked if known else True
                item.setCheckState(qt.Qt.Checked if ticked else qt.Qt.Unchecked)
            else:
                item.setFlags(qt.Qt.ItemIsEnabled)
            item.setToolTip(_labelTip(name, value, present, ticks))
            if value is not None:
                item.setForeground(qt.QBrush(_labelTextColour(value, darkTheme)))
            table.setItem(row, 0, item)

            text, tip, flag = _labelState(inCase.get(name), session.labels, present)
            stateItem = _readOnlyItem(text)
            stateItem.setToolTip(tip)
            table.setItem(row, 1, stateItem)
            if name in inDataset:
                datasetItem = _readOnlyItem(status_text(inDataset[name]))
                datasetItem.setToolTip(session.labels.describe(inDataset[name]))
            else:
                datasetItem = _readOnlyItem(_("not listed"))
                datasetItem.setToolTip(_("Subject_info does not mention this label yet."))
            table.setItem(row, 2, datasetItem)
            table.setItem(row, 3, _readOnlyItem(_("yes") if present else _("no")))

            if flag:
                # What a reviewer sent back is what the editor is here for: the row is tinted,
                # translucent so that it reads over a light and a dark theme alike.
                font = stateItem.font()
                font.setBold(True)
                stateItem.setFont(font)
                tint = qt.QBrush(qt.QColor(*_FLAG_COLOURS[flag], 70))
                for column in range(table.columnCount):
                    table.item(row, column).setBackground(tint)
        table.resizeRowsToContents()

        unknown = sorted(name for name, value in inSegmentation.items() if value is None)
        toAdd = [name for name in _missingLabels(session.handout) if name not in inSegmentation]
        unwanted = [name for name in _rejectedLabels(session.handout, "absent") if name in inSegmentation]
        if unknown:
            self.setStatus(
                self.ui.labelsSummaryLabel,
                _("Not BoneHub labels, so the upload would be refused: {names}. Rename or delete them.").format(
                    names=", ".join(unknown)
                ),
                ok=False,
            )
        elif self.logic.segmentationNode is None:
            self.setStatus(self.ui.labelsSummaryLabel, "")
        else:
            if inSegmentation:
                parts = [_("{n} label(s) in your segmentation.").format(n=len(inSegmentation))]
            else:
                parts = [_("No segments yet. Pick a label below, press 'Add segment', and paint it in the "
                           "Segment Editor.")]
            if toAdd:
                parts.append(_("Reported missing, still to add: {names}.").format(names=", ".join(toAdd)))
            if unwanted:
                parts.append(_("A reviewer says these should not be there: {names}.").format(names=", ".join(unwanted)))
            if inSegmentation and ticks:
                parts.append(_("Of the labels you change or add, and those a reviewer rejected, the ticked ones "
                               "are accepted on your word; the others go back to a reviewer."))
            elif inSegmentation:
                parts.append(_("The labels you change or add, and those a reviewer rejected, go back to a "
                               "reviewer."))
            self.setStatus(self.ui.labelsSummaryLabel, " ".join(parts))

    def checkedLabels(self):
        """Names the editor vouches for, in table order: none, when ticks do not count."""
        table = self.ui.labelsTableWidget
        names = []
        for row in range(table.rowCount):
            item = table.item(row, 0)
            if item is not None and item.checkState() == qt.Qt.Checked:
                names.append(item.text())
        return names

    def _checkableLabels(self):
        """Names the table currently offers a tick box for, whether ticked or not."""
        table = self.ui.labelsTableWidget
        names = set()
        for row in range(table.rowCount):
            item = table.item(row, 0)
            if item is not None and int(item.flags()) & int(qt.Qt.ItemIsUserCheckable):
                names.add(item.text())
        return names

    def setAllLabelsChecked(self, checked):
        table = self.ui.labelsTableWidget
        for row in range(table.rowCount):
            item = table.item(row, 0)
            if item is not None and int(item.flags()) & int(qt.Qt.ItemIsUserCheckable):
                item.setCheckState(qt.Qt.Checked if checked else qt.Qt.Unchecked)

    def onLabelCellClicked(self, row, column):
        """A click on a label the segmentation lacks -- a bone reported missing, say -- picks
        it for 'Add segment'."""
        item = self.ui.labelsTableWidget.item(row, 0)
        if item is not None and item.text() not in self.logic.segmentLabelValues():
            self.pickLabelToAdd(item.text())

    def pickLabelToAdd(self, name):
        """Make ``name`` the label 'Add segment' adds, if it is a BoneHub label."""
        index = self.ui.addLabelComboBox.findText(name)
        if index >= 0:
            self.ui.addLabelComboBox.setCurrentIndex(index)

    def pickNextMissingLabel(self):
        """Pick the first bone reported missing that the segmentation still lacks for 'Add
        segment', so that adding it takes one press."""
        painted = self.logic.segmentLabelValues()
        toAdd = [name for name in _missingLabels(self.logic.session.handout) if name not in painted]
        if toAdd:
            self.pickLabelToAdd(toAdd[0])

    def onAddSegment(self):
        name = self.ui.addLabelComboBox.currentText.strip()
        try:
            self.logic.addEmptySegment(name)
        except Exception as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Could not add the segment"))
            return
        self.updateLabelsTable()
        self.pickNextMissingLabel()

    def onOpenSegmentEditor(self):
        try:
            self.logic.openSegmentEditor()
        except Exception as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Could not open the Segment Editor"))

    # ---------------------------------------------------------------- verdict
    def onConfirm(self):
        session = self.logic.session
        # Read the scene again first: the upload must be about the segments that are there
        # now, not about a row for a segment deleted in the Segment Editor meanwhile.
        self.updateLabelsTable()
        vouched = self.checkedLabels()

        try:
            slicer.app.setOverrideCursor(qt.Qt.WaitCursor)
            written = self.logic.exportReviewedSegmentation(session.reviewed_path())
        except Exception as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Could not write your correction"))
            return
        finally:
            slicer.app.restoreOverrideCursor()

        empty = [name for name in vouched if name not in written]
        if empty:
            slicer.util.errorDisplay(
                _("These labels are ticked but have no voxels in the segmentation -- empty, or "
                  "covered entirely by an overlapping segment -- so you cannot vouch for them: "
                  "{names}").format(names=", ".join(empty)),
                windowTitle=_("Empty labels"),
            )
            return

        if not slicer.util.confirmYesNoDisplay(self.uploadQuestion(written, vouched), windowTitle=_("Upload correction")):
            return

        path = session.reviewed_path()
        comment = self.ui.commentTextEdit.plainText

        # With no tick boxes, nothing is vouched for: should the server start taking an
        # editor's word meanwhile, the correction still goes to a reviewer.
        def upload():
            return session.submit(True, segmentation_path=path, confirmed_labels=vouched, comment=comment)

        try:
            result = self.runWithProgress(_("Uploading your correction..."), upload)
        except QCClientError as error:
            slicer.util.errorDisplay(
                _("The server refused the upload, so you are still holding the subject:\n\n{detail}").format(
                    detail=str(error)
                ),
                windowTitle=_("Upload refused"),
            )
            return

        self.finishSubject(_submissionSummary(result), ok=True, result=result)

    def uploadQuestion(self, written, vouched):
        """What uploading ``written``, vouching for ``vouched``, will do, asked before it is done.

        The server compares the upload with the segmentation it replaces, voxel by voxel, so
        only it knows which labels were changed. What can be told here -- the labels taken
        out, the missing bones still not there, the bones that should not be there and still
        are -- is spelt out, since the reviewers asked for them.
        """
        session = self.logic.session
        handout = session.handout
        labels = sorted(handout.get("labels") or [], key=_anatomicalOrder)
        removed = [label["name"] for label in labels if label.get("painted") and label["name"] not in written]
        absent = _rejectedLabels(handout, "absent")
        asked = [name for name in removed if name in absent]
        unasked = [name for name in removed if name not in absent]
        notAdded = [name for name in _missingLabels(handout) if name not in written]
        unwanted = [name for name in absent if name in written]
        inCase = _caseLabels(handout)
        empty = [name for name in self.logic.segmentLabelValues() if name not in written and name not in inCase]

        paragraphs = [
            _("Upload your correction of {key}?").format(key=session.subject_key),
            _("It waits on the server: nothing reaches the dataset before the administrator approves the subject."),
        ]
        if session.edits_need_review:
            paragraphs.append(_("The labels you changed or added, and those a reviewer rejected, go back to a "
                                "reviewer. The labels you left alone keep their verdicts."))
            if asked:
                paragraphs.append(_("Taken out, as a reviewer asked: {names}.").format(names=", ".join(asked)))
            if unasked:
                paragraphs.append(_("Taken out, although nobody asked: {names}. A reviewer must agree before "
                                    "they are removed.").format(names=", ".join(unasked)))
            if notAdded:
                paragraphs.append(_("Reported missing, and not in your upload: {names}. A reviewer will see that "
                                    "you left them out.").format(names=", ".join(notAdded)))
        else:
            paragraphs.append(_("Of the labels you changed or added, and those a reviewer rejected, the ticked "
                                "ones are accepted on your word and the others go back to a reviewer. The labels "
                                "you left alone keep their verdicts, ticked or not."))
            paragraphs.append(_("Ticked: {names}.").format(names=", ".join(vouched) or _("none")))
            if removed:
                paragraphs.append(_("Taken out: {names}.").format(names=", ".join(removed)))
            if notAdded:
                paragraphs.append(_("Reported missing, and not in your upload: {names}. They stay out of the "
                                    "segmentation.").format(names=", ".join(notAdded)))
        if unwanted:
            paragraphs.append(_("A reviewer said these should not be there, and they are still in: {names}.").format(
                names=", ".join(unwanted)
            ))
        if empty:
            paragraphs.append(_("Empty, so left out of the upload: {names}.").format(names=", ".join(empty)))
        return "\n\n".join(paragraphs)

    def onReject(self):
        session = self.logic.session
        comment = self.ui.commentTextEdit.plainText.strip()
        if not comment and not slicer.util.confirmYesNoDisplay(
            _("Send {key} to the administrator without a comment? The comment is what tells them why you "
              "could not correct it.").format(key=session.subject_key),
            windowTitle=_("No comment"),
        ):
            self.ui.commentTextEdit.setFocus()
            return
        if not slicer.util.confirmYesNoDisplay(
            _("Reject {key}? It goes to the administrator with your comment, and they decide what becomes of "
              "it. Nothing in the dataset changes.").format(key=session.subject_key),
            windowTitle=_("Reject subject"),
        ):
            return

        def upload():
            return session.submit(False, comment=comment)

        try:
            result = self.runWithProgress(_("Sending the subject to the administrator..."), upload)
        except QCClientError as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Rejection refused"))
            return

        self.finishSubject(_submissionSummary(result), ok=True, result=result)

    def finishSubject(self, message, ok=True, result=None):
        """Common tail of submitting, rejecting and releasing."""
        session = self.logic.session
        if not self.ui.keepFilesCheckBox.checked:
            session.discard_files(session.finished_folder)
        self.clearReview()
        if result:
            logging.info("BoneHub quality check: %s", json.dumps(result))
        self.setStatus(self.ui.submitStatusLabel, message, ok=ok)
        self.updateGuiFromSession()
        if self.ui.autoNextCheckBox.checked and session.connected:
            self.onNextSubject()

    # ------------------------------------------------------------------- misc
    def onOpenWorkspace(self):
        self.applySettingsToSession()
        workspace = self.logic.session.workspace
        workspace.mkdir(parents=True, exist_ok=True)
        qt.QDesktopServices.openUrl(qt.QUrl.fromLocalFile(str(workspace)))

    def runWithProgress(self, labelText, work):
        """Run a network call off the main thread while a busy dialog keeps the UI alive.

        Only ``work`` runs in the worker thread, and it never touches the scene -- MRML
        stays on the main thread.
        """
        outcome = {}

        def runner():
            try:
                outcome["value"] = work()
            except BaseException as error:  # re-raised on the calling thread below
                outcome["error"] = error

        dialog = slicer.util.createProgressDialog(
            parent=slicer.util.mainWindow(), windowTitle=_("BoneHub Quality Check"), labelText=labelText, maximum=0
        )
        dialog.setCancelButton(None)
        # Slicer's progress dialog is not modal, so the events processed below would let the
        # editor press 'Get next subject' again mid-download and load a second subject over
        # the first, the scene holding one subject's files while the session holds the other.
        dialog.setWindowModality(qt.Qt.ApplicationModal)
        dialog.show()
        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        try:
            while thread.is_alive():
                slicer.app.processEvents()
                thread.join(0.05)
        finally:
            dialog.close()
            dialog.deleteLater()
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("value")

    def setStatus(self, label, text, ok=None):
        """Write a status line. ``ok`` True is green, False is red, None is unstyled."""
        label.text = text
        if not text or ok is None:
            label.styleSheet = ""
        else:
            label.styleSheet = "color: #157f3b;" if ok else "color: #b22222;"

    def updateGuiFromSession(self):
        session = self.logic.session
        connected = session.connected
        holding = session.has_subject
        # A subject in hand with no segmentation in the scene, not even an empty one to paint,
        # can only be rejected.
        loaded = holding and self.logic.segmentationNode is not None

        self.ui.subjectCollapsibleButton.enabled = connected
        self.ui.reviewCollapsibleButton.enabled = loaded
        self.ui.submitCollapsibleButton.enabled = holding
        self.ui.confirmButton.enabled = loaded
        self.ui.nextSubjectButton.enabled = connected
        self.ui.reloadSubjectButton.enabled = connected
        self.ui.extendLeaseButton.enabled = holding
        self.ui.releaseSubjectButton.enabled = holding
        self.ui.connectButton.text = _("Reconnect") if connected else _("Connect")
        # Ticks count only when the server takes an editor's word for a correction.
        ticks = not session.edits_need_review
        self.ui.selectAllLabelsButton.visible = ticks
        self.ui.selectNoLabelsButton.visible = ticks
        if ticks:
            self.ui.confirmButton.toolTip = _(
                "Upload your corrected segmentation. It waits on the server: of the labels you changed or added, "
                "and those a reviewer rejected, the ticked ones are accepted and the others go back to a reviewer. "
                "Nothing reaches the dataset before the administrator approves the subject."
            )
        else:
            self.ui.confirmButton.toolTip = _(
                "Upload your corrected segmentation. It waits on the server: the labels you changed or added, and "
                "those a reviewer rejected, go back to a reviewer. Nothing reaches the dataset before the "
                "administrator approves the subject."
            )

        if holding:
            handout = session.handout
            text = _("{key}  (dataset {dataset}, subject {subject})").format(
                key=handout.get("subject_key", ""),
                dataset=handout.get("dataset_id", ""),
                subject=handout.get("subject_id", ""),
            )
            if not handout.get("has_segmentation") and not handout.get("has_image"):
                text += "\n" + _("Neither the image nor the segmentation was sent: nothing to work on.")
            elif not handout.get("has_segmentation"):
                text += "\n" + _("Image only: no segmentation was sent, so add the labels and paint them.")
            elif not handout.get("has_image"):
                text += "\n" + _("Segmentation only: no image was sent.")
            self.ui.subjectKeyLabel.text = text
            self.ui.expiresLabel.text = _("Lease expires at {when}").format(when=_when(handout.get("expires_at", "?")))
            self.ui.caseTextBrowser.html = _caseHtml(handout, session.labels, session.user_name)
            self.ui.subjectInfoTextBrowser.html = _subjectInfoHtml(handout)
        else:
            self.ui.subjectKeyLabel.text = _("No subject in hand.")
            self.ui.expiresLabel.text = ""
            self.ui.caseTextBrowser.html = ""
            self.ui.subjectInfoTextBrowser.html = ""

        self.updateLabelsTable()


#
# BoneHubQualityCheckLogic
#


class BoneHubQualityCheckLogic(ScriptedLoadableModuleLogic):
    """Everything the review does to the scene, and the session it does it for."""

    def __init__(self):
        ScriptedLoadableModuleLogic.__init__(self)
        self.session = BoneHubQCSession()
        #: The subject's image, or None when the server did not send one.
        self.imageVolumeNode = None
        #: The volume whose voxel grid the segmentation is edited and written on: the image,
        #: or without it a blank volume on the segmentation's own grid.
        self.referenceVolumeNode = None
        self.segmentationNode = None

    # ------------------------------------------------------------------ scene
    def clearScene(self):
        """Close the scene, so that every subject starts from an empty one.

        Everything goes, not only the nodes loaded here: markups the editor placed, a volume
        rendering's ROI, or a node a failed load lost track of would otherwise stay under the
        next subject and pass for part of it.
        """
        slicer.mrmlScene.Clear(0)
        self.imageVolumeNode = None
        self.referenceVolumeNode = None
        self.segmentationNode = None

    def loadSubject(self, handout, segmentationPath=None, imagePath=None):
        """Load what was sent of one subject: its BoneHub segmentation, one segment per label,
        its image, or both.

        Either is enough. Without the segmentation, an empty one is made on the image for the
        editor to add labels to and paint. Without the image, the segmentation is shown,
        edited and written back on its own voxel grid, over a blank volume standing in for
        the image.
        """
        self.clearScene()
        subjectKey = handout.get("subject_key", "subject")
        if segmentationPath is None and imagePath is None:
            raise RuntimeError(f"Neither the image nor the segmentation of {subjectKey} was sent; nothing to load.")

        try:
            if segmentationPath is not None:
                self.segmentationNode = self.importSegmentation(segmentationPath, subjectKey)
            else:
                self.segmentationNode = self.createEmptySegmentation(f"{subjectKey}_segmentation")
            if imagePath is not None:
                self.imageVolumeNode = slicer.util.loadVolume(str(imagePath), {"name": subjectKey, "show": False})
                self.referenceVolumeNode = self.imageVolumeNode
            else:
                self.referenceVolumeNode = self.createBlankVolume(segmentationPath, f"{subjectKey} (no image)")
        except Exception:
            self.clearScene()  # half a subject in the scene would pass for a whole one
            raise

        self.segmentationNode.SetReferenceImageGeometryParameterFromVolumeNode(self.referenceVolumeNode)
        self.nameSegmentsAfterTheirLabels()

        displayNode = self.segmentationNode.GetDisplayNode()
        if displayNode is not None:
            displayNode.SetVisibility2DFill(True)
            displayNode.SetVisibility2DOutline(True)

        slicer.util.setSliceViewerLayers(background=self.referenceVolumeNode, fit=True)
        self.centerViewsOnSegmentation()
        return self.segmentationNode

    def createBlankVolume(self, segmentationPath, name):
        """A blank volume on the voxel grid of a segmentation file, standing in for its image.

        The Segment Editor edits with a source volume, and the upload is written on a voxel
        grid. The grid is read from the file's header, so it is exactly the one the
        segmentation is stored on -- the image's, which the server checks the upload against.
        """
        size, geometry = read_voxel_grid(segmentationPath)
        imageData = vtk.vtkImageData()
        imageData.SetDimensions(*size)
        imageData.AllocateScalars(vtk.VTK_UNSIGNED_CHAR, 1)
        imageData.GetPointData().GetScalars().Fill(0)

        volumeNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", name)
        volumeNode.SetIJKToRASMatrix(slicer.util.vtkMatrixFromArray(ijk_to_ras_from_geometry(geometry)))
        volumeNode.SetAndObserveImageData(imageData)
        volumeNode.CreateDefaultDisplayNodes()
        return volumeNode

    def centerViewsOnSegmentation(self):
        """Move the slice views onto the segmentation, so the editor lands on the bones.

        Fitting to the image alone leaves the slices wherever the volume's middle happens
        to be, which for a segmentation off to one side shows nothing at all.
        """
        if self.segmentationNode is None:
            return
        bounds = [0.0] * 6
        self.segmentationNode.GetBounds(bounds)
        if bounds[0] > bounds[1]:  # an empty segmentation has inverted bounds
            return
        center = [(bounds[0] + bounds[1]) / 2, (bounds[2] + bounds[3]) / 2, (bounds[4] + bounds[5]) / 2]
        slicer.vtkMRMLSliceNode.JumpAllSlices(
            slicer.mrmlScene, center[0], center[1], center[2], slicer.vtkMRMLSliceNode.CenteredJumpSlice
        )

    def importSegmentation(self, segmentationPath, subjectKey):
        """Read a BoneHub segmentation file (``.seg.nrrd``), which Slicer opens natively.

        The file header names each segment after its label and gives it its colour.
        """
        segmentationNode = slicer.util.loadSegmentation(str(segmentationPath), {"name": f"{subjectKey}_segmentation"})
        if segmentationNode is None:
            raise RuntimeError(f"'{segmentationPath}' could not be loaded as a segmentation.")
        return segmentationNode

    def createEmptySegmentation(self, name):
        """A segmentation with no segments, for a subject sent without one: the editor adds
        each label with ``addEmptySegment`` and paints it."""
        segmentationNode = slicer.mrmlScene.AddNewNodeByClass("vtkMRMLSegmentationNode", name)
        segmentationNode.CreateDefaultDisplayNodes()
        return segmentationNode

    # ------------------------------------------------------------- the labels
    def nameSegmentsAfterTheirLabels(self):
        """Make the name of each loaded segment its BoneHub label, and the only record of it.

        A BoneHub file records a segment's label twice: as its name, and as a BoneHubValue
        tag, which the dataset's reader trusts first. In the scene the name is what the
        editor sees and edits, so a segment whose tag names a label takes that label's
        name, and the tags are dropped so they cannot contradict a later rename. The upload
        writes fresh tags from the names.
        """
        if self.segmentationNode is None:
            return
        segmentation = self.segmentationNode.GetSegmentation()
        for segmentId in _segmentIds(segmentation):
            segment = segmentation.GetSegment(segmentId)
            name = self.session.labels.name_of(_segmentTag(segment, VALUE_TAG) or None)
            if name is not None and segment.GetName() != name:
                segment.SetName(name)
            for tag in (VALUE_TAG, LABEL_TAG):
                if segment.HasTag(tag):
                    segment.RemoveTag(tag)

    def segmentLabelValues(self):
        """``{segment name: BoneLabelMap value}`` for the segmentation in hand.

        A segment's name is its label. A segment not named after a BoneHub label maps to
        None; the server would refuse the upload, so the widget shows it as a problem.
        """
        if self.segmentationNode is None:
            return {}
        segmentation = self.segmentationNode.GetSegmentation()
        names = [segmentation.GetSegment(segmentId).GetName() for segmentId in _segmentIds(segmentation)]
        return {name: self.session.labels.value_of(name) for name in names}

    def addEmptySegment(self, labelName):
        """Add an empty segment for a BoneHub label, ready to be painted."""
        if self.segmentationNode is None:
            raise RuntimeError("No subject is loaded.")
        value = self.session.labels.value_of(labelName)
        if value is None:
            raise RuntimeError(
                f"'{labelName}' is not a BoneHub label. Pick one from the list; the server refuses anything else."
            )
        segmentation = self.segmentationNode.GetSegmentation()
        if segmentation.GetSegmentIdBySegmentName(labelName):
            raise RuntimeError(f"The segmentation already has a segment named '{labelName}'.")
        red, green, blue = label_color(value)
        return segmentation.AddEmptySegment(labelName, labelName, [red, green, blue])

    # ------------------------------------------------------------ the upload
    def exportReviewedSegmentation(self, path):
        """Write the segmentation as a BoneHub ``.seg.nrrd`` on the reference volume's voxel grid.

        That is the image's grid, or without the image the grid the segmentation arrived on.
        Returns ``{label name: value}`` for the labels that actually carry voxels. As in the
        dataset's own files, each label gets one segment number, in ascending label value. A
        BoneHub mask holds one label per voxel, so segments are painted in that order and,
        where two overlap, the higher label value wins -- deterministic, rather than
        dependent on segment order.
        """
        if self.segmentationNode is None or self.referenceVolumeNode is None:
            raise RuntimeError("No subject is loaded.")

        values = self.segmentLabelValues()
        unknown = sorted(name for name, value in values.items() if value is None)
        if unknown:
            raise RuntimeError(
                "These segments are not BoneHub labels, and the server would refuse the upload: "
                + ", ".join(unknown)
                + ". Rename or delete them."
            )
        if not values:
            raise RuntimeError("The segmentation is empty; there is nothing to confirm.")

        labelValues = sorted(set(values.values()))
        numberOf = {value: number for number, value in enumerate(labelValues, start=1)}
        labels = [(self.session.labels.name_of(value), value) for value in labelValues]

        segmentation = self.segmentationNode.GetSegmentation()
        shape = slicer.util.arrayFromVolume(self.referenceVolumeNode).shape
        numbers = np.zeros(shape, dtype=segment_number_dtype(len(labelValues)))
        byValue = sorted(_segmentIds(segmentation), key=lambda sid: values[segmentation.GetSegment(sid).GetName()])
        for segmentId in byValue:
            name = segmentation.GetSegment(segmentId).GetName()
            mask = slicer.util.arrayFromSegmentBinaryLabelmap(self.segmentationNode, segmentId, self.referenceVolumeNode)
            if mask is None or mask.size == 0:
                continue
            if mask.shape != shape:
                raise RuntimeError(f"Segment '{name}' could not be brought onto the subject's voxel grid.")
            numbers[mask > 0] = numberOf[values[name]]

        if not numbers.any():
            raise RuntimeError("The segmentation is empty; there is nothing to confirm.")

        ijkToRas = vtk.vtkMatrix4x4()
        self.referenceVolumeNode.GetIJKToRASMatrix(ijkToRas)
        geometry = geometry_from_ijk_to_ras([[ijkToRas.GetElement(row, column) for column in range(4)] for row in range(4)])
        written = write_segmentation(numbers, labels, geometry, path)
        return {name: self.session.labels.value_of(name) for name in written}

    # ---------------------------------------------------------------- editing
    def openSegmentEditor(self):
        """Switch to the Segment Editor with this subject's nodes already selected.

        Without the image, the blank stand-in is the source volume: painting, erasing and the
        other tools that do not follow image intensity all work on it.
        """
        if self.segmentationNode is None or self.referenceVolumeNode is None:
            raise RuntimeError("No subject is loaded.")
        slicer.util.selectModule("SegmentEditor")
        editor = slicer.modules.segmenteditor.widgetRepresentation().self().editor
        editor.setSegmentationNode(self.segmentationNode)
        if hasattr(editor, "setSourceVolumeNode"):
            editor.setSourceVolumeNode(self.referenceVolumeNode)
        else:  # Slicer 5.0 and older
            editor.setMasterVolumeNode(self.referenceVolumeNode)


#
# helpers
#


def _segmentIds(segmentation):
    """Segment ids of a vtkSegmentation, as a Python list."""
    ids = vtk.vtkStringArray()
    segmentation.GetSegmentIDs(ids)
    return [ids.GetValue(index) for index in range(ids.GetNumberOfValues())]


def _segmentTag(segment, name):
    """Read a segment tag. ``vtkSegment::GetTag`` answers through an out-parameter."""
    if not segment.HasTag(name):
        return ""
    value = vtk.reference("")
    segment.GetTag(name, value)
    return str(value)


def _readOnlyItem(text):
    item = qt.QTableWidgetItem(text)
    item.setFlags(qt.Qt.ItemIsEnabled)
    return item


#: How the labels a reviewer sent back stand out: rejected ones in red, missing ones in amber.
#: The labels table tints their rows, translucent; the Subject section writes them in colour.
_FLAG_COLOURS = {"rejected": (178, 34, 34), "missing": (180, 83, 9)}


def _labelTextColour(value, darkTheme):
    """A label's colour, made readable as text: darkened on a light theme, lightened on a dark one."""
    red, green, blue = label_color(value)
    if darkTheme:
        return qt.QColor.fromRgbF(0.45 + red * 0.55, 0.45 + green * 0.55, 0.45 + blue * 0.55)
    return qt.QColor.fromRgbF(red * 0.6, green * 0.6, blue * 0.6)


def _caseLabels(handout):
    """``{name: label}`` of the subject's labels, each with what the quality check has made of it."""
    return {label["name"]: label for label in handout.get("labels") or []}


def _anatomicalOrder(label):
    value = label.get("value")
    return (value is None, value or 0, label["name"])


def _rejectedLabels(handout, reason=None):
    """The labels a reviewer rejected that the segmentation paints, in anatomical order; with
    ``reason``, only those rejected for it."""
    labels = [
        label
        for label in handout.get("labels") or []
        if label.get("state") == "rejected" and label.get("painted") and reason in (None, label.get("reason"))
    ]
    return [label["name"] for label in sorted(labels, key=_anatomicalOrder)]


def _missingLabels(handout):
    """The bones a reviewer reported missing -- rejected, and not in the segmentation -- in
    anatomical order."""
    labels = [
        label for label in handout.get("labels") or [] if label.get("state") == "rejected" and not label.get("painted")
    ]
    return [label["name"] for label in sorted(labels, key=_anatomicalOrder)]


def _labelTip(name, value, present, ticks):
    """The tooltip of a label's name in the labels table."""
    if value is None:
        if present:
            return _("{name}: not a BoneHub label, so the upload would be refused. Rename or delete it.").format(
                name=name
            )
        return _("{name}: not a BoneHub label.").format(name=name)
    tip = _("{name}, BoneLabelMap value {value}.").format(name=name, value=value)
    if not present:
        return tip + " " + _("Not in your segmentation: click it to pick it for 'Add segment'.")
    if ticks:
        return tip + " " + _("Tick it to vouch for it: if you changed or added it, or a reviewer rejected it, it "
                             "is accepted on your word.")
    return tip


def _labelState(label, labels, present):
    """What the quality check has made of a label: a few words for the labels table, a sentence
    for their tooltip, and "rejected" or "missing" when a reviewer sent the label back, so that
    its row stands out.

    ``label`` is the label as the handout gives it, None for one the quality check does not
    know of; ``labels`` the label map, which words the reasons to reject a label; ``present``
    whether the editor's segmentation paints the label now.
    """
    if label is None:
        if present:
            return _("added"), _("Not in the segmentation you were sent: you added it. Uploaded, it counts as "
                                 "corrected."), None
        return "", _("Not part of this subject's quality check."), None

    state, by, editor = label.get("state"), label.get("by"), label.get("edited_by")
    who = " ({by})".format(by=by) if by else ""
    reviewer = by or _("a reviewer")
    if state == "rejected" and not label.get("painted"):
        tip = _("Reported missing by {by}: the bone should be segmented, and is not.").format(by=reviewer)
        tip += " " + (_("You have added it.") if present else _("Add it with 'Add segment', and paint it."))
        return _("missing") + who, tip, "missing"
    if state == "rejected":
        reason = labels.reason_text(label.get("reason"))
        tip = _("Rejected by {by}: {reason}.").format(by=reviewer, reason=reason)
        if not present:
            tip += " " + _("You took it out of the segmentation.")
        elif label.get("reason") == "absent":
            tip += " " + _("Delete its segment, unless you disagree.")
        else:
            tip += " " + _("Correct it in the Segment Editor.")
        return _("rejected: {reason}").format(reason=reason) + who, tip, "rejected"
    if state == "accepted":
        tip = _("Accepted by {by}.").format(by=reviewer)
        if editor:
            tip += " " + _("Corrected by {editor}.").format(editor=editor)
        return _("accepted") + who, tip + " " + _("It keeps that verdict unless you change it."), None
    if state == "pending" and not label.get("painted"):
        return (
            _("to review: removed by {editor}").format(editor=editor or _("an editor")),
            _("{editor} took it out of the segmentation, and a reviewer has yet to agree. Add it back if the bone "
              "should be segmented.").format(editor=editor or _("An editor")),
            None,
        )
    if state == "pending":
        if editor:
            return _("to review"), _("Waits for a reviewer: {editor} corrected it.").format(editor=editor), None
        return _("to review"), _("Waits for a reviewer: nobody has reviewed it yet."), None
    if state == "kept":
        return _("kept"), _("Not under review: the dataset has it as reviewed already. If you change it, it counts "
                            "as corrected."), None
    if state == "removed":
        return _("removed") + who, _("Not in the segmentation, and nobody needs to look at it again. Add it if the "
                                     "bone should be segmented."), None
    return str(state), "", None


def _when(timestamp):
    """A server timestamp (UTC, ISO 8601) in local time, to the minute; the text as it is when
    it is not one."""
    try:
        moment = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return str(timestamp)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%Y-%m-%d %H:%M")


def _requestText(request):
    """An open request for an editor about no single label: the administrator's, or a
    reviewer's rejection of the subject as a whole."""
    if request.get("role") == "admin":
        text = _("The administrator sent it back to the editors")
    else:
        text = _("{by} rejected the subject as a whole").format(by=request.get("by") or _("A reviewer"))
    comment = request.get("comment")
    return text + (': "{comment}"'.format(comment=comment) if comment else ".")


def _eventWho(event):
    """Who took a step of the quality check, and in what role."""
    if event.get("role") == "admin":
        return _("the administrator")
    return "{by} ({role})".format(by=event.get("by", "?"), role=event.get("role", "?"))


def _eventText(event, labels):
    """What one step of a subject's quality check did, in words."""
    details = event.get("details") or {}
    action = event.get("action")
    if action == "review":
        parts = []
        if details.get("accepted"):
            parts.append(_("accepted {names}").format(names=", ".join(details["accepted"])))
        rejected = [
            "{name} ({reason})".format(name=name, reason=labels.reason_text(reason))
            for name, reason in (details.get("rejected") or {}).items()
            if reason != "missing"
        ]
        if rejected:
            parts.append(_("rejected {names}").format(names=", ".join(rejected)))
        if details.get("missing"):
            parts.append(_("reported missing {names}").format(names=", ".join(details["missing"])))
        return "; ".join(parts) or _("reviewed it")
    if action == "edit":
        parts = []
        if details.get("edited"):
            parts.append(_("corrected {names}").format(names=", ".join(details["edited"])))
        if details.get("removed"):
            parts.append(_("removed {names}").format(names=", ".join(details["removed"])))
        return "; ".join(parts) or _("uploaded the segmentation unchanged")
    if action == "escalate":
        return _("sent it to the administrator")
    if action == "return":
        return _("sent it back to the editors") if details.get("to") == "edit" else _("sent it back to the reviewers")
    if action == "approve":
        return _("approved it")
    if action == "close":
        return _("closed it")
    return str(action)


def _stagedNote(handout, user):
    """Where the segmentation under review comes from, when it is not the dataset's own: an
    earlier editor's correction, waiting on the server. Empty otherwise."""
    if handout.get("segmentation_source") != "staged":
        return ""
    edits = [event for event in handout.get("history") or [] if event.get("action") == "edit"]
    if not edits:
        text = _("The segmentation is an earlier editor's correction.")
    elif edits[-1].get("by") == user:
        text = _("The segmentation is your own earlier correction, of {when}.").format(when=_when(edits[-1].get("at")))
    else:
        text = _("The segmentation is {editor}'s correction, of {when}.").format(
            editor=edits[-1].get("by"), when=_when(edits[-1].get("at"))
        )
    return text + " " + _("It waits on the server and is not in the dataset yet: the dataset keeps its own "
                          "segmentation until the administrator approves the subject.")


def _caseHtml(handout, labels, user):
    """Why the subject came to the editor, where the segmentation they were sent comes from,
    and the subject's quality check so far with its comments, as HTML for the Subject section."""
    caseLabels = _caseLabels(handout)
    why = []
    for name in _rejectedLabels(handout):
        label = caseLabels[name]
        why.append(("rejected", _("{name}, rejected by {by}: {reason}.").format(
            name="<b>{}</b>".format(_escape(name)),
            by=_escape(label.get("by") or _("a reviewer")),
            reason=_escape(labels.reason_text(label.get("reason"))),
        )))
    for name in _missingLabels(handout):
        why.append(("missing", _("{name}, reported missing by {by}.").format(
            name="<b>{}</b>".format(_escape(name)), by=_escape(caseLabels[name].get("by") or _("a reviewer"))
        )))
    for request in handout.get("requests") or []:
        why.append((None, _escape(_requestText(request))))
    if not handout.get("segmentation_source"):
        why.append((None, _escape(_("It has no segmentation yet: add each bone with 'Add segment', and paint it."))))

    parts = []
    if why:
        parts.append("<p><b>{}</b></p>".format(_escape(_("Why it came to you"))))
        parts.append("<ul>{}</ul>".format("".join("<li>{}</li>".format(_coloured(flag, text)) for flag, text in why)))
    note = _stagedNote(handout, user)
    if note:
        parts.append("<p><i>{}</i></p>".format(_escape(note)))
    history = handout.get("history") or []
    if history:
        parts.append("<p><b>{}</b></p>".format(_escape(_("History"))))
        steps = []
        for event in history:
            step = _escape("{when}, {who}: {what}".format(
                when=_when(event.get("at")), who=_eventWho(event), what=_eventText(event, labels)
            ))
            if event.get("comment"):
                step += '<br><i>"{}"</i>'.format(_escape(event["comment"]))
            steps.append("<li>{}</li>".format(step))
        parts.append("<ul>{}</ul>".format("".join(steps)))
    return "".join(parts)


def _coloured(flag, html):
    """``html`` in the colour of a label a reviewer sent back, or as it is."""
    if flag not in _FLAG_COLOURS:
        return html
    return "<span style='color: #{:02x}{:02x}{:02x}'>{}</span>".format(*_FLAG_COLOURS[flag], html)


def _submissionSummary(result):
    """The server's word on a verdict, followed by the labels it names."""
    lines = [result.get("message") or ""]
    for key, text in (
        ("edited_labels", _("Changed or added: {names}.")),
        ("accepted_labels", _("Accepted on your word: {names}.")),
        ("removed_labels", _("Taken out: {names}.")),
        ("pending_labels", _("Waiting for a reviewer: {names}.")),
    ):
        if result.get(key):
            lines.append(text.format(names=", ".join(result[key])))
    return " ".join(line for line in lines if line)


def _toBool(value):
    """QSettings hands back strings on some platforms and bools on others."""
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


def _subjectInfoHtml(handout):
    """The subject's metadata as a small table, without the per-label dictionaries."""
    subject = dict(handout.get("subject_info") or {})
    for key in ("segmentation", "mesh", "nurbs"):
        subject.pop(key, None)
    dataset = handout.get("dataset_info") or {}
    if dataset.get("name"):
        subject["dataset"] = dataset["name"]

    rows = "".join(
        f"<tr><td><b>{_escape(str(key))}</b></td><td>{_escape(str(value))}</td></tr>"
        for key, value in subject.items()
    )
    return f"<table cellspacing='4'>{rows}</table>" if rows else ""


def _escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


#
# BoneHubQualityCheckTest
#


class BoneHubQualityCheckTest(ScriptedLoadableModuleTest):
    """Self-test, runnable from the Reload and Test section and from ctest.

    It exercises the parts that do not need a server: the label map, what the session
    accepts of the server, how the quality check of a subject is put into words, and the round
    trip of a segmentation through the scene -- loaded from a BoneHub ``.seg.nrrd``, with or
    without its image, or painted from scratch on the image, edited, and written back --
    which is where the dataset could silently be corrupted.
    """

    #: Labels of BoneHub data schema 0.3, with their values.
    LABELS = {"SKULL": 100000000, "FEMUR_LEFT": 710000001, "FEMUR_RIGHT": 710000002}

    #: What a quality-check server of version 0.4 answers an editor's ``GET /api/v1/ping``.
    PING = {
        "status": "ok",
        "server": "bonehub-dataset-quality-check-server",
        "server_version": "0.4.0",
        "schema_version": "0.3.0",
        "user": "eddie",
        "role": "editor",
        "roles": ["editor"],
        "allowed_dataset_ids": None,
        "data_access": "image_and_segmentation",
        "edits_need_review": True,
        "mark_removed_labels_absent": True,
        "lease_ttl_seconds": 86400,
        "max_concurrent_assignments": 1,
    }

    #: ... and its ``GET /api/v1/labels``, with the labels of :data:`LABELS`.
    LABELS_PAYLOAD = {
        "schema_version": "0.3.0",
        "label_name_to_value": dict(LABELS),
        "label_status_values": {
            "0": "not available",
            "1": "available, not reviewed or corrected",
            "2": "available, reviewed and corrected (if necessary)",
        },
        "reject_reasons": {"quality": "needs correction", "absent": "should not be there", "missing": "is missing"},
        "segmentation_suffix": ".seg.nrrd",
    }

    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        self.setUp()
        self.test_LabelColoursFollowTheDataset()
        self.test_LabelMapReadsTheServerPayload()
        self.test_EveryAccountCanConnectWhateverItIsSent()
        self.test_OnlyAServerOfVersion04IsAccepted()
        self.test_TheQualityCheckIsPutIntoWords()
        self.test_SegmentationSurvivesTheRoundTrip()
        self.test_RenamingASegmentRelabelsIt()
        self.test_TagsInTheFileNameTheSegments()
        self.test_AMissingBoneCanBeAdded()
        self.test_ASegmentationIsEditedWithoutItsImage()
        self.test_ASubjectIsSegmentedFromScratchOnItsImage()
        self.test_ASubjectNeedsItsImageOrItsSegmentation()
        self.test_ANewSubjectStartsFromAnEmptyScene()
        self.test_UnknownSegmentsAreRefused()

    # ------------------------------------------------------------------ tests
    def test_LabelColoursFollowTheDataset(self):
        """A segment added here must look like the same label loaded from a dataset file."""
        self.delayDisplay("Label colours")
        # The colours bonehub_data_schema writes into the files, for two labels.
        for value, expected in ((100000000, (0.609, 0.4578, 0.231)), (710000001, (0.28392, 0.1932, 0.6468))):
            for actual, wanted in zip(label_color(value), expected):
                self.assertAlmostEqual(actual, wanted, places=5)
        for value in (100000000, 710000011, 831305002, 990000000):
            for channel in label_color(value):
                self.assertTrue(0.0 <= channel <= 1.0, f"{value} gave a channel outside 0..1")
        self.assertNotEqual(label_color(100000000), label_color(710000001), "regions differ in hue")

    def test_LabelMapReadsTheServerPayload(self):
        self.delayDisplay("Label map")
        labelMap = LabelMap.from_payload(
            {
                "schema_version": "0.3.0",
                "label_name_to_value": {"FEMUR_LEFT": 710000001, "SKULL": 100000000, "BACKGROUND": 0},
                "label_status_values": {"1": "not reviewed yet", "2": "reviewed"},
                "reject_reasons": {"quality": "needs correcting"},
            }
        )
        self.assertEqual(labelMap.value_of("FEMUR_LEFT"), 710000001)
        self.assertEqual(labelMap.name_of(100000000), "SKULL")
        self.assertIsNone(labelMap.value_of("NOT_A_BONE"))
        self.assertIsNone(labelMap.value_of("BACKGROUND"), "background is never a segment")
        self.assertEqual(labelMap.sorted_names(), ["SKULL", "FEMUR_LEFT"])
        # The short form is what the table shows; the server's own wording is the tooltip.
        self.assertEqual(status_text(2), "2 reviewed")
        self.assertEqual(status_text(1), "1 unreviewed")
        self.assertIn("not reviewed", status_text(1, short=False))
        self.assertEqual(labelMap.describe(1), "not reviewed yet")
        self.assertEqual(labelMap.describe(0), LABEL_STATUS_VALUES[0])
        # The server words the reasons to reject a label; a reason it leaves out keeps the usual words.
        self.assertEqual(labelMap.reason_text("quality"), "needs correcting")
        self.assertEqual(labelMap.reason_text("absent"), REJECT_REASONS["absent"])
        self.assertEqual(labelMap.reason_text("unheard_of"), "unheard_of")
        # A server of another schema would hand out masks this extension cannot read.
        self.assertTrue(schema_is_supported("0.3.0"))
        self.assertFalse(schema_is_supported("0.2.0"))
        self.assertFalse(schema_is_supported(None), "servers before schema 0.3 report none")

    def test_EveryAccountCanConnectWhateverItIsSent(self):
        """The image, the segmentation, or both: an editor can work with any of them."""
        self.delayDisplay("What the account is sent")
        for access in ("image_and_segmentation", "segmentation", "image"):
            session = self._connect(data_access=access)
            self.assertTrue(session.connected, f"an account sent '{access}' can edit")
            self.assertEqual(session.labels.value_of("FEMUR_LEFT"), 710000001)
            self.assertEqual(session.labels.reason_text("absent"), "should not be there")

    def test_OnlyAServerOfVersion04IsAccepted(self):
        """The panel promises that a correction waits on the server until the administrator
        approves it, and that the labels it corrects go back to a reviewer or are accepted as
        the server says. Only a server of version 0.4 keeps those promises."""
        self.delayDisplay("Server version")
        for version in ("0.4.0", "0.4.3"):
            self.assertTrue(self._connect(server_version=version).connected, version)
        for version in (None, "0.3.0", "0.5.0"):
            with self.assertRaises(QCClientError, msg=f"version {version}") as context:
                self._connect(server_version=version)
            self.assertIn("0.4", str(context.exception), "the editor is told which version it takes")

        self.assertTrue(self._connect(edits_need_review=True).edits_need_review)
        self.assertFalse(self._connect(edits_need_review=False).edits_need_review)
        self.assertTrue(BoneHubQCSession().edits_need_review, "until the server says, corrections are reviewed")

    def test_TheQualityCheckIsPutIntoWords(self):
        """What the handout says of each label, of the requests and of the history, as the
        editor reads it in the panel."""
        self.delayDisplay("The quality check in words")
        labels = LabelMap.from_payload(self.LABELS_PAYLOAD)

        def describe(present=True, **label):
            return _labelState(label, labels, present)

        # A label a reviewer sent back stands out, with the reason and the reviewer.
        self.assertEqual(
            describe(state="rejected", painted=True, reason="quality", by="rita"),
            ("rejected: needs correction (rita)", "Rejected by rita: needs correction. Correct it in the Segment Editor.",
             "rejected"),
        )
        text, tip, flag = describe(state="rejected", painted=True, reason="absent", by="rita")
        self.assertEqual((text, flag), ("rejected: should not be there (rita)", "rejected"))
        self.assertIn("Delete its segment", tip)
        text, tip, flag = describe(present=False, state="rejected", painted=False, reason="missing", by="rita")
        self.assertEqual((text, flag), ("missing (rita)", "missing"))
        self.assertIn("Add segment", tip)
        self.assertIn("You have added it", describe(state="rejected", painted=False, reason="missing", by="rita")[1])
        # The others do not.
        for label, present, wanted in (
            ({"state": "accepted", "painted": True, "by": "rita"}, True, "accepted (rita)"),
            ({"state": "pending", "painted": True}, True, "to review"),
            ({"state": "pending", "painted": True, "edited_by": "eddie"}, True, "to review"),
            ({"state": "pending", "painted": False, "edited_by": "eddie"}, False, "to review: removed by eddie"),
            ({"state": "kept", "painted": True}, True, "kept"),
            ({"state": "removed", "painted": False}, False, "removed"),
            ({"state": "removed", "painted": False, "by": "rita"}, False, "removed (rita)"),
        ):
            text, _tip, flag = _labelState(label, labels, present)
            self.assertEqual((text, flag), (wanted, None), label)
        self.assertEqual(_labelState(None, labels, True)[0], "added", "a label the editor added")

        # The history, step by step.
        review = {
            "action": "review", "by": "rita", "role": "reviewer",
            "details": {"accepted": ["SKULL"], "rejected": {"FEMUR_LEFT": "quality", "FEMUR_RIGHT": "missing"},
                        "missing": ["FEMUR_RIGHT"]},
        }
        self.assertEqual(
            _eventText(review, labels), "accepted SKULL; rejected FEMUR_LEFT (needs correction); reported missing FEMUR_RIGHT"
        )
        self.assertEqual(_eventWho(review), "rita (reviewer)")
        edit = {"action": "edit", "by": "eddie", "role": "editor", "details": {"edited": ["FEMUR_LEFT"], "removed": ["SKULL"]}}
        self.assertEqual(_eventText(edit, labels), "corrected FEMUR_LEFT; removed SKULL")
        self.assertEqual(_eventText({"action": "edit", "details": {}}, labels), "uploaded the segmentation unchanged")
        back = {"action": "return", "by": "admin", "role": "admin", "details": {"to": "edit"}}
        self.assertEqual((_eventWho(back), _eventText(back, labels)), ("the administrator", "sent it back to the editors"))
        self.assertEqual(_eventText({"action": "escalate"}, labels), "sent it to the administrator")

        # Requests about no single label.
        self.assertEqual(
            _requestText({"by": "admin", "role": "admin", "comment": "Check the knee too."}),
            'The administrator sent it back to the editors: "Check the knee too."',
        )
        self.assertEqual(_requestText({"by": "rita", "role": "reviewer"}), "rita rejected the subject as a whole.")

        # Server timestamps are UTC; the panel shows them in local time.
        local = datetime(2026, 9, 24, 14, 3, 5, tzinfo=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
        self.assertEqual(_when("2026-09-24T14:03:05Z"), local)
        self.assertEqual(_when("?"), "?")

        # An earlier editor's correction is not the dataset's own segmentation.
        handout = {"segmentation_source": "staged", "history": [dict(edit, at="2026-09-24T14:03:05Z")]}
        self.assertIn("eddie's correction", _stagedNote(handout, "carol"))
        self.assertIn("your own earlier correction", _stagedNote(handout, "eddie"))
        self.assertEqual(_stagedNote(dict(handout, segmentation_source="dataset"), "carol"), "")

        # What the server did with an upload, with the labels it names.
        summary = _submissionSummary({
            "stage": "review", "message": "Uploaded. 1 label(s) go to a reviewer.",
            "edited_labels": ["FEMUR_LEFT"], "accepted_labels": [], "removed_labels": ["SKULL"],
            "pending_labels": ["FEMUR_LEFT"],
        })
        self.assertEqual(
            summary,
            "Uploaded. 1 label(s) go to a reviewer. Changed or added: FEMUR_LEFT. Taken out: SKULL. "
            "Waiting for a reviewer: FEMUR_LEFT.",
        )

    def test_SegmentationSurvivesTheRoundTrip(self):
        """Loaded from a BoneHub file and written back: same labels, voxels and voxel grid."""
        self.delayDisplay("Round trip through the scene")
        logic, _volumeNode, expected = self._buildSubject()

        self.assertEqual(logic.segmentLabelValues(), {"SKULL": 100000000, "FEMUR_LEFT": 710000001})

        path = Path(tempfile.mkdtemp()) / f"reviewed{SEGMENTATION_SUFFIX}"
        written = logic.exportReviewedSegmentation(path)
        self.assertEqual(written, {"SKULL": 100000000, "FEMUR_LEFT": 710000001})

        image, values, segments = self._readBoneHubFile(path)
        self.assertTrue(np.array_equal(values, expected), "every voxel keeps its label")
        self.assertEqual(
            [segments[number][0] for number in sorted(segments)], ["SKULL", "FEMUR_LEFT"], "numbered by label value"
        )
        self.assertEqual(segments[2][1], {"BoneHubLabel": "FEMUR_LEFT", "BoneHubValue": "710000001"})
        self._assertSameGridAsTheImage(image, self.imagePath)
        self.delayDisplay("Labels, voxels and geometry are unchanged")

    def test_RenamingASegmentRelabelsIt(self):
        """The fix for a bone segmented on the wrong side is to rename its segment."""
        self.delayDisplay("Relabelling by renaming")
        logic, _volumeNode, expected = self._buildSubject()
        segmentation = logic.segmentationNode.GetSegmentation()
        segmentation.GetSegment(segmentation.GetSegmentIdBySegmentName("FEMUR_LEFT")).SetName("FEMUR_RIGHT")

        path = Path(tempfile.mkdtemp()) / f"reviewed{SEGMENTATION_SUFFIX}"
        self.assertEqual(logic.exportReviewedSegmentation(path), {"SKULL": 100000000, "FEMUR_RIGHT": 710000002})
        _image, values, _segments = self._readBoneHubFile(path)
        self.assertTrue(np.array_equal(values, np.where(expected == 710000001, 710000002, expected)))

    def test_TagsInTheFileNameTheSegments(self):
        """As in the dataset's reader, a segment's BoneHubValue tag says which label it is."""
        self.delayDisplay("Segments named by their tags")
        logic, _volumeNode, _expected = self._buildSubject(names={1: "Segment_1"})
        self.assertEqual(logic.segmentLabelValues(), {"SKULL": 100000000, "FEMUR_LEFT": 710000001})
        segmentation = logic.segmentationNode.GetSegmentation()
        for segmentId in _segmentIds(segmentation):
            self.assertFalse(segmentation.GetSegment(segmentId).HasTag(VALUE_TAG), "the name is the only record")

    def test_AMissingBoneCanBeAdded(self):
        """A bone the automatic segmentation missed: the editor adds its label and paints it."""
        self.delayDisplay("Adding a missing bone")
        logic, volumeNode, expected = self._buildSubject()

        segmentId = logic.addEmptySegment("FEMUR_RIGHT")
        with self.assertRaises(RuntimeError):
            logic.addEmptySegment("FEMUR_RIGHT")
        with self.assertRaises(RuntimeError):
            logic.addEmptySegment("NOT_A_BONE")
        painted = np.zeros(expected.shape, dtype=np.uint8)
        painted[7:10, 2:6, 2:7] = 1
        slicer.util.updateSegmentBinaryLabelmapFromArray(painted, logic.segmentationNode, segmentId, volumeNode)

        path = Path(tempfile.mkdtemp()) / f"reviewed{SEGMENTATION_SUFFIX}"
        self.assertEqual(
            logic.exportReviewedSegmentation(path),
            {"SKULL": 100000000, "FEMUR_LEFT": 710000001, "FEMUR_RIGHT": 710000002},
        )
        image, values, _segments = self._readBoneHubFile(path)
        self.assertTrue(np.array_equal(values, np.where(painted > 0, 710000002, expected)))
        self._assertSameGridAsTheImage(image, self.imagePath)

    def test_ASegmentationIsEditedWithoutItsImage(self):
        """An account sent segmentations only: edited, and written back, on the segmentation's own grid.

        That grid is the image's on the server, which refuses an upload on any other.
        """
        self.delayDisplay("Segmentation without its image")
        logic, volumeNode, expected = self._buildSubject(withImage=False)
        self.assertIsNone(logic.imageVolumeNode)
        self.assertIsNotNone(volumeNode, "a volume stands in for the image")
        self.assertEqual(slicer.util.arrayFromVolume(volumeNode).shape, expected.shape)
        self.assertFalse(slicer.util.arrayFromVolume(volumeNode).any(), "and it is blank")
        self.assertEqual(logic.segmentLabelValues(), {"SKULL": 100000000, "FEMUR_LEFT": 710000001})

        # The left femur was the right one, and part of the skull is not bone.
        segmentation = logic.segmentationNode.GetSegmentation()
        segmentation.GetSegment(segmentation.GetSegmentIdBySegmentName("FEMUR_LEFT")).SetName("FEMUR_RIGHT")
        skull = (expected == 100000000).astype(np.uint8)
        skull[2:5, 2:6, 2:4] = 0
        slicer.util.updateSegmentBinaryLabelmapFromArray(
            skull, logic.segmentationNode, segmentation.GetSegmentIdBySegmentName("SKULL"), volumeNode
        )

        path = Path(tempfile.mkdtemp()) / f"reviewed{SEGMENTATION_SUFFIX}"
        self.assertEqual(logic.exportReviewedSegmentation(path), {"SKULL": 100000000, "FEMUR_RIGHT": 710000002})
        image, values, _segments = self._readBoneHubFile(path)
        wanted = np.where(expected == 710000001, 710000002, np.where(skull > 0, 100000000, 0))
        self.assertTrue(np.array_equal(values, wanted), "every voxel has the label it was given")
        self._assertSameGridAsTheImage(image, self.imagePath)
        self._assertSameGridAsTheImage(image, self.segmentationPath)
        self.delayDisplay("Written back on the image's grid, without the image")

    def test_ASubjectIsSegmentedFromScratchOnItsImage(self):
        """Sent the image only: the editor adds each label, paints it, and it goes back on the image's grid."""
        self.delayDisplay("Segmenting from scratch")
        logic, volumeNode, expected = self._buildSubject(withSegmentation=False)
        self.assertIs(volumeNode, logic.imageVolumeNode, "the image is the grid")
        self.assertIsNotNone(logic.segmentationNode, "an empty segmentation waits to be painted")
        self.assertEqual(logic.segmentLabelValues(), {})
        with self.assertRaises(RuntimeError):
            logic.exportReviewedSegmentation(Path(tempfile.mkdtemp()) / f"empty{SEGMENTATION_SUFFIX}")

        for name, value in (("SKULL", 100000000), ("FEMUR_LEFT", 710000001)):
            segmentId = logic.addEmptySegment(name)
            painted = (expected == value).astype(np.uint8)
            slicer.util.updateSegmentBinaryLabelmapFromArray(painted, logic.segmentationNode, segmentId, volumeNode)

        path = Path(tempfile.mkdtemp()) / f"reviewed{SEGMENTATION_SUFFIX}"
        self.assertEqual(logic.exportReviewedSegmentation(path), {"SKULL": 100000000, "FEMUR_LEFT": 710000001})
        image, values, _segments = self._readBoneHubFile(path)
        self.assertTrue(np.array_equal(values, expected), "every voxel has the label it was painted with")
        self._assertSameGridAsTheImage(image, self.imagePath)
        self.delayDisplay("Painted from scratch, written on the image's grid")

    def test_ASubjectNeedsItsImageOrItsSegmentation(self):
        """A subject sent neither is not loaded, and the last subject goes all the same."""
        self.delayDisplay("Neither image nor segmentation")
        logic, volumeNode, _expected = self._buildSubject()
        lastSegmentation = logic.segmentationNode
        with self.assertRaises(RuntimeError):
            logic.loadSubject({"subject_key": "001_000002"}, None, None)
        self.assertIsNone(logic.segmentationNode)
        self.assertIsNone(logic.referenceVolumeNode)
        self.assertIsNone(logic.imageVolumeNode)
        self.assertFalse(slicer.mrmlScene.IsNodePresent(lastSegmentation), "the last subject's segmentation is gone")
        self.assertFalse(slicer.mrmlScene.IsNodePresent(volumeNode))
        self.assertEqual(logic.segmentLabelValues(), {})

    def test_ANewSubjectStartsFromAnEmptyScene(self):
        """Nothing of the last subject may stay under the next one and pass for part of it."""
        self.delayDisplay("An empty scene for every subject")
        logic, _volumeNode, _expected = self._buildSubject()
        lastSubject = [logic.segmentationNode, logic.imageVolumeNode]
        # What an editor or a failed load can leave behind: a note, and a volume nobody tracks.
        leftovers = [
            slicer.mrmlScene.AddNewNodeByClass("vtkMRMLMarkupsFiducialNode", "a note on 001_000001"),
            slicer.mrmlScene.AddNewNodeByClass("vtkMRMLScalarVolumeNode", "001_000001 lost track of"),
        ]

        logic.loadSubject({"subject_key": "001_000002"}, self.segmentationPath, self.imagePath)
        for node in lastSubject + leftovers:
            self.assertFalse(slicer.mrmlScene.IsNodePresent(node), f"'{node.GetName()}' outlived its subject")
        self.assertEqual(slicer.mrmlScene.GetNodesByClass("vtkMRMLSegmentationNode").GetNumberOfItems(), 1)
        self.assertEqual(slicer.mrmlScene.GetNodesByClass("vtkMRMLScalarVolumeNode").GetNumberOfItems(), 1)
        self.assertEqual(slicer.mrmlScene.GetNodesByClass("vtkMRMLMarkupsNode").GetNumberOfItems(), 0)

    def test_UnknownSegmentsAreRefused(self):
        """A segment that is not a BoneHub label must be caught here, not by the server."""
        self.delayDisplay("Unknown segments")
        logic, _volumeNode, _expected = self._buildSubject()
        logic.segmentationNode.GetSegmentation().AddEmptySegment("MY_SCRATCH_SEGMENT", "MY_SCRATCH_SEGMENT")
        self.assertIsNone(logic.segmentLabelValues()["MY_SCRATCH_SEGMENT"])
        with self.assertRaises(RuntimeError):
            logic.exportReviewedSegmentation(Path(slicer.app.temporaryPath) / f"refused{SEGMENTATION_SUFFIX}")
        self.delayDisplay("Refused, as it should be")

    # ---------------------------------------------------------------- helpers
    def _connect(self, **ping):
        """A session connected to a fake server, which answers ``/ping`` with :data:`PING`
        changed by ``ping``, and ``/labels`` with :data:`LABELS_PAYLOAD`."""
        from BoneHubQualityCheckLib import session as sessionModule

        pingPayload = {**self.PING, **ping}
        labelsPayload = dict(self.LABELS_PAYLOAD)

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.timeout = kwargs.get("timeout")

            def ping(self):
                return dict(pingPayload)

            def labels(self):
                return dict(labelsPayload)

        original = sessionModule.BoneHubQCClient
        sessionModule.BoneHubQCClient = FakeClient
        try:
            session = BoneHubQCSession()
            session.connect("http://server", "bhqc_key")
        finally:
            sessionModule.BoneHubQCClient = original
        return session

    def _buildSubject(self, withImage=True, withSegmentation=True, names=None):
        """A small subject in the scene: a BoneHub segmentation of two labels, and its image.

        The image is oblique, so a mix-up between Slicer's RAS and the files' LPS, or of the
        axis order, shows. Both files are always written, as the server keeps them, but only
        loaded ``withImage`` and ``withSegmentation``. ``names`` renames segments by number in
        the file, leaving their BoneHubValue tags as they are. Returns the logic, its
        reference volume, and the label value of each voxel.
        """
        import SimpleITK as sitk

        folder = Path(tempfile.mkdtemp())
        shape = (12, 14, 16)
        expected = np.zeros(shape, dtype=np.int64)
        expected[2:5, 2:6, 2:7] = 100000000  # SKULL
        expected[7:10, 8:12, 9:14] = 710000001  # FEMUR_LEFT

        image = sitk.GetImageFromArray(np.zeros(shape, dtype=np.int16))
        image.SetSpacing((0.8, 0.9, 1.3))
        image.SetOrigin((13.5, -21.25, -4.75))
        cos, sin = np.cos(0.3), np.sin(0.3)
        image.SetDirection((cos, -sin, 0.0, sin, cos, 0.0, 0.0, 0.0, 1.0))
        imagePath = folder / "001_000001.nii.gz"
        sitk.WriteImage(image, str(imagePath))

        # Written as the dataset's converters write it, on the grid of the image as read back.
        reference = sitk.ReadImage(str(imagePath))
        numbers = np.zeros(shape, dtype=np.uint8)
        numbers[expected == 100000000] = 1
        numbers[expected == 710000001] = 2
        segmentationPath = folder / f"001_000001{SEGMENTATION_SUFFIX}"
        geometry = (reference.GetOrigin(), reference.GetSpacing(), reference.GetDirection())
        write_segmentation(numbers, [("SKULL", 100000000), ("FEMUR_LEFT", 710000001)], geometry, segmentationPath)
        if names:
            mask = sitk.ReadImage(str(segmentationPath))
            for number, name in names.items():
                mask.SetMetaData(f"Segment{number - 1}_Name", name)
                mask.SetMetaData(f"Segment{number - 1}_ID", name)
            sitk.WriteImage(mask, str(segmentationPath), useCompression=True)

        logic = BoneHubQualityCheckLogic()
        logic.session.labels = LabelMap(self.LABELS)
        logic.loadSubject(
            {"subject_key": "001_000001"},
            segmentationPath if withSegmentation else None,
            imagePath if withImage else None,
        )
        self.imagePath = imagePath
        self.segmentationPath = segmentationPath
        return logic, logic.referenceVolumeNode, expected

    def _readBoneHubFile(self, path):
        """The voxels as label values, and each segment's name and tags, read from the header
        the way the dataset's reader does -- independently of the code under test."""
        import SimpleITK as sitk

        image = sitk.ReadImage(str(path))
        numbers = sitk.GetArrayFromImage(image)
        segments = {}
        i = 0
        while image.HasMetaDataKey(f"Segment{i}_LabelValue"):
            tagText = image.GetMetaData(f"Segment{i}_Tags")
            tags = dict(tag.split(":", 1) for tag in tagText.split("|") if ":" in tag)
            segments[int(image.GetMetaData(f"Segment{i}_LabelValue"))] = (image.GetMetaData(f"Segment{i}_Name"), tags)
            i += 1
        values = np.zeros(numbers.shape, dtype=np.int64)
        for number, (_name, tags) in segments.items():
            values[numbers == number] = int(tags[VALUE_TAG])
        self.assertEqual(set(np.unique(numbers)) - {0}, set(segments), "every painted number is in the header")
        return image, values, segments

    def _assertSameGridAsTheImage(self, image, imagePath):
        """The server refuses an upload whose voxel grid differs from the image."""
        import SimpleITK as sitk

        reference = sitk.ReadImage(str(imagePath))
        self.assertEqual(image.GetSize(), reference.GetSize())
        for actual, expected in (
            (image.GetOrigin(), reference.GetOrigin()),
            (image.GetSpacing(), reference.GetSpacing()),
            (image.GetDirection(), reference.GetDirection()),
        ):
            for a, b in zip(actual, expected):
                self.assertAlmostEqual(a, b, places=5)
