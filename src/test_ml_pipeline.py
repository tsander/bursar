import os
import joblib
import pandas as pd
from sklearn.metrics import classification_report, accuracy_score

def test_model():
    model_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model.joblib")
    if not os.path.exists(model_path):
        print("Model not found. Please run train_model.py first.")
        return

    print("Loading model...")
    model = joblib.load(model_path)

    # Let's test with a few targeted synthetic transactions
    test_data = [
        {"Account": "Chase Credit Card", "payee": "TARGET STORES", "description": "TARGET", "amount": 105.42},
        {"Account": "Checking", "payee": "PG&E", "description": "PGE WEB ONLINE PAY", "amount": -145.00},
        {"Account": "Checking", "payee": "UNKNOWN COFFEE SHOP", "description": "COFFEE", "amount": 4.50},
        {"Account": "Amex", "payee": "AMAZON.COM", "description": "AMZN MKTP US", "amount": 35.99},
    ]

    df_test = pd.DataFrame(test_data)
    
    print("\nPredicting on test data...")
    probs = model.predict_proba(df_test)
    max_probs = probs.max(axis=1)
    preds = model.classes_[probs.argmax(axis=1)]

    for i, row in df_test.iterrows():
        print(f"\nTransaction: {row['description']} | Payee: {row['payee']} | Amount: {row['amount']}")
        print(f"Predicted Category: {preds[i]} (Confidence: {max_probs[i]:.2%})")
        if max_probs[i] > 0.90:
            print(" -> Status: Would be auto-categorized (>90%)")
        else:
            print(" -> Status: Would NOT be auto-categorized (<90%)")

if __name__ == "__main__":
    test_model()
