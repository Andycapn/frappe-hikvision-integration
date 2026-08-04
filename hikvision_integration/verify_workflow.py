import frappe
from frappe.utils import today, add_days

def run_verification():
    frappe.init(site="dev.local")
    frappe.connect()

    print("--- 1. Creating Attendance Deduction Review ---")
    company = "Test Company"
        
    start_date = "2026-06-01"
    end_date = "2026-06-30"

    review = frappe.get_doc({
        "doctype": "Attendance Deduction Review",
        "company": company,
        "start_date": start_date,
        "end_date": end_date,
        "rules": [
            {
                "min_missed_days": 10,
                "deduction_percentage": 5
            },
            {
                "min_missed_days": 15,
                "deduction_percentage": 10
            }
        ]
    })
    
    review.insert(ignore_permissions=True)
    print(f"Created Review Doc: {review.name}")

    print("\n--- 2. Fetching Employees ---")
    review.fetch_employees_for_review()
    review.save(ignore_permissions=True)
    print(f"Fetched {len(review.deductions)} employee deductions.")
    
    for d in review.deductions:
        print(f"Emp: {d.employee}, Missed: {d.missed_days}, Historical: {d.has_historical_checkins}, " 
              f"Matched %: {d.matched_percentage}, Apply: {d.apply_deduction}, Leave: {d.on_leave}")

    print("\n--- 3. Submitting Review ---")
    review.submit()
    print("Submitted successfully.")

    print("\n--- 4. Testing Salary Slip Deduction ---")
    emp = frappe.get_doc({
        "doctype": "Employee",
        "employee": "verify_emp@example.com",
        "employee_name": "Verify Employee",
        "status": "Active"
    }).insert(ignore_permissions=True)
    
    # 26 days minus 10 missed days = 16 checkins
    for i in range(1, 17):
        day = f"2026-06-{i:02d} 08:00:00"
        frappe.get_doc({
            "doctype": "Employee Checkin",
            "employee": emp.name,
            "time": day,
            "log_type": "IN",
            "device_id": "Test"
        }).insert(ignore_permissions=True)
    frappe.db.commit()

    if emp:
        print(f"Testing slip for {emp.name}")
        
        slip = frappe.get_doc({
            "doctype": "Salary Slip",
            "employee": emp.name,
            "start_date": start_date,
            "end_date": end_date,
            "company": company,
            "total_working_days": 26,
            "earnings": [
                {
                    "salary_component": "Basic",
                    "amount": 5000,
                    "abbr": "B",
                    "category": "Earning"
                }
            ],
            "deductions": []
        })
        from hikvision_integration.payroll import calculate_attendance_deduction
        calculate_attendance_deduction(slip)
        print("Salary slip saved.")
        
        attd_deduction = [d for d in slip.deductions if d.salary_component == "Attendance Deduction"]
        if attd_deduction:
            print(f"Deduction Applied! Amount: {attd_deduction[0].amount}")
        else:
            print("No deduction applied.")

    frappe.db.rollback()
    print("\nTest finished and DB rolled back.")

if __name__ == "__main__":
    run_verification()
