import unittest
import pandas as pd
from rules import apply_rules

class TestRules(unittest.TestCase):
    def test_apply_rules_string_match(self):
        df = pd.DataFrame([
            {'id': 1, 'description': 'Target Store 123', 'amount': -50.0},
            {'id': 2, 'description': 'Walmart', 'amount': -30.0}
        ])
        
        rules = [{
            'description': 'Target',
            'Replacement': 'Category=Groceries;payee=Target'
        }]
        
        result = apply_rules(df, rules)
        
        self.assertEqual(result.loc[0, 'Category'], 'Groceries')
        self.assertEqual(result.loc[0, 'payee'], 'Target')
        
        # Walmart should be unaffected
        self.assertTrue('Category' not in result.columns or pd.isna(result.loc[1, 'Category']) or result.loc[1, 'Category'] == "")
        
    def test_apply_rules_amount_match(self):
        df = pd.DataFrame([
            {'id': 1, 'description': 'HOA Dues', 'amount': -603.00},
            {'id': 2, 'description': 'HOA Dues', 'amount': -500.00}
        ])
        
        rules = [{
            'description': 'HOA',
            'amount': '% == -603.00',
            'Replacement': 'Category=Mortgage & Rent;payee=Montecito HOA'
        }]
        
        result = apply_rules(df, rules)
        self.assertEqual(result.loc[0, 'Category'], 'Mortgage & Rent')
        self.assertEqual(result.loc[0, 'payee'], 'Montecito HOA')
        
        # -500.00 should be unaffected
        self.assertTrue('Category' not in result.columns or pd.isna(result.loc[1, 'Category']) or result.loc[1, 'Category'] == "")
        
    def test_apply_rules_amount_inequality(self):
        df = pd.DataFrame([
            {'id': 1, 'description': 'Gas', 'amount': -40.00},
            {'id': 2, 'description': 'Gas', 'amount': -100.00}
        ])
        
        rules = [{
            'description': 'Gas',
            'amount': '% < -50',
            'Replacement': 'Category=Big Gas'
        }]
        
        result = apply_rules(df, rules)
        self.assertTrue('Category' not in result.columns or pd.isna(result.loc[0, 'Category']) or result.loc[0, 'Category'] == "")
        self.assertEqual(result.loc[1, 'Category'], 'Big Gas')

if __name__ == '__main__':
    unittest.main()
