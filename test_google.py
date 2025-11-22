import os
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# Load .env from the current working directory or its parents
load_dotenv()

SERVICE_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
SHEET_ID = os.getenv("GOOGLE_SHEET_ID")
WORKSHEET = os.getenv("GOOGLE_WORKSHEET", "").strip()

print("🔎 DEBUG")
print(f"  CWD: {os.getcwd()}")
print(f"  .env present? {'Yes' if os.path.exists('.env') else 'No'}")
print(f"  GOOGLE_SERVICE_ACCOUNT_JSON = {SERVICE_JSON!r}")
print(f"  GOOGLE_SHEET_ID            = {SHEET_ID!r}")
print(f"  GOOGLE_WORKSHEET           = {WORKSHEET!r}")

if not SERVICE_JSON:
    raise SystemExit("❌ GOOGLE_SERVICE_ACCOUNT_JSON is not set. Fix your .env.")
if not os.path.exists(SERVICE_JSON):
    raise SystemExit(f"❌ Service account JSON not found at: {SERVICE_JSON}")

if not SHEET_ID:
    raise SystemExit("❌ GOOGLE_SHEET_ID is not set. Fix your .env.")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

print("\n🔐 Authenticating…")
creds = Credentials.from_service_account_file(SERVICE_JSON, scopes=SCOPES)
gc = gspread.authorize(creds)

print("📄 Opening sheet…")
sh = gc.open_by_key(SHEET_ID)
ws = sh.worksheet(WORKSHEET) if WORKSHEET else sh.sheet1

print("⬇️  Fetching records…")
records = ws.get_all_records()
print(f"✅ Connected! Rows: {len(records)}")
print(records[:5])
