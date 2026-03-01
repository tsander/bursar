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


def normalize_amount(val):
    if pd.isnull(val) or val == "":
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    val = str(val).replace('$', '').replace(',', '').strip()
    try:
        return float(val)
    except ValueError:
        return 0.0

def reconcile_and_merge(ws_df, sf_df, columns):
    """
    Reconciles the Google Sheet Data (ws_df) with SimpleFin Feed Data (sf_df).
    Logic:
    1. Update existing IDs (Exact Match).
    2. Reconcile Orphans (Match on Amt/Date/Desc -> Update ID/Account).
    3. Add New Arrivals (with Time-Based Suppression for Zombies).
    """
    ws_df = ws_df.copy()
    sf_df = sf_df.copy()

    # 1. Setup
    # Ensure date parsing is robust
    ws_df['posted_dt'] = pd.to_datetime(ws_df['posted'], format="mixed", dayfirst=False, errors='coerce')
    sf_df['posted_dt'] = pd.to_datetime(sf_df['posted'], format="mixed", dayfirst=False, errors='coerce')
    
    # Normalize amounts
    ws_df['amount_num'] = ws_df['amount'].apply(normalize_amount)
    sf_df['amount_num'] = sf_df['amount'].apply(normalize_amount)
    
    # Track consumed feed IDs to prevent double-adding
    feed_ids_consumed = set()
    
    # Prepare result rows
    final_rows = []
    
    # Helper to find match in feed
    def find_match(row, candidates):
        # Filter the feed to find rows with an exact amount match that haven't been consumed yet
        matches = candidates[
            (candidates['amount_num'] == row['amount_num']) & 
            (~candidates['id'].isin(feed_ids_consumed))
        ].copy()
        
        if matches.empty:
            return None
            
        # Filter anything that isn't within 4 days of the posted date.
        if pd.notnull(row['posted_dt']):
            matches['date_diff'] = (matches['posted_dt'] - row['posted_dt']).abs().dt.days
            matches = matches[matches['date_diff'] <= 4]
        
        if matches.empty:
            return None
            
        # Description filter (Strict exact match for now)
        exact_desc = matches[matches['description'] == row['description']]
        if not exact_desc.empty:
            return exact_desc.iloc[0]
            
        return None
    
    # 2. Process Existing Sheet Rows
    for _, row in ws_df.iterrows():
        row_id = row.get('id', '')
        if not row_id:
            continue
        new_row = row.copy()
        
        # Check if ID exists in Feed (Exact Match)
        if row_id in sf_df['id'].values:
            # Get the feed with the same id
            feed_match = sf_df[sf_df['id'] == row_id].iloc[0]
            # Mark as consumed
            feed_ids_consumed.add(row_id)
            # Copy certain fields from the feed
            new_row['posted'] = feed_match['posted']
            new_row['amount'] = feed_match['amount']
            new_row['description'] = feed_match['description']
            new_row['account'] = feed_match['account']
        else:
            # Orphan! See if there is a possible duplicate
            match = find_match(new_row, sf_df)
            if match is not None:
                # Duplicate found! Update the id and mark as consumed.
                new_row['id'] = match['id']
                feed_ids_consumed.add(match['id'])

                # Update Account shouldn't happen often, but SimpleFin sometimes renames
                # accounts, or a new card will change the account name.
                if new_row['account'] != match['account']:
                    new_row['account'] = match['account']
                    new_row['Notes'] = f"{row.get('Notes', '')} [Acct Updated]".strip()
                
                # Update other fields
                new_row['posted'] = match['posted']
                new_row['description'] = match['description']
                new_row['amount'] = match['amount'] # update to raw format
            else:
                # TRUE ORPHAN
                
                # We need to know the oldest date we fetched for this specific account
                # to know if this transaction *should* be in the feed
                account_name = row.get('account', '')
                acct_feed = sf_df[sf_df['account'] == account_name]
                
                if not acct_feed.empty:
                    min_feed_dt = acct_feed['posted_dt'].min()
                    
                    # If the transaction is newer than the oldest updated feed item for this account,
                    # AND it is missing, it may be an Orphan let the user decide.
                    if pd.notnull(min_feed_dt) and pd.notnull(new_row['posted_dt']):
                        if new_row['posted_dt'] >= min_feed_dt:
                            cols_to_check = ['Notes'] if 'Notes' in row else []
                            if cols_to_check:
                                curr_notes = str(row.get('Notes', ''))
                                if "[Orphan?]" not in curr_notes:
                                    new_row['Notes'] = f"{curr_notes} [Orphan?]".strip()
        
        final_rows.append(new_row.to_dict())

    # 3. Process New Arrivals (Feed IDs not consumed)
    # Get all feed rows where ID is not in consumed
    new_arrivals = sf_df[~sf_df['id'].isin(feed_ids_consumed)].copy()
    
    for _, feed_row in new_arrivals.iterrows():
        # PASS 3: Time-Based Suppression
        # Collision Check: Does this match ANY existing sheet row (content-wise)?
        # Regardless of ID.
        has_collision = False
        
        # Check against original ws_df for consistency
        # Find rows with same Amount & Description
        # (Date fuzzy match is expensive here, lets do exact date for collision check or strict window)
        collision_candidates = ws_df[
            (ws_df['amount_num'] == feed_row['amount_num']) &
            (ws_df['description'] == feed_row['description'])
        ]
        
        if not collision_candidates.empty:
            # Check dates
            feed_dt = feed_row['posted_dt']
            if pd.notnull(feed_dt):
                # If any existing row is within 2 days
                for _, exist_row in collision_candidates.iterrows():
                    exist_dt = exist_row['posted_dt']
                    if pd.notnull(exist_dt) and abs((feed_dt - exist_dt).days) <= 2:
                        has_collision = True
                        break

        # Check against final_rows (new arrivals added in THIS run)
        if not has_collision:
            for f_row in final_rows:
                if 'amount' in f_row and normalize_amount(f_row['amount']) == feed_row['amount_num']:
                    if f_row.get('description') == feed_row['description']:
                        f_dt = f_row.get('posted_dt')
                        if pd.notnull(f_dt) and pd.notnull(feed_row['posted_dt']):
                            if abs((feed_row['posted_dt'] - f_dt).days) <= 2:
                                has_collision = True
                                break
                            
        if has_collision:
            # We suspect this is a duplicate (same amount, description, and very close date)
            # but maybe a different ID.
            
            # Let the user review it rather than silently ignoring it.
            new_feed_dict = feed_row.to_dict()
            cols_to_check = ['Notes'] if 'Notes' in ws_df.columns else [] # Use ws_df to see if column exists
            
            if cols_to_check:
                # new_feed_row is a series from the feed, it might not have 'Notes' yet
                curr_notes = str(new_feed_dict.get('Notes', ''))
                if "[Duplicate?]" not in curr_notes:
                    new_feed_dict['Notes'] = f"{curr_notes} [Duplicate?]".strip()
                    
            final_rows.append(new_feed_dict)
            continue # Moved to the next row
            
        else:
            # No collision, definitely new
            final_rows.append(feed_row.to_dict())
            
    # Reconstruct DataFrame
    result_df = pd.DataFrame(final_rows)
    # Ensure columns match
    # Fill missing cols
    for col in columns:
        if col not in result_df.columns:
            result_df[col] = ""
            
    return result_df


def update_worksheet(ws: gspread.Worksheet, subset, columns):
    last_col = chr(ord("A") + len(columns) - 1)
    
    # Read existing sheet
    raw_vals = ws.get_values(f"A1:{last_col}")
    if len(raw_vals) > 1:
        # len(columns) isn't needed, but it is protective. Adding blank text ensures the rows are the same length as the columns
        data_rows = [r[:len(columns)] + [""] * (len(columns) - len(r)) for r in raw_vals[1:]]
        ws_data = pd.DataFrame(data_rows, columns=columns)
    else:
        ws_data = pd.DataFrame(columns=columns)

    sf_data = subset.copy()

    # Run Reconciliation
    merged_df = reconcile_and_merge(ws_data, sf_data, columns)
    
    # Sort and Format
    new_data = (
        merged_df
        .loc[:, columns]
        # .drop_duplicates(subset=["id"], keep="first") # Logic handled in reconcile
        .assign(posted=lambda x: pd.to_datetime(x["posted"], format="mixed", dayfirst=False, errors='coerce').dt.strftime("%m/%d/%Y"))
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
    data = res.json()

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

    # get columns to update
    columns = [c.strip() for c in os.environ.get("TEMPLATE_COLUMNS").split(",")]

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

        update_worksheet(ws, subset, columns)

    print(f"Completed {days_to_fetch} day update at {datetime.datetime.now()}.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        days = int(sys.argv[1])
    else:
        days = 1
    run_update(days)
