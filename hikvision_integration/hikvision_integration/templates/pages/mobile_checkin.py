import frappe
from frappe import _
from frappe.utils import today, now_datetime

def get_context(context):
	# Disable standard Frappe website navbar and footer for a clean app portal look
	context.no_breadcrumbs = True
	context.show_sidebar = False
	context.no_header = True
	context.no_footer = True
	
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_to = "/login?redirect-to=/mobile-checkin"
		raise frappe.Redirect
		
	# Find employee linked to user
	from hikvision_integration.api import _get_employee_for_user
	employee = _get_employee_for_user(frappe.session.user)
		
	if not employee:
		context.no_employee = True
		return
		
	context.no_employee = False
	context.employee = employee
	context.employee_name = frappe.db.get_value("Employee", employee, "employee_name") or employee
	
	# Fetch today's check-ins
	today_start = today() + " 00:00:00"
	context.today_checkins = frappe.get_all(
		"Employee Checkin",
		filters={
			"employee": employee,
			"time": [">=", today_start]
		},
		fields=["time", "log_type", "device_id"],
		order_by="time desc"
	)
	
	# Check if there is an active random presence check campaign where this employee is Pending
	active_campaigns = frappe.get_all(
		"Random Presence Check",
		filters={"status": "Pending"},
		fields=["name", "time_initiated", "time_limit_minutes"]
	)
	
	pending_checks = []
	for campaign in active_campaigns:
		status = frappe.db.get_value(
			"Random Presence Check Employee",
			{"parent": campaign.name, "employee": employee, "status": "Pending"},
			"status"
		)
		if status:
			limit_time = frappe.utils.add_to_date(campaign.time_initiated, minutes=campaign.time_limit_minutes, as_datetime=True)
			if now_datetime() <= limit_time:
				pending_checks.append({
					"campaign": campaign.name,
					"limit_time": limit_time.strftime("%I:%M %p"),
					"remaining_minutes": max(0, int((limit_time - now_datetime()).total_seconds() / 60))
				})
				
	context.pending_random_checks = pending_checks
