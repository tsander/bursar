import datetime
import json
import os
import sys

import dotenv
import gspread
import pandas as pd
import requests

if not os.environ.get("IS_DOCKER", False):
    dotenv.load_dotenv()


def get_maps(maps_sheet):
    maps_raw = maps_sheet.get_values()
    maps = []

    headers = maps_raw[0]

    splits = [i for i, x in enumerate(maps_raw[1]) if x == ""] + [len(maps_raw[1])]
    min = 0

    for split_ind in splits:
        source_col = min
        dest_cols = [i for i in range(min + 1, split_ind)]
        min = split_ind + 1

        for dest_col in dest_cols:
            curr_map = {row[source_col]: row[dest_col] for row in maps_raw[1:]}
            maps.append(
                {
                    "source_col": headers[source_col],
                    "dest_col": headers[dest_col],
                    "map": curr_map,
                }
            )

    return maps

def simplefin_accounts_to_dataframe(simplefin_data, maps):
    df = pd.DataFrame(columns=['account', 'balance', 'posted'])

    for account in simplefin_data["accounts"]:
        name = f"{account['org']['name']} - {account['name']}"
        df = pd.concat([pd.DataFrame([[name, account['balance'], pd.to_datetime(account['balance-date'], unit="s")]], columns=df.columns), df], ignore_index=True)

    for map in maps:
        df[map["dest_col"]] = df[map["source_col"]].map(map["map"])
    
    df = df.replace({pd.NA: ""})

    return df


def simplefin_to_dataframe(simplefin_data, maps):
    df = pd.DataFrame()

    for account in simplefin_data["accounts"]:
        name = f"{account['org']['name']} - {account['name']}"

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

    if len(df) == 0:
        return df

    for map in maps:
        df[map["dest_col"]] = df[map["source_col"]].map(map["map"])

    df = df.replace({pd.NA: ""})

    return df


def update_worksheet(ws: gspread.Worksheet, subset, columns, ml_model=None):
    last_col = chr(ord("A") + len(columns) - 1)
    ws_data = pd.DataFrame(ws.get_values(f"A1:{last_col}"), columns=columns).iloc[1:, :]
    sf_data = subset.copy()

    # Identify which transactions from the new batch (sf_data) are NOT already in the Google Sheet
    existing_ids = set(ws_data["id"]) if "id" in ws_data.columns else set()
    new_rows_mask = ~sf_data["id"].isin(existing_ids)
    
    # If the ML model is available, only predict on NEW transactions that are STILL missing a Category
    if ml_model is not None and new_rows_mask.any() and "Category" in sf_data.columns:
        # Create a boolean mask for rows that are both new AND have no category assigned
        uncategorized = new_rows_mask & (sf_data["Category"].fillna("").str.strip() == "")
        
        if uncategorized.any():
            # 1. Extract the features needed by the model for these specific rows
            X_new = sf_data.loc[uncategorized, ["Account", "payee", "description", "amount"]].copy()
            
            # 2. Preprocess the amount column: remove any currency symbols/commas and convert to float
            X_new["amount"] = X_new["amount"].astype(str).str.replace(r"[$,]", "", regex=True)
            X_new["amount"] = pd.to_numeric(X_new["amount"], errors="coerce").fillna(0.0)
            
            # 3. Preprocess the text columns: ensure they are strings and replace NaNs with empty strings
            for col in ["Account", "payee", "description"]:
                X_new[col] = X_new[col].fillna("").astype(str)
                
            # 4. Generate prediction probabilities for each possible category
            probs = ml_model.predict_proba(X_new)
            
            # 5. Find the highest probability for each transaction (this is our confidence score)
            max_probs = probs.max(axis=1)
            
            # 6. Map the highest probability back to the actual category string
            preds = ml_model.classes_[probs.argmax(axis=1)]
            
            # 7. Apply the confidence threshold - only keep predictions where we are >90% sure
            confident_mask = max_probs > 0.90
            
            if confident_mask.any():
                # Get the indices of the rows that met the confidence threshold
                confident_indices = X_new.index[confident_mask]
                
                # Update the original dataframe containing the new batch with these predictions
                sf_data.loc[confident_indices, "Category"] = preds[confident_mask]
                print(f"ML Categorized {len(confident_indices)} transactions with >90% confidence.")

    new_data = (
        pd.concat([ws_data, sf_data])
        .loc[:, columns]
        .drop_duplicates(subset=["id"], keep="first")
        .assign(posted=lambda x: pd.to_datetime(x["posted"], format="mixed", dayfirst=False).dt.strftime("%m/%d/%Y"))
        .sort_values(by=["posted", "account"], ascending=[False, True])
        .fillna("")
        .values.tolist()
    )

    ws.update(f"A2:{last_col}", new_data, value_input_option="USER_ENTERED")

def update_overview(ws: gspread.Worksheet, subset, columns):
    last_col = chr(ord("A") + len(columns) - 1)
    ws_data = pd.DataFrame(ws.get_values(f"A1:{last_col}"), columns=columns).iloc[1:, :]
    sf_data = subset.copy()
    
    new_data = ws_data.merge(sf_data, on='account', how='left', suffixes=[None, "_x"])
    # Update in place the new balance, and posted then drop the temp columns
    new_data['balance'].update(new_data['balance_x'])
    new_data['posted'].update(new_data['posted_x'])
    new_data['Account'].update(new_data['Account_x'])
    # Actually don't think I need this because the columns are dropped below
    new_data.drop(['posted_x', 'balance_x', 'Account_x'], axis=1, inplace=True)

    new_data = (new_data.loc[:, columns]
        .assign(posted=lambda x: pd.to_datetime(x["posted"], format="mixed", dayfirst=False).dt.strftime("%m/%d/%Y"))
        .fillna("").values.tolist())

    ws.update(f"A2:{last_col}", new_data, value_input_option="USER_ENTERED")


def run_update(days_to_fetch):
    # load credentials
    gs_auth = json.load(
        open(os.path.join(os.environ.get("CONFIG_PATH"), "google_auth.json"))
    )
    sf_auth = json.load(
        open(os.path.join(os.environ.get("CONFIG_PATH"), "simplefin_auth.json"))
    )

    gs = gspread.service_account_from_dict(gs_auth)
    sh = gs.open_by_key(os.environ.get("SHEET_ID"))

    # load sheets
    try:
        template_sheet = sh.get_worksheet_by_id(int(os.environ.get("TEMPLATE_GID")))
        maps_sheet = sh.get_worksheet_by_id(int(os.environ.get("MAPS_GID")))
        overview_sheet = sh.get_worksheet_by_id(int(os.environ.get("OVERVIEW_GID")))
    except Exception as e:
        print("Template or maps sheet GID not found. Exiting update.py.")
        exit()

    # fetch data from SimpleFIN
    end = datetime.datetime.now()
    start = end - datetime.timedelta(days=days_to_fetch)
    mparams = {
        "start-date": str(int(start.timestamp())),
        "end-date": str(int(end.timestamp())),
    }
    res = requests.get(
        sf_auth["url"], auth=(sf_auth["username"], sf_auth["password"]), params=mparams
    )
    try:
        data = res.json()
    except json.decoder.JSONDecodeError:
        import sys
        print(f"Failed to decode JSON from response. Raw response text:\n{res.text}", file=sys.stderr)
        raise

    # transform response into dataframe
    maps = get_maps(maps_sheet)
    df_overview = simplefin_accounts_to_dataframe(data, maps)
    
    if len(df_overview) == 0:
        print(
            f"No accounts found for update at {datetime.datetime.now()}."
        )
        return
    
    # get columns to update
    overview_columns = [c.strip() for c in os.environ.get("OVERVIEW_COLUMNS").split(",")]

    update_overview(overview_sheet, df_overview, overview_columns)


    df = simplefin_to_dataframe(data, maps)

    if len(df) == 0:
        print(
            f"No transactions found for {days_to_fetch} day update at {datetime.datetime.now()}."
        )
        return
    
    # Apply Drop Rules from environment (e.g., DROP_RULE_1="Account|YYYY-MM-DD")
    for key, value in os.environ.items():
        if key.startswith("DROP_RULE_"):
            try:
                # 1. Parse and Clean Rule
                clean_val = value.strip('"').strip("'")
                if "|" not in clean_val:
                    continue
                
                print(f"Applying drop rule from {key}: '{clean_val}'")
                rule_account, rule_date_str = clean_val.split("|")
                cutoff_dt = pd.to_datetime(rule_date_str.strip())
                
                # 2. Convert feed dates to datetime objects for safe comparison
                # Note: 'posted' is MM/DD/YYYY string format here
                feed_dates = pd.to_datetime(df['posted'], format='%m/%d/%Y')

                # 3. Create a mask for rows that match account AND date cutoff
                mask = (df['account'] == rule_account.strip()) & (feed_dates >= cutoff_dt)

                dropped_count = mask.sum()
                if dropped_count > 0:
                    print(f"[{key}] Dropping {dropped_count} transactions from '{rule_account.strip()}' on/after {rule_date_str.strip()}")
                    df = df[~mask].reset_index(drop=True)
                else:
                    print(f"[{key}] No matching transactions found to drop for '{rule_account.strip()}'")
                    
            except Exception as e:
                print(f"Error processing {key} ('{value}'): {e}")

    if len(df) == 0:
        print(f"All transactions filtered out by drop rules.")
        return

    # apply rules
    try:
        rules_sheet = sh.worksheet("Rules")
        from rules import apply_rules
        df = apply_rules(df, rules_sheet)
    except gspread.exceptions.WorksheetNotFound:
        print("Rules worksheet not found. Skipping rule application.")
    except Exception as e:
        print(f"Error evaluating rules: {e}")

    # get columns to update
    columns = [c.strip() for c in os.environ.get("TEMPLATE_COLUMNS").split(",")]

    # load ML model if it exists
    ml_model = None
    model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model.joblib")
    if os.path.exists(model_path):
        import joblib
        try:
            ml_model = joblib.load(model_path)
            print("Loaded ML Categorization model.")
        except Exception as e:
            print(f"Failed to load ML model: {e}")

    # update affected sheets
    worksheets = {s.title: s.id for s in sh.worksheets()}
    for period in sorted(set(df["period"])):
        subset = df.loc[df["period"] == period, :].reset_index(drop=True)

        if period in worksheets.keys():
            ws = sh.get_worksheet_by_id(worksheets[period])
        else:
            ws = sh.duplicate_sheet(
                template_sheet.id, insert_sheet_index=1, new_sheet_name=period
            )
            worksheets[period] = ws.id

        update_worksheet(ws, subset, columns, ml_model)

    print(f"Completed {days_to_fetch} day update at {datetime.datetime.now()}.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        days = int(sys.argv[1])
    else:
        days = 1
    run_update(days)
