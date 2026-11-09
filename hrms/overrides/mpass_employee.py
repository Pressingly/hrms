# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt
"""Employee records for users admitted through mPass SSO.

The HRMS app only serves users linked to an active Employee. Under SSO every session is created by
a login that already passed the mPass tenant check, so that session's user is given an Employee:
an unlinked active Employee carrying the user's email is linked, otherwise one is created with the
fields mPass can supply. HR completes gender and date of birth on the first edit.
"""

from collections import Counter
from enum import StrEnum

import frappe
from frappe.mpass_sso import sso_enabled
from frappe.utils import today

SAVEPOINT = "mpass_employee"


class Outcome(StrEnum):
	INELIGIBLE = "ineligible"
	EXISTS = "exists"
	LINKED = "linked"
	CREATED = "created"
	NO_COMPANY = "no_company"
	UNSUPPORTED_NAMING = "unsupported_naming"
	AMBIGUOUS = "ambiguous"
	FAILED = "failed"


def ensure_employee_on_session_creation(login_manager=None) -> None:
	if sso_enabled():
		ensure_employee(frappe.session.user)


def backfill_employees() -> dict[str, int]:
	"""Give every enabled system user an Employee. Idempotent; run with `bench execute`."""
	users = frappe.get_all(
		"User",
		filters={"enabled": 1, "user_type": "System User", "name": ("not in", frappe.STANDARD_USERS)},
		pluck="name",
	)
	outcomes = Counter()
	for user in users:
		outcomes[ensure_employee(user)] += 1
		frappe.db.commit()
	return dict(outcomes)


def ensure_employee(user: str) -> Outcome:
	"""Never raises: a failure here must not fail the login that triggered it."""
	if not is_eligible(user):
		return Outcome.INELIGIBLE
	if frappe.db.exists("Employee", {"user_id": user}):
		return Outcome.EXISTS

	frappe.db.savepoint(SAVEPOINT)
	try:
		outcome = link_or_create_employee(user)
	except Exception:
		restore_after_failure()
		logger().exception(f"could not give {user} an Employee")
		return Outcome.FAILED

	frappe.db.release_savepoint(SAVEPOINT)
	if outcome not in (Outcome.LINKED, Outcome.CREATED):
		logger().warning(f"no Employee for {user}: {outcome}")
	return outcome


def restore_after_failure() -> None:
	"""A deadlock or MariaDB 1020 rolls the whole transaction back and takes the savepoint with it.
	The session row is already committed, so a full rollback loses nothing of the login."""
	try:
		frappe.db.rollback(save_point=SAVEPOINT)
	except Exception:
		frappe.db.rollback()


def is_eligible(user: str) -> bool:
	return (
		user not in frappe.STANDARD_USERS and frappe.db.get_value("User", user, "user_type") == "System User"
	)


def link_or_create_employee(user: str) -> Outcome:
	email = frappe.db.get_value("User", user, "email")
	unlinked = unlinked_employees_with_email(email)
	if len(unlinked) > 1:
		return Outcome.AMBIGUOUS
	if unlinked:
		link_employee(unlinked[0], user)
		return Outcome.LINKED

	if not names_by_naming_series():
		return Outcome.UNSUPPORTED_NAMING

	company = default_company()
	if not company:
		return Outcome.NO_COMPANY

	create_employee(user, company)
	return Outcome.CREATED


def unlinked_employees_with_email(email: str | None) -> list[str]:
	if not email:
		return []

	return frappe.get_all(
		"Employee",
		filters={"status": "Active", "user_id": ("is", "not set")},
		or_filters={"company_email": email, "personal_email": email},
		pluck="name",
	)


def names_by_naming_series() -> bool:
	"""The other naming methods need a unique full name or an employee number, which mPass lacks."""
	return frappe.db.get_single_value("HR Settings", "emp_created_by") == "Naming Series"


def default_company() -> str | None:
	company = frappe.db.get_single_value("Global Defaults", "default_company")
	if company:
		return company

	companies = frappe.get_all("Company", pluck="name", limit=2)
	return companies[0] if len(companies) == 1 else None


def link_employee(employee: str, user: str) -> None:
	"""Linked and created Employees get no User Permission: one would hide every other Employee
	from an HR or admin user."""
	doc = frappe.get_doc("Employee", employee)
	doc.user_id = user
	doc.create_user_permission = 0
	doc.flags.ignore_permissions = True
	doc.save()


def create_employee(user: str, company: str) -> None:
	names = frappe.db.get_value("User", user, ["first_name", "last_name"], as_dict=True)
	doc = frappe.new_doc("Employee")
	doc.update(
		{
			"first_name": names.first_name or user,
			"last_name": names.last_name,
			"company": company,
			"status": "Active",
			"date_of_joining": today(),
			"user_id": user,
			"create_user_permission": 0,
		}
	)
	doc.flags.ignore_permissions = True
	doc.flags.ignore_mandatory = True
	doc.insert()


def logger():
	return frappe.logger("mpass_employee")
