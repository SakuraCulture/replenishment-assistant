"""Category overrides use existing model inputs; old configurations stay exact."""
import copy
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import parameter_schema as schema
import store_parameter_import as spi
import engine
import model
import work_repository


def loaded():
    rows=[]
    for i,(category,override,wh) in enumerate([('休闲食品',None,'山东仓'),('个人洗护',None,'河北仓'),('未知分类',None,'山东仓'),('个人洗护','快销','河北仓')],2):
        d=dict(A='测试店',B='T',D='测试商品',E=str(i),H=.5,I=3,J=4,K=8,L=0,N=0,O=1,V=8,S='启用',AB=.4,AC=.6,AD=.2,AU=wh,_source='资料匹配')
        m=dict(export_row=i,product_row=i,match='商品编码精确匹配',blocking=[],notes=[],category=category,export_status='正常',export_updated=None,profile_override=override)
        rows.append((d,m))
    return dict(normalized=rows,config=engine.config(),hashes={},stores={'T':'测试店'})


class ProfileParametersTests(unittest.TestCase):
    def test_old_config_has_no_new_field_and_retains_effective_values(self):
        config=schema.standard();self.assertNotIn('profile_parameters',config)
        before=copy.deepcopy(config);value=schema.apply(loaded(),config)
        self.assertEqual(config,before);self.assertNotIn('profile_parameters',value['config'])
        self.assertTrue(all(d['AI']==12 and d['AJ']==6 for d,m in value['normalized']))

    def test_category_values_warehouse_independence_and_no_source_mutation(self):
        config=schema.standard();config['profile_parameters']={'快销':{'moq_days':7,'moq_price':10},'日百':{'moq_days':12,'moq_price':6}}
        source=loaded();before=copy.deepcopy(source)
        result=schema.apply(source,config)
        self.assertEqual([(d['AI'],d['AJ']) for d,m in result['normalized']],[(7,10),(12,6),(7,10),(7,10)])
        self.assertEqual(source,before)
        for d,m in result['normalized']:
            out=model.calculate(d)
            # The existing model consumes AI/AJ; no new formula is needed.
            self.assertEqual((out['AI'],out['AJ']),(d['AI'],d['AJ']))
        other=schema.apply(source,schema.standard())
        self.assertTrue(all(d['AJ']==6 for d,m in other['normalized']))
        reset=schema.apply(result,schema.standard())
        self.assertNotIn('profile_parameters',reset['config'])
        self.assertTrue(all(d['AJ']==6 for d,m in reset['normalized']))

    def test_threshold_changes_actual_eligibility(self):
        source=loaded();a=schema.standard();b=copy.deepcopy(a)
        b['profile_parameters']={'快销':{'moq_days':7,'moq_price':10}}
        old=schema.apply(source,a)['normalized'][0][0];new=schema.apply(source,b)['normalized'][0][0]
        self.assertEqual(model.calculate(old)['BI'],0)
        self.assertEqual(model.calculate(new)['BI'],1)

    def test_validation_fail_closed(self):
        for values in ({'未知':{'moq_days':1}},{'快销':{'unknown':1}},{'快销':{'moq_days':float('nan')}},{'快销':{'moq_price':True}},{'快销':{'moq_days':-1}},{'快销':{'probability':.49}},{'日百':{'probability':1}}):
            c=schema.standard();c['profile_parameters']=values
            with self.assertRaises(ValueError):schema.validate(c)

    def test_probability_uses_existing_formula_per_category_and_store(self):
        c=schema.standard();c['profile_parameters']={'快销':{'probability':.95},'日百':{'probability':.9}}
        source=loaded()
        for d,m in source['normalized']:d.update(H=10,I=70,J=300,K=0,L=0,V=1)
        configured=schema.apply(source,c)
        result=engine.run_calculation(configured,'T',c['warehouses'],False)
        for (inp,meta),actual in zip(configured['normalized'],result['baseline']):
            d=copy.deepcopy(inp);d.update(AG=c['warehouses'][d['AU']][0],AH=c['warehouses'][d['AU']][1])
            probability=.9 if d['U']=='日百' else .95
            expected=model.calculate(d,model.Rules(probability=probability))
            for key in ['BE','BF','BG','AL','AQ','AR','AS','BV']:self.assertEqual(actual[key],expected[key])
            self.assertEqual(actual['_effective_probability'],probability)
        standard=engine.run_calculation(schema.apply(source,schema.standard()),'T',c['warehouses'],False)
        self.assertTrue(all('_effective_probability' not in d for d in standard['rows']))
        self.assertGreater(result['rows'][0]['BF'],standard['rows'][0]['BF'])

    def test_import_export_persistence_and_clear(self):
        with tempfile.TemporaryDirectory() as temp:
            c=spi.base();c['preset']['config']['profile_parameters']={'快销':{'moq_days':7,'moq_price':10},'日百':{'moq_days':12,'moq_price':6}}
            c['ui'].update(primary='河北仓',secondary='山东仓')
            p=Path(temp)/'parameters.xlsx';spi.write(p,{'T':c})
            plan=spi.plan(spi.read([p]),{})
            self.assertFalse(plan['errors']);self.assertEqual(plan['values']['T']['preset']['config'],c['preset']['config'])
            repo=work_repository.Repository(temp);repo.save_stores(plan['values'],expected={'T':None})
            # Existing SQLite JSON contract stores warehouse tuples as arrays.
            self.assertEqual(repo.stores()['T']['preset']['config'],work_repository.loads(work_repository.dumps(c['preset']['config'])))
            field=spi.field('分类参数/快销/常规资格周转阈值')
            new=spi.patch({'values':{field:'沿用门店'}},c)
            self.assertNotIn('moq_days',new['preset']['config']['profile_parameters']['快销'])
            self.assertEqual(schema.profile_values(new['preset']['config'],'快销')['moq_days'],12)
            # Blank cells in the import are never an instruction to clear.
            self.assertEqual(spi.patch({'values':{}},c),c)

    def test_full_template_exposes_every_editable_parameter(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'template.xlsx';spi.write(path)
            book=openpyxl.load_workbook(path,read_only=True);headers=next(book['门店参数'].values)
            for key,entry in schema.HELP.items():
                if key not in spi.FIXED:self.assertIn(entry[0],headers)
            for group in ('快销','日百'):
                for key in schema.PROFILE_KEYS:self.assertIn('分类参数/'+group+'/'+schema.HELP[key][0],headers)
            book.close()


if __name__=='__main__':unittest.main()
