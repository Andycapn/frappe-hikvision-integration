frappe.listview_settings['Attendance Anomaly'] = {
	add_fields: ["status", "employee", "employee_name", "attendance_date", "matched_shift", "orphan_in_time", "anomaly_reason"],
	get_indicator: function(doc) {
		if (doc.status === "Approved") {
			return [__("Approved"), "green", "status,=,Approved"];
		} else if (doc.status === "Rejected") {
			return [__("Rejected"), "red", "status,=,Rejected"];
		} else if (doc.status === "Under HR Review") {
			return [__("Under HR Review"), "blue", "status,=,Under HR Review"];
		} else {
			return [__("Pending Appeal"), "orange", "status,=,Pending Appeal"];
		}
	},
	onload: function(listview) {
		listview.page.add_inner_button(__("🟢 Approve Selected as Remote Duty (8h)"), function() {
			let selected = listview.get_checked_items();
			if (selected.length === 0) {
				frappe.msgprint(__("Please select at least one anomaly using the checkboxes."));
				return;
			}
			let names = selected.map(item => item.name);
			frappe.call({
				method: "hikvision_integration.axon_engine.resolve_anomaly_batch",
				args: { anomaly_names: names, action: "REMOTE_DUTY" },
				freeze: true,
				freeze_message: __("Approving selected anomalies as Remote Duty..."),
				callback: function(r) {
					frappe.show_alert({ message: __("Selected anomalies approved as Remote Duty."), indicator: "green" });
					listview.refresh();
				}
			});
		}, __("Actions"));

		listview.page.add_inner_button(__("🔴 Mark Selected as Unexcused Absent"), function() {
			let selected = listview.get_checked_items();
			if (selected.length === 0) {
				frappe.msgprint(__("Please select at least one anomaly using the checkboxes."));
				return;
			}
			let names = selected.map(item => item.name);
			frappe.call({
				method: "hikvision_integration.axon_engine.resolve_anomaly_batch",
				args: { anomaly_names: names, action: "UNEXCUSED_ABSENT" },
				freeze: true,
				freeze_message: __("Marking selected anomalies as Unexcused Absent..."),
				callback: function(r) {
					frappe.show_alert({ message: __("Selected anomalies marked as Unexcused Absent."), indicator: "red" });
					listview.refresh();
				}
			});
		}, __("Actions"));

		listview.page.add_inner_button(__("⚡ Run Axon Attendance Sync"), function() {
			frappe.call({
				method: "hikvision_integration.axon_engine.process_axon_attendance",
				freeze: true,
				freeze_message: __("Running Axon Attendance Engine..."),
				callback: function(r) {
					frappe.msgprint(r.message || __("Attendance sync completed."));
					listview.refresh();
				}
			});
		});
	}
};
