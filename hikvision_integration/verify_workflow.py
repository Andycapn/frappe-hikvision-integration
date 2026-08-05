import frappe
from hikvision_integration.axon_engine import process_axon_attendance

def run_verification():
    print("Running Axon Engine verification...")
    result = process_axon_attendance(from_date="2026-06-01", to_date="2026-08-04")
    print(result)

if __name__ == "__main__":
    run_verification()
