import frappe
from frappe import _
from frappe.utils import flt, getdate

def check_and_create_salary_component():
	"""
	Ensure the 'Attendance Deduction' Salary Component exists in the database.
	"""
	if not frappe.db.exists("Salary Component", "Attendance Deduction"):
		frappe.get_doc({
			"doctype": "Salary Component",
			"salary_component": "Attendance Deduction",
			"salary_component_abbr": "ATTD",
			"type": "Deduction"
		}).insert(ignore_permissions=True)
		frappe.db.commit()


def calculate_attendance_deduction(doc, method=None):
	"""
	Hook running on Salary Slip before_save.
	Retrieves the approved attendance deduction from an Attendance Deduction Review document.
	Does NOT calculate deductions on the fly if a review document is missing.
	"""
	if not (doc.employee and doc.start_date and doc.end_date):
		return

	# Ensure Salary Component exists
	check_and_create_salary_component()

	# Check if a Submitted Attendance Deduction Review exists for this company and period
	review_name = frappe.db.get_value(
		"Attendance Deduction Review",
		{
			"company": doc.company,
			"start_date": doc.start_date,
			"end_date": doc.end_date,
			"docstatus": 1
		},
		"name",
		order_by="creation desc"
	)

	deduction_amount = 0.0

	if review_name:
		# Retrieve the approved deduction details for the employee
		detail = frappe.db.get_value(
			"Attendance Deduction Review Detail",
			{
				"parent": review_name,
				"employee": doc.employee
			},
			["apply_deduction", "on_leave", "matched_percentage", "missed_days"],
			as_dict=True
		)
		
		# Only apply if marked 'apply_deduction' and NOT 'on_leave'
		if detail and detail.apply_deduction and not detail.on_leave:
			matched_percentage = flt(detail.matched_percentage)
			
			if matched_percentage > 0:
				gross_earnings = sum(flt(d.amount) for d in doc.earnings)
				deduction_amount = gross_earnings * (matched_percentage / 100.0)

	# Round the deduction amount
	deduction_amount = flt(deduction_amount, 2)

	# Update or append the "Attendance Deduction" in doc.deductions table
	updated = False
	for d in doc.deductions:
		if d.salary_component == "Attendance Deduction":
			d.amount = deduction_amount
			d.base_amount = deduction_amount
			updated = True
			break

	if not updated and deduction_amount > 0:
		doc.append(
			"deductions",
			{
				"salary_component": "Attendance Deduction",
				"abbr": "ATTD",
				"amount": deduction_amount,
				"base_amount": deduction_amount,
				"category": "Deduction",
			},
		)

	# Recalculate totals
	if hasattr(doc, "calculate_salary_slip"):
		doc.calculate_salary_slip()
