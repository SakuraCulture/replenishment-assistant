"""Application entrypoint: Qt setup, logging, exceptions and window creation."""
import sys,os,logging
from pathlib import Path

def main():
    from PySide6.QtCore import QStandardPaths
    from PySide6.QtGui import QColor,QIcon,QPalette
    from PySide6.QtWidgets import QApplication,QMessageBox
    from ui_controls import STYLE
    app=QApplication(sys.argv);app.setApplicationName('补货助手');app.setOrganizationName('YoutuanTools');app.setStyle('Fusion')
    palette=app.style().standardPalette()
    for role,color in [(QPalette.ColorRole.Window,'#F3F6F7'),(QPalette.ColorRole.WindowText,'#22383C'),(QPalette.ColorRole.Base,'#FFFFFF'),(QPalette.ColorRole.AlternateBase,'#F7FAFA'),(QPalette.ColorRole.Text,'#22383C'),(QPalette.ColorRole.Button,'#FFFFFF'),(QPalette.ColorRole.ButtonText,'#22383C'),(QPalette.ColorRole.Highlight,'#DDEFEA'),(QPalette.ColorRole.HighlightedText,'#153C43')]:palette.setColor(role,QColor(color))
    app.setPalette(palette);app.setStyleSheet(STYLE)
    icon=Path(__file__).parent/'app.ico'
    if icon.exists():app.setWindowIcon(QIcon(str(icon)))
    logs=Path(os.environ.get('REPLENISHMENT_DATA_DIR') or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))
    from single_instance import SingleInstance,scope
    primary=SingleInstance(scope(sys.argv,logs),parent=app)
    try:
        if not primary.acquire():return 0
    except Exception as exc:
        QMessageBox.warning(None,'补货助手已在运行',str(exc));return 1
    from application_reset import recover,InstanceLease
    try:
        recover(logs)
        from order_migration import recover as recover_orders
        recover_orders(logs)
        if (logs/'迁入原文件映射.json').is_file():os.environ['REPLENISHMENT_PORTABLE_PATHS']=str(logs/'迁入原文件映射.json')
        else:os.environ.pop('REPLENISHMENT_PORTABLE_PATHS',None)
        instance_lease=InstanceLease(logs)
    except Exception as exc:
        QMessageBox.warning(None,'资料目录未打开',str(exc));return 1
    app.data_root=logs
    logs.mkdir(parents=True,exist_ok=True);logging.basicConfig(filename=logs/'app.log',level=logging.INFO,encoding='utf-8',force=True,format='%(asctime)s %(levelname)s %(message)s')
    def unexpected(t,v,tb):
        logging.error('界面异常',exc_info=(t,v,tb))
        from readable_ui import failure
        failure(None,'操作未完成',f'{t.__name__}: {v}\n日志位置：{logs}')
    sys.excepthook=unexpected
    from workbench import Workbench
    if any(flag in sys.argv for flag in ['--batch-smoke','--warehouse-smoke','--work-smoke','--intake-smoke']):win=Workbench()
    else:
        from application_window import ApplicationWindow
        win=ApplicationWindow()
    win.show()
    primary.bind(win)
    # GUI acceptance installs isolated transports before explicitly exercising
    # startup; normal launches always attempt connection after the first paint.
    from browser_startup_ui import start_application_connections
    start_application_connections(win, sys.argv)
    if '--operations-0510-smoke' in sys.argv:
        from operations_0510_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--operations-0510-smoke')+1:])
    if '--deletion-lifecycle-smoke' in sys.argv:
        from deletion_lifecycle_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--deletion-lifecycle-smoke')+1:])
    if '--reset-selected-smoke' in sys.argv:
        from reset_selected_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--reset-selected-smoke')+1:])
    if '--startup-connections-smoke' in sys.argv:
        from browser_startup_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--startup-connections-smoke')+1:])
    if '--stockout-smoke' in sys.argv:
        from stockout_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--stockout-smoke')+1:])
    if '--collection-reexport-smoke' in sys.argv:
        from collection_reexport_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--collection-reexport-smoke')+1:])
    if '--collection-ended-smoke' in sys.argv:
        from collection_ended_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--collection-ended-smoke')+1:])
    if '--mandatory-holding-smoke' in sys.argv:
        from mandatory_holding_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--mandatory-holding-smoke')+1:])
    if '--deletion-void-smoke' in sys.argv:
        from deletion_void_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--deletion-void-smoke')+1:])
    if '--purchase-recovery-smoke' in sys.argv:
        from purchase_recovery_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--purchase-recovery-smoke')+1:])
    if '--purchase-pipeline-smoke' in sys.argv:
        from purchase_pipeline_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--purchase-pipeline-smoke')+1:])
    if '--catalog-lifetime-smoke' in sys.argv:
        from catalog_lifetime_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--catalog-lifetime-smoke')+1:])
    if '--admission-overview-smoke' in sys.argv:
        from admission_overview_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--admission-overview-smoke')+1:])
    elif '--parameter-quick-smoke' in sys.argv:
        from parameter_quick_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--parameter-quick-smoke')+1:])
    elif '--parameter-presets-smoke' in sys.argv:
        from parameter_preset_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--parameter-presets-smoke')+1:])
    if '--gui-maintenance-smoke' in sys.argv:
        from gui_maintenance_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--gui-maintenance-smoke')+1:])
    if '--automation-portability-smoke' in sys.argv:
        from automation_portability_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--automation-portability-smoke')+1:])
    if '--inventory-smoke' in sys.argv:
        from inventory_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--inventory-smoke')+1:])
    if '--coverage-smoke' in sys.argv:
        from coverage_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--coverage-smoke')+1:])
    if '--analytics-smoke' in sys.argv:
        from analytics_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--analytics-smoke')+1:])
    if '--scheduled-smoke' in sys.argv:
        from scheduled_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--scheduled-smoke')+1:])
    if '--business-flow-acceptance' in sys.argv:
        from business_flow_acceptance import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--business-flow-acceptance')+1:])
    if '--automation-smoke' in sys.argv:
        from automation_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--automation-smoke')+1:])
    if '--parameter-workspace-smoke' in sys.argv:
        from parameter_workspace_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--parameter-workspace-smoke')+1:])
    if '--collection-maintenance-smoke' in sys.argv:
        from collection_maintenance_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--collection-maintenance-smoke')+1:])
    if '--export-menu-smoke' in sys.argv:
        from export_menu_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--export-menu-smoke')+1:])
    if '--jst-batch-smoke' in sys.argv:
        from jst_batch_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--jst-batch-smoke')+1:])
    if '--jst-basis-smoke' in sys.argv:
        from jst_basis_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--jst-basis-smoke')+1:])
    if '--retry-profile-smoke' in sys.argv:
        from retry_profile_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--retry-profile-smoke')+1:])
    if '--deletion-smoke' in sys.argv:
        from deletion_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--deletion-smoke')+1:])
    if '--governance-smoke' in sys.argv:
        from governance_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--governance-smoke')+1:])
    if '--catalog-sync-smoke' in sys.argv:
        from catalog_sync_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--catalog-sync-smoke')+1:])
    if '--lifecycle-comparison-smoke' in sys.argv:
        from lifecycle_comparison_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--lifecycle-comparison-smoke')+1:])
    if '--restock-smoke' in sys.argv:
        from restock_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--restock-smoke')+1:])
    if '--readability-smoke' in sys.argv:
        from readability_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--readability-smoke')+1:])
    if '--maintenance-smoke' in sys.argv:
        from maintenance_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--maintenance-smoke')+1:])
    if '--store-parameters-smoke' in sys.argv:
        from store_parameter_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--store-parameters-smoke')+1:])
    if '--browser-status-smoke' in sys.argv:
        from browser_status_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--browser-status-smoke')+1:])
    if '--aoxiang-smoke' in sys.argv:
        from aoxiang_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--aoxiang-smoke')+1:])
    if '--payment-source-smoke' in sys.argv:
        from payment_source_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--payment-source-smoke')+1:])
    if '--jst-smoke' in sys.argv:
        from jst_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--jst-smoke')+1:])
    if '--jst-results-smoke' in sys.argv:
        from jst_results_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--jst-results-smoke')+1:])
    if '--preissue-smoke' in sys.argv:
        from preissue_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--preissue-smoke')+1:])
    if '--final-filters-smoke' in sys.argv:
        from final_filters_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--final-filters-smoke')+1:])
    if '--layout-smoke' in sys.argv:
        from layout_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--layout-smoke')+1:])
    if '--payment-matching-smoke' in sys.argv:
        from payment_matching_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--payment-matching-smoke')+1:])
    if '--payment-reference-smoke' in sys.argv:
        from payment_reference_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--payment-reference-smoke')+1:])
    if '--analytics-identifiers-smoke' in sys.argv:
        from analytics_identifiers_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--analytics-identifiers-smoke')+1:])
    if '--purchase-pricing-smoke' in sys.argv:
        from purchase_pricing_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--purchase-pricing-smoke')+1:])
    if '--jst-payment-smoke' in sys.argv:
        from jst_payment_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--jst-payment-smoke')+1:])
    if '--workflow-refinements-smoke' in sys.argv:
        from workflow_refinements_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--workflow-refinements-smoke')+1:])
    if '--business-flows-smoke' in sys.argv:
        from business_flows_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--business-flows-smoke')+1:])
    if '--reconciliation-smoke' in sys.argv:
        from reconciliation_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--reconciliation-smoke')+1:])
    if '--export-location-smoke' in sys.argv:
        from export_location_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--export-location-smoke')+1:])
    if '--lifecycle-smoke' in sys.argv:
        from lifecycle_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--lifecycle-smoke')+1:])
    if '--followup-smoke' in sys.argv:
        from followup_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--followup-smoke')+1:])
    if '--warehouse-delivery-smoke' in sys.argv:
        from warehouse_delivery_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--warehouse-delivery-smoke')+1:])
    if '--inbound-smoke' in sys.argv:
        from inbound_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--inbound-smoke')+1:])
    if '--fixes-smoke' in sys.argv:
        from fixes_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--fixes-smoke')+1:])
    if '--business-smoke' in sys.argv:
        from business_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--business-smoke')+1:])
    if '--storage-smoke' in sys.argv:
        from storage_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--storage-smoke')+1:])
    if '--work-io-smoke' in sys.argv:
        from work_io_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--work-io-smoke')+1:])
    if '--workflow-smoke' in sys.argv:
        from workflow_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--workflow-smoke')+1:])
    if '--history-smoke' in sys.argv:
        from history_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--history-smoke')+1:])
    if '--delivery-smoke' in sys.argv:
        from delivery_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--delivery-smoke')+1:])
    if '--normal-smoke' in sys.argv:
        from normal_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--normal-smoke')+1:])
    if '--batch-smoke' in sys.argv:
        from batch_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--batch-smoke')+1:])
    if '--warehouse-smoke' in sys.argv:
        from warehouse_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--warehouse-smoke')+1:])
    if '--work-smoke' in sys.argv:
        from work_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--work-smoke')+1:])
    if '--intake-smoke' in sys.argv:
        from intake_smoke import start_smoke
        start_smoke(app,win,sys.argv[sys.argv.index('--intake-smoke')+1:])
    code=app.exec()
    reset_request=getattr(win,'_factory_reset_request',None)
    order_import_request=getattr(win,'_order_import_request',None)
    # Dispose the entire widget tree while QApplication is still alive. The new
    # archive dialogs retain callbacks; interpreter-finalization order is unsafe
    # for Qt objects in the frozen Windows runtime.
    import shiboken6
    if shiboken6.isValid(win):shiboken6.delete(win)
    instance_lease.close()
    if order_import_request:
        logging.shutdown()
        from order_migration import apply
        try:
            result=apply(order_import_request)
        except Exception as exc:
            QMessageBox.warning(None,'订单迁入未完成',str(exc)+'\n原资料或完整备份已保留，重新打开时会核对恢复记录。');return 1
        QMessageBox.information(None,'订单已迁入','完整订单已迁入，作业和计划保持暂停。重新打开软件后先核验连接和原平台结果。\n原本机完整备份：'+result['backup'])
    if reset_request:
        logging.shutdown()
        from application_reset import perform
        try:
            result=perform(reset_request)
        except Exception as exc:
            QMessageBox.warning(None,'初始化未完成',str(exc)+'\n重新打开程序前，请查看资料及备份目录。');return 1
        QMessageBox.information(None,'全部初始化完成','本机应用资料已初始化。重新打开程序即可使用初始设置。\n完整备份：'+result['backup'])
    primary.close()
    return code


def __getattr__(name):
    # Historical external source callers only; active views never import app.
    if name=="MainWindow":
        from legacy_window import MainWindow
        return MainWindow
    raise AttributeError(name)

if __name__=="__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    if '--catalog-lifetime-helper' in sys.argv:
        from catalog_lifetime_smoke import parent_helper
        parent_helper(sys.argv[sys.argv.index('--catalog-lifetime-helper')+1:])
    if '--catalog-validate' in sys.argv:
        from catalog_sync_validation import cli
        sys.exit(cli(sys.argv[sys.argv.index('--catalog-validate')+1:]))
    if '--storage-copy' in sys.argv:
        from storage_maintenance import cli
        sys.exit(cli(sys.argv[sys.argv.index('--storage-copy')+1:]))
    sys.exit(main())
