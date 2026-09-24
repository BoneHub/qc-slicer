"""Panel-level tests for BoneHub Quality Check, run against a real module widget.

The tests in ``BoneHubQualityCheck.py`` itself cover the logic that touches the dataset, and
how the quality check of a subject is put into words. These cover the panel: that the .ui
file still carries every widget the code reaches for, that the sections stay locked until
there is something to do, that the editor sees why a subject came to them -- the labels
rejected and why, the bones reported missing, the administrator's requests, the history with
its comments, and whose correction the segmentation is -- that a missing bone is ready to
add, that a subject sent without its segmentation is painted from scratch and one sent
without its image is corrected, while one sent neither can only be rejected, that the tick
boxes are there only when the server takes an editor's word and behave across a refresh, and
what confirming, rejecting and an empty queue tell the editor, and send. They need a module
widget, so they only run in a Slicer with a main window.
"""

import qt
import slicer
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleTest

import BoneHubQualityCheck
from BoneHubQualityCheckLib import QCClientError
from BoneHubQualityCheckLib.labels import LabelMap

#: Every widget the module code reaches for through ``self.ui``.
EXPECTED_WIDGETS = [
    "serverUrlLineEdit", "apiKeyLineEdit", "showKeyCheckBox", "rememberKeyCheckBox",
    "connectButton", "connectionStatusLabel", "serverCollapsibleButton",
    "nextSubjectButton", "reloadSubjectButton", "subjectKeyLabel", "expiresLabel",
    "caseTextBrowser", "subjectInfoTextBrowser", "extendLeaseButton", "releaseSubjectButton",
    "subjectCollapsibleButton", "labelsTableWidget", "labelsSummaryLabel",
    "refreshLabelsButton", "selectAllLabelsButton", "selectNoLabelsButton",
    "addLabelComboBox", "addSegmentButton", "segmentEditorButton",
    "reviewCollapsibleButton", "commentTextEdit", "confirmButton", "rejectButton",
    "autoNextCheckBox", "submitStatusLabel", "submitCollapsibleButton",
    "workspacePathLineEdit", "timeoutSpinBox", "keepFilesCheckBox", "openWorkspaceButton",
    "advancedCollapsibleButton",
]

#: BoneHub data schema 0.3 values of the labels these tests use.
SKULL, FEMUR_LEFT, FEMUR_RIGHT, TIBIA_LEFT = 100000000, 710000001, 710000002, 730000001


def label(name, value, state, painted=True, reason=None, by=None, edited_by=None, status=1):
    """One label of a handout, as a quality-check server of version 0.4 sends it."""
    return {
        "name": name, "value": value, "dataset_status": status, "state": state, "painted": painted,
        "reason": reason, "by": by, "edited_by": edited_by,
    }


#: A bone a reviewer, rita, reported missing from the two-label subject.
TIBIA_MISSING = label("TIBIA_LEFT", TIBIA_LEFT, "rejected", painted=False, reason="missing", by="rita", status=None)


def handout(**changes):
    """The handout of the subject ``subjectInScene`` builds -- a skull and a left femur -- as a
    server of version 0.4 sends it to an editor: rita rejected the skull and accepted the femur."""
    fields = {
        "assignment_id": "a1", "dataset_id": 1, "subject_id": 1, "subject_key": "001_000001",
        "expires_at": "2026-09-25T14:03:05Z", "role": "editor", "stage": "edit",
        "data_access": "image_and_segmentation", "has_image": True, "has_segmentation": True,
        "segmentation_source": "dataset", "segmentation_labels": {"SKULL": 1, "FEMUR_LEFT": 1},
        "labels": [
            label("SKULL", SKULL, "rejected", reason="quality", by="rita"),
            label("FEMUR_LEFT", FEMUR_LEFT, "accepted", by="rita"),
        ],
        "requests": [],
        "history": [
            {
                "at": "2026-09-24T14:03:05Z", "by": "rita", "role": "reviewer", "action": "review", "stage": "edit",
                "comment": "The skull is cut off at the top.",
                "details": {"accepted": ["FEMUR_LEFT"], "rejected": {"SKULL": "quality"}, "missing": []},
            },
        ],
        "stored_segmentation_issue": None, "subject_info": {}, "dataset_info": {},
    }
    fields.update(changes)
    return fields


class BoneHubQualityCheckModuleTest(ScriptedLoadableModuleTest):
    """Uses ScriptedLoadableModuleTest, so it runs from Reload and Test and from ctest."""

    def setUp(self):
        slicer.mrmlScene.Clear()

    def runTest(self):
        if slicer.util.mainWindow() is None:
            self.delayDisplay("No main window, so the panel cannot be built; skipping.")
            return
        self.setUp()
        self.test_PanelHasEveryWidgetTheCodeUses()
        self.test_SectionsAreLockedUntilThereIsSomethingToDo()
        self.test_TheKeyIsMaskedUnlessAsked()
        self.test_LabelPickerIsInAnatomicalOrder()
        self.test_TheEditorSeesWhyTheSubjectCameToThem()
        self.test_AMissingBoneIsReadyToAdd()
        self.test_ASubjectWithoutASegmentationIsSegmentedFromScratch()
        self.test_ASubjectSentNothingCanOnlyBeRejected()
        self.test_ASubjectWithoutAnImageCanBeCorrected()
        self.test_TicksAreThereOnlyWhenTheyCount()
        self.test_TicksSurviveARefresh()
        self.test_ConfirmingUploadsACorrectionThatWaitsOnTheServer()
        self.test_RejectingSendsTheSubjectToTheAdministrator()
        self.test_AnEmptyQueueSpeaksOfCorrecting()

    # ---------------------------------------------------------------- helpers
    def widget(self):
        slicer.util.selectModule("BoneHubQualityCheck")
        return slicer.modules.bonehubqualitycheck.widgetRepresentation().self()

    def subjectInScene(self, widget, withImage=True, editsNeedReview=True, **changes):
        """Put the two-label subject in the scene and hand it to the panel, as held from a
        server that sends the labels an editor corrects back to a reviewer, or not."""
        logic = BoneHubQualityCheck.BoneHubQualityCheckTest()
        logic.setUp()
        builtLogic, _volume, _expected = logic._buildSubject(withImage=withImage)
        self.imagePath = logic.imagePath
        self.segmentationPath = logic.segmentationPath
        widget.logic = builtLogic
        session = builtLogic.session
        session.server_info = {"user": "carol", "role": "editor", "edits_need_review": editsNeedReview}
        session.labels = LabelMap(
            {"SKULL": SKULL, "FEMUR_LEFT": FEMUR_LEFT, "FEMUR_RIGHT": FEMUR_RIGHT, "TIBIA_LEFT": TIBIA_LEFT}
        )
        session.handout = handout(has_image=withImage, **changes)
        widget.populateAddLabelComboBox()
        widget.ui.reviewCollapsibleButton.collapsed = False
        widget.ui.labelsTableWidget.setRowCount(0)  # the last subject's ticks are not this one's
        widget.updateGuiFromSession()
        return builtLogic

    def rows(self, widget):
        """``{label: row}`` of the labels table."""
        table = widget.ui.labelsTableWidget
        return {table.item(row, 0).text(): row for row in range(table.rowCount)}

    def column(self, widget, column):
        """``{label: text}`` of one column of the labels table."""
        table = widget.ui.labelsTableWidget
        return {table.item(row, 0).text(): table.item(row, column).text() for row in range(table.rowCount)}

    def submitted(self, widget, verdict, result):
        """Give the verdict with ``verdict()`` against a server that answers ``result``; return
        what was sent and the questions the editor was asked, which are all answered yes."""
        session = widget.logic.session
        sent, asked = [], []

        def submit(confirmed, **kwargs):
            sent.append((confirmed, kwargs))
            return dict(result)

        session.submit = submit
        original = slicer.util.confirmYesNoDisplay
        slicer.util.confirmYesNoDisplay = lambda text, **kwargs: asked.append(text) or True
        autoNext = widget.ui.autoNextCheckBox.checked
        widget.ui.autoNextCheckBox.checked = False
        try:
            verdict()
        finally:
            slicer.util.confirmYesNoDisplay = original
            widget.ui.autoNextCheckBox.checked = autoNext
        return sent, asked

    # ------------------------------------------------------------------ tests
    def test_PanelHasEveryWidgetTheCodeUses(self):
        """A widget renamed in the .ui but not in the code fails here, not in an editor's face."""
        self.delayDisplay("Panel widgets")
        ui = self.widget().ui
        missing = [name for name in EXPECTED_WIDGETS if not hasattr(ui, name)]
        self.assertEqual(missing, [], f"the .ui file is missing: {missing}")

    def test_SectionsAreLockedUntilThereIsSomethingToDo(self):
        self.delayDisplay("Locked sections")
        widget = self.widget()
        widget.logic.session.disconnect()
        widget.updateGuiFromSession()
        self.assertFalse(widget.ui.subjectCollapsibleButton.enabled)
        self.assertFalse(widget.ui.reviewCollapsibleButton.enabled)
        self.assertFalse(widget.ui.submitCollapsibleButton.enabled)

    def test_TheKeyIsMaskedUnlessAsked(self):
        """The key is a credential; it must not sit on screen in a shared lab."""
        self.delayDisplay("API key masking")
        widget = self.widget()
        self.assertEqual(widget.ui.apiKeyLineEdit.echoMode, qt.QLineEdit.Password)
        widget.ui.showKeyCheckBox.checked = True
        self.assertEqual(widget.ui.apiKeyLineEdit.echoMode, qt.QLineEdit.Normal)
        widget.ui.showKeyCheckBox.checked = False
        self.assertEqual(widget.ui.apiKeyLineEdit.echoMode, qt.QLineEdit.Password)

    def test_LabelPickerIsInAnatomicalOrder(self):
        self.delayDisplay("Label picker")
        widget = self.widget()
        widget.logic.session.labels = LabelMap({"FEMUR_LEFT": FEMUR_LEFT, "SKULL": SKULL})
        widget.populateAddLabelComboBox()
        self.assertEqual(widget.ui.addLabelComboBox.count, 2)
        self.assertEqual(widget.ui.addLabelComboBox.itemText(0), "SKULL")

    def test_TheEditorSeesWhyTheSubjectCameToThem(self):
        """The labels rejected and why, the bones reported missing, the administrator's word, the
        history with its comments, and whose correction the segmentation is."""
        self.delayDisplay("Why the subject came")
        widget = self.widget()
        # rita rejected the skull; eddie corrected it; rita rejected it again and reported the left
        # tibia missing; the administrator asked for the knee to be checked as well.
        logic = self.subjectInScene(
            widget,
            segmentation_source="staged",
            labels=[
                label("SKULL", SKULL, "rejected", reason="quality", by="rita", edited_by="eddie"),
                label("FEMUR_LEFT", FEMUR_LEFT, "accepted", by="rita"),
                TIBIA_MISSING,
            ],
            requests=[{"by": "admin", "role": "admin", "at": "2026-09-24T17:00:00Z", "comment": "Check the knee too."}],
            history=handout()["history"] + [
                {
                    "at": "2026-09-24T15:10:00Z", "by": "eddie", "role": "editor", "action": "edit", "stage": "review",
                    "comment": "Redrew the top of the skull.", "details": {"edited": ["SKULL"], "removed": []},
                },
                {
                    "at": "2026-09-24T16:00:00Z", "by": "rita", "role": "reviewer", "action": "review", "stage": "edit",
                    "comment": None,
                    "details": {"accepted": [], "rejected": {"SKULL": "quality", "TIBIA_LEFT": "missing"},
                                "missing": ["TIBIA_LEFT"]},
                },
                {
                    "at": "2026-09-24T17:00:00Z", "by": "admin", "role": "admin", "action": "return", "stage": "edit",
                    "comment": "Check the knee too.", "details": {"to": "edit"},
                },
            ],
        )

        why = widget.ui.caseTextBrowser.toPlainText()
        for words in (
            "SKULL, rejected by rita: needs correction.",
            "TIBIA_LEFT, reported missing by rita.",
            'The administrator sent it back to the editors: "Check the knee too."',
            "rita (reviewer): accepted FEMUR_LEFT; rejected SKULL (needs correction)",
            '"The skull is cut off at the top."',
            "eddie (editor): corrected SKULL",
            '"Redrew the top of the skull."',
            "rita (reviewer): rejected SKULL (needs correction); reported missing TIBIA_LEFT",
            "the administrator: sent it back to the editors",
            "The segmentation is eddie's correction",
        ):
            self.assertIn(words, why)
        self.assertNotIn("FEMUR_LEFT, rejected", why, "an accepted label is no reason to correct")

        # The labels table says the same, and the labels sent back stand out.
        self.assertEqual(
            self.column(widget, 1),
            {"SKULL": "rejected: needs correction (rita)", "FEMUR_LEFT": "accepted (rita)", "TIBIA_LEFT": "missing (rita)"},
        )
        table = widget.ui.labelsTableWidget
        tinted = {name for name, row in self.rows(widget).items() if table.item(row, 1).background().style() != qt.Qt.NoBrush}
        self.assertEqual(tinted, {"SKULL", "TIBIA_LEFT"})
        self.assertTrue(table.item(self.rows(widget)["TIBIA_LEFT"], 1).font().bold())
        self.assertEqual(self.column(widget, 3), {"SKULL": "yes", "FEMUR_LEFT": "yes", "TIBIA_LEFT": "no"})
        self.assertIn("Reported missing, still to add: TIBIA_LEFT.", widget.ui.labelsSummaryLabel.text)

        # The dataset's own segmentation is nobody's correction.
        logic.session.handout["segmentation_source"] = "dataset"
        widget.updateGuiFromSession()
        self.assertNotIn("The segmentation is", widget.ui.caseTextBrowser.toPlainText())

    def test_AMissingBoneIsReadyToAdd(self):
        """A bone reported missing is picked for 'Add segment' when the subject is loaded, and a
        click on any label the segmentation lacks picks that one."""
        self.delayDisplay("A missing bone, ready to add")
        widget = self.widget()
        logic = self.subjectInScene(widget)  # the last subject
        sentBack = handout(labels=handout()["labels"] + [TIBIA_MISSING])
        logic.session.handout = sentBack
        logic.session.download_segmentation = lambda: self.segmentationPath
        logic.session.download_image = lambda: self.imagePath
        widget.ui.addLabelComboBox.setCurrentIndex(0)
        widget.downloadAndLoad(sentBack)
        self.assertEqual(widget.ui.addLabelComboBox.currentText, "TIBIA_LEFT", "picked for 'Add segment'")

        widget.onAddSegment()
        self.assertEqual(logic.segmentLabelValues(), {"SKULL": SKULL, "FEMUR_LEFT": FEMUR_LEFT, "TIBIA_LEFT": TIBIA_LEFT})
        self.assertEqual(self.column(widget, 3)["TIBIA_LEFT"], "yes")
        self.assertNotIn("still to add", widget.ui.labelsSummaryLabel.text)

        segmentation = logic.segmentationNode.GetSegmentation()
        segmentation.RemoveSegment(segmentation.GetSegmentIdBySegmentName("FEMUR_LEFT"))
        widget.updateLabelsTable()
        widget.ui.addLabelComboBox.setCurrentIndex(0)
        widget.onLabelCellClicked(self.rows(widget)["FEMUR_LEFT"], 1)
        self.assertEqual(widget.ui.addLabelComboBox.currentText, "FEMUR_LEFT")
        widget.onLabelCellClicked(self.rows(widget)["SKULL"], 1)
        self.assertEqual(widget.ui.addLabelComboBox.currentText, "FEMUR_LEFT", "a label already painted is not picked")

    def test_ASubjectWithoutASegmentationIsSegmentedFromScratch(self):
        """Sent the image only: an empty segmentation on the image, and the panel open to paint it.

        An account sent images only is warned when the server has a segmentation of the
        subject, which it will not let the account replace unseen.
        """
        self.delayDisplay("Subject without a segmentation")
        widget = self.widget()
        logic = self.subjectInScene(widget)  # the last subject, which must not stand in for this one
        # Only the image is downloaded for such a subject, so no server is needed.
        logic.session.download_image = lambda: self.imagePath
        warnings = []
        original = slicer.util.warningDisplay
        slicer.util.warningDisplay = lambda text, **kwargs: warnings.append(text)
        try:
            for access, source, warned in (
                ("image_and_segmentation", None, False),
                ("image", None, False),
                ("image", "dataset", True),
                ("image", "staged", True),
            ):
                sent = handout(
                    assignment_id="a2", subject_key="001_000002", data_access=access, has_image=True,
                    has_segmentation=False, segmentation_source=source, segmentation_labels={}, labels=[], history=[],
                )
                logic.session.handout = sent
                del warnings[:]
                widget.downloadAndLoad(sent)

                self.assertIsNotNone(logic.segmentationNode)
                self.assertEqual(logic.segmentLabelValues(), {}, "nothing of the last subject is left")
                self.assertIs(logic.referenceVolumeNode, logic.imageVolumeNode, "painted on the image")
                self.assertEqual(slicer.mrmlScene.GetNodesByClass("vtkMRMLSegmentationNode").GetNumberOfItems(), 1)
                self.assertTrue(widget.ui.reviewCollapsibleButton.enabled)
                self.assertTrue(widget.ui.confirmButton.enabled)
                self.assertIn("Image only", widget.ui.subjectKeyLabel.text)
                self.assertIn("Add segment", widget.ui.labelsSummaryLabel.text, "the editor is told where to start")
                self.assertEqual(bool(warnings), warned, f"{access}, {source}: {warnings}")
                if warned:
                    self.assertIn("not sent segmentations", warnings[0])
                else:
                    self.assertIn("It has no segmentation yet", widget.ui.caseTextBrowser.toPlainText())
        finally:
            slicer.util.warningDisplay = original

        logic.addEmptySegment("FEMUR_LEFT")
        widget.updateLabelsTable()
        self.assertEqual(self.column(widget, 1), {"FEMUR_LEFT": "added"}, "a label the editor added")

    def test_ASubjectSentNothingCanOnlyBeRejected(self):
        """There is nothing to work on, and the last subject's segmentation must not stand in for it."""
        self.delayDisplay("Subject sent without image or segmentation")
        widget = self.widget()
        logic = self.subjectInScene(widget)
        sent = handout(assignment_id="a2", subject_key="001_000002", has_image=False, has_segmentation=False,
                       segmentation_source=None, segmentation_labels={}, labels=[], history=[])
        logic.session.handout = sent
        warnings = []
        original = slicer.util.warningDisplay
        slicer.util.warningDisplay = lambda text, **kwargs: warnings.append(text)
        try:
            # Nothing is downloaded for such a subject, so no server is needed.
            widget.downloadAndLoad(sent)
        finally:
            slicer.util.warningDisplay = original

        self.assertIsNone(logic.segmentationNode)
        self.assertEqual(slicer.mrmlScene.GetNodesByClass("vtkMRMLSegmentationNode").GetNumberOfItems(), 0)
        self.assertEqual(widget.ui.labelsTableWidget.rowCount, 0)
        self.assertFalse(widget.ui.reviewCollapsibleButton.enabled)
        self.assertFalse(widget.ui.confirmButton.enabled)
        self.assertTrue(widget.ui.submitCollapsibleButton.enabled)
        self.assertTrue(widget.ui.rejectButton.enabled)
        self.assertNotEqual(widget.ui.commentTextEdit.plainText, "", "the rejection comes with its reason")
        self.assertEqual(len(warnings), 1)
        self.assertIn("sends it to the administrator", warnings[0])

    def test_ASubjectWithoutAnImageCanBeCorrected(self):
        """Sent the segmentation only: the panel is open, and the Segment Editor has a volume to edit with."""
        self.delayDisplay("Subject without an image")
        widget = self.widget()
        logic = self.subjectInScene(widget, withImage=False)
        self.assertTrue(widget.ui.reviewCollapsibleButton.enabled)
        self.assertTrue(widget.ui.confirmButton.enabled)
        self.assertIn("no image", widget.ui.subjectKeyLabel.text)

        logic.openSegmentEditor()
        try:
            editor = slicer.modules.segmenteditor.widgetRepresentation().self().editor
            self.assertEqual(editor.segmentationNode().GetID(), logic.segmentationNode.GetID())
            self.assertEqual(editor.sourceVolumeNode().GetID(), logic.referenceVolumeNode.GetID())
            editor.setActiveEffectByName("Paint")
            self.assertIsNotNone(editor.activeEffect(), "the editing tools are available")
            editor.setActiveEffect(None)
        finally:
            slicer.util.selectModule("BoneHubQualityCheck")

    def test_TicksAreThereOnlyWhenTheyCount(self):
        """A tick is the editor's word, which the server takes only when corrections need no
        review. Otherwise there is nothing to tick, and nothing is vouched for."""
        self.delayDisplay("Tick boxes only where ticks count")
        widget = self.widget()
        self.subjectInScene(widget, editsNeedReview=True)
        self.assertEqual(widget._checkableLabels(), set())
        self.assertEqual(widget.checkedLabels(), [])
        self.assertFalse(widget.ui.selectAllLabelsButton.visible)
        self.assertFalse(widget.ui.selectNoLabelsButton.visible)
        self.assertIn("go back to a reviewer", widget.ui.labelsSummaryLabel.text)

        self.subjectInScene(widget, editsNeedReview=False)
        self.assertEqual(widget._checkableLabels(), {"SKULL", "FEMUR_LEFT"})
        self.assertEqual(widget.checkedLabels(), ["SKULL", "FEMUR_LEFT"])
        self.assertTrue(widget.ui.selectAllLabelsButton.visible)
        self.assertTrue(widget.ui.selectNoLabelsButton.visible)
        self.assertIn("the ticked ones are accepted on your word", widget.ui.labelsSummaryLabel.text)

    def test_TicksSurviveARefresh(self):
        """The ticks are the editor's word, so refreshing must not quietly rewrite them."""
        self.delayDisplay("Ticks across a refresh")
        widget = self.widget()
        logic = self.subjectInScene(widget, editsNeedReview=False)

        widget.ui.labelsTableWidget.setRowCount(0)
        widget.updateLabelsTable()
        self.assertEqual(widget.checkedLabels(), ["SKULL", "FEMUR_LEFT"])

        widget.ui.labelsTableWidget.item(0, 0).setCheckState(qt.Qt.Unchecked)
        widget.updateLabelsTable()
        self.assertEqual(widget.checkedLabels(), ["FEMUR_LEFT"], "an untick must survive")

        logic.addEmptySegment("TIBIA_LEFT")
        widget.updateLabelsTable()
        self.assertEqual(
            widget.checkedLabels(), ["FEMUR_LEFT", "TIBIA_LEFT"],
            "a label the editor added should start ticked, and the untick should hold",
        )
        self.delayDisplay("Ticks behave")

    def test_ConfirmingUploadsACorrectionThatWaitsOnTheServer(self):
        """What the editor is asked before the upload, what is sent, and what they are told after:
        the correction waits on the server, and what becomes of it depends on whether the
        server sends corrections back to a reviewer."""
        self.delayDisplay("Confirming")
        widget = self.widget()

        # Corrections go back to a reviewer. The editor takes the femur out, which nobody asked
        # for, and leaves out the tibia reported missing.
        logic = self.subjectInScene(widget, labels=handout()["labels"] + [TIBIA_MISSING])
        segmentation = logic.segmentationNode.GetSegmentation()
        segmentation.RemoveSegment(segmentation.GetSegmentIdBySegmentName("FEMUR_LEFT"))
        answer = {
            "assignment_id": "a1", "subject_key": "001_000001", "quality_check_confirmed": True, "state": "submitted",
            "stage": "review", "segmentation_staged": True, "accepted_labels": [], "rejected_labels": {},
            "missing_labels": [], "edited_labels": ["SKULL"], "removed_labels": ["FEMUR_LEFT"],
            "pending_labels": ["FEMUR_LEFT", "SKULL", "TIBIA_LEFT"],
            "message": "Uploaded. 3 label(s) go to a reviewer. The correction waits on the server; nothing is "
                       "written into the dataset before the administrator approves it.",
        }
        sent, asked = self.submitted(widget, widget.onConfirm, answer)
        self.assertEqual(len(sent), 1)
        confirmed, fields = sent[0]
        self.assertTrue(confirmed)
        self.assertEqual(fields["confirmed_labels"], [], "nothing is vouched for when ticks do not count")
        self.assertTrue(str(fields["segmentation_path"]).endswith("001_000001_reviewed.seg.nrrd"))
        question = asked[0]
        for words in (
            "Upload your correction of 001_000001?",
            "nothing reaches the dataset before the administrator approves the subject",
            "and those a reviewer rejected, go back to a reviewer",
            "Taken out, although nobody asked: FEMUR_LEFT. A reviewer must agree",
            "Reported missing, and not in your upload: TIBIA_LEFT.",
        ):
            self.assertIn(words, question)
        self.assertNotIn("Ticked", question)
        status = widget.ui.submitStatusLabel.text
        self.assertIn(answer["message"], status)
        self.assertIn("Waiting for a reviewer: FEMUR_LEFT, SKULL, TIBIA_LEFT.", status)

        # Corrections the editor vouches for are accepted: the ticked ones are sent as such.
        self.subjectInScene(widget, editsNeedReview=False)
        widget.ui.labelsTableWidget.item(self.rows(widget)["FEMUR_LEFT"], 0).setCheckState(qt.Qt.Unchecked)
        answer.update(stage="approval", accepted_labels=["SKULL"], removed_labels=[], pending_labels=[])
        sent, asked = self.submitted(widget, widget.onConfirm, answer)
        self.assertEqual(sent[0][1]["confirmed_labels"], ["SKULL"])
        self.assertIn("the ticked ones are accepted on your word", asked[0])
        self.assertIn("Ticked: SKULL.", asked[0])
        self.assertIn("Accepted on your word: SKULL.", widget.ui.submitStatusLabel.text)

    def test_RejectingSendsTheSubjectToTheAdministrator(self):
        """An editor who cannot correct a subject sends it to the administrator, with a comment."""
        self.delayDisplay("Rejecting")
        widget = self.widget()
        self.subjectInScene(widget)
        widget.ui.commentTextEdit.plainText = "The image stops above the knees."
        answer = {
            "assignment_id": "a1", "subject_key": "001_000001", "quality_check_confirmed": False, "state": "submitted",
            "stage": "escalated", "message": "Sent to the administrator with your comment. Nothing in the dataset changed.",
        }
        sent, asked = self.submitted(widget, widget.onReject, answer)
        self.assertEqual(sent, [(False, {"comment": "The image stops above the knees."})])
        self.assertEqual(len(asked), 1, "a comment was given, so it is not asked for")
        self.assertIn("It goes to the administrator with your comment", asked[0])
        self.assertIn("Nothing in the dataset changes", asked[0])
        self.assertEqual(widget.ui.submitStatusLabel.text, answer["message"])

    def test_AnEmptyQueueSpeaksOfCorrecting(self):
        """The editor is told there is nothing to correct, and when there will be."""
        self.delayDisplay("Nothing to correct")
        widget = self.widget()
        logic = self.subjectInScene(widget)
        logic.session.clear_subject()

        def nothingWaits():
            raise QCClientError("HTTP 404: No subject is waiting for an editor right now.", status_code=404)

        logic.session.next_subject = nothingWaits
        shown = []
        original = slicer.util.infoDisplay
        slicer.util.infoDisplay = lambda text, windowTitle=None, **kwargs: shown.append((windowTitle, text))
        try:
            widget.onNextSubject()
        finally:
            slicer.util.infoDisplay = original
        self.assertEqual(len(shown), 1)
        title, text = shown[0]
        self.assertEqual(title, "Nothing to correct")
        self.assertIn("There is nothing for you to correct right now.", text)
        self.assertIn("reports a bone missing", text)
        self.assertIn("No subject is waiting for an editor right now.", text, "the server's own words")
