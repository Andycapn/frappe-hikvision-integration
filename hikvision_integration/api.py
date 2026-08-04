import json
import frappe
from frappe import _
import math

# Monkeypatch strip_exif_data in Frappe to handle string contents gracefully
# (avoids TypeErrors in environments where file contents are read as strings)
import frappe.utils.image
try:
	original_strip_exif_data = frappe.utils.image.strip_exif_data
	def robust_strip_exif_data(content, content_type):
		if isinstance(content, str):
			content = content.encode("utf-8", errors="ignore")
		try:
			return original_strip_exif_data(content, content_type)
		except Exception:
			return content
	frappe.utils.image.strip_exif_data = robust_strip_exif_data
except Exception:
	pass

# Hard cap on incoming webhook body to prevent memory exhaustion
_MAX_PAYLOAD_BYTES = 5 * 1024 * 1024  # 5 MB


# ---------------------------------------------------------------------------
# Webhook authentication
# ---------------------------------------------------------------------------

def _verify_webhook_request():
	"""
	Reject requests that don't come from a known device IP.

	Configure in site_config.json:
	  "hikvision_allowed_ips": ["192.168.1.10", "192.168.1.11"]

	If the key is absent or empty the check is skipped (backwards-compatible),
	but a warning is logged so admins know authentication is unconfigured.
	"""
	allowed_ips = frappe.conf.get("hikvision_allowed_ips")

	if not allowed_ips:
		# Warn once per process restart — don't flood the log
		if not getattr(frappe.local, "_hik_auth_warned", False):
			frappe.log_error(
				title=_("Hikvision Webhook: No IP Allowlist Configured"),
				message=_(
					"The webhook endpoint is open to any IP address. "
					"Set 'hikvision_allowed_ips' in site_config.json to restrict access."
				)
			)
			frappe.local._hik_auth_warned = True
		return  # allow through but warned

	remote_addr = frappe.request.environ.get("HTTP_X_FORWARDED_FOR") or \
				  frappe.request.environ.get("REMOTE_ADDR", "")
	# X-Forwarded-For may be a comma-separated list; take the first (client) IP
	client_ip = remote_addr.split(",")[0].strip()

	if client_ip not in allowed_ips:
		frappe.throw(
			_("Webhook request from unauthorized IP: {0}").format(client_ip),
			frappe.PermissionError
		)


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

def _parse_payload():
	"""
	Try every format Hikvision devices use. Returns (payload_dict, image_file_or_None).
	Returns (None, None) if nothing could be parsed.
	"""
	# Guard against oversized bodies early
	content_length = getattr(frappe.request, "content_length", 0) or 0
	if content_length > _MAX_PAYLOAD_BYTES:
		frappe.throw(_("Webhook payload too large ({0} bytes)").format(content_length))

	image_file = None

	# 1. Standard JSON body
	try:
		payload = frappe.request.get_json(silent=True)
		if payload:
			return payload, None
	except Exception:
		pass

	# 2. Multipart form data (most Hikvision models with images)
	try:
		for field in ("AccessControllerEvent", "event_log"):
			event_json = frappe.request.form.get(field)
			if event_json:
				payload = json.loads(event_json)
				if "Picture" in frappe.request.files:
					image_file = frappe.request.files["Picture"]
				return payload, image_file
	except Exception:
		pass

	# 3. Raw body fallback — some firmware sends malformed multipart
	try:
		raw = frappe.request.get_data()
		if not raw:
			return None, None

		if len(raw) > _MAX_PAYLOAD_BYTES:
			frappe.throw(_("Webhook payload too large"))

		text = raw.decode("utf-8", errors="replace")

		if "--MIME_boundary" in text:
			parts = text.split("--MIME_boundary")
			for part in parts:
				if 'name="AccessControllerEvent"' in part or 'name="event_log"' in part:
					start = part.find("{")
					end = part.rfind("}")
					if start != -1 and end != -1:
						return json.loads(part[start:end + 1]), None
		else:
			return json.loads(text), None
	except Exception:
		pass

	return None, None


# ---------------------------------------------------------------------------
# Public webhook endpoint
# ---------------------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
def webhook():
	_verify_webhook_request()

	payload, image_file = _parse_payload()

	if not payload:
		frappe.log_error(
			title=_("Hikvision Webhook: No Payload"),
			message=(
				f"Headers: {dict(frappe.request.headers)}\n"
				f"Form keys: {list(frappe.request.form.keys()) if frappe.request.form else 'None'}"
			)
		)
		return {"status": "error", "message": "No payload received"}

	# Heartbeat — keep alive, no employee data involved
	if payload.get("eventType") == "heartBeat":
		return _handle_heartbeat(payload)

	event_data = payload.get("AccessControllerEvent", {})
	device_serial = payload.get("shortSerialNumber") or "UNKNOWN"
	attendance_status = event_data.get("attendanceStatus")

	# Determine device role (default to Attendance Gate for unknown devices)
	device_role = (
		frappe.db.get_value("Hikvision Device", device_serial, "device_role")
		or "Attendance Gate"
	)

	# Presence terminals: skip entirely if there is no employee number
	# (pure door-open events with no identity claim are noise)
	if not event_data.get("employeeNoString"):
		return {"status": "received", "message": "No employee identity — event ignored"}

	# Attendance gates: drop undefined-status events (plain door open, not a checkin)
	# Presence terminals: allow undefined-status through — stored as presence-only
	if device_role == "Attendance Gate" and attendance_status in ("undefined", None):
		return {"status": "received", "message": "Non-attendance event ignored"}

	serial_no = event_data.get("serialNo") or frappe.utils.generate_hash(length=10)
	event_name = f"HIK-EV-{device_serial}-{serial_no}"

	if frappe.db.exists("Hikvision Event", event_name):
		return {"status": "received", "message": "Duplicate event ignored"}

	# Burst protection: same employee + device within 5 seconds
	employee_no = event_data.get("employeeNoString")
	if device_serial and employee_no:
		recent = frappe.db.sql(
			"""
			SELECT name FROM `tabHikvision Event`
			WHERE device_serial = %s AND employee_no = %s
			  AND creation > NOW() - INTERVAL 5 SECOND
			LIMIT 1
			""",
			(device_serial, employee_no),
		)
		if recent:
			return {"status": "received", "message": "Burst event ignored"}

	try:
		doc = _create_hikvision_event(payload)
		process_event(doc, image_file=image_file, device_role=device_role)
	except Exception:
		frappe.log_error(title=_("Hikvision Webhook Error"), message=frappe.get_traceback())
		return {"status": "error", "message": "Internal error — see Error Log"}

	return {"status": "received"}


# ---------------------------------------------------------------------------
# Heartbeat handler
# ---------------------------------------------------------------------------

def _infer_device_role(device_name):
	"""
	Best-effort role inference from the device name string sent by the hardware.
	DS-K1T673 series → Attendance Gate; DS-K1T808 series → Presence Terminal.
	Admins should confirm/correct this in the Hikvision Device list after first contact.
	"""
	name_upper = (device_name or "").upper()
	if "K1T808" in name_upper:
		return "Presence Terminal"
	return "Attendance Gate"


def _handle_heartbeat(payload):
	"""
	Heartbeats arrive with the JSON in the AccessControllerEvent form field.
	The payload is the event object itself — it contains ipAddress and macAddress
	but NOT shortSerialNumber (that lives one level up in a normal event wrapper).
	We identify the device by MAC address (stable) with IP as fallback.
	"""
	mac = (payload.get("macAddress") or "").upper().replace("-", ":").strip()
	ip  = (payload.get("ipAddress") or "").strip()

	if not mac and not ip:
		return {"status": "received", "message": "Heartbeat ignored: no device identifier"}

	now = frappe.utils.now_datetime()

	# Try to find existing device by MAC address first, then by IP
	device_name = None
	if mac:
		device_name = frappe.db.get_value("Hikvision Device", {"mac_address": mac}, "name")
	if not device_name and ip:
		device_name = frappe.db.get_value("Hikvision Device", {"ip_address": ip}, "name")

	if device_name:
		update = {"last_heartbeat": now}
		# Keep IP current in case it changed (DHCP)
		if ip:
			update["ip_address"] = ip
		frappe.db.set_value("Hikvision Device", device_name, update)
	else:
		# First contact from this device — auto-register it
		# Serial unknown at this point; use MAC as the serial placeholder
		serial = mac or ip
		label  = f"Device {serial}"
		frappe.get_doc({
			"doctype": "Hikvision Device",
			"device_serial": serial,
			"device_name": label,
			"mac_address": mac,
			"ip_address": ip,
			"device_role": "Attendance Gate",  # default; admin should correct
			"last_heartbeat": now,
		}).insert(ignore_permissions=True)

	frappe.db.commit()
	return {"status": "received", "message": "Heartbeat updated"}


# ---------------------------------------------------------------------------
# Event creation
# ---------------------------------------------------------------------------

def _create_hikvision_event(payload):
	event_data = payload.get("AccessControllerEvent", {})
	device_serial = payload.get("shortSerialNumber")

	if device_serial:
		if not frappe.db.exists("Hikvision Device", device_serial):
			# Infer role from model prefix in serial or device name when auto-creating.
			# Admins should review and correct this in the Hikvision Device list.
			inferred_role = _infer_device_role(event_data.get("deviceName") or "")
			frappe.get_doc({
				"doctype": "Hikvision Device",
				"device_serial": device_serial,
				"device_name": event_data.get("deviceName") or f"Device {device_serial}",
				"ip_address": payload.get("ipAddress"),
				"device_role": inferred_role,
			}).insert(ignore_permissions=True)

		frappe.db.set_value("Hikvision Device", device_serial, "last_heartbeat", frappe.utils.now_datetime())

	event_time = payload.get("dateTime")
	if event_time:
		event_time = event_time.replace("T", " ").split("+")[0]
	else:
		event_time = frappe.utils.now_datetime()

	doc = frappe.get_doc({
		"doctype": "Hikvision Event",
		"device_serial": device_serial,
		"device": device_serial,
		"event_serial_no": event_data.get("serialNo"),
		"employee_no": event_data.get("employeeNoString"),
		"employee_name": event_data.get("name"),
		"attendance_status": event_data.get("attendanceStatus"),
		"verify_mode": event_data.get("currentVerifyMode"),
		"event_time": event_time,
		"raw_event": json.dumps(payload, indent=2),
	})

	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------

_CHECKIN_STATUSES = {"checkIn", "checkOut", "breakIn", "breakOut", "overtimeIn", "overtimeOut"}


def process_event(event, image_file=None, device_role=None):
	"""
	Process a Hikvision Event and create an Employee Checkin if appropriate.

	device_role is passed in from the webhook to avoid an extra DB query.
	When called from the background reprocessing job it is looked up fresh.

	processed values:
	  0  = pending
	  1  = converted to Employee Checkin
	  2  = presence-only (stored, no checkin needed)
	 -1  = failed or skipped
	"""
	if not event.employee_no:
		event.db_set("processed", -1)
		return False

	if not frappe.db.exists("DocType", "Employee"):
		event.db_set("processed", -1)
		return False

	# Resolve device role if not supplied (background job path)
	if device_role is None:
		device_role = (
			frappe.db.get_value("Hikvision Device", event.device_serial, "device_role")
			or "Attendance Gate"
		)

	# Presence terminals with no explicit attendance status → presence-only, no checkin
	if device_role == "Presence Terminal" and event.attendance_status not in _CHECKIN_STATUSES:
		event.db_set("processed", 2)
		return True  # successfully handled, just not a checkin

	employee = _resolve_employee(event)

	if not employee:
		frappe.log_error(
			title=_("Hikvision: Employee Not Found"),
			message=_("No employee matched device ID: {0}").format(event.employee_no)
		)
		event.db_set("processed", -1)
		return False

	# Verify if employee has an active pending random presence check campaign
	try:
		verify_random_presence_check(employee, event)
	except Exception:
		frappe.log_error(
			title="Random Presence Check Verification Error",
			message=frappe.get_traceback()
		)

	if image_file:
		_update_employee_image(employee, image_file)

	status_map = {
		"checkIn": "IN",
		"checkOut": "OUT",
		"breakIn": "IN",
		"breakOut": "OUT",
		"overtimeIn": "IN",
		"overtimeOut": "OUT",
	}
	log_type = status_map.get(event.attendance_status, "IN")

	try:
		if not frappe.db.exists("DocType", "Employee Checkin"):
			frappe.log_error(
				title=_("Hikvision: DocType Missing"),
				message=_("Employee Checkin DocType not found. Is ERPNext HR installed?")
			)
			return False

		checkin_data = {
			"doctype": "Employee Checkin",
			"employee": employee,
			"time": event.event_time,
			"log_type": log_type,
			"device_id": event.device_serial,
		}

		meta = frappe.get_meta("Employee Checkin")
		if meta.has_field("attendance_device_id"):
			checkin_data["attendance_device_id"] = event.employee_no

		# Do NOT set latitude/longitude — triggers HRMS shift-location validation
		# even when no shift location is configured.

		frappe.get_doc(checkin_data).insert(ignore_permissions=True)
		event.db_set("processed", 1)
		frappe.db.commit()
		return True

	except Exception:
		frappe.log_error(
			title=_("Hikvision Checkin Error"),
			message=f"Employee: {employee}\n\n{frappe.get_traceback()}"
		)
		event.db_set("processed", -1)
		frappe.db.commit()
		return False


def _resolve_employee(event):
	"""Return the ERPNext Employee name for this event, or None."""
	# 1. attendance_device_id (canonical)
	employee = frappe.db.get_value(
		"Employee", {"attendance_device_id": event.employee_no}, "name"
	)
	if employee:
		return employee

	# 2. Employee ID directly
	if frappe.db.exists("Employee", event.employee_no):
		return event.employee_no

	# 3. Name-based fallback — only if explicitly enabled
	if frappe.conf.get("hikvision_autolink_by_name") and event.employee_name:
		return _match_employee_by_name(event.employee_name, event.employee_no)

	return None


def _match_employee_by_name(employee_name, device_id):
	"""
	Leniently match an employee by name and update their attendance_device_id.

	This is disabled by default because a device-supplied name could
	accidentally (or maliciously) re-link an employee. Enable with:
	  "hikvision_autolink_by_name": true  in site_config.json
	"""
	search_name = employee_name.strip().lower()
	if not search_name:
		return None

	employees = frappe.db.get_all(
		"Employee",
		filters={"status": "Active"},
		fields=["name", "employee_name"],
	)

	search_parts = set(search_name.split())
	matches = []

	for emp in employees:
		if not emp.employee_name:
			continue
		emp_lower = emp.employee_name.strip().lower()
		emp_parts = set(emp_lower.split())

		if search_name == emp_lower:
			matches.append(emp.name)
		elif search_parts == emp_parts and len(search_parts) > 1:
			matches.append(emp.name)

	if len(matches) != 1:
		return None  # ambiguous or no match — don't guess

	matched = matches[0]
	try:
		if frappe.get_meta("Employee").has_field("attendance_device_id"):
			frappe.db.set_value("Employee", matched, "attendance_device_id", device_id)
			frappe.db.commit()
			frappe.log_error(
				title=_("Hikvision Auto-Link"),
				message=_("Linked employee {0} to device ID {1} via name match").format(
					matched, device_id
				)
			)
		return matched
	except Exception:
		frappe.log_error(title=_("Hikvision Auto-Link Error"), message=frappe.get_traceback())
		return None


# ---------------------------------------------------------------------------
# Employee image update
# ---------------------------------------------------------------------------

def _update_employee_image(employee_name, image_file):
	"""
	Replace an employee's profile picture with the photo from the Hikvision terminal.
	Stored as private so it is not publicly accessible.
	"""
	try:
		from frappe.utils.file_manager import save_file
		import base64

		content = image_file.read()
		if not content:
			return

		# Base64 encode image to avoid byte/string type mismatches in Frappe save_file/exif stripping
		b64_content = base64.b64encode(content).decode("utf-8")

		file_name = f"hik_{employee_name}_{frappe.utils.generate_hash(length=6)}.jpg"

		# Remove previous Hikvision-sourced photos to avoid accumulation
		old_files = frappe.get_all(
			"File",
			filters={
				"attached_to_doctype": "Employee",
				"attached_to_name": employee_name,
				"file_name": ["like", "hik_%"],
			},
			fields=["name"],
		)
		for f in old_files:
			try:
				frappe.delete_doc("File", f.name, ignore_permissions=True)
			except Exception:
				pass

		saved = save_file(
			file_name,
			b64_content,
			"Employee",
			employee_name,
			decode=True,
			is_private=1,  # employee photos must not be publicly accessible
		)

		if frappe.get_meta("Employee").has_field("image"):
			frappe.db.set_value("Employee", employee_name, "image", saved.file_url)

		frappe.db.commit()
		image_file.seek(0)

	except Exception:
		frappe.log_error(
			title=_("Hikvision Image Update Error"),
			message=f"Employee: {employee_name}\n\n{frappe.get_traceback()}"
		)


# ---------------------------------------------------------------------------
# Background reprocessing
# ---------------------------------------------------------------------------

@frappe.whitelist()
def enqueue_process_unprocessed_events():
	"""
	Manually trigger background processing of unprocessed events.
	Call via: /api/method/hikvision_integration.api.enqueue_process_unprocessed_events
	"""
	frappe.only_for("System Manager")
	frappe.enqueue(
		"hikvision_integration.api.process_unprocessed_events",
		queue="long",
		timeout=3600,
	)
	return {"status": "enqueued", "message": _("Processing of unprocessed events has been enqueued.")}


def process_unprocessed_events(batch_size=100):
	"""
	Background job: convert pending Hikvision Events to Employee Checkins.
	Runs in batches to avoid memory/timeout issues.
	"""
	total_processed = 0
	total_failed = 0

	while True:
		unprocessed = frappe.get_all(
			"Hikvision Event",
			filters={"processed": 0},
			fields=["name"],
			limit_page_length=batch_size,
			order_by="event_time asc",
		)

		if not unprocessed:
			break

		for entry in unprocessed:
			event = frappe.get_doc("Hikvision Event", entry.name)
			if process_event(event):
				total_processed += 1
			else:
				total_failed += 1

		frappe.db.commit()

		if len(unprocessed) < batch_size:
			break

	frappe.log_error(
		title=_("Hikvision Background Processing Complete"),
		message=_("Processed: {0} | Failed/Skipped: {1}").format(total_processed, total_failed)
	)


# ---------------------------------------------------------------------------
# Mobile Geofencing & Random Presence Check Business Logic
# ---------------------------------------------------------------------------

def get_distance(lat1, lon1, lat2, lon2):
	# Radius of the Earth in km
	R = 6371.0
	
	dlat = math.radians(lat2 - lat1)
	dlon = math.radians(lon2 - lon1)
	
	a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
	c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
	
	distance = R * c * 1000  # convert to meters
	return distance


@frappe.whitelist()
def check_geofence_status(latitude, longitude):
	"""
	Check if the given coordinates are within any active geofence zone.
	Returns: {"in_zone": True/False, "zone_name": "...", "distance": ...}
	"""
	try:
		lat = float(latitude)
		lon = float(longitude)
	except (ValueError, TypeError):
		return {"in_zone": False, "error": _("Invalid coordinates provided")}
		
	zones = frappe.get_all(
		"Geofence Zone",
		filters={"is_active": 1},
		fields=["name", "zone_name", "latitude", "longitude", "radius"]
	)
	
	closest_zone = None
	min_distance = float('inf')
	
	for zone in zones:
		dist = get_distance(lat, lon, zone.latitude, zone.longitude)
		if dist <= zone.radius:
			return {
				"in_zone": True,
				"zone_name": zone.zone_name,
				"distance": dist,
				"zone_id": zone.name
			}
		if dist < min_distance:
			min_distance = dist
			closest_zone = zone
			
	if closest_zone:
		return {
			"in_zone": False,
			"closest_zone": closest_zone.zone_name,
			"distance": min_distance
		}
		
	return {"in_zone": False, "message": _("No active geofence zones defined")}


def _get_employee_for_user(user):
	"""
	Safely resolve Employee record linked to the Frappe User.
	In ERPNext HRMS, this is standard `user_id`.
	For mock/dev testing environments where the field is missing,
	we fallback to matching the employee ID or name.
	"""
	meta = frappe.get_meta("Employee")
	if meta.has_field("user_id"):
		emp = frappe.db.get_value("Employee", {"user_id": user}, "name")
		if emp:
			return emp
	if meta.has_field("user"):
		emp = frappe.db.get_value("Employee", {"user": user}, "name")
		if emp:
			return emp
	if meta.has_field("employee"):
		emp = frappe.db.get_value("Employee", {"employee": user}, "name")
		if emp:
			return emp
	if frappe.db.exists("Employee", user):
		return user
	emp_by_name = frappe.db.get_value("Employee", {"employee_name": user}, "name")
	if emp_by_name:
		return emp_by_name
	short_user = user.split("@")[0]
	if meta.has_field("employee"):
		emp = frappe.db.get_value("Employee", {"employee": short_user}, "name")
		if emp:
			return emp
	if frappe.db.exists("Employee", short_user):
		return short_user
	emp_by_short = frappe.db.get_value("Employee", {"employee_name": short_user}, "name")
	if emp_by_short:
		return emp_by_short
	return None


@frappe.whitelist()
def mobile_check_in(latitude, longitude, log_type="IN"):
	"""
	Perform a mobile check-in if within geofence.
	"""
	if frappe.session.user == "Guest":
		frappe.throw(_("Please log in to check in."), frappe.PermissionError)
		
	employee = _get_employee_for_user(frappe.session.user)
		
	if not employee:
		frappe.throw(_("Your user account is not linked to any Employee. Please contact HR."))
		
	status = check_geofence_status(latitude, longitude)
	if not status.get("in_zone"):
		msg = _("Check-in rejected: You are not within any authorized geofenced zone.")
		if status.get("closest_zone"):
			msg += " " + _("Closest zone: {0} ({1:.1f}m away)").format(status["closest_zone"], status["distance"])
		frappe.throw(msg, frappe.ValidationError)
		
	# Check-in is valid! Create the Employee Checkin document.
	checkin_data = {
		"doctype": "Employee Checkin",
		"employee": employee,
		"time": frappe.utils.now_datetime(),
		"log_type": log_type,
		"device_id": f"Mobile Geofence: {status['zone_name']}",
		"latitude": float(latitude),
		"longitude": float(longitude)
	}
	
	# Check if Employee Checkin table has latitude/longitude (standard)
	meta = frappe.get_meta("Employee Checkin")
	if not meta.has_field("latitude"):
		del checkin_data["latitude"]
	if not meta.has_field("longitude"):
		del checkin_data["longitude"]
		
	doc = frappe.get_doc(checkin_data)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	
	return {"status": "success", "message": _("Successfully checked {0} at {1}").format(log_type, status["zone_name"])}


def verify_random_presence_check(employee, event):
	"""
	Check if there is an active random presence check campaign for this employee,
	and mark them verified if they scanned at a terminal.
	"""
	active_checks = frappe.get_all(
		"Random Presence Check",
		filters={"status": "Pending"},
		fields=["name", "time_initiated", "time_limit_minutes"]
	)
	
	for check in active_checks:
		# Double-check expiration
		limit_time = frappe.utils.add_to_date(check.time_initiated, minutes=check.time_limit_minutes, as_datetime=True)
		if event.event_time > limit_time:
			continue
			
		# Check if employee has a pending check in this campaign
		row_name = frappe.db.get_value(
			"Random Presence Check Employee",
			{"parent": check.name, "employee": employee, "status": "Pending"},
			"name"
		)
		if row_name:
			frappe.db.set_value("Random Presence Check Employee", row_name, {
				"status": "Verified",
				"verification_event": event.name,
				"verification_time": event.event_time,
				"verification_method": "Terminal Scan"
			})
			
			# Check if all employees in this check are now processed (Verified or Missed)
			# If so, complete the check campaign
			check_doc = frappe.get_doc("Random Presence Check", check.name)
			all_done = True
			for emp in check_doc.employees:
				if emp.status == "Pending":
					all_done = False
					break
			if all_done:
				check_doc.db_set("status", "Completed")
				
			frappe.db.commit()


def check_expired_presence_checks():
	"""
	Scheduler job: find expired pending random presence checks,
	mark unverified employees as Missed, and mark checks as Expired.
	"""
	import frappe
	from frappe.utils import now_datetime, add_to_date
	
	now = now_datetime()
	
	pending_checks = frappe.get_all(
		"Random Presence Check",
		filters={"status": "Pending"},
		fields=["name", "time_initiated", "time_limit_minutes"]
	)
	
	for check in pending_checks:
		limit_time = add_to_date(check.time_initiated, minutes=check.time_limit_minutes, as_datetime=True)
		if now >= limit_time:
			doc = frappe.get_doc("Random Presence Check", check.name)
			has_missed = False
			for emp in doc.employees:
				if emp.status == "Pending":
					emp.db_set("status", "Missed")
					has_missed = True
			
			new_status = "Expired" if has_missed else "Completed"
			doc.db_set("status", new_status)
			frappe.db.commit()


# ---------------------------------------------------------------------------
# Auto Generate Attendance ID logic
# ---------------------------------------------------------------------------

def get_next_attendance_id(exclude_ids=None):
	"""
	Generate a unique numeric ID for attendance_device_id.
	Finds the maximum integer-like ID already registered on any Employee
	and increments it. Starts at 10001 if none exist.
	"""
	if exclude_ids is None:
		exclude_ids = set()

	existing = frappe.get_all(
		"Employee",
		fields=["attendance_device_id"]
	)
	numeric_ids = set()
	for d in existing:
		val = d.get("attendance_device_id")
		if val and val.strip().isdigit():
			numeric_ids.add(int(val.strip()))

	# Include any IDs generated in the current batch
	numeric_ids.update(exclude_ids)

	next_id = max(numeric_ids) + 1 if numeric_ids else 10001
	return next_id


def auto_generate_attendance_id(doc, method=None):
	"""
	Hook running on Employee before_insert.
	Ensures every new employee gets a unique numeric attendance ID if not provided.
	"""
	if not doc.attendance_device_id:
		doc.attendance_device_id = str(get_next_attendance_id())


def fill_missing_attendance_ids():
	"""
	Hourly background task to check through employees and fill in those without an ID.
	"""
	# Query all employees that lack attendance_device_id
	employees = frappe.get_all(
		"Employee",
		filters=[
			["attendance_device_id", "is", "not set"]
		],
		fields=["name"]
	)

	if not employees:
		return

	exclude_ids = set()
	for emp in employees:
		# Check if indeed empty (get_all filters are usually reliable, but let's be double sure)
		val = frappe.db.get_value("Employee", emp.name, "attendance_device_id")
		if not val:
			next_id = get_next_attendance_id(exclude_ids)
			frappe.db.set_value("Employee", emp.name, "attendance_device_id", str(next_id))
			exclude_ids.add(next_id)

	frappe.db.commit()


@frappe.whitelist()
def enqueue_fill_missing_attendance_ids():
	"""
	Manually enqueue the fill_missing_attendance_ids job.
	"""
	frappe.only_for("System Manager")
	frappe.enqueue(
		"hikvision_integration.api.fill_missing_attendance_ids",
		queue="long",
		timeout=3600,
	)
	return {"status": "enqueued", "message": _("Job to fill missing attendance IDs has been enqueued.")}

