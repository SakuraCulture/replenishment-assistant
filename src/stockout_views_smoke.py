"""Synthetic Qt acceptance of multi-dimension editors and the real export button."""


def verify(page,entry,out,wait,idle,request,require):
    import openpyxl
    from PySide6.QtCore import Qt,QTimer
    from PySide6.QtWidgets import QApplication,QDialog,QFileDialog,QTabWidget
    from unittest.mock import patch
    from stockout_views_ui import CombinationDialog,FilterDialog,ExportDialog,check_item
    from stockout_metrics import DIMENSIONS

    def drive(action,kind,callback):
        errors=[]
        def interact():
            dialog=QApplication.activeModalWidget()
            try:
                assert isinstance(dialog,kind),str(type(dialog))
                callback(dialog)
            except BaseException as exc:
                errors.append(exc)
                if isinstance(dialog,QDialog):dialog.reject()
        QTimer.singleShot(100,interact);action()
        if errors:raise errors[0]
        wait(idle)

    page.findChild(QTabWidget).setCurrentIndex(1)
    dims=['store','warehouse','supplier','category','mandatory','sku']
    def configure(dialog):
        dialog.add();dialog.name.setText('门店六维组合');dialog.fields.clear()
        for key in dims+[k for k in DIMENSIONS if k not in dims]:check_item(dialog.fields,DIMENSIONS[key][1],key,key in dims)
        dialog.grab().save(str(out/'07-custom-combinations.png'));dialog.accept()
    drive(page.manage_views_btn.click,CombinationDialog,configure)
    require(page.dimensions()==dims and len(page.cut_dims)==6,'Actual custom editor saves and applies six ordered dimensions')
    require(request('open')['views']['active']==dims,'Saved combinations persist in analysis-only preferences')
    require([page.cuts.horizontalHeaderItem(i).text() for i in range(8)]==['门店编码','门店名称','供货仓','仓库上游供货商','商品类目','必采属性','商品编码','商品名称'],
            'One cut row contains all chosen dimensions with store and product identities')
    require(sum(r['base'] for r in page.cut_rows)==8 and sum(r['zero'] for r in page.cut_rows)==5,'Six-dimension cuts conserve the default status-filtered totals')
    require(all(r['warehouse']=='地采' for r in page.cut_rows if r['sku']=='L'),'Local product warehouse reads as local purchase')
    page.grab().save(str(out/'08-six-dimension-cut.png'))

    def select_filters(dialog):
        wait(idle)
        require(all('测试门店' in dialog.values.item(i).text() for i in range(dialog.values.count())),'Multi-select store candidates show code and name')
        dialog.check_page(True)
        warehouse=next(i for i in range(dialog.dimensions.count()) if dialog.dimensions.item(i).data(Qt.ItemDataRole.UserRole)=='warehouse')
        dialog.dimensions.setCurrentRow(warehouse);wait(idle)
        item=next(dialog.values.item(i) for i in range(dialog.values.count()) if dialog.values.item(i).data(Qt.ItemDataRole.UserRole)=='地采')
        item.setCheckState(Qt.CheckState.Checked)
        dialog.selected_only.setChecked(True)
        require(dialog.values.count()==1 and dialog.values.item(0).text()=='地采','Selected-only filter view retains chosen warehouse')
        dialog.grab().save(str(out/'09-multiselect-filters.png'));dialog.accept()
    drive(page.filter_btn.click,FilterDialog,select_filters)
    require(set(page.filters['store'])=={'S','T'} and page.filters['warehouse']==['地采'],'Actual filter dialog applies OR within stores and AND across warehouse')
    require('缺货 2 / 有销 2' in page.total.text() and len(page.detail_rows)==2 and all(r['sku']=='L' for r in page.detail_rows),'Cards, cuts and detail honor the same multi-dimension filter')

    target=out/'custom-views.xlsx'
    def choose_export(dialog):
        dialog.check_all(False)
        for i in range(dialog.listing.count()):
            item=dialog.listing.item(i)
            if item.data(Qt.ItemDataRole.UserRole)['name'] in {'门店六维组合','门店总览'}:item.setCheckState(Qt.CheckState.Checked)
        dialog.grab().save(str(out/'10-export-combinations.png'));dialog.accept()
    with patch.object(QFileDialog,'getSaveFileName',return_value=(str(target),'Excel (*.xlsx)')):
        drive(page.export_btn.click,ExportDialog,choose_export)
    require(target.is_file(),'Actual export button writes the selected combination workbook')
    book=openpyxl.load_workbook(target)
    try:
        for name in ('门店六维组合','门店总览'):
            ws=book[name];rows=list(ws.values);headers=rows[0]
            require(sum(r[headers.index('有销基数')] for r in rows[1:])==2 and sum(r[headers.index('缺货数')] for r in rows[1:])==2,
                    name+' sheet reconciles with the filtered page')
            require(ws.freeze_panes=='A2' and ws.auto_filter.ref==f'A1:{openpyxl.utils.get_column_letter(ws.max_column)}{ws.max_row}',
                    name+' sheet contains a real Excel filter and frozen header')
        require('当前切面' not in book.sheetnames and '门店供货结构' not in book.sheetnames,'Unselected cut combinations are not exported')
    finally:book.close()

    def close_pending(dialog):
        dialog.search.setText('不存在的候选');dialog.restart();dialog.reject()
    drive(page.filter_btn.click,FilterDialog,close_pending)
    page.clear_filters();wait(idle)
    require('缺货 5 / 有销 8' in page.total.text(),'Closing an in-flight filter and clearing selection leaves the page usable')
