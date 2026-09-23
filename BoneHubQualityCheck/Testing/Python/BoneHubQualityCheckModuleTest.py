"""Panel-level tests for BoneHub Quality Check, run against a real module widget.

The tests in ``BoneHubQualityCheck.py`` itself cover the logic that touches the dataset.
These cover the panel: that the .ui file still carries every widget the code reaches for,
that the sections stay locked until there is something to do, and that the tick boxes --
which decide which labels are marked "reviewed and corrected" -- behave across a refresh.
They need a module widget, so they only run in a Slicer with a main window.
"""

import qt
import slicer
from slicer.ScriptedLoadableModule import ScriptedLoadableModuleTest

import BoneHubQualityCheck
from BoneHubQualityCheckLib.labels import LabelMap

#: Every widget the module code reaches for through ``self.ui``.
EXPECTED_WIDGETS = [
    "serverUrlLineEdit", "apiKeyLineEdit", "showKeyCheckBox", "rememberKeyCheckBox",
    "connectButton", "connectionStatusLabel", "serverCollapsibleButton",
    "nextSubjectButton", "reloadSubjectButton", "subjectKeyLabel", "expiresLabel",
    "subjectInfoTextBrowser", "extendLeaseButton", "releaseSubjectButton",
    "subjectCollapsibleButton", "labelsTableWidget", "labelsSummaryLabel",
    "refreshLabelsButton", "selectAllLabelsButton", "selectNoLabelsButton",
    "addLabelComboBox", "addSegmentButton", "segmentEditorButton",
    "reviewCollapsibleButton", "commentTextEdit", "confirmButton", "rejectButton",
    "autoNextCheckBox", "submitStatusLabel", "submitCollapsibleButton",
    "workspacePathLineEdit", "timeoutSpinBox", "keepFilesCheckBox", "openWorkspaceButton",
    "advancedCollapsibleButton",
]

#: BoneHub data schema 0.3 values of the labels these tests use.
SKULL, FEMUR_LEFT, TIBIA_LEFT = 100000000, 710000001, 730000001


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
        self.test_TicksSurviveARefresh()

    # ---------------------------------------------------------------- helpers
    def widget(self):
        slicer.util.selectModule("BoneHubQualityCheck")
        return slicer.modules.bonehubqualitycheck.widgetRepresentation().self()

    def subjectInScene(self, widget):
        """Put a two-label subject in the scene and point the panel at it."""
        logic = BoneHubQualityCheck.BoneHubQualityCheckTest()
        logic.setUp()
        builtLogic, _volume, _expected = logic._buildSubject()
        widget.logic = builtLogic
        builtLogic.session.handout = {"segmentation_labels": {"SKULL": 1, "FEMUR_LEFT": 1}}
        return builtLogic

    # ------------------------------------------------------------------ tests
    def test_PanelHasEveryWidgetTheCodeUses(self):
        """A widget renamed in the .ui but not in the code fails here, not in a reviewer's face."""
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

    def test_TicksSurviveARefresh(self):
        """The ticks are the verdict, so refreshing must not quietly rewrite them."""
        self.delayDisplay("Ticks across a refresh")
        widget = self.widget()
        logic = self.subjectInScene(widget)

        widget.ui.labelsTableWidget.setRowCount(0)
        widget.updateLabelsTable()
        self.assertEqual(widget.checkedLabels(), ["SKULL", "FEMUR_LEFT"])

        widget.ui.labelsTableWidget.item(0, 0).setCheckState(qt.Qt.Unchecked)
        widget.updateLabelsTable()
        self.assertEqual(widget.checkedLabels(), ["FEMUR_LEFT"], "an untick must survive")

        logic.session.labels = LabelMap({"SKULL": SKULL, "FEMUR_LEFT": FEMUR_LEFT, "TIBIA_LEFT": TIBIA_LEFT})
        logic.addEmptySegment("TIBIA_LEFT")
        widget.updateLabelsTable()
        self.assertEqual(
            widget.checkedLabels(), ["FEMUR_LEFT", "TIBIA_LEFT"],
            "a label the reviewer added should start ticked, and the untick should hold",
        )
        self.delayDisplay("Ticks behave")
