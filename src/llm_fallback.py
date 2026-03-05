import os
import json
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import Optional, List

class TransactionCategory(BaseModel):
    id: str = Field(description="The exact transaction id provided")
    category: Optional[str] = Field(description="The chosen category string EXACTLY as it appears in Valid Categories, or null if unsure")
    reasoning: Optional[str] = Field(description="A brief explanation of why this category was chosen (under 15 words)")

class CategorizationResponse(BaseModel):
    transactions: List[TransactionCategory]

def categorize_transactions_with_llm(transactions, valid_categories):
    """
    transactions: list of dicts with 'id', 'Account', 'payee', 'description', 'amount'
    valid_categories: list of strings representing valid category names
    
    Returns a list of dicts with 'id', 'category', 'reasoning'
    """
    if not transactions:
        return []
        
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("GEMINI_API_KEY not found in environment. Skipping LLM Fallback.")
        return []
        
    client = genai.Client(api_key=api_key)
    valid_categories_list = list(valid_categories)
        
    prompt = f"""
You are an expert financial transaction categorizer.
Categorize the following transactions into one of the provided valid categories.
If none of the categories fit perfectly or you are unsure, return null for the category.

Valid Categories:
{json.dumps(valid_categories_list)}

Transactions to categorize:
{json.dumps(transactions)}
"""
    
    try:
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CategorizationResponse,
                temperature=0.1,
            ),
        )
        # response.text is guaranteed to match the CategorizationResponse schema
        result = json.loads(response.text)
        if 'transactions' in result:
            return result['transactions']
        return []
    except Exception as e:
        print(f"LLM Categorization failed: {e}")
        return []

if __name__ == "__main__":
    # Quick test if run standalone
    from dotenv import load_dotenv
    load_dotenv()
    
    sample_txs = [
        {"id": "tx1", "Account": "Amex", "payee": "GOUGE.COM", "description": "GOUGE STORE 123", "amount": 15.99},
        {"id": "tx2", "Account": "Checking", "payee": "CITY UTILITIES", "description": "WATER BILL", "amount": 45.00}
    ]
    sample_cats = ["Groceries", "Utilities", "Shopping", "Dining"]
    
    results = categorize_transactions_with_llm(sample_txs, sample_cats)
    print("Test Results:")
    print(json.dumps(results, indent=2))
