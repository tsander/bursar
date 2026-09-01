import operator
import pandas as pd
import re
from retry_utils import retry_call

def parse_amount_condition(amount_val, rule_str):
    """
    Evaluates whether a transaction amount matches a given rule condition.
    
    The rule condition can be:
      1. A mathematical comparison starting with `% `, e.g., `% == -603.00`, `% < -50`
      2. An exact numerical match, e.g., `-603.00`
      3. An exact string match, e.g., `"-603.00"`
      
    Args:
        amount_val (float/str): The transaction amount from the data feed.
        rule_str (str): The rule condition from the Google Sheet cell.
        
    Returns:
        bool: True if the amount matches the rule condition, False otherwise.
    """
    # If the rule cell is completely blank, we consider it a match (i.e. we don't drop the row for this rule)
    if pd.isna(rule_str) or not str(rule_str).strip():
        return True
    
    rule_str = str(rule_str).strip()

    # Look for patterns like `% == 100`, `%<50`, `%>=-603`
    # Group 1 extracts the operator (==, !=, >=, <=, >, <)
    # Group 2 extracts the number itself (supporting negative signs and decimals)
    match = re.search(r'%\s*(==|!=|>=|<=|>|<)\s*(-?\d+(\.\d+)?)', rule_str)
    
    # Try converting the transaction's amount into a float so we can do math on it.
    try:
        amt = float(amount_val)
    except (ValueError, TypeError):
        # If we can't parse the transaction's amount as a number, we fail the match.
        return False

    # If the user provided a mathematical comparison (e.g., `% < 0`)
    if match:
        op = match.group(1)       # e.g., '=='
        val = float(match.group(2)) # e.g., '-603.00'
        
        # Evaluate the mathematical operator against the transaction amount
        ops = {
            '==': operator.eq,
            '!=': operator.ne,
            '>': operator.gt,
            '<': operator.lt,
            '>=': operator.ge,
            '<=': operator.le
        }
        return ops[op](amt, val)
        
    # If the user just typed a number or string into the rule sheet without the `% ==` prefix
    else:
        try:
            # First try parsing their rule as an exact number match
            val = float(rule_str)
            return amt == val
        except ValueError:
            # If it's not a number, fallback to an exact string comparison
            return str(amount_val).strip() == rule_str
            
    return False

def check_string_match(val, rule_val):
    """
    Evaluates whether a string column (like description or payee) contains the rule text.
    Handles three cases as defined by the sheet format:
      1. Exact match: Starts with double quote (e.g. `"ExactValue"`)
      2. Regular Expression match: Starts and ends with slash (e.g. `/regex pattern/`)
      3. Substring match: Fallback default (e.g. `SubstringToFind`)
    
    Args:
        val (str): The actual transaction field value (e.g. "Target Store 123")
        rule_val (str): The rule substring to look for, possibly wrapped in quotes or slashes.
        
    Returns:
        bool: True if it's a match, False otherwise.
    """
    # If the rule cell is empty, it doesn't restrict the match (returns True).
    if pd.isna(rule_val) or not str(rule_val).strip():
        return True
        
    # If the transaction has a blank value but the rule requires a string, it fails.
    if pd.isna(val):
        return False
        
    rule_str = str(rule_val).strip()
    val_str = str(val).strip()
    
    # 1. Exact Empty Match Handling
    if rule_str == '--':
        return val_str == ''

    # 2. Exact Match Handling
    if rule_str.startswith('"') and rule_str.endswith('"'):
        # Strip exact match wrapper
        exact_text = rule_str[1:-1]
        # Regex equivalent in script: '^'+value.substring(1, value.length - 1)+'$'
        # Can just use string == here since it's cleaner than regex compiling
        return val_str.lower() == exact_text.lower()
        
    # 2. Regex Match Handling
    if rule_str.startswith('/') and rule_str.endswith('/'):
        # Strip regex slash wrapper
        regex_pattern = rule_str[1:-1]
        try:
            return bool(re.search(regex_pattern, val_str, re.IGNORECASE))
        except re.error:
            # If the user's regex is invalid, we'll fail the match rather than crash the scraper
            return False

    # 3. Substring (Default) Handling
    return rule_str.lower() in val_str.lower()

def apply_rules(df, rules_ws):
    """
    Applies the replacement rules defined in the Google Sheet to the incoming bank transactions.
    
    The Google Sheet "Rules" tab is expected to have columns like `description`, `amount`, and `Replacement`.
    A rule matches if ALL provided column criteria patch the transaction.
    When a match occurs, the `Replacement` string (e.g. `Category=Groceries;payee=Target`) is parsed
    and those respective columns are updated in the data payload.
    
    Args:
        df (pd.DataFrame): The incoming new transactions from SimpleFin.
        rules_ws (gspread.Worksheet or list): The Google Sheets 'Rules' worksheet (or list of dicts for testing).
        
    Returns:
        pd.DataFrame: A modified dataframe with the matching rows updated according to the replacements.
    """
    # Nothing to do if we have no transactions
    if df.empty:
        return df
        
    # Fetch all the rule rows from the Google Sheet
    try:
        # Support passing a list of dicts directly for easier local testing
        if isinstance(rules_ws, list):
            rules_data = rules_ws
        else:
            # get_all_records() turns the sheet into a list of dictionaries, one per row.
            rules_data = retry_call(rules_ws.get_all_records)
    except Exception as e:
        print(f"Error fetching rules: {e}")
        return df
        
    # If the rules tab is completely empty, return the original data untouched
    if not rules_data:
        return df
        
    # Create a copy so we aren't modifying the parameter by reference directly
    df_result = df.copy()
    
    # Process each rule row one by one from top to bottom
    for rule in rules_data:
        # The 'Replacement' column tells us what to change if the rule matches
        replacement_str = rule.get('Replacement', '')
        
        # If there is no Replacement command, skip this rule
        if not replacement_str or pd.isna(replacement_str):
            continue
            
        # Create a boolean mask initialized to True for every row in the dataframe.
        # Iterate through each rule column if it does not match change the mask to False.
        # Thus, only rows that match ALL non-empty rule columns remain True.
        mask = pd.Series(True, index=df_result.index, dtype=bool)
        rule_applied = False
        
        # Evaluate each column constraint in the rule
        for col, rule_val in rule.items():
            # Skip the Replacement column itself resulting from the iteration
            # Also skip any criteria columns where the user left the cell blank
            if col == 'Replacement' or pd.isna(rule_val) or not str(rule_val).strip():
                continue
                
            # If the rule tries to filter on a column that doesn't exist in our feed yet, 
            # no transactions can possibly match this condition. We can simply fail the rule match.
            if col not in df_result.columns:
                rule_applied = False
                break
                
            rule_applied = True
            
            # Apply the appropriate logic based on whether we're filtering on 'amount' or a string column
            if col.lower() == 'amount':
                # Apply the special amount parser which supports percent signs (e.g. `% == 100`)
                mask = mask & df_result['amount'].apply(lambda x: parse_amount_condition(x, rule_val)).astype(bool)  # type: ignore
            else:
                # Apply the case-insensitive substring search for normal text fields
                mask = mask & df_result[col].apply(lambda x: check_string_match(x, rule_val)).astype(bool)  # type: ignore
                
        # If the rule had valid criteria and found at least one matching transaction
        if rule_applied and any(mask):
            # Parse the Replacement string into a dictionary. 
            # Example: 'Category=Mortgage & Rent;payee=Montecito HOA' -> {'Category': 'Mortgage & Rent', 'payee': 'Montecito HOA'}
            replacements = {}
            parts = str(replacement_str).split(';')
            for part in parts:
                if '=' in part:
                    # Split only on the first '=' to handle values that might contain an '=' symbol
                    k, v = part.split('=', 1)
                    replacements[k.strip()] = v.strip()
                    
            # If the parsing failed to find any valid replacements, skip this rule
            if not replacements:
                continue

            # For each key=value pair in our configured Replacement string
            for k, v in replacements.items():
                # If the target substitution column doesn't exist yet, create it and fill with empty strings.
                if k not in df_result.columns:
                    df_result[k] = ""
                    
                # Finally, overwrite the target column with the target value for ONLY the matched rows.
                df_result.loc[mask, k] = v
                
            # Append "Source=Rule" to the Tags column to track that a rule modified this transaction
            if 'Tags' not in df_result.columns:
                df_result['Tags'] = ""
            df_result.loc[mask, 'Tags'] = df_result.loc[mask, 'Tags'].astype(str).apply(
                lambda t: t + ", Source=Rule" if t and "Source=Rule" not in t else ("Source=Rule" if "Source=Rule" not in t else t)
            )
                    
    return df_result
