# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt
import os
from unittest.mock import patch

import frappe
from frappe.mpass_sso import ACCESS_TOKEN_HEADER, EMAIL_HEADER
from frappe.tests.test_api import FrappeAPITestCase, make_request
from frappe.tests.test_mpass_sso import SSO_ENV, corporate_token, purge_test_users
from frappe.utils import get_test_client

from hrms.overrides import mpass_employee
from hrms.overrides.mpass_employee import (
	Outcome,
	backfill_employees,
	ensure_employee,
	ensure_employee_on_session_creation,
)
from hrms.tests.utils import HRMSTestSuite, make_user

COMPANY = "_Test Company"


def employee_of(user: str) -> dict | None:
	return frappe.db.get_value(
		"Employee",
		{"user_id": user},
		["name", "company", "status", "create_user_permission"],
		as_dict=True,
	)


def make_unlinked_employee(email: str) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Employee",
			"first_name": email.split("@")[0],
			"company": COMPANY,
			"gender": "Female",
			"date_of_birth": "1990-05-08",
			"date_of_joining": "2020-01-01",
			"company_email": email,
			"status": "Active",
		}
	).insert()
	return doc.name


class TestMpassEmployee(HRMSTestSuite):
	def setUp(self):
		patcher = patch.object(mpass_employee, "default_company", return_value=COMPANY)
		self.default_company = patcher.start()
		self.addCleanup(patcher.stop)

	def test_creates_an_active_employee_without_user_permissions(self):
		user = make_user("mpass_new_employee@example.com", role="System Manager")

		self.assertEqual(ensure_employee(user), Outcome.CREATED)

		employee = employee_of(user)
		self.assertEqual((employee.company, employee.status), (COMPANY, "Active"))
		self.assertEqual(employee.create_user_permission, 0)
		self.assertIn("Employee", frappe.get_roles(user))
		self.assertFalse(frappe.db.exists("User Permission", {"user": user}))

	def test_a_second_login_keeps_the_one_employee(self):
		user = make_user("mpass_repeat_login@example.com")

		ensure_employee(user)

		self.assertEqual(ensure_employee(user), Outcome.EXISTS)
		self.assertEqual(frappe.db.count("Employee", {"user_id": user}), 1)

	def test_a_departed_employee_is_not_recreated(self):
		user = make_user("mpass_departed@example.com")
		ensure_employee(user)
		frappe.db.set_value("Employee", employee_of(user).name, "status", "Left")

		self.assertEqual(ensure_employee(user), Outcome.EXISTS)
		self.assertEqual(frappe.db.count("Employee", {"user_id": user}), 1)

	def test_links_the_unlinked_employee_carrying_the_users_email(self):
		user = make_user("mpass_hr_created@example.com")
		employee = make_unlinked_employee(user)

		self.assertEqual(ensure_employee(user), Outcome.LINKED)
		self.assertEqual(employee_of(user).name, employee)
		self.assertFalse(frappe.db.exists("User Permission", {"user": user}))

	def test_two_unlinked_employees_with_the_email_are_left_alone(self):
		user = make_user("mpass_ambiguous@example.com")
		make_unlinked_employee(user)
		make_unlinked_employee(user)

		self.assertEqual(ensure_employee(user), Outcome.AMBIGUOUS)
		self.assertIsNone(employee_of(user))

	def test_no_default_company_creates_nothing(self):
		user = make_user("mpass_no_company@example.com")
		self.default_company.return_value = None

		self.assertEqual(ensure_employee(user), Outcome.NO_COMPANY)
		self.assertIsNone(employee_of(user))

	def test_naming_other_than_the_series_creates_nothing(self):
		user = make_user("mpass_full_name_naming@example.com")

		with patch.object(mpass_employee, "names_by_naming_series", return_value=False):
			self.assertEqual(ensure_employee(user), Outcome.UNSUPPORTED_NAMING)
		self.assertIsNone(employee_of(user))

	def test_standard_and_website_users_are_ineligible(self):
		website_user = make_user("mpass_website_user@example.com", role="Customer")
		frappe.db.set_value("User", website_user, "user_type", "Website User")

		self.assertEqual(ensure_employee("Administrator"), Outcome.INELIGIBLE)
		self.assertEqual(ensure_employee(website_user), Outcome.INELIGIBLE)

	def test_a_failed_insert_keeps_earlier_writes_and_does_not_raise(self):
		user = make_user("mpass_failed_insert@example.com")
		frappe.db.set_value("User", user, "bio", "written before the hook")

		with patch.object(mpass_employee, "create_employee", side_effect=frappe.ValidationError):
			self.assertEqual(ensure_employee(user), Outcome.FAILED)

		self.assertEqual(frappe.db.get_value("User", user, "bio"), "written before the hook")
		self.assertIsNone(employee_of(user))

	def test_a_transaction_lost_mid_insert_does_not_raise(self):
		user = make_user("mpass_lost_transaction@example.com")

		def lose_the_transaction(_user):
			frappe.db.rollback()
			raise frappe.QueryDeadlockError

		with patch.object(mpass_employee, "link_or_create_employee", side_effect=lose_the_transaction):
			self.assertEqual(ensure_employee(user), Outcome.FAILED)

	def test_the_session_hook_runs_only_under_sso(self):
		user = make_user("mpass_session_hook@example.com")
		frappe.set_user(user)
		self.addCleanup(frappe.set_user, "Administrator")

		with patch.object(mpass_employee, "sso_enabled", return_value=False):
			ensure_employee_on_session_creation()
		self.assertIsNone(employee_of(user))

		with patch.object(mpass_employee, "sso_enabled", return_value=True):
			ensure_employee_on_session_creation()
		self.assertIsNotNone(employee_of(user))

	def test_backfill_gives_every_system_user_an_employee(self):
		user = make_user("mpass_backfill@example.com")

		with patch.object(frappe.db, "commit"):
			outcomes = backfill_employees()

		self.assertGreaterEqual(outcomes.get(Outcome.CREATED, 0), 1)
		self.assertIsNotNone(employee_of(user))


class TestMpassEmployeeOnSsoLogin(FrappeAPITestCase):
	user = "mpass_sso_login_employee@example.com"

	def setUp(self):
		super().setUp()
		for patcher in (
			patch.dict(os.environ, SSO_ENV),
			patch.object(mpass_employee, "default_company", return_value=COMPANY),
		):
			patcher.start()
			self.addCleanup(patcher.stop)
		self.client = get_test_client()

	@classmethod
	def tearDownClass(cls):
		frappe.db.delete("Employee", {"user_id": cls.user})
		purge_test_users(cls.user)
		super().tearDownClass()

	def get(self, path: str):
		headers = {EMAIL_HEADER: self.user, ACCESS_TOKEN_HEADER: corporate_token()}
		return make_request(target=self.client.get, args=(path,), kwargs={"headers": headers})

	def test_the_first_sso_request_opens_the_hrms_app(self):
		response = self.get("/api/method/hrms.api.get_current_employee_info")

		self.assertEqual(response.status_code, 200)
		self.assertEqual(response.json["message"]["user_id"], self.user)
		self.assertEqual(response.json["message"]["company"], COMPANY)
