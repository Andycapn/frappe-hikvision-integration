import frappe

def debug_issue():
    frappe.init(site="dev.local", sites_path="sites")
    frappe.connect()

    reviews = frappe.get_all("Attendance Deduction Review", fields=["name", "docstatus"])
    if not reviews:
        print("No reviews found.")
        return

    # Get latest review
    latest_review = sorted(reviews, key=lambda x: x.name, reverse=True)[0]
    print(f"Latest Review: {latest_review.name} (Docstatus: {latest_review.docstatus})")

    doc = frappe.get_doc("Attendance Deduction Review", latest_review.name)
    print("Deductions in Review:")
    for d in doc.deductions:
        if d.has_historical_checkins == 0 or d.apply_deduction == 1:
            print(f"  Emp: {d.employee}, Actual: {d.actual_days_checked_in}, Missed: {d.missed_days}, "
                  f"Hist: {d.has_historical_checkins}, Apply: {d.apply_deduction}, Pct: {d.matched_percentage}")

if __name__ == "__main__":
    debug_issue()
