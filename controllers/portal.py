# -*- coding: utf-8 -*-
# =============================================================================
# === HUMAN ===
# The member-portal pages for timecards: an employee reviews/approves their own
# cards, requests time fixes and chats per shift, and a supervisor sees everyone
# they approve (grouped by area) and applies/approves. All behind a login.
#
# === AI AGENT ===
# SECURITY-CRITICAL. Every route is auth='user' (NO public, NO access token —
# see 19.0.4.6). _elks_get_timecard is the single gatekeeper: it sudo-browses
# then allows ONLY owner / approver / officer, else None -> redirect /my. Never
# reintroduce _document_check_access / token access here. Actions run on the
# sudo recordset but the model's _elks_sign / identity helpers re-check the real
# user. Volunteer cards are filtered out via _ELKS_NOT_VOLUNTEER.
# =============================================================================
"""Employee portal for reviewing and approving timecards.

Routes:
    /my/timecards            — current pay period (days broken out) plus a
                               searchable list of past periods
    /my/timecard/<id>        — one timecard: shifts, totals, signatures
    /my/timecard/<id>/approve — POST: employee or approver sign-off

Access is login-only and identity-checked: every route requires an
authenticated user, and a timecard is reachable ONLY by the employee it
belongs to, that employee's Attendance approver, or an attendance
administrator (``hr_attendance.group_hr_attendance_user``). There is no
public or access-token entry point — the email link forces a login.
"""
from collections import OrderedDict
from datetime import date, datetime

import pytz

from odoo import http, fields, _
from odoo.http import request
from odoo.tools import plaintext2html
from odoo.addons.portal.controllers.portal import CustomerPortal, pager as portal_pager


# === HUMAN ===
# The portal controller: home-page counters, the access gate, and all the
# /my/timecard* pages.
# === AI AGENT ===
# Extends portal.CustomerPortal. _prepare_home_portal_values feeds the two home
# cards (timecard_count / timecard_approval_count). The _elks_*_domain helpers
# all start from _ELKS_NOT_VOLUNTEER. Owner match is user_id OR work_contact_id
# (multi-role person under one contact).
class TimecardPortal(CustomerPortal):

    # ------------------------------------------------------------------
    # Home counters + access gate + visibility domains
    # === HUMAN ===
    # How many cards to badge on the portal home, and the rules for what each
    # person is allowed to see.
    # === AI AGENT ===
    # _elks_get_timecard = THE per-record gate (owner/approver/officer only).
    # _elks_timecard_domain = what the employee view lists; _elks_approver_domain
    # = what the approver view lists. Both exclude Volunteer-dept cards.
    # ------------------------------------------------------------------
    def _prepare_home_portal_values(self, counters):
        values = super()._prepare_home_portal_values(counters)
        if 'timecard_count' in counters:
            values['timecard_count'] = request.env['elks.timecard'].search_count(
                self._elks_timecard_domain())
        if 'timecard_approval_count' in counters:
            values['timecard_approval_count'] = request.env['elks.timecard'].search_count(
                self._elks_approver_domain())
        return values

    def _elks_approver_domain(self):
        """Timecards the current user is the Attendance approver for."""
        return self._ELKS_NOT_VOLUNTEER + [('approver_id', '=', request.env.user.id)]

    def _elks_get_timecard(self, timecard_id):
        """Return the timecard ONLY if the logged-in user may access it.

        Access is strictly: the employee (owner), their Attendance
        approver, or an attendance administrator. No public/token access.
        Returns a sudo recordset (so we can read/post) or None.
        """
        tc = request.env['elks.timecard'].sudo().browse(timecard_id)
        if not tc.exists():
            return None
        user = request.env.user
        if (tc._elks_is_owner_for(user)
                or tc._elks_is_approver_for(user)
                or tc._is_officer(user)):
            return tc
        return None

    # Volunteer-department cards are never payroll and must never show.
    _ELKS_NOT_VOLUNTEER = [('employee_id.department_id.name', '!=', 'Volunteers')]

    def _elks_timecard_domain(self):
        """Timecards the current portal user may see: their own / approved."""
        user = request.env.user
        return self._ELKS_NOT_VOLUNTEER + ['|', '|',
                ('employee_id.user_id', '=', user.id),
                ('employee_id.work_contact_id', '=', user.partner_id.id),
                ('approver_id', '=', user.id)]

    # ------------------------------------------------------------------
    # Main page: current period (detailed) + past periods (searchable)
    # === HUMAN ===
    # The employee's own timecards page: current pay period(s) broken out by day
    # at the top, past periods collapsed and filterable below.
    # === AI AGENT ===
    # Lazily ensures cards exist (_elks_ensure_for_user) on load. 'current' is a
    # RECORDSET (a person may hold several role-employees, each a card). Empty
    # searchbar_* defaults are passed so portal.portal_searchbar renders only the
    # Filters menu without NameError.
    # ------------------------------------------------------------------
    @http.route(['/my/timecards', '/my/timecards/page/<int:page>'],
                type='http', auth='user', website=True)
    def portal_my_timecards(self, page=1, filterby=None, **kw):
        Timecard = request.env['elks.timecard']
        # Make sure the current period (and past periods with hours) exist.
        Timecard.sudo()._elks_ensure_for_user(request.env.user)

        today = fields.Date.context_today(request.env.user)
        base = self._elks_timecard_domain()

        # Current pay period — one card per employee record (a person may
        # hold several: Kitchen, Bar, Board…), all shown broken out at top.
        current = Timecard.search(
            base + [('period_start', '<=', today), ('period_end', '>=', today)],
            order='employee_id')

        # Search filters for the (collapsed) past periods.
        this_year = date(today.year, 1, 1)
        last_year = date(today.year - 1, 1, 1)
        searchbar_filters = {
            'this_year': {'label': _('This Year'),
                          'domain': [('period_start', '>=', this_year)]},
            'last_year': {'label': _('Last Year'),
                          'domain': [('period_start', '>=', last_year),
                                     ('period_start', '<', this_year)]},
            'all': {'label': _('All Time'), 'domain': []},
        }
        if not filterby:
            filterby = 'this_year'
        filter_domain = searchbar_filters[filterby]['domain']

        past_domain = base + filter_domain
        if current:
            past_domain += [('id', 'not in', current.ids)]

        total = Timecard.search_count(past_domain)
        pager = portal_pager(
            url="/my/timecards", total=total, page=page, step=12,
            url_args={'filterby': filterby})
        past = Timecard.search(
            past_domain, limit=12, offset=pager['offset'],
            order='period_start desc')

        all_emps = current.mapped('employee_id') | past.mapped('employee_id')
        values = {
            'current': current,
            'past': past,
            'show_tips': any(all_emps.mapped('x_receives_tips')),
            'pager': pager,
            'page_name': 'timecard',
            'default_url': '/my/timecards',
            'searchbar_filters': searchbar_filters,
            'filterby': filterby,
            # Empty defaults so portal_searchbar only renders the Filters menu
            'searchbar_sortings': {},
            'sortby': None,
            'searchbar_inputs': [],
            'searchbar_groupby': {},
            'groupby': None,
            'user': request.env.user,
        }
        return request.render('elksattendance.portal_my_timecards', values)

    # ------------------------------------------------------------------
    # Detail
    # === HUMAN ===
    # One timecard's full page: shifts, totals, signatures, requests, and a
    # message thread.
    # === AI AGENT ===
    # Gated by _elks_get_timecard. Only PUBLIC comments are shown (internal log
    # notes filtered by subtype.internal). This is the page the email link opens
    # — login is forced because the route is auth='user'.
    # ------------------------------------------------------------------
    @http.route(['/my/timecard/<int:timecard_id>'],
                type='http', auth='user', website=True)
    def portal_my_timecard(self, timecard_id, **kw):
        tc_sudo = self._elks_get_timecard(timecard_id)
        if not tc_sudo:
            return request.redirect('/my')
        # Public comments only (not internal log notes)
        messages = tc_sudo.message_ids.filtered(
            lambda m: m.message_type in ('comment', 'email')
            and m.subtype_id and not m.subtype_id.internal)
        values = {
            'timecard': tc_sudo,
            'card_messages': messages,
            'page_name': 'timecard',
            'user': request.env.user,
        }
        return request.render('elksattendance.portal_timecard_page', values)

    # ------------------------------------------------------------------
    # Approve (employee or approver)
    # === HUMAN ===
    # The "I approve" / "Approve as supervisor" button target.
    # === AI AGENT ===
    # Routes to _elks_sign with role based on who the user is. The model re-checks
    # identity and (for approver from draft) records a supervisor OVERRIDE.
    # Optional 'redirect' kw lets the approvals list send the user back to itself.
    # ------------------------------------------------------------------
    @http.route(['/my/timecard/<int:timecard_id>/approve'],
                type='http', auth='user', methods=['POST'], website=True)
    def portal_timecard_approve(self, timecard_id, **kw):
        tc_sudo = self._elks_get_timecard(timecard_id)
        if not tc_sudo:
            return request.redirect('/my')

        user = request.env.user
        # tc_sudo is a sudo recordset; _elks_sign re-checks identity against
        # the real logged-in user and writes with sudo (portal users are
        # read-only on this model).
        if tc_sudo._elks_is_owner_for(user):
            tc_sudo._elks_sign(user, 'employee')
        elif tc_sudo._elks_is_approver_for(user):
            tc_sudo._elks_sign(user, 'approver')
        return request.redirect(kw.get('redirect') or '/my/timecard/%s' % timecard_id)

    # ------------------------------------------------------------------
    # Per-line view + adjustment suggestion + per-line chat
    # === HUMAN ===
    # The single-shift page: see the shift, request a time fix (only while the
    # period is open), and chat about that specific shift.
    # === AI AGENT ===
    # The line view, the suggest POST, and the line-message POST. Suggest is
    # owner-only AND state=='draft', rejects empty submits (no change + no
    # reason -> ?e=empty). Times come in as datetime-local (naive local) and are
    # converted to naive UTC via the user's tz. Messages post to the ATTENDANCE
    # chatter (so they also show in the backend) as public comments, sudo.
    # ------------------------------------------------------------------
    @http.route(['/my/timecard/<int:timecard_id>/line/<int:attendance_id>'],
                type='http', auth='user', website=True)
    def portal_timecard_line(self, timecard_id, attendance_id, **kw):
        tc_sudo = self._elks_get_timecard(timecard_id)
        if not tc_sudo:
            return request.redirect('/my')
        att = tc_sudo.attendance_ids.filtered(lambda a: a.id == attendance_id)
        if not att:
            return request.redirect('/my/timecard/%s' % timecard_id)
        user = request.env.user
        suggestions = tc_sudo.adjustment_ids.filtered(
            lambda a: a.attendance_id.id == attendance_id)

        def to_local_input(dt):
            if not dt:
                return ''
            return fields.Datetime.context_timestamp(
                tc_sudo, dt).strftime('%Y-%m-%dT%H:%M')

        # Public comments on this attendance line (skip internal log notes)
        messages = att.sudo().message_ids.filtered(
            lambda m: m.message_type in ('comment', 'email')
            and m.subtype_id and not m.subtype_id.internal)

        values = {
            'timecard': tc_sudo,
            'att': att,
            'suggestions': suggestions,
            'line_messages': messages,
            'is_owner': tc_sudo._elks_is_owner_for(user),
            'is_approver': tc_sudo._elks_is_approver_for(user),
            # Requests are only allowed while the period is still open (draft)
            'can_request': tc_sudo._elks_is_owner_for(user) and tc_sudo.state == 'draft',
            'in_value': to_local_input(att.check_in),
            'out_value': to_local_input(att.check_out),
            'error': kw.get('e'),
            'page_name': 'timecard',
            'user': user,
        }
        return request.render('elksattendance.portal_timecard_line', values)

    @http.route(['/my/timecard/<int:timecard_id>/line/<int:attendance_id>/suggest'],
                type='http', auth='user', methods=['POST'], website=True)
    def portal_timecard_suggest(self, timecard_id, attendance_id, **kw):
        tc_sudo = self._elks_get_timecard(timecard_id)
        if not tc_sudo:
            return request.redirect('/my')
        user = request.env.user
        att = tc_sudo.attendance_ids.filtered(lambda a: a.id == attendance_id)
        line_url = '/my/timecard/%s/line/%s' % (timecard_id, attendance_id)

        # Only the owner, and only while the period is still open.
        if not (att and tc_sudo._elks_is_owner_for(user) and tc_sudo.state == 'draft'):
            return request.redirect(line_url)

        tz = pytz.timezone(user.tz or 'UTC')

        def to_utc(s):
            if not s:
                return False
            try:
                naive = datetime.strptime(s, '%Y-%m-%dT%H:%M')
            except ValueError:
                return False
            return tz.localize(naive).astimezone(pytz.utc).replace(tzinfo=None)

        pin = to_utc(kw.get('proposed_check_in'))
        pout = to_utc(kw.get('proposed_check_out'))
        reason = (kw.get('reason') or '').strip()

        # Reject an empty submit: no time change AND no reason.
        has_change = (pin and pin != att.check_in) or (pout and pout != att.check_out)
        if not (has_change or reason):
            return request.redirect(line_url + '?e=empty')

        tc_sudo._elks_create_suggestion(att, pin, pout, reason, user)
        return request.redirect(line_url)

    @http.route(['/my/timecard/<int:timecard_id>/line/<int:attendance_id>/message'],
                type='http', auth='user', methods=['POST'], website=True)
    def portal_timecard_line_message(self, timecard_id, attendance_id, **kw):
        tc_sudo = self._elks_get_timecard(timecard_id)
        if not tc_sudo:
            return request.redirect('/my')
        user = request.env.user
        att = tc_sudo.attendance_ids.filtered(lambda a: a.id == attendance_id)
        body = (kw.get('body') or '').strip()
        if att and body:
            att.sudo().message_post(
                body=plaintext2html(body),
                author_id=user.partner_id.id,
                message_type='comment',
                subtype_xmlid='mail.mt_comment')
        return request.redirect(
            '/my/timecard/%s/line/%s' % (timecard_id, attendance_id))

    # === HUMAN ===
    # Posts a message on the whole timecard's discussion thread.
    # === AI AGENT ===
    # Period-level chatter (vs the per-line one above). Public comment, sudo,
    # authored by the real user. Replaced the old portal.message_thread widget
    # that allowed public posting (security fix).
    @http.route(['/my/timecard/<int:timecard_id>/message'],
                type='http', auth='user', methods=['POST'], website=True)
    def portal_timecard_message(self, timecard_id, **kw):
        tc_sudo = self._elks_get_timecard(timecard_id)
        if not tc_sudo:
            return request.redirect('/my')
        body = (kw.get('body') or '').strip()
        if body:
            tc_sudo.message_post(
                body=plaintext2html(body),
                author_id=request.env.user.partner_id.id,
                message_type='comment',
                subtype_xmlid='mail.mt_comment')
        return request.redirect('/my/timecard/%s' % timecard_id)

    # ------------------------------------------------------------------
    # Approver workflow: everyone I can approve, grouped by area/dept
    # === HUMAN ===
    # The supervisor's page: every timecard they can approve, grouped by area
    # (Kitchen, Lounge…), with inline Approve and a pending-request badge.
    # === AI AGENT ===
    # Ensures cards exist for all approvees (even if they never logged in). Groups
    # by department name into an OrderedDict {dept: recordset}. Default filter
    # 'pending' = state != approved (includes draft, so override is reachable).
    # The adjustment route applies/rejects a request (approver/officer only).
    # ------------------------------------------------------------------
    @http.route(['/my/timecard-approvals'], type='http', auth='user', website=True)
    def portal_timecard_approvals(self, filterby=None, **kw):
        Timecard = request.env['elks.timecard']
        # Make sure cards exist for everyone this user approves (even if the
        # employee never opened the portal).
        Timecard.sudo()._elks_ensure_for_approver(request.env.user)
        today = fields.Date.context_today(request.env.user)
        base = self._elks_approver_domain()

        this_year = date(today.year, 1, 1)
        last_year = date(today.year - 1, 1, 1)
        searchbar_filters = {
            'pending': {'label': _('Needs Approval'),
                        'domain': [('state', '!=', 'approved')]},
            'this_year': {'label': _('This Year'),
                          'domain': [('period_start', '>=', this_year)]},
            'last_year': {'label': _('Last Year'),
                          'domain': [('period_start', '>=', last_year),
                                     ('period_start', '<', this_year)]},
            'all': {'label': _('All Time'), 'domain': []},
        }
        if not filterby:
            filterby = 'pending'
        domain = base + searchbar_filters[filterby]['domain']
        cards = Timecard.search(domain, order='period_start desc')

        # Group by department (area): {dept_name: [timecards]}
        groups = OrderedDict()
        for tc in cards.sorted(
                key=lambda t: (t.employee_id.department_id.name or 'Unassigned',
                               t.employee_id.name or '')):
            dept = tc.employee_id.department_id.name or _('Unassigned')
            groups.setdefault(dept, request.env['elks.timecard'])
            groups[dept] |= tc

        values = {
            'groups': groups,
            'page_name': 'timecard_approval',
            'default_url': '/my/timecard-approvals',
            'searchbar_filters': searchbar_filters,
            'filterby': filterby,
            'searchbar_sortings': {}, 'sortby': None,
            'searchbar_inputs': [], 'searchbar_groupby': {}, 'groupby': None,
            'user': request.env.user,
        }
        return request.render('elksattendance.portal_timecard_approvals', values)

    @http.route(['/my/timecard/<int:timecard_id>/adjustment/<int:adjustment_id>/<string:decision>'],
                type='http', auth='user', methods=['POST'], website=True)
    def portal_timecard_adjustment(self, timecard_id, adjustment_id, decision,
                                   **kw):
        tc_sudo = self._elks_get_timecard(timecard_id)
        if not tc_sudo:
            return request.redirect('/my')
        user = request.env.user
        adj = tc_sudo.adjustment_ids.filtered(lambda a: a.id == adjustment_id)
        # Only the approver (or an officer) may apply/reject.
        if adj and (tc_sudo._elks_is_approver_for(user)
                    or tc_sudo._is_officer(user)):
            if decision == 'apply':
                adj.with_user(user).action_apply()
            elif decision == 'reject':
                adj.with_user(user).action_reject()
        return request.redirect(kw.get('redirect') or '/my/timecard-approvals')
