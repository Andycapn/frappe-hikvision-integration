frappe.ui.form.on('Payroll Entry', {
	refresh: function(frm) {
		if (frm.doc.company && frm.doc.start_date && frm.doc.end_date) {
			check_pending_anomalies(frm);
		}
	},
	start_date: function(frm) {
		if (frm.doc.company && frm.doc.start_date && frm.doc.end_date) {
			check_pending_anomalies(frm);
		}
	},
	end_date: function(frm) {
		if (frm.doc.company && frm.doc.start_date && frm.doc.end_date) {
			check_pending_anomalies(frm);
		}
	}
});

function check_pending_anomalies(frm) {
	frappe.call({
		method: 'hikvision_integration.axon_engine.get_pending_anomalies_count',
		args: {
			company: frm.doc.company,
			start_date: frm.doc.start_date,
			end_date: frm.doc.end_date
		},
		callback: function(r) {
			let count = r.message || 0;
			
			// Remove existing custom banner if any
			frm.dashboard.clear_comment_board();

			if (count > 0) {
				// Render Warning Banner at the top of Payroll Entry
				let banner_html = `
					<div style="background: #fef3c7; border: 1px solid #fde68a; border-radius: 8px; padding: 12px 16px; margin-bottom: 15px; display: flex; justify-content: space-between; align-items: center;">
						<div style="display: flex; align-items: center; gap: 10px;">
							<span style="font-size: 22px;">⚠️</span>
							<div>
								<div style="font-weight: 700; color: #92400e; font-size: 14px;">PAYROLL LOCK ACTIVE: ${count} Attendance Anomalies Pending</div>
								<div style="font-size: 12.5px; color: #b45309;">Review and resolve pending missing punches for ${frm.doc.start_date} to ${frm.doc.end_date} before generating Salary Slips.</div>
							</div>
						</div>
						<button class="btn btn-warning btn-sm" id="btn-review-anomalies-banner" style="font-weight: 600;">
							🛡️ Review Anomalies (${count})
						</button>
					</div>
				`;
				
				frm.dashboard.set_headline(banner_html);

				// Add Header Button
				frm.add_custom_button(__('🛡️ Review Anomalies ({0})', [count]), function() {
					open_anomaly_modal(frm);
				}).addClass('btn-warning');

				// Bind Banner Button
				setTimeout(() => {
					$('#btn-review-anomalies-banner').on('click', function() {
						open_anomaly_modal(frm);
					});
				}, 300);

			} else {
				let banner_html = `
					<div style="background: #dcfce7; border: 1px solid #bbf7d0; border-radius: 8px; padding: 10px 16px; margin-bottom: 15px; display: flex; align-items: center; gap: 10px;">
						<span style="font-size: 20px;">🟢</span>
						<div>
							<div style="font-weight: 700; color: #166534; font-size: 13.5px;">ATTENDANCE CLEAN: 0 Anomalies Pending</div>
							<div style="font-size: 12px; color: #15803d;">All check-in logs for this period have been processed. Payroll is ready to run.</div>
						</div>
					</div>
				`;
				frm.dashboard.set_headline(banner_html);
			}
		}
	});
}

function open_anomaly_modal(frm) {
	frappe.call({
		method: 'hikvision_integration.axon_engine.get_anomalies_for_review',
		args: {
			company: frm.doc.company,
			start_date: frm.doc.start_date,
			end_date: frm.doc.end_date
		},
		callback: function(r) {
			let list = r.message || [];

			let dialog = new frappe.ui.Dialog({
				title: __('🛡️ Batch Review Attendance Anomalies ({0})', [list.length]),
				size: 'extra-large',
				fields: [
					{
						fieldname: 'anomalies_html',
						fieldtype: 'HTML'
					}
				],
				primary_action_label: __('Approve Selected as Remote Duty (8h)'),
				primary_action: function() {
					let selected = [];
					dialog.$wrapper.find('.chk-modal-row:checked').each(function() {
						selected.push($(this).data('name'));
					});

					if (selected.length === 0) {
						frappe.msgprint('Please select at least one anomaly.');
						return;
					}

					frappe.call({
						method: 'hikvision_integration.axon_engine.resolve_anomaly_batch',
						args: { anomaly_names: selected, action: 'REMOTE_DUTY' },
						callback: function() {
							dialog.hide();
							check_pending_anomalies(frm);
							frappe.show_alert({ message: 'Selected anomalies approved as Remote Duty.', indicator: 'green' });
						}
					});
				}
			});

			let html = `
				<div style="max-height: 400px; overflow-y: auto;">
					<table class="table table-bordered table-hover style="font-size: 13px;">
						<thead style="background: #f8fafc;">
							<tr>
								<th style="width: 30px;"><input type="checkbox" id="chk-modal-select-all"></th>
								<th>Employee</th>
								<th>Department</th>
								<th>Date</th>
								<th>IN Time</th>
								<th>Action</th>
							</tr>
						</thead>
						<tbody>
			`;

			list.forEach(a => {
				html += `
					<tr>
						<td><input type="checkbox" class="chk-modal-row" data-name="${a.name}"></td>
						<td><strong>${a.employee_name}</strong><br><small class="text-muted">${a.employee}</small></td>
						<td>${a.department || 'Operations'}</td>
						<td>${a.attendance_date}</td>
						<td>${a.orphan_in_time ? a.orphan_in_time.split(' ')[1] : '--:--'}</td>
						<td>
							<button class="btn btn-xs btn-success btn-modal-remote" data-name="${a.name}">🟢 Remote Duty</button>
							<button class="btn btn-xs btn-danger btn-modal-absent" data-name="${a.name}">🔴 Absent</button>
						</td>
					</tr>
				`;
			});

			html += `</tbody></table></div>`;
			dialog.fields_dict.anomalies_html.$wrapper.html(html);

			dialog.$wrapper.find('#chk-modal-select-all').on('change', function() {
				dialog.$wrapper.find('.chk-modal-row').prop('checked', $(this).is(':checked'));
			});

			dialog.$wrapper.find('.btn-modal-remote').on('click', function() {
				let name = $(this).data('name');
				frappe.call({
					method: 'hikvision_integration.axon_engine.resolve_anomaly_batch',
					args: { anomaly_names: [name], action: 'REMOTE_DUTY' },
					callback: function() {
						dialog.hide();
						check_pending_anomalies(frm);
						frappe.show_alert({ message: 'Approved as Remote Duty', indicator: 'green' });
					}
				});
			});

			dialog.$wrapper.find('.btn-modal-absent').on('click', function() {
				let name = $(this).data('name');
				frappe.call({
					method: 'hikvision_integration.axon_engine.resolve_anomaly_batch',
					args: { anomaly_names: [name], action: 'UNEXCUSED_ABSENT' },
					callback: function() {
						dialog.hide();
						check_pending_anomalies(frm);
						frappe.show_alert({ message: 'Marked Absent', indicator: 'red' });
					}
				});
			});

			dialog.show();
		}
	});
}
