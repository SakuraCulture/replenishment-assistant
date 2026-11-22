"""Known acceptance commands. Imports occur only for requested smoke hooks."""
from importlib import import_module


WINDOW_SMOKES = {
    '--order-redo-smoke': 'order_redo_smoke',
    '--warehouse-must-sell-smoke': 'warehouse_must_sell_smoke',
    '--data-location-smoke': 'data_location_smoke',
    '--global-crud-smoke': 'global_crud_smoke',
    '--five-fixes-smoke': 'five_fixes_smoke',
    '--signing-memory-smoke': 'signing_memory_smoke',
    '--performance-log-smoke': 'performance_log_smoke',
    '--analytics-export-fix-smoke': 'analytics_export_fix_smoke',
    '--perf-core-smoke': 'perf_core_smoke',
    '--memory-load-smoke': 'memory_load_smoke',
    '--migration-shutdown-smoke': 'migration_shutdown_smoke',
    '--order-merge-smoke': 'order_merge_smoke',
    '--operations-lifecycle-smoke': 'operations_lifecycle_smoke',
    '--jst-carton-smoke': 'jst_carton_smoke',
    '--stockout-hub-smoke': 'stockout_hub_smoke',
    '--monthly-first-fill-smoke': 'monthly_first_fill_smoke',
    '--paid-return-reallocation-smoke': 'paid_return_reallocation_smoke',
    '--warehouse-stockout-export-smoke': 'warehouse_stockout_export_smoke',
    '--business-repairs-smoke': 'business_repairs_smoke',
    '--operations-0510-smoke': 'operations_0510_smoke',
    '--deletion-lifecycle-smoke': 'deletion_lifecycle_smoke',
    '--reset-selected-smoke': 'reset_selected_smoke',
    '--startup-connections-smoke': 'browser_startup_smoke',
    '--stockout-smoke': 'stockout_smoke',
    '--collection-reexport-smoke': 'collection_reexport_smoke',
    '--collection-ended-smoke': 'collection_ended_smoke',
    '--mandatory-holding-smoke': 'mandatory_holding_smoke',
    '--deletion-void-smoke': 'deletion_void_smoke',
    '--purchase-recovery-smoke': 'purchase_recovery_smoke',
    '--payment-preview-smoke': 'payment_preview_smoke',
    '--purchase-pipeline-smoke': 'purchase_pipeline_smoke',
    '--catalog-lifetime-smoke': 'catalog_lifetime_smoke',
    '--admission-overview-smoke': 'admission_overview_smoke',
    '--parameter-quick-smoke': 'parameter_quick_smoke',
    '--parameter-presets-smoke': 'parameter_preset_smoke',
    '--gui-maintenance-smoke': 'gui_maintenance_smoke',
    '--automation-portability-smoke': 'automation_portability_smoke',
    '--inventory-smoke': 'inventory_smoke',
    '--coverage-smoke': 'coverage_smoke',
    '--analytics-smoke': 'analytics_smoke',
    '--scheduled-smoke': 'scheduled_smoke',
    '--business-flow-acceptance': 'business_flow_acceptance',
    '--automation-smoke': 'automation_smoke',
    '--parameter-workspace-smoke': 'parameter_workspace_smoke',
    '--collection-maintenance-smoke': 'collection_maintenance_smoke',
    '--export-menu-smoke': 'export_menu_smoke',
    '--jst-batch-smoke': 'jst_batch_smoke',
    '--jst-basis-smoke': 'jst_basis_smoke',
    '--jst-auto-order-smoke': 'jst_auto_order_smoke',
    '--jst-one-click-smoke': 'jst_one_click_smoke',
    '--jst-buyer-recovery-smoke': 'jst_buyer_recovery_smoke',
    '--jst-fulfillment-smoke': 'jst_fulfillment_smoke',
    '--retry-profile-smoke': 'retry_profile_smoke',
    '--deletion-smoke': 'deletion_smoke',
    '--governance-smoke': 'governance_smoke',
    '--catalog-sync-smoke': 'catalog_sync_smoke',
    '--catalog-rebind-smoke': 'catalog_rebind_smoke',
    '--lifecycle-comparison-smoke': 'lifecycle_comparison_smoke',
    '--restock-smoke': 'restock_smoke',
    '--readability-smoke': 'readability_smoke',
    '--maintenance-smoke': 'maintenance_smoke',
    '--store-parameters-smoke': 'store_parameter_smoke',
    '--browser-status-smoke': 'browser_status_smoke',
    '--aoxiang-smoke': 'aoxiang_smoke',
    '--payment-source-smoke': 'payment_source_smoke',
    '--jst-smoke': 'jst_smoke',
    '--jst-results-smoke': 'jst_results_smoke',
    '--preissue-smoke': 'preissue_smoke',
    '--final-filters-smoke': 'final_filters_smoke',
    '--layout-smoke': 'layout_smoke',
    '--payment-matching-smoke': 'payment_matching_smoke',
    '--payment-shop-names-smoke': 'payment_shop_names_smoke',
    '--payment-reference-smoke': 'payment_reference_smoke',
    '--analytics-identifiers-smoke': 'analytics_identifiers_smoke',
    '--first-fill-smoke': 'first_fill_smoke',
    '--purchase-pricing-smoke': 'purchase_pricing_smoke',
    '--jst-payment-smoke': 'jst_payment_smoke',
    '--workflow-refinements-smoke': 'workflow_refinements_smoke',
    '--business-flows-smoke': 'business_flows_smoke',
    '--reconciliation-smoke': 'reconciliation_smoke',
    '--export-location-smoke': 'export_location_smoke',
    '--lifecycle-smoke': 'lifecycle_smoke',
    '--lifecycle-duplicate-smoke': 'lifecycle_duplicate_smoke',
    '--followup-smoke': 'followup_smoke',
    '--warehouse-delivery-smoke': 'warehouse_delivery_smoke',
    '--inbound-smoke': 'inbound_smoke',
    '--fixes-smoke': 'fixes_smoke',
    '--business-smoke': 'business_smoke',
    '--storage-smoke': 'storage_smoke',
    '--signing-feedback-smoke': 'signing_feedback_smoke',
    '--work-io-smoke': 'work_io_smoke',
    '--worker-recovery-smoke': 'worker_recovery_smoke',
    '--linked-orders-smoke': 'linked_orders_smoke',
    '--workflow-smoke': 'workflow_smoke',
    '--history-smoke': 'history_smoke',
    '--delivery-smoke': 'delivery_smoke',
    '--normal-smoke': 'normal_smoke',
    '--batch-smoke': 'batch_smoke',
    '--jst-stock-evidence-smoke': 'jst_stock_evidence_smoke',
    '--analytics-templates-smoke': 'analytics_templates_smoke',
    '--lifecycle-preflight-smoke': 'lifecycle_preflight_smoke',
    '--return-save-smoke': 'return_save_smoke',
    '--lifecycle-performance-smoke': 'lifecycle_perf_smoke',
    '--lifecycle-followup-smoke': 'lifecycle_followup_smoke',
    '--lifecycle-recovery-smoke': 'lifecycle_recovery_smoke',
    '--jst-batch-recovery-smoke': 'jst_batch_recovery_smoke',
    '--runtime-lifetime-smoke': 'runtime_lifetime_smoke',
    '--warehouse-execution-smoke': 'warehouse_execution_smoke',
    '--warehouse-smoke': 'warehouse_smoke',
    '--work-smoke': 'work_smoke',
    '--intake-smoke': 'intake_smoke',
}

SETTINGS_SMOKE = "--data-location-smoke"
CATALOG_HELPER = "--catalog-lifetime-helper"
WORKBENCH_SMOKES = ("--batch-smoke", "--warehouse-smoke", "--work-smoke", "--intake-smoke")
EXCLUSIVE_SMOKES = (
    '--admission-overview-smoke',
    '--parameter-quick-smoke',
    '--parameter-presets-smoke',
)
SMOKE_MODULES = tuple(dict.fromkeys((*WINDOW_SMOKES.values(), "catalog_lifetime_smoke")))


def install_startup(app, argv):
    if SETTINGS_SMOKE not in argv:
        return False
    module = import_module(WINDOW_SMOKES[SETTINGS_SMOKE])
    module.install_startup(app, argv[argv.index(SETTINGS_SMOKE) + 1:])
    return True


def use_workbench(argv):
    return any(flag in argv for flag in WORKBENCH_SMOKES)


def start_window_smokes(app, window, argv):
    exclusive = next((flag for flag in EXCLUSIVE_SMOKES if flag in argv), None)
    for flag, name in WINDOW_SMOKES.items():
        if flag in argv and (flag not in EXCLUSIVE_SMOKES or flag == exclusive):
            import_module(name).start_smoke(app, window, argv[argv.index(flag) + 1:])


def run_catalog_helper(argv):
    if CATALOG_HELPER in argv:
        module = import_module("catalog_lifetime_smoke")
        module.parent_helper(argv[argv.index(CATALOG_HELPER) + 1:])
