import frappe

from hrms.www.hrms import mpass_logout_url


def get_context(context):
	csrf_token = frappe.sessions.get_csrf_token()
	frappe.db.commit()  # nosempgrep
	context = frappe._dict()
	context.csrf_token = csrf_token
	context.mpass_logout_url = mpass_logout_url() or ""
	return context
