# -*- coding: utf-8 -*-
# =============================================================================
# === HUMAN ===
# The "Link Roles to Contact" pop-up. When one person has several employee
# records (Board volunteer, Kitchen, Bar — each with its own pay), this points
# them all at one shared contact so a single portal login covers every role.
# Optionally fires the portal invite afterward.
#
# === AI AGENT ===
# TransientModel elks.employee.link.wizard, launched from the employee list
# Action menu or the form button (action_elks_link_roles). It just writes a
# shared work_contact_id onto the selected employees — that's the key the portal
# matches on (Odoo forbids one user_id across employees). default_get pre-fills
# from the selection. Does NOT create the portal user itself; invite_portal hands
# off to the standard portal.wizard.
# =============================================================================
"""Link several employee records to one shared Work Contact.

A single person can hold several employee records (e.g. Board/Volunteer,
Kitchen, Bar) so each role can carry its own pay rate and approver.
Odoo forbids linking one *Related User* to multiple employees in a
company, but multiple employees may share one **Work Contact**. The
portal recognises a person by that contact, so sharing it lets one
login review and approve every role's timecard.
"""
from odoo import api, fields, models, _
from odoo.exceptions import UserError


# === HUMAN ===
# The pop-up form: choose the shared contact, confirm the employees, optionally
# send the portal invite.
# === AI AGENT ===
# default_get reads active_ids (employees) from context and pre-selects a shared
# partner if they already agree, else the linked user's partner. action_link
# writes work_contact_id; if invite_portal, returns the portal.wizard act_window.
class ElksEmployeeLinkWizard(models.TransientModel):
    _name = 'elks.employee.link.wizard'
    _description = 'Link Employees to One Shared Contact'

    partner_id = fields.Many2one(
        'res.partner', string="Person (Shared Contact)", required=True,
        help="The single contact representing this person. Every selected "
             "employee will share it, so one portal login covers them all.")
    employee_ids = fields.Many2many(
        'hr.employee', string="Employees", required=True)
    invite_portal = fields.Boolean(
        "Send portal invitation", default=False,
        help="After linking, open the portal-access wizard for the shared "
             "contact so they can log in.")

    # === HUMAN ===
    # Pre-fills the form from whichever employees you launched it on.
    # === AI AGENT ===
    # Only acts when active_model is hr.employee. Defaults partner_id to the
    # common contact if all selected already share one.
    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        ctx = self.env.context
        emp_ids = ctx.get('active_ids') or []
        if ctx.get('active_model') == 'hr.employee' and emp_ids:
            emps = self.env['hr.employee'].browse(emp_ids)
            res['employee_ids'] = [(6, 0, emps.ids)]
            contacts = emps.mapped('work_contact_id')
            if len(contacts) == 1:
                res['partner_id'] = contacts.id
            else:
                users = emps.mapped('user_id')
                if users and users[0].partner_id:
                    res['partner_id'] = users[0].partner_id.id
        return res

    # === HUMAN ===
    # Applies the shared contact to all selected employees, then optionally opens
    # the portal-invite step.
    # === AI AGENT ===
    # Single write of work_contact_id across employee_ids. Returns either an
    # act_window_close or the portal.wizard action (when invite_portal is set).
    def action_link(self):
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_("Choose the shared contact first."))
        if not self.employee_ids:
            raise UserError(_("Select at least one employee."))
        self.employee_ids.write({'work_contact_id': self.partner_id.id})

        if self.invite_portal:
            return {
                'type': 'ir.actions.act_window',
                'name': _("Grant Portal Access"),
                'res_model': 'portal.wizard',
                'view_mode': 'form',
                'target': 'new',
                'context': {
                    'active_model': 'res.partner',
                    'active_ids': self.partner_id.ids,
                },
            }
        return {'type': 'ir.actions.act_window_close'}
