import copy
import json
import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import parameter_guidance as guide
import parameter_schema as schema
import parameter_maintenance as pm
import automation_jobs
from automation_protocol import validate_call, CommandError, public_tools
from business_presenter import business_help
from store_parameter_import import base


class GuidanceTests(unittest.TestCase):
    def test_original_help_is_the_single_business_explanation_source(self):
        for key, value in schema.HELP.items():
            d=guide.describe(('parameters',key))
            self.assertEqual(d['explanation'],business_help(value[2]))
            self.assertIn(d['explanation'],guide.help_text(('parameters',key)))
        for key,value in schema.WEIGHT_HELP.items():
            self.assertEqual(guide.describe(('weight_profiles','快销',key))['explanation'],business_help(value))

    def test_all_editable_paths_resolve_without_mutating_config(self):
        record=base();c=record['preset']['config'];before=copy.deepcopy(record)
        definitions=guide.definitions();self.assertEqual(len(definitions),len({d['id'] for d in definitions}))
        rows=[{'parameters':automation_jobs.parameters(record)}]
        data=guide.referenced(rows)
        for f in pm.fields([c])+pm.fields([c],'common'):
            d=guide.describe(f);self.assertTrue(d['ai_trial_editable']);self.assertIn(d['id'],data['definitions'])
        self.assertEqual(record,before)
        data['definitions']['parameters.probability']['constraints']['minimum']=0
        self.assertEqual(guide.describe(('parameters','probability'))['constraints']['minimum'],.5)

    def test_common_constraints_agree_with_existing_validator(self):
        for key in schema.HELP:
            d=guide.describe(('parameters',key));bounds=d['constraints']
            candidates=[(bounds.get('minimum',bounds.get('exclusiveMinimum',0))-.01,False)]
            if 'const' in bounds:candidates=[(29,False),(30,True),(31,False)]
            else:
                if 'maximum' in bounds:candidates.extend([(bounds['maximum'],True),(bounds['maximum']+.01,False)])
                edge=bounds.get('minimum',bounds.get('exclusiveMinimum',0))
                candidates.append((edge,'minimum' in bounds))
                candidates.append((edge+.01,True))
            for value,expected in candidates:
                with self.subTest(key=key,value=value):
                    c=schema.standard();c['parameters'][key]=value
                    # Satisfy unrelated cross-field constraints for this boundary probe.
                    if key=='exit_monthly':c['parameters']['strict_monthly']=0
                    if key=='strict_monthly':c['parameters']['exit_monthly']=max(10,value+1)
                    try:schema.validate(c);actual=True
                    except ValueError:actual=False
                    self.assertEqual(actual,expected)

    def test_dependencies_warehouse_weights_and_monthly_thresholds(self):
        for field,changes in [
            (('warehouses','山东仓',0),{('warehouses','山东仓',0):0,('warehouses','山东仓',1):0}),
            (('weight_profiles','快销','w1'),{('weight_profiles','快销','w1'):.7,('weight_profiles','快销','w2'):.4}),
            (('parameters','strict_monthly'),{('parameters','strict_monthly'):10,('parameters','exit_monthly'):10})]:
            self.assertTrue(guide.describe(field)['dependencies'])
            with self.assertRaises(ValueError):pm.patch(schema.standard(),changes)
        c=pm.patch(schema.standard(),{('warehouses','山东仓',0):0,('warehouses','山东仓',1):1})
        self.assertEqual(c['warehouses']['山东仓'],[0.0,1.0])

    def test_probability_encoding_and_category_inheritance(self):
        path=('profile_parameters','快销','probability');d=guide.describe(path)
        self.assertIn('0.95',d['input_encoding']);self.assertIn('沿用门店',d['alternatives'])
        c=pm.patch(schema.standard(),{path:.95});self.assertEqual(schema.profile_values(c,'快销')['probability'],.95)
        c=pm.patch(c,{path:'沿用门店'});self.assertEqual(schema.profile_values(c,'快销')['probability'],c['parameters']['probability'])
        for value in (95,'95%',True,float('nan'),.49,1):
            with self.subTest(value=value),self.assertRaises(ValueError):pm.patch(schema.standard(),{path:value})

    def test_fixed_derived_policy_and_mapping_cannot_be_trial_writes(self):
        paths=[('parameters','depth'),('parameters','lead'),('parameters','extreme_days'),
               ('weight_profiles','快销','w3'),('exception',),('restock_switches','new_stocking'),('category_profiles','测试')]
        for path in paths:
            self.assertFalse(guide.describe(path)['ai_trial_editable'])
            with self.assertRaises(ValueError):pm.patch(schema.standard(),{path:1})

    def test_catalog_pages_search_and_unknown_fields(self):
        all_rows=guide.catalog({});items=[];offset=0
        while offset is not None:
            page=guide.catalog({'offset':offset,'limit':3});items.extend(page['items']);offset=page['next_offset']
        self.assertEqual(items,all_rows['items'])
        self.assertTrue(guide.catalog({'search':'安全库存'})['items'])
        self.assertTrue(all(x['group']=='policies' for x in guide.catalog({'group':'policies'})['items']))
        for args in ({'limit':101},{'group':'budget'},{'set':1},{'offset':-1}):
            with self.assertRaises(CommandError):validate_call('parameter_definitions',args)
        self.assertTrue(next(t for t in public_tools() if t['name']=='parameter_definitions')['annotations']['readOnlyHint'])
        with self.assertRaises(ValueError):guide.describe(('parameters','does-not-exist'))

    def test_shared_response_not_repeated_per_store(self):
        original=base();one=[{'parameters':automation_jobs.parameters(original)}]
        many=[{'parameters':automation_jobs.parameters(original)} for _ in range(100)]
        self.assertEqual(guide.referenced(one),guide.referenced(many))
        self.assertLess(len(json.dumps(guide.referenced(many),ensure_ascii=False)),40000)


if __name__=='__main__':unittest.main()
