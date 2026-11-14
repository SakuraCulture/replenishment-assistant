"""Local-only procurement preflight and unchanged final payment validation."""
import hashlib
import sqlite3
import sys
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
import test_lifecycle as fixture
from test_aoxiang_purchase import config
import archive_orders
import order_lifecycle as lc
import operations_flow
from aoxiang_purchase import preflight_payment, freeze_payment, PaymentPreparationError
from runtime_support import Cancelled


class PaymentPreflight(unittest.TestCase):
    def setUp(self):
        self.f = fixture.LifecycleTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.suppliers = {"山东仓": "ZXC2025", "河北仓": "2078246"}

    def paid(self, modern=False):
        self.f.save("return")
        if modern:
            operations_flow.adopt(self.f.path, [self.f.h])
        self.f.save("payment", [{"付款单商品数量": 7, "实际金额": .99}])

    def check_error(self, code, **kw):
        with self.assertRaises(PaymentPreparationError) as caught:
            preflight_payment(self.f.path, self.f.h, **kw)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.details["head"]["number"], self.f.h["number"])
        return caught.exception

    def test_missing_payment_explains_exact_order_and_next_step(self):
        error = self.check_error("PAYMENT_MISSING")
        self.assertIn("付款核对", str(error))
        self.assertIn(self.f.h["number"], str(error))
        self.assertIsNone(error.details["payment"])

    def test_pending_saved_without_upstream_is_separate_from_missing(self):
        self.f.save("payment")
        error = self.check_error("PAYMENT_PENDING")
        self.assertEqual(error.details["payment"]["version"], 1)
        self.assertEqual(error.details["unready_stages"], ["return"])
        self.f.save("return")
        self.check_error("PAYMENT_PENDING")
        lc.commit(self.f.path, lc.recheck(self.f.path, self.f.h, "payment"))
        self.assertTrue(preflight_payment(self.f.path, self.f.h))

    def test_legacy_valid_payment_remains_valid_and_returns_only_identity(self):
        self.paid()
        before = self.f.view()
        result = preflight_payment(self.f.path, self.f.h)
        self.assertEqual(result["flow_version"], 1)
        self.assertEqual(result["payment"], {k: before["documents"]["payment"][k] for k in ("version", "digest")})
        self.assertNotIn("rows", result)
        self.assertEqual(self.f.view(), before)
        plan = freeze_payment(self.f.path, self.f.h, config(), self.suppliers)
        self.assertEqual(plan["total_quantity"], "7")
        self.assertEqual(plan["total_amount"], "0.98")

    def test_no_signed_result_or_receipt_snapshot_on_normal_preflight(self):
        self.paid(modern=True)
        with patch("order_lifecycle.snapshot", side_effect=AssertionError("large snapshot should wait for freeze")):
            result = preflight_payment(self.f.path, self.f.h)
        self.assertEqual(result["flow_version"], 2)

    def test_calendar_age_does_not_expire_payment(self):
        self.paid()
        with closing(sqlite3.connect(self.f.path)) as db, db:
            db.execute("UPDATE lifecycle_versions SET saved='2000-01-01T00:00:00+08:00'")
        self.assertTrue(preflight_payment(self.f.path, self.f.h))
        self.assertEqual(freeze_payment(self.f.path, self.f.h, config(), self.suppliers)["total_quantity"], "7")

    def test_return_change_names_versions_and_requires_explicit_recheck(self):
        self.paid()
        self.f.save("return", [{"门店确认数量": 9}])
        original = self.f.view()["documents"]["payment"]
        error = self.check_error("PAYMENT_BASIS_CHANGED")
        changes = error.details["dependency_changes"]
        self.assertEqual([(c["stage"], c["expected"]["version"], c["current"]["version"]) for c in changes], [("return", 1, 2)])
        self.assertIn("V1 → V2", str(error))
        self.assertEqual(self.f.view()["documents"]["payment"], original)
        lc.commit(self.f.path, lc.recheck(self.f.path, self.f.h, "payment"))
        self.assertTrue(preflight_payment(self.f.path, self.f.h))

    def test_operations_change_is_explained_without_rewriting_paid_fact(self):
        self.paid(modern=True)
        original = self.f.view()["documents"]["payment"]
        self.f.save("operations", [{"门店确认数量": 3}])
        error = self.check_error("PAYMENT_BASIS_CHANGED")
        self.assertEqual(error.details["dependency_changes"][0]["stage"], "operations")
        self.assertEqual(self.f.view()["documents"]["payment"], original)
        lc.commit(self.f.path, lc.recheck(self.f.path, self.f.h, "payment"))
        self.assertTrue(preflight_payment(self.f.path, self.f.h))

    def test_return_change_invalidates_operations_before_payment(self):
        self.paid(modern=True)
        self.f.save("return", [{"门店确认数量": 9}])
        error = self.check_error("PAYMENT_UPSTREAM_NOT_READY")
        self.assertEqual(error.details["unready_stages"], ["operations"])
        self.assertEqual(error.details["dependency_changes"][0]["stage"], "return")

    def test_adding_operations_explicitly_changes_old_payment_dependencies(self):
        self.paid()
        self.assertEqual(preflight_payment(self.f.path, self.f.h)["flow_version"], 1)
        operations_flow.adopt(self.f.path, [self.f.h])
        error = self.check_error("PAYMENT_BASIS_CHANGED")
        self.assertIsNone(error.details["dependency_changes"][0]["expected"])
        self.assertEqual(error.details["dependency_changes"][0]["current"]["version"], 1)

    def test_preflight_does_not_authorize_stale_freeze_after_concurrent_change(self):
        self.paid()
        preflight_payment(self.f.path, self.f.h)
        self.f.save("return", [{"门店确认数量": 9}])
        with self.assertRaises(PaymentPreparationError) as caught:
            freeze_payment(self.f.path, self.f.h, config(), self.suppliers)
        self.assertEqual(caught.exception.code, "PAYMENT_BASIS_CHANGED")

    def test_preflight_and_error_paths_do_not_write_database(self):
        self.paid()
        before = hashlib.sha256(self.f.path.read_bytes()).hexdigest()
        with closing(sqlite3.connect(self.f.path)) as db:
            db.execute("BEGIN")
            before_changes = db.total_changes
            preflight_payment(self.f.path, self.f.h, db=db)
            self.assertEqual(db.total_changes, before_changes)
        self.assertEqual(hashlib.sha256(self.f.path.read_bytes()).hexdigest(), before)
        self.f.save("return", [{"门店确认数量": 9}])
        before = hashlib.sha256(self.f.path.read_bytes()).hexdigest()
        self.check_error("PAYMENT_BASIS_CHANGED")
        self.assertEqual(hashlib.sha256(self.f.path.read_bytes()).hexdigest(), before)

    def test_cancelled_and_deleted_source_still_block(self):
        self.paid()
        cancel = threading.Event(); cancel.set()
        with self.assertRaises(Cancelled):
            preflight_payment(self.f.path, self.f.h, cancel=cancel)
        archive_orders.change(self.f.path, self.f.h, True)
        with self.assertRaisesRegex(lc.wr.Conflict, "回收站|可见性|删除"):
            preflight_payment(self.f.path, self.f.h)

    def test_missing_database_is_not_created(self):
        target = self.f.root / "missing.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            preflight_payment(target, self.f.h)
        self.assertFalse(target.exists())

    def test_original_snapshot_integrity_errors_are_not_relabelled(self):
        self.paid()
        with closing(sqlite3.connect(self.f.path)) as db, db:
            db.execute("UPDATE lifecycle_versions SET digest='bad' WHERE stage='payment'")
        with self.assertRaisesRegex(ValueError, "快照校验失败") as caught:
            preflight_payment(self.f.path, self.f.h)
        self.assertNotIsInstance(caught.exception, PaymentPreparationError)

    def test_new_route_pending_operations_still_requires_confirmation(self):
        self.f.save("return")
        with closing(sqlite3.connect(self.f.path)) as db, db:
            operations_flow.schema(db)
            db.execute("INSERT INTO lifecycle_routes VALUES(?,?,2)", (self.f.h["oid"], self.f.h["revision"]))
        self.f.save("payment")
        error = self.check_error("PAYMENT_PENDING")
        self.assertEqual(error.details["unready_stages"], ["operations"])
        self.assertEqual(error.details["flow_version"], 2)

    def test_schema_one_payment_keeps_original_health_semantics(self):
        self.paid()
        with closing(sqlite3.connect(self.f.path)) as db, db:
            doc = lc.current(db, self.f.h, "payment")
            doc["payload"]["schema"] = 1
            # Historical schema-1 health deliberately did not use dependency tokens.
            doc["payload"]["basis"]["dependencies"] = {}
            db.execute("UPDATE lifecycle_versions SET payload=?,digest=? WHERE stage='payment'",
                       (lc.wr.dumps(doc["payload"]), lc.fingerprint(doc["payload"])))
        self.assertTrue(self.f.view()["health"]["payment"])
        self.assertTrue(preflight_payment(self.f.path, self.f.h))

    def test_complete_legacy_feedback_uses_canonical_completeness_check(self):
        with closing(sqlite3.connect(self.f.path)) as db, db:
            db.execute("CREATE TABLE merchant_feedback(oid TEXT,revision INTEGER,version INTEGER,saved TEXT,payload TEXT)")
            db.execute("INSERT INTO merchant_feedback VALUES(?,?,?,?,?)", (self.f.h["oid"], 1, 1, lc.wr.now(), lc.wr.dumps({"values": {"0001": 5}, "warehouses": {}})))
        self.f.save("payment", [{"付款单商品数量": 7, "实际金额": .99}])
        self.assertTrue(preflight_payment(self.f.path, self.f.h))
        self.assertEqual(freeze_payment(self.f.path, self.f.h, config(), self.suppliers)["total_quantity"], "7")


if __name__ == "__main__":
    unittest.main(verbosity=2)
