"""Editor client for the BoneHub dataset quality-check server.

The module walks an editor through one subject at a time:

1. connect to the server with the editor's API key,
2. lease the next subject and load its segmentation, and its image if the server sends one,
   into the scene,
3. correct the segmentation in the Segment Editor,
4. confirm (the corrected segmentation goes back and its labels are marked "reviewed and
   corrected", status 2) or reject (the dataset is left untouched).

The server gives each account the role of reviewer, editor, or both, and this module works as
an editor. An account that is a reviewer only is refused when connecting and pointed to the
server's review page, where reviewers work.

The segmentation is what an editor corrects, so it is required; the image is not. Without
the image, the segmentation is edited and written back on its own voxel grid, which is the
image's on the server. An account the server sends images only is refused when connecting,
and a subject that comes without a segmentation is not loaded, only offered for rejection.

Segmentations travel in the BoneHub dataset's own format, ``.seg.nrrd``, which 3D Slicer
opens natively with one segment per label. In the scene a segment's name is its label, so
renaming a segment in the Segment Editor relabels it. See ``BoneHubQualityCheckLib`` for the
server-facing half, which has no dependency on Slicer.
"""

import json
import logging
import tempfile
import threading
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
from BoneHubQualityCheckLib.labels import LABEL_STATUS_VALUES, LabelMap, label_color, schema_is_supported, status_text
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
send the verdict back.
<p>The key must belong to an account with the editor role. Reviewers work in the browser
instead, on the server's review page, and the key of an account that is a reviewer only is
refused here.
<p>The segmentation is what you correct, so it must be sent; the image need not be. An
account sent segmentations only edits each one on its own voxel grid, over a blank volume.
An account sent images only is refused, since it would have nothing to correct.
<p>Confirming uploads the reviewed segmentation, which replaces the one in the dataset, and
sets the labels you vouch for in <code>Subject_info_XXX.json</code> to status 2, "reviewed
and corrected". Rejecting changes nothing in the dataset and is only recorded in the audit
trail.
<p>A segment's name is its BoneHub label: rename a segment to relabel it.
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
        # The panel is a narrow dock, so only the label column is allowed to grow; the
        # other three are kept to their contents and explained by their tooltips.
        self.ui.labelsTableWidget.setHorizontalHeaderLabels(
            [_("Label"), _("Value"), _("Dataset"), _("Painted")]
        )
        header = self.ui.labelsTableWidget.horizontalHeader()
        header.setSectionResizeMode(0, qt.QHeaderView.Stretch)
        for column in range(1, 4):
            header.setSectionResizeMode(column, qt.QHeaderView.ResizeToContents)
        for column, tip in enumerate(
            (
                _("BoneHub label name. A segment's name is its label."),
                _("The label's BoneLabelMap value, which the segmentation file's header records."),
                _("The label's status in Subject_info today: 0 not available, 1 not reviewed, 2 reviewed."),
                _("Whether this label has a segment in the segmentation you are reviewing."),
            )
        ):
            self.ui.labelsTableWidget.horizontalHeaderItem(column).setToolTip(tip)

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

        try:
            handout = self.runWithProgress(_("Asking the server for the next subject..."), session.next_subject)
        except QCClientError as error:
            if error.status_code == 404:
                slicer.util.infoDisplay(
                    _("There is no subject left for you to review.\n\n{detail}").format(detail=str(error)),
                    windowTitle=_("Queue empty"),
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
            slicer.util.infoDisplay(
                _("You are not holding any subject. Use 'Get next subject'."), windowTitle=_("Nothing in hand")
            )
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

        The segmentation is what an editor corrects, so a subject that comes without one is
        not loaded, and all that is left to do with it is to reject it. The image is optional:
        without it, the segmentation is edited on its own voxel grid.
        """
        session = self.logic.session
        key = handout.get("subject_key", "")
        # Whatever happens next, the last subject's segmentation is not this one's, and must
        # not be what confirming this one uploads.
        self.logic.clearScene()
        self.ui.labelsTableWidget.setRowCount(0)  # so the new subject does not inherit ticks
        self.ui.commentTextEdit.plainText = ""
        self.setStatus(self.ui.submitStatusLabel, "")

        if not handout.get("has_segmentation"):
            self.ui.commentTextEdit.plainText = _("No segmentation was sent, so there was nothing to correct.")
            self.ui.submitCollapsibleButton.collapsed = False
            self.updateGuiFromSession()
            slicer.util.warningDisplay(
                _("The server sent no segmentation for {key}. 3D Slicer corrects segmentations, so there is "
                  "nothing here to work on.\n\nReject the subject, so that it is not handed to you again. "
                  "Releasing it puts it back in the queue, and you may be given it again.").format(key=key),
                windowTitle=_("No segmentation"),
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

        # With the image, the upload is written on the image's grid whatever grid the stored
        # segmentation is on. Without it, the segmentation's own grid is all there is.
        issue = handout.get("stored_segmentation_issue")
        if imagePath is None and issue and self.logic.segmentationNode is not None:
            slicer.util.warningDisplay(
                _("The server reports a problem with the stored segmentation of {key}:\n\n{issue}\n\nWithout "
                  "the image, 3D Slicer can only write the segmentation back on its own voxel grid, so the "
                  "server will refuse to confirm it. Reject the subject with a comment instead.").format(
                    key=key, issue=issue
                ),
                windowTitle=_("Segmentation off the image's grid"),
            )

    def onExtendLease(self):
        try:
            self.runWithProgress(_("Extending the lease..."), self.logic.session.extend_lease)
        except QCClientError as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Could not extend the lease"))
        self.updateGuiFromSession()

    def onReleaseSubject(self):
        if not slicer.util.confirmYesNoDisplay(
            _("Return {key} to the queue without judging it? Your corrections are lost.").format(
                key=self.logic.session.subject_key
            ),
            windowTitle=_("Release subject"),
        ):
            return
        try:
            self.runWithProgress(_("Releasing the subject..."), self.logic.session.release_subject)
        except QCClientError as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Could not release the subject"))
            return
        self.finishSubject(_("Subject released. It is back in the queue."), ok=True)

    def confirmDiscardingCurrentSubject(self):
        """Warn before walking away from a subject that is still leased."""
        session = self.logic.session
        if not session.has_subject:
            return True
        return slicer.util.confirmYesNoDisplay(
            _("You are still holding {key}, and your corrections to it have not been submitted. "
              "They will be discarded. The subject stays leased to you, and if you are already "
              "at your limit the server will simply hand {key} back. Continue?").format(
                key=session.subject_key
            ),
            windowTitle=_("Subject still in hand"),
        )

    # ---------------------------------------------------------------- review
    def populateAddLabelComboBox(self):
        self.ui.addLabelComboBox.clear()
        for name in self.logic.session.labels.sorted_names():
            self.ui.addLabelComboBox.addItem(name)

    def updateLabelsTable(self):
        """Show every label of this subject: what the dataset says, and what is in the scene.

        Ticks the editor already made are kept, so refreshing after an edit in the
        Segment Editor does not undo their choices.
        """
        table = self.ui.labelsTableWidget
        # A label already in the table keeps whatever the editor did with it; a label that
        # has just appeared -- one they added and painted -- starts ticked, like the labels
        # the subject arrived with.
        previouslyChecked = self.checkedLabels() if table.rowCount else None
        previouslyListed = self._checkableLabels() if table.rowCount else set()

        session = self.logic.session
        inSegmentation = self.logic.segmentLabelValues()
        inDataset = dict(session.handout.get("segmentation_labels") or {})

        def sortKey(name):
            value = inSegmentation.get(name)
            if value is None:
                value = session.labels.value_of(name)
            return (value is None, value or 0, name)

        names = sorted(set(inDataset) | set(inSegmentation), key=sortKey)
        table.setRowCount(len(names))
        for row, name in enumerate(names):
            present = name in inSegmentation
            value = inSegmentation.get(name) if present else session.labels.value_of(name)

            item = qt.QTableWidgetItem(name)
            if present:
                item.setFlags(qt.Qt.ItemIsUserCheckable | qt.Qt.ItemIsEnabled | qt.Qt.ItemIsSelectable)
                known = previouslyChecked is not None and name in previouslyListed
                ticked = name in previouslyChecked if known else True
                item.setCheckState(qt.Qt.Checked if ticked else qt.Qt.Unchecked)
                item.setToolTip(name)  # the column is too narrow for the longer names
            else:
                item.setFlags(qt.Qt.ItemIsEnabled)
                item.setToolTip(_("{name}: not in the segmentation, so there is nothing to confirm.").format(name=name))
            if value is not None:
                red, green, blue = label_color(value)
                item.setForeground(qt.QBrush(qt.QColor.fromRgbF(red * 0.6, green * 0.6, blue * 0.6)))
            table.setItem(row, 0, item)

            table.setItem(row, 1, _readOnlyItem("?" if value is None else str(value)))
            if name in inDataset:
                datasetItem = _readOnlyItem(status_text(inDataset[name]))
                datasetItem.setToolTip(session.labels.describe(inDataset[name]))
            else:
                datasetItem = _readOnlyItem(_("not listed"))
                datasetItem.setToolTip(_("Subject_info does not mention this label yet."))
            table.setItem(row, 2, datasetItem)
            table.setItem(row, 3, _readOnlyItem(_("yes") if present else _("no")))

        unknown = sorted(name for name, value in inSegmentation.items() if value is None)
        if unknown:
            self.setStatus(
                self.ui.labelsSummaryLabel,
                _("Not BoneHub labels, so the upload would be refused: {names}. Rename or delete them.").format(
                    names=", ".join(unknown)
                ),
                ok=False,
            )
        else:
            self.setStatus(
                self.ui.labelsSummaryLabel,
                _("{n} label(s) in the segmentation; ticked ones are marked 'reviewed' (status 2).").format(
                    n=len(inSegmentation)
                )
                if inSegmentation
                else "",
            )

    def checkedLabels(self):
        """Names the editor vouches for, in table order."""
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

    def onAddSegment(self):
        name = self.ui.addLabelComboBox.currentText.strip()
        try:
            self.logic.addEmptySegment(name)
        except Exception as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Could not add the segment"))
            return
        self.updateLabelsTable()

    def onOpenSegmentEditor(self):
        try:
            self.logic.openSegmentEditor()
        except Exception as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Could not open the Segment Editor"))

    # ---------------------------------------------------------------- verdict
    def onConfirm(self):
        session = self.logic.session
        # Read the scene again first: the verdict must be about the segments that are there
        # now, not about a row for a segment deleted in the Segment Editor meanwhile.
        self.updateLabelsTable()
        confirmed = self.checkedLabels()
        if not confirmed:
            slicer.util.errorDisplay(
                _("Tick at least one label you vouch for, or reject the subject instead."),
                windowTitle=_("Nothing to confirm"),
            )
            return

        try:
            slicer.app.setOverrideCursor(qt.Qt.WaitCursor)
            written = self.logic.exportReviewedSegmentation(session.reviewed_path())
        except Exception as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Could not write the reviewed segmentation"))
            return
        finally:
            slicer.app.restoreOverrideCursor()

        missing = [name for name in confirmed if name not in written]
        if missing:
            slicer.util.errorDisplay(
                _("These labels are ticked but have no voxels in the segmentation -- empty, or "
                  "covered entirely by an overlapping segment -- so they cannot be confirmed: "
                  "{names}").format(names=", ".join(missing)),
                windowTitle=_("Empty labels"),
            )
            return

        extra = [name for name in written if name not in confirmed]
        message = _("Confirm {key}?\n\n{n} label(s) will be marked 'reviewed and corrected' (status 2) "
                    "and the uploaded segmentation will replace the one in the dataset.").format(
            key=session.subject_key, n=len(confirmed)
        )
        if extra:
            message += _("\n\nAlso in the segmentation but not ticked, so left as they are: {names}.").format(
                names=", ".join(extra)
            )
        if not slicer.util.confirmYesNoDisplay(message, windowTitle=_("Confirm subject")):
            return

        path = session.reviewed_path()
        comment = self.ui.commentTextEdit.plainText

        def upload():
            return session.submit(True, segmentation_path=path, confirmed_labels=confirmed, comment=comment)

        try:
            result = self.runWithProgress(_("Uploading the reviewed segmentation..."), upload)
        except QCClientError as error:
            slicer.util.errorDisplay(
                _("The server refused the submission, so you are still holding the subject:\n\n{detail}").format(
                    detail=str(error)
                ),
                windowTitle=_("Submission refused"),
            )
            return

        self.finishSubject(result.get("message", _("Confirmed.")), ok=True, result=result)

    def onReject(self):
        session = self.logic.session
        comment = self.ui.commentTextEdit.plainText.strip()
        if not comment and not slicer.util.confirmYesNoDisplay(
            _("Reject {key} without saying what is wrong with it? A comment is what makes the "
              "audit trail useful.").format(key=session.subject_key),
            windowTitle=_("No comment"),
        ):
            self.ui.commentTextEdit.setFocus()
            return
        if not slicer.util.confirmYesNoDisplay(
            _("Reject {key}? Nothing in the dataset is changed; only the audit trail records it.").format(
                key=session.subject_key
            ),
            windowTitle=_("Reject subject"),
        ):
            return

        def upload():
            return session.submit(False, comment=comment)

        try:
            result = self.runWithProgress(_("Sending the rejection..."), upload)
        except QCClientError as error:
            slicer.util.errorDisplay(str(error), windowTitle=_("Submission refused"))
            return

        self.finishSubject(result.get("message", _("Rejected.")), ok=True, result=result)

    def finishSubject(self, message, ok=True, result=None):
        """Common tail of submitting, rejecting and releasing."""
        session = self.logic.session
        if not self.ui.keepFilesCheckBox.checked:
            session.discard_files(session.finished_folder)
        self.logic.clearScene()
        self.ui.labelsTableWidget.setRowCount(0)
        self.ui.commentTextEdit.plainText = ""
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
        # A subject in hand whose segmentation is not in the scene can only be rejected.
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

        if holding:
            handout = session.handout
            text = _("{key}  (dataset {dataset}, subject {subject})").format(
                key=handout.get("subject_key", ""),
                dataset=handout.get("dataset_id", ""),
                subject=handout.get("subject_id", ""),
            )
            if not handout.get("has_segmentation"):
                text += "\n" + _("No segmentation was sent: nothing to correct.")
            elif not handout.get("has_image"):
                text += "\n" + _("Segmentation only: no image was sent.")
            self.ui.subjectKeyLabel.text = text
            self.ui.expiresLabel.text = _("Lease expires at {when}").format(when=handout.get("expires_at", "?"))
            self.ui.subjectInfoTextBrowser.html = _subjectInfoHtml(handout)
        else:
            self.ui.subjectKeyLabel.text = _("No subject in hand.")
            self.ui.expiresLabel.text = ""
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
        """Remove what the last review put in the scene, leaving the rest of it alone."""
        for node in (self.segmentationNode, self.referenceVolumeNode, self.imageVolumeNode):
            if node is not None and slicer.mrmlScene.IsNodePresent(node):
                slicer.mrmlScene.RemoveNode(node)
        self.imageVolumeNode = None
        self.referenceVolumeNode = None
        self.segmentationNode = None

    def loadSubject(self, handout, segmentationPath, imagePath=None):
        """Load one subject's BoneHub segmentation, one segment per label, and its image if sent.

        The segmentation is what the editor corrects, so it is required. The image is not:
        without it, the segmentation is shown, edited and written back on its own voxel grid,
        over a blank volume standing in for the image.
        """
        self.clearScene()
        subjectKey = handout.get("subject_key", "subject")

        try:
            self.segmentationNode = self.importSegmentation(segmentationPath, subjectKey)
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
        if segmentationPath is None:
            raise RuntimeError(f"{subjectKey} has no segmentation, and the segmentation is what is reviewed.")
        segmentationNode = slicer.util.loadSegmentation(str(segmentationPath), {"name": f"{subjectKey}_segmentation"})
        if segmentationNode is None:
            raise RuntimeError(f"'{segmentationPath}' could not be loaded as a segmentation.")
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
    accepts of the server, and the round trip of a segmentation through the scene -- loaded
    from a BoneHub ``.seg.nrrd``, with or without its image, edited, and written back --
    which is where the dataset could silently be corrupted.
    """

    #: Labels of BoneHub data schema 0.3, with their values.
    LABELS = {"SKULL": 100000000, "FEMUR_LEFT": 710000001, "FEMUR_RIGHT": 710000002}

    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        self.setUp()
        self.test_LabelColoursFollowTheDataset()
        self.test_LabelMapReadsTheServerPayload()
        self.test_AnAccountSentImagesOnlyIsRefused()
        self.test_SegmentationSurvivesTheRoundTrip()
        self.test_RenamingASegmentRelabelsIt()
        self.test_TagsInTheFileNameTheSegments()
        self.test_AMissingBoneCanBeAdded()
        self.test_ASegmentationIsEditedWithoutItsImage()
        self.test_TheSegmentationIsRequired()
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
        # A server of another schema would hand out masks this extension cannot read.
        self.assertTrue(schema_is_supported("0.3.0"))
        self.assertFalse(schema_is_supported("0.2.0"))
        self.assertFalse(schema_is_supported(None), "servers before schema 0.3 report none")

    def test_AnAccountSentImagesOnlyIsRefused(self):
        """The segmentation is what an editor corrects; the image is optional."""
        self.delayDisplay("What the account is sent")
        from BoneHubQualityCheckLib import session as sessionModule

        labels = dict(self.LABELS)

        class FakeClient:
            """Answers as a schema 0.3 server does, for an account of ``dataAccess``."""

            dataAccess = None

            def __init__(self, *args, **kwargs):
                self.timeout = kwargs.get("timeout")

            def ping(self):
                return {"user": "editor", "schema_version": "0.3.0", "data_access": FakeClient.dataAccess}

            def labels(self):
                return {"schema_version": "0.3.0", "label_name_to_value": labels}

        original = sessionModule.BoneHubQCClient
        sessionModule.BoneHubQCClient = FakeClient
        try:
            # None stands for a server that does not say.
            for access in ("image_and_segmentation", "segmentation", None):
                FakeClient.dataAccess = access
                session = BoneHubQCSession()
                session.connect("http://server", "bhqc_key")
                self.assertTrue(session.connected, f"an account sent '{access}' can edit")

            FakeClient.dataAccess = "image"
            session = BoneHubQCSession()
            with self.assertRaises(QCClientError):
                session.connect("http://server", "bhqc_key")
            self.assertFalse(session.connected, "an account sent the image only has nothing to correct")
        finally:
            sessionModule.BoneHubQCClient = original

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

    def test_TheSegmentationIsRequired(self):
        """A subject is not loaded without its segmentation, even with its image, and the last one goes."""
        self.delayDisplay("No segmentation")
        logic, volumeNode, _expected = self._buildSubject()
        lastSegmentation = logic.segmentationNode
        with self.assertRaises(RuntimeError):
            logic.loadSubject({"subject_key": "001_000002"}, None, self.imagePath)
        self.assertIsNone(logic.segmentationNode)
        self.assertIsNone(logic.referenceVolumeNode)
        self.assertIsNone(logic.imageVolumeNode)
        self.assertFalse(slicer.mrmlScene.IsNodePresent(lastSegmentation), "the last subject's segmentation is gone")
        self.assertFalse(slicer.mrmlScene.IsNodePresent(volumeNode))
        self.assertEqual(logic.segmentLabelValues(), {})

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
    def _buildSubject(self, withImage=True, names=None):
        """A small subject in the scene: a BoneHub segmentation of two labels, and its image.

        The image is oblique, so a mix-up between Slicer's RAS and the files' LPS, or of the
        axis order, shows. It is always written, as the server keeps it, but only loaded
        ``withImage``. ``names`` renames segments by number in the file, leaving their
        BoneHubValue tags as they are. Returns the logic, its reference volume, and the
        label value of each voxel.
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
        logic.loadSubject({"subject_key": "001_000001"}, segmentationPath, imagePath if withImage else None)
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
