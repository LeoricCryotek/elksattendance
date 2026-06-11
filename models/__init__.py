# -*- coding: utf-8 -*-
# ============================================================================
# === HUMAN ===
# Registers every model file in this folder.
# === AI AGENT ===
# Import order is load order; elks_timecard before its adjustment is fine.
# ============================================================================
from . import hr_attendance
from . import hr_employee
from . import res_config_settings
from . import timecard_cron
from . import elks_timecard
from . import elks_timecard_adjustment
