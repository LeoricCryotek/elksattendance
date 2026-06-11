# -*- coding: utf-8 -*-
# =============================================================================
# === HUMAN ===
# Lodge additions to the employee record: a "receives tips" flag, a cached list
# of who manages this person (for security), a default clock-in PIN of 0000 when
# there's no phone, and two buttons — invite the person to the web portal, and
# link several role-employees (Board/Kitchen/Bar) to one shared contact.
#
# === AI AGENT ===
# Inherits hr.employee. x_manager_user_ids is a STORED recursive compute used by
# the record rules in elksattendance_security.xml. create/write set a default
# PIN '0000' when no phone exists (only fills an empty pin). The two action_*
# methods just open wizards (portal.wizard / elks.employee.link.wizard); the
# multi-employee story relies on a SHARED work_contact_id, not user_id (Odoo
# forbids one user_id across employees).
# =============================================================================
"""Extend hr.employee with attendance-related fields.

``x_manager_user_ids`` stores all ``res.users`` IDs that sit above this
employee in the org tree (via ``parent_id``).  The field is recomputed
whenever a manager assignment changes, cascading to all subordinates.

The record rule in ``elksattendance_security.xml`` uses this field so
that any manager in the chain gets attendance access.

``x_receives_tips`` is a simple Boolean flag set by a manager to
indicate that this employee receives tips or gratuity.  When True,
every attendance record for this employee is auto-flagged via the
related field ``x_is_tipped_shift`` on ``hr.attendance``.

``action_invite_to_portal`` opens Odoo's standard portal-grant wizard
for the employee's contact so an admin can give them a portal login to
review and approve their timecards.
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


# =============================================================================
# === HUMAN ===
# The employee record with our extra fields and the create/write hooks that set
# the default clock PIN.
#
# === AI AGENT ===
# _ELKS_PHONE_FIELDS lists the phone fields that count as "has a phone". write()
# re-applies the PIN default only when a phone field or pin changed (avoids
# churn). Field defs (x_receives_tips, x_manager_user_ids) follow the hooks.
# =============================================================================
class HrEmployee(models.Model):
    _inherit = "hr.employee"

    # Phone fields checked when defaulting the kiosk clock-in/out PIN.
    _ELKS_PHONE_FIELDS = ("work_phone", "mobile_phone", "private_phone")

    # === HUMAN ===
    # On create/save, give a phone-less employee a default clock code of 0000.
    # === AI AGENT ===
    # write() re-checks only when a phone field or 'pin' is in vals. Setting
    # emp.pin recurses through write() but is a no-op (pin now set), so safe.
    @api.model_create_multi
    def create(self, vals_list):
        employees = super().create(vals_list)
        employees._elks_default_clock_pin()
        return employees

    def write(self, vals):
        res = super().write(vals)
        if set(vals) & set(self._ELKS_PHONE_FIELDS + ("pin",)):
            self._elks_default_clock_pin()
        return res

    # === HUMAN ===
    # If someone has no phone number, their kiosk PIN defaults to 0000 so they
    # can still clock in/out. Existing PINs are never overwritten.
    # === AI AGENT ===
    # Only fills an EMPTY pin and only when no phone field has a value. Uses
    # _fields guard since private_phone may not exist on all builds. A migration
    # (19.0.4.7) backfilled existing phone-less employees.
    def _elks_default_clock_pin(self):
        """Give employees with no phone number a default clock code of 0000.

        The kiosk PIN is normally derived from the phone number; with no
        phone there's nothing to derive from, so fall back to ``0000`` so
        the person can still clock in/out. Only fills an empty PIN —
        existing codes are left alone.
        """
        for emp in self:
            if emp.pin:
                continue
            phone_fields = [f for f in self._ELKS_PHONE_FIELDS
                            if f in emp._fields]
            if not any(emp[f] for f in phone_fields):
                emp.pin = '0000'

    x_receives_tips = fields.Boolean(
        "Receives Tips / Gratuity",
        default=False,
        tracking=True,
        help="Check this box if the employee receives tips or gratuity. "
             "Their attendance records will be automatically flagged "
             "for tip tracking on payroll timecards.",
    )

    x_manager_user_ids = fields.Many2many(
        "res.users",
        "hr_employee_manager_user_rel",
        "employee_id",
        "user_id",
        string="Manager Chain (Users)",
        compute="_compute_manager_user_ids",
        store=True,
        recursive=True,
        help="All res.users linked to managers above this employee in "
             "the org tree.  Used by attendance record rules.",
    )

    # === HUMAN ===
    # Builds the list of everyone above this person in the org chart, so a
    # manager automatically gets access to their reports' time.
    # === AI AGENT ===
    # STORED recursive compute: depends on parent_id.x_manager_user_ids so a
    # change high in the tree cascades down. Consumed by the team-manager record
    # rules ('x_manager_user_ids','in',user.id). Keep recursive=True.
    @api.depends('parent_id', 'parent_id.user_id', 'parent_id.x_manager_user_ids')
    def _compute_manager_user_ids(self):
        """Walk up the org tree and collect every manager's user_id.

        Because the depends includes ``parent_id.x_manager_user_ids``,
        changing a manager high in the tree cascades automatically to
        all employees below — Odoo's ORM triggers recompute on every
        record whose dependency changed.
        """
        for emp in self:
            user_ids = set()
            manager = emp.parent_id
            if manager and manager.user_id:
                user_ids.add(manager.user_id.id)
                # Add the parent's already-computed chain
                user_ids |= set(manager.x_manager_user_ids.ids)
            emp.x_manager_user_ids = [(6, 0, list(user_ids))]

    # ------------------------------------------------------------------
    # Portal access + multi-role linking (buttons on the employee form)
    # === HUMAN ===
    # Two admin actions: invite this person to the web portal, and link several
    # of their role-employees to one shared contact so a single login covers all.
    # === AI AGENT ===
    # _elks_portal_partner picks the contact to grant (work_contact_id, else the
    # linked user's partner). Both actions just RETURN act_window dicts opening a
    # wizard; the actual portal grant is the admin confirming inside portal.wizard
    # (no silent account creation). Link wizard sets a shared work_contact_id.
    # ------------------------------------------------------------------
    def _elks_portal_partner(self):
        """The contact to grant portal access to (must have an email)."""
        self.ensure_one()
        partner = self.work_contact_id
        if not partner and self.user_id:
            partner = self.user_id.partner_id
        return partner

    def action_invite_to_portal(self):
        """Open the standard portal-grant wizard for this employee's contact.

        The admin confirms the grant inside the wizard (Odoo then sends the
        invitation and creates the portal user) — we never create the
        account silently.
        """
        self.ensure_one()
        partner = self._elks_portal_partner()
        if not partner:
            raise UserError(_(
                "This employee has no linked contact. Set a Work Contact "
                "(or link a user) before inviting them to the portal."))
        if not partner.email:
            raise UserError(_(
                "%s has no email address. Add one to the contact before "
                "sending a portal invitation.") % partner.display_name)
        return {
            'type': 'ir.actions.act_window',
            'name': _("Grant Portal Access"),
            'res_model': 'portal.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'active_model': 'res.partner',
                'active_ids': partner.ids,
            },
        }

    def action_elks_link_roles(self):
        """Open the wizard to share one Work Contact across employee records.

        Lets one person hold several employee records (Board, Kitchen,
        Bar…) under a single portal identity, since Odoo won't allow one
        Related User on multiple employees.
        """
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Link Roles to One Contact"),
            'res_model': 'elks.employee.link.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {
                'active_model': 'hr.employee',
                'active_ids': self.ids,
            },
        }
