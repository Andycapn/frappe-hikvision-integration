frappe.ui.form.on('Attendance Deduction Review', {
	refresh: function(frm) {
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(__('Get Attendance Deductions'), function() {
				if (!frm.doc.company || !frm.doc.start_date || !frm.doc.end_date) {
					frappe.msgprint(__('Please set Company, Start Date and End Date first.'));
					return;
				}
				frm.call({
					doc: frm.doc,
					method: 'fetch_draft_salary_slip_deductions',
					callback: function(r) {
						if (r.message) {
							frm.refresh_field('deductions');
							frappe.show_alert({
								message: __('Fetched {0} employee deductions.', [r.message.length]),
								indicator: 'green'
							});
						}
					}
				});
			});
		}
	}
});
