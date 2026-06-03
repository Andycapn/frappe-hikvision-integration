import json
import frappe

@frappe.whitelist(allow_guest=True)
def webhook():
	payload = frappe.request.get_json()

	frappe.logger().info(
		json.dumps(payload, indent=2)
	)

	return {"status": "received"}
