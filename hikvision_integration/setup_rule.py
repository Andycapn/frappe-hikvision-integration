import frappe

def setup_rule_doctype():
    if not frappe.db.exists("DocType", "Attendance Deduction Rule"):
        doc = frappe.get_doc({
            "doctype": "DocType",
            "name": "Attendance Deduction Rule",
            "module": "Hikvision Integration",
            "custom": 0,
            "istable": 1,
            "fields": [
                {
                    "fieldname": "min_missed_days",
                    "fieldtype": "Int",
                    "label": "Minimum Missed Days",
                    "in_list_view": 1,
                    "reqd": 1,
                    "description": "Trigger this deduction if missed days is greater than or equal to this value."
                },
                {
                    "fieldname": "deduction_percentage",
                    "fieldtype": "Percent",
                    "label": "Deduction Percentage",
                    "in_list_view": 1,
                    "reqd": 1,
                    "description": "Percentage of Gross Pay to deduct."
                }
            ],
            "permissions": []
        })
        doc.insert(ignore_permissions=True)
        print("Created Attendance Deduction Rule DocType")
    else:
        print("Attendance Deduction Rule DocType already exists")

    frappe.db.commit()
