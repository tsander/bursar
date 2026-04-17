import os
import json
import re
import pandas as pd
import gspread
import joblib
import dotenv
from sklearn.pipeline import Pipeline
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score

if not os.environ.get("IS_DOCKER", False):
    dotenv.load_dotenv()
else:
    for key, value in os.environ.items():
        if isinstance(value, str):
            os.environ[key] = value.strip('\'"\r\n')

def train_model():
    print("Loading credentials...")
    config_path = os.environ.get("CONFIG_PATH", ".")
    gs_auth_path = os.path.join(config_path, "google_auth.json")
    
    if not os.path.exists(gs_auth_path):
        print(f"Google auth file not found at {gs_auth_path}")
        return
        
    with open(gs_auth_path, "r") as f:
        gs_auth = json.load(f)
        
    gc = gspread.service_account_from_dict(gs_auth)
    sh = gc.open_by_key(os.environ.get("SHEET_ID"))

    print("Fetching historical data from yearly sheets...")
    all_data = []
    
    template_cols_str = os.environ.get("TEMPLATE_COLUMNS")
    expected_cols = [c.strip() for c in template_cols_str.split(",") if c.strip()] if template_cols_str else None
    
    # Identify worksheets that are 4-digit years
    for ws in sh.worksheets():
        if re.match(r"^\d{4}$", ws.title):
            print(f"Reading data from {ws.title}...")
            data = ws.get_all_values()
            if data and len(data) > 1:
                headers = data[0]
                df_ws = pd.DataFrame(data[1:], columns=headers)
                
                if expected_cols:
                    # Filter to only keep expected columns that exist in this sheet
                    valid_cols = [c for c in expected_cols if c in df_ws.columns]
                    df_ws = df_ws[valid_cols]
                    
                all_data.append(df_ws)
            
    if not all_data:
        print("No yearly sheets found to train on.")
        return

    df = pd.concat(all_data, ignore_index=True)
    
    # Preprocessing
    required_cols = ["Account", "payee", "description", "amount", "Category"]
    for col in required_cols:
        if col not in df.columns:
            print(f"Required column '{col}' missing from data. Expected columns: {list(df.columns)}")
            return
            
    # Filter rows: only rows with a Category, not empty
    df_train = df[df["Category"].str.strip() != ""]
    
    if df_train.empty:
        print("No categorized transactions found for training.")
        return
        
    print(f"Training on {len(df_train)} categorized transactions.")
    
    # Features and Target
    X = df_train[["Account", "payee", "description", "amount"]].copy()
    y = df_train["Category"]
    
    # Convert amount to numeric
    # Remove any commas or monetary symbols if present before converting
    X["amount"] = X["amount"].astype(str).str.replace(r"[$,]", "", regex=True)
    X["amount"] = pd.to_numeric(X["amount"], errors="coerce").fillna(0.0)
    
    # Ensure text columns are strings and handle NaNs
    for col in ["Account", "payee", "description"]:
        X[col] = X[col].fillna("").astype(str)
        
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.15, random_state=42)
    print(f"Split data: {len(X_train)} train, {len(X_test)} test.")
        
    # Build Scikit-Learn Pipeline
    print("Building pipeline...")
    preprocessor = ColumnTransformer(
        transformers=[
            ('desc_tfidf', TfidfVectorizer(max_features=3000, ngram_range=(1, 2)), 'description'),
            ('payee_tfidf', TfidfVectorizer(max_features=1000), 'payee'),
            ('account_tfidf', TfidfVectorizer(max_features=100), 'Account'),
            ('amount_scaler', StandardScaler(), ['amount'])
        ]
    )
    
    pipeline = Pipeline(steps=[
        ('preprocessor', preprocessor),
        ('classifier', RandomForestClassifier(n_estimators=100, random_state=42, n_jobs=-1))
    ])
    
    print("Fitting model...")
    pipeline.fit(X_train, y_train)
    
    print("\nEvaluating on test set...")
    preds = pipeline.predict(X_test)
    probs = pipeline.predict_proba(X_test)
    max_probs = probs.max(axis=1)
    
    acc = accuracy_score(y_test, preds)
    print(f"Test Accuracy: {acc:.2%}")
    
    thresholds = [0.5, 0.6, 0.7, 0.8, 0.85, 0.90, 0.95]
    print("\nConfidence Threshold Analysis (Test Set):")
    for t in thresholds:
        count = (max_probs > t).sum()
        pct = count / len(max_probs)
        # Calculate accuracy only for predictions above the threshold
        idx = max_probs > t
        if count > 0:
            t_acc = accuracy_score(y_test.values[idx], preds[idx])
        else:
            t_acc = 0.0
        print(f" > {t:.2f} confidence: {count}/{len(max_probs)} ({pct:.1%}) of transactions, Accuracy: {t_acc:.1%}")
    print()

    # Retrain on full dataset
    print("Retraining on full dataset before saving...")
    pipeline.fit(X, y)
    
    # Save model
    model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model.joblib")
    joblib.dump(pipeline, model_path)
    print(f"Model saved to {model_path}")

if __name__ == "__main__":
    train_model()
