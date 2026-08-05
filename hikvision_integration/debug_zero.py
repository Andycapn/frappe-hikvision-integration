import frappe
from hikvision_integration.axon_engine import process_axon_attendance

def debug_axon():
    print("Testing Axon Engine...")
    result = process_axon_attendance(from_date="2026-07-01", to_date="2026-07-31")
    print(result)

if __name__ == "__main__":
    debug_axon()
