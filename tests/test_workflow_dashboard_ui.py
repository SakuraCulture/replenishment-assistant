"""Native dashboard controls and render bounds on isolated analytics fixtures."""
from unittest.mock import patch
import unittest
import test_order_analytics_ui as fixture

class Dashboard(unittest.TestCase):
    def setUp(self):
        fixture.AnalyticsUiTests.setUpClass();self.f=fixture.AnalyticsUiTests();self.f.setUp();self.w=self.f.widget
    def tearDown(self):self.f.tearDown()
    def test_chart_has_space_and_click_drills_without_mutation(self):
        from PySide6.QtCore import Qt,QPoint
        from PySide6.QtTest import QTest
        self.f.host.resize(1280,800);self.f.host.show();self.w.resize(1200,740);self.w.show();self.w.show_report(self.f.f.report());self.w.tabs.setCurrentWidget(self.w.workflow);self.f.app.processEvents()
        d=self.w.workflow;self.assertGreaterEqual(d.chart.height(),140);self.assertFalse(self.w.funnel.isVisible())
        before=self.f.f.path.read_bytes();QTest.mouseClick(d.chart,Qt.MouseButton.LeftButton,pos=QPoint(70,50))
        self.assertEqual(d.visual_tabs.currentIndex(),2);self.assertEqual(d.details.rowCount(),2)
        self.assertEqual(self.f.f.path.read_bytes(),before);self.assertEqual(d.details.horizontalHeaderItem(16).text(),'签发单号')
        self.w.tabs.setCurrentIndex(0);self.f.app.processEvents();self.assertTrue(self.w.funnel.isVisible())
    def test_custom_scope_controls_and_export_options_stay_in_sync(self):
        self.w.show_report(self.f.f.report());self.w.tabs.setCurrentWidget(self.w.workflow);d=self.w.workflow
        d.filters={'warehouse':['河北仓']};d.grain.setCurrentIndex(2);d.mode.setCurrentIndex(0);d.dimension.setCurrentIndex(2);d.render()
        self.assertEqual(d.details.rowCount(),1);self.assertEqual(self.w.report['_workflow_options']['grain'],'month')
        with patch.object(self.w,'export') as export:d.export();export.assert_called_once_with(False)
        self.assertEqual(self.w.export_kind.currentData(),'workflow')
    def test_legacy_summary_selection_remains_unchanged(self):
        self.w.show_report(self.f.f.report());self.assertEqual(len(self.w.report['metrics']['pairs']),4)
        self.w.tabs.setCurrentWidget(self.w.workflow);self.w.tabs.setCurrentIndex(2)
        self.assertEqual(self.w.key(),'order');self.assertEqual(self.w.models['order'].rowCount(),2)
