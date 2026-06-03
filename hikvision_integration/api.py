import json
import frappe
from frappe import _

@frappe.whitelist(allow_guest=True)
def webhook():
	payload = frappe.request.get_json()

	if not payload:
		return {"status": "error", "message": "No payload received"}

	# Log the raw payload for debugging
	frappe.logger().info(
		json.dumps(payload, indent=2)
	)

	try:
		create_hikvision_event(payload)
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), _("Hikvision Webhook Error"))
		return {"status": "error", "message": str(e)}

	return {"status": "received"}

def create_hikvision_event(payload):
	event_data = payload.get("AccessControllerEvent", {})

	doc = frappe.get_doc({
		"doctype": "Hikvision Event",
		"device_serial": payload.get("shortSerialNumber"),
		"event_serial_no": event_data.get("serialNo"),
		"employee_no": event_data.get("employeeNoString"),
		"employee_name": event_data.get("name"),
		"attendance_status": event_data.get("attendanceStatus"),
		"verify_mode": event_data.get("currentVerifyMode"),
		"event_time": payload.get("dateTime"),
		"raw_event": json.dumps(payload, indent=2)
	})

	doc.insert(ignore_permissions=True)
	frappe.db.commit()
