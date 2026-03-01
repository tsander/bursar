import datetime
import json
import os
import sys
import dotenv
import gspread
import pandas as pd
import requests

dotenv.load_dotenv()

# load credentials
gs_auth = json.load(
    open(os.path.join(os.environ.get("CONFIG_PATH"), "google_auth.json"))
)
sf_auth = json.load(
    open(os.path.join(os.environ.get("CONFIG_PATH"), "simplefin_auth.json"))
)

gs = gspread.service_account_from_dict(gs_auth)
sh = gs.open_by_key(os.environ.get("SHEET_ID"))

columns = [c.strip() for c in os.environ.get("TEMPLATE_COLUMNS").split(",")]
print("Columns:", columns)

# Get 2026.01 sheet
ws = sh.worksheet("2026.01")
last_col = chr(ord("A") + len(columns) - 1)
raw_vals = ws.get_values(f"A1:{last_col}")
data_rows = [r[:len(columns)] + [""] * (len(columns) - len(r)) for r in raw_vals[1:]]
ws_data = pd.DataFrame(data_rows, columns=columns)

# filter specifically for Vanguard
vanguard_ws = ws_data[ws_data['account'].str.contains("Vanguard", na=False, case=False)]
print("\n--- Vanguard in Sheet ---")
print(vanguard_ws[['posted', 'description', 'Category', 'amount', 'account', 'id']].head(20).to_string())

# fetch data from SimpleFIN for 246 days
end = datetime.datetime.now()
start = end - datetime.timedelta(days=246)
mparams = {
    "start-date": str(int(start.timestamp())),
    "end-date": str(int(end.timestamp())),
}
res = requests.get(
    sf_auth["url"], auth=(sf_auth["username"], sf_auth["password"]), params=mparams
)
data = res.json()

# filter specifically for Vanguard in SimpleFin
def simplefin_to_dataframe(simplefin_data):
    df = pd.DataFrame()
    for account in simplefin_data["accounts"]:
        name = f"{account['org']['name']} - {account['name']}"
        if "Vanguard" not in name: continue
        if len(account["transactions"]) == 0:
            continue
        transactions = (
            pd.json_normalize(account["transactions"])
            .assign(
                period=lambda x: pd.to_datetime(x["posted"], unit="s").dt.strftime(
                    "%Y.%m"
                )
            )
            .assign(
                posted=lambda x: pd.to_datetime(x["posted"], unit="s").dt.strftime(
                    "%m/%d/%Y"
                )
            )
            .assign(account=name)
        )
        df = pd.concat([df, transactions])
    return df

sf_data = simplefin_to_dataframe(data)
if not sf_data.empty:
    sf_data = sf_data[sf_data['period'] == '2026.01']
    print("\n--- Vanguard in Feed (2026.01) ---")
    print(sf_data[['id', 'amount', 'posted', 'description']].head(20).to_string())
    
    # Simulate user deleting the duplicates from the sheet
    # The user kept the ones with Category="Transfer".
    # We will remove the rows from vanguard_ws where Category == ""
    vanguard_ws_cleaned = vanguard_ws[vanguard_ws['Category'] == 'Transfer'].copy()
    
    print(f"\n--- Simulating User Deleting Duplicates ---")
    print(f"Cleaned Sheet Rows: {len(vanguard_ws_cleaned)}")

    # Test our new reconcile_and_merge directly
    import sys
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from src.update import reconcile_and_merge
    
    # Test it
    merged_df = reconcile_and_merge(vanguard_ws_cleaned, sf_data, columns)
    print(f"\n--- After Reconcile and Merge ---")
    print(f"Original Sheet Rows (Cleaned): {len(vanguard_ws_cleaned)}")
    print(f"Original Feed Rows: {len(sf_data)}")
    print(f"Final Merged Rows: {len(merged_df)}")
    print("\nMerged Data:")
    print(merged_df[['posted', 'description', 'Category', 'amount', 'account', 'id', 'Notes']].to_string())
    
else:
    print("\n--- No Vanguard data in Feed ---")
