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

	event_name = f"HIK-EV-{payload.get('shortSerialNumber')}-{payload.get('AccessControllerEvent', {}).get('serialNo')}"
	if frappe.db.exists("Hikvision Event", event_name):
		# We check if it's processed. If not, maybe we should try processing again?
		# But usually, it means it's already in the system.
		return {"status": "received", "message": "Duplicate event ignored"}

	# Burst protection: Check for same employee, same device, within last 5 seconds
	event_data = payload.get("AccessControllerEvent", {})
	device_serial = payload.get("shortSerialNumber")
	employee_no = event_data.get("employeeNoString")

	if device_serial and employee_no:
		recent_event = frappe.db.sql("""
			SELECT name FROM `tabHikvision Event`
			WHERE device_serial = %s AND employee_no = %s
			AND creation > NOW() - INTERVAL 5 SECOND
			LIMIT 1
		""", (device_serial, employee_no))

		if recent_event:
			return {"status": "received", "message": "Burst event ignored"}

	try:
		doc = create_hikvision_event(payload)
		process_event(doc)
	except Exception as e:
		frappe.log_error(title=_("Hikvision Webhook Error"), message=frappe.get_traceback())
		return {"status": "error", "message": str(e)}

	return {"status": "received"}

def create_hikvision_event(payload):
	event_data = payload.get("AccessControllerEvent", {})
	device_serial = payload.get("shortSerialNumber")

	# Link to Hikvision Device if it exists, otherwise create it
	device = None
	if device_serial:
		if not frappe.db.exists("Hikvision Device", device_serial):
			device_doc = frappe.get_doc({
				"doctype": "Hikvision Device",
				"device_serial": device_serial,
				"device_name": event_data.get("deviceName") or f"Device {device_serial}",
				"ip_address": payload.get("ipAddress")
			})
			device_doc.insert(ignore_permissions=True)
			frappe.db.commit()

		device = device_serial
		# Update last heartbeat
		frappe.db.set_value("Hikvision Device", device_serial, "last_heartbeat", frappe.utils.now_datetime())

	doc = frappe.get_doc({
		"doctype": "Hikvision Event",
		"device_serial": device_serial,
		"device": device,
		"event_serial_no": event_data.get("serialNo"),
		"employee_no": event_data.get("employeeNoString"),
		"employee_name": event_data.get("name"),
		"attendance_status": event_data.get("attendanceStatus"),
		"verify_mode": event_data.get("currentVerifyMode"),
		"event_time": payload.get("dateTime").replace("T", " ").split("+")[0],
		"raw_event": json.dumps(payload, indent=2)
	})

	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc

def process_event(event):
	"""
	Process Hikvision Event and create Employee Checkin
	"""
	if not event.employee_no:
		return

	# Find Employee by attendance_device_id
	employee = None
	if frappe.db.exists("DocType", "Employee"):
		employee = frappe.db.get_value("Employee", {"attendance_device_id": event.employee_no}, "name")

	if not employee:
		# Fallback to employee_no if it matches ERPNext Employee ID (optional, but good for testing)
		if frappe.db.exists("DocType", "Employee") and frappe.db.exists("Employee", event.employee_no):
			employee = event.employee_no
		else:
			frappe.log_error(title=_("Hikvision Processing Error"), message=_("Employee not found for device ID: {0}").format(event.employee_no))
			return

	# Map attendanceStatus to log_type
	# Hikvision uses checkIn/checkOut, ERPNext uses IN/OUT
	log_type = "IN" if event.attendance_status == "checkIn" else "OUT"

	try:
		if not frappe.db.exists("DocType", "Employee Checkin"):
			frappe.log_error("Employee Checkin DocType not found. Is erpnext installed?", _("Hikvision Processing Error"))
			return
		checkin = frappe.get_doc({
			"doctype": "Employee Checkin",
			"employee": employee,
			"time": event.event_time,
			"log_type": log_type,
			"attendance_device_id": event.employee_no,
			"device_id": event.device_serial
		})
		checkin.insert(ignore_permissions=True)

		# Mark event as processed
		event.processed = 1
		event.save()
		frappe.db.commit()
	except Exception as e:
		frappe.log_error(frappe.get_traceback(), _("Hikvision Checkin Error"))
