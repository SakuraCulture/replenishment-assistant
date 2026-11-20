"""Contracts at the signing handoff and application startup boundaries."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import app_smoke_dispatch as smokes
import normal_delivery


class SmokeDispatchTests(unittest.TestCase):
    def test_normal_launch_loads_no_acceptance_module(self):
        with patch.object(smokes, "import_module") as load:
            self.assertFalse(smokes.install_startup(object(), ["app"]))
            self.assertFalse(smokes.use_workbench(["app"]))
            smokes.start_window_smokes(object(), object(), ["app"])
            smokes.run_catalog_helper(["app"])
        load.assert_not_called()

    def test_window_commands_keep_dispatch_order_and_group_precedence(self):
        invoked = []

        def load(name):
            def start(app, window, args):
                invoked.append((name, app, window, args))
            return SimpleNamespace(start_smoke=start)

        app, window = object(), object()
        args = [
            "app", "--parameter-presets-smoke", "presets",
            "--admission-overview-smoke", "admission", "--batch-smoke", "batch",
            "--global-crud-smoke", "crud",
        ]
        self.assertTrue(smokes.use_workbench(args))
        with patch.object(smokes, "import_module", side_effect=load):
            smokes.start_window_smokes(app, window, args)
        self.assertEqual(invoked, [
            ("global_crud_smoke", app, window, ["crud"]),
            ("admission_overview_smoke", app, window, args[4:]),
            ("batch_smoke", app, window, args[6:]),
        ])

    def test_settings_and_catalog_hooks_pass_through_arguments(self):
        app = object()
        settings, catalog = Mock(), Mock()
        with patch.object(smokes, "import_module", side_effect=[settings, catalog]) as load:
            self.assertTrue(smokes.install_startup(app, ["app", "--data-location-smoke", "isolated", "reopen"]))
            smokes.run_catalog_helper(["app", "--catalog-lifetime-helper", "isolated"])
        self.assertEqual([call.args[0] for call in load.call_args_list], ["data_location_smoke", "catalog_lifetime_smoke"])
        settings.install_startup.assert_called_once_with(app, ["isolated", "reopen"])
        catalog.parent_helper.assert_called_once_with(["isolated"])


class SigningPreparationTests(unittest.TestCase):
    def setUp(self):
        self.full = {
            "day": "2026-11-19", "input_dirty": False, "model_version": "model",
            "states": {code: {"state": "已完成", "name": code} for code in ("C", "B", "A")},
            "results": {code: {"summary": {"amount": 1}} for code in ("C", "B", "A")},
            "execution_warehouses": {"B": ["河北仓"]},
            "preissue_drafts": {"C": {"pending": True}},
        }
        self.codes = ["A", "B"]
        self.warehouses = {"B": ["河北仓"]}

    def test_existing_revision_only_requests_selected_preview(self):
        prepared = {"plans": [{"same": True}]}
        request, permit = Mock(return_value=prepared), Mock()
        self.assertEqual(
            normal_delivery.prepare_export(self.full, self.codes, self.warehouses, request, permit),
            (prepared, False),
        )
        op, (payload, codes, warehouses) = request.call_args.args
        self.assertEqual(op, "delivery_probe")
        self.assertEqual(list(payload["results"]), self.codes)
        self.assertEqual(payload["_draft_summary"]["completed"], 3)
        self.assertEqual((codes, warehouses), (self.codes, self.warehouses))
        self.assertEqual(request.call_count, 1)
        permit.assert_not_called()

    def test_new_signing_expands_after_permission_in_original_draft_order(self):
        for allowed in (True, False):
            with self.subTest(allowed=allowed):
                trace = Mock()
                prepared = {"plans": [{"same": True}, {"same": False}]}
                trace.request.side_effect = [prepared, True]
                trace.permit.return_value = allowed
                result = normal_delivery.prepare_export(
                    self.full, self.codes, self.warehouses, trace.request, trace.permit,
                )
                self.assertEqual([call[0] for call in trace.mock_calls],
                                 ["request", "permit", "request"] if allowed else ["request", "permit"])
                if not allowed:
                    self.assertIsNone(result)
                    continue
                self.assertEqual(result, (prepared, True))
                op, (tail, order) = trace.request.call_args.args
                self.assertEqual(op, "delivery_expand")
                self.assertEqual(list(tail["results"]), ["C"])
                self.assertEqual(order, ["C", "B", "A"])
                self.assertEqual(tail["preissue_drafts"], self.full["preissue_drafts"])
                self.assertEqual(list(self.full["results"]), order)