"""Keyword rules that decide the taxonomy type ahead of the model, for wording the taxonomy makes
unambiguous.

Each rule is a pure function of the cleaned subject and description (text_clean.py) that returns a
type id or None. They come from the type definitions in taxonomy.json and the tie-breaks in
prompts/classify_v2.md, not from reading tickets. ticket_types.classify() runs them before the model and
reports the source as rule:<name>; baseline.py --labels measures how often each fires and how often it
agrees with Claude's labels.
"""

from __future__ import annotations

import re

_H = r"[^\S\r\n]"

# --- staff_logon: recurring log-on and password keep-alive tasks are staff_access ----------

_STAFF_TASK = re.compile(r"(?i)\[" + _H + r"*(?:log" + _H + r"*on|login|sign" + _H + r"*in|change" + _H
                         + r"+password)" + _H + r"+(?:to|for)\b")


def staff_logon(subject: str, text: str) -> str | None:
    """[Log on to ...] and [Change Password for ...] tasks."""
    return "staff_access" if _STAFF_TASK.search(subject) else None


# --- validation_results: Validation Results reports are always outage_availability ----------

_VALIDATION = re.compile(r"(?i)^\W*(?:(?:re|fw|fwd)" + _H + r"*:" + _H + r"*)*validation" + _H + r"+results\b")


def validation_results(subject: str, text: str) -> str | None:
    """FDDBA post-restart Validation Results reports, by subject."""
    return "outage_availability" if _VALIDATION.search(subject) else None


# --- sip_review: a SIP review of one area takes that area's type ----------------------------

_SIP_AREAS = [
    (re.compile(r"(?i)\bsecurity" + _H + r"+(?:audit|review)\b"), "access_security"),
    (re.compile(r"(?i)\b(?:index|performance)\b(?:" + _H + r"+(?:and|&)" + _H
                + r"+(?:index|performance))?" + _H + r"+review\b"), "performance"),
    (re.compile(r"(?i)\bcapacity" + _H + r"+review\b"), "capacity_storage"),
    (re.compile(r"(?i)\b(?:dr" + _H + r"*/" + _H + r"*ha|ha" + _H + r"*/" + _H + r"*dr)" + _H + r"+review\b"),
     "replication_ha_dr"),
    (re.compile(r"(?i)\b(?:database" + _H + r"+)?maintenance" + _H + r"+review\b|\bpatching\b"),
     "maintenance_patching"),
]
_SIP = re.compile(r"\bSIP\b")


def sip_review(subject: str, text: str) -> str | None:
    """SIP Security Audit, Index and Performance, Capacity, DR/HA and Database Maintenance reviews and
    SIP patching, by subject. The subject decides even when the body carries another template."""
    if not _SIP.search(subject):
        return None
    hits = {kind for pat, kind in _SIP_AREAS if pat.search(subject)}
    return hits.pop() if len(hits) == 1 else None


# --- database_refresh: a subject that names a database refresh ---------------------------------

_REFRESH = r"\brefresh(?:es|ed|ing)?\b"
_DB_REFRESH = re.compile(r"(?i)\b(?:database|db)s?" + _H + r"+" + _REFRESH)
# Looser wording: refresh of a named database, or refresh from or to an environment
_REFRESH_DB = re.compile(r"(?i)" + _REFRESH + r"(?:" + _H + r"+(?:of|the|a|an))*(?:" + _H + r"+\S+){0,2}?" + _H
                         + r"+(?:database|db)s?\b"
                         r"|" + _REFRESH + r"[^\n]{0,60}\b(?:from|to)" + _H
                         + r"+(?:prod\w*|pre-?prod\w*|dev\w*|test|qa|uat|stag\w*|sandbox)\b")
# Refreshes that are not copying a database
_NOT_DB = re.compile(r"(?i)\b(?:stat(?:istic)?s|reports?|cache|tokens?|views?|cubes?|ssas|power" + _H
                     + r"*bi|dashboards?|pages?|credentials?|passwords?)\b")


def database_refresh(subject: str, text: str) -> str | None:
    """A Database refresh or DB refresh subject, or one that refreshes a named database or refreshes
    from or to an environment (not statistics, reports, caches, cubes or tokens)."""
    if _DB_REFRESH.search(subject):
        return "database_refresh"
    if _REFRESH_DB.search(subject) and not _NOT_DB.search(subject):
        return "database_refresh"
    return None


# --- access_request: database access for client people, service accounts and applications ---

_ACCESS = re.compile(
    r"(?i)\b(?:read|write|read" + _H + r"*[-/]?" + _H + r"*(?:only|write)|readonly|select|execute|insert|update"
    + r"|owner|dbo|sysadmin)" + _H + r"+(?:access|permissions?|rights|privileges?)\b"
    r"|\bgrant(?:ed|ing|s)?\b[^.\n]{0,60}\b(?:access|permissions?|rights|privileges?|roles?|db_\w+|select|execute)\b"
    r"|\b(?:give|provide|add)\b[^.\n]{0,40}\b(?:access|permissions?)" + _H + r"+(?:to|on|for)\b"
    r"|\bdb_(?:datareader|datawriter|owner|ddladmin|securityadmin|accessadmin|executor|backupoperator)\b"
    r"|\b(?:add|create)" + _H + r"+(?:the" + _H + r"+|a" + _H + r"+|an" + _H + r"+|new" + _H + r"+)*(?:sql"
    + _H + r"+|windows" + _H + r"+|ad" + _H + r"+|domain" + _H + r"+)?logins?\b"
    r"|\bpermissions?" + _H + r"+to\b"
    r"|\b(?:revoke|remove)\b[^.\n]{0,40}\b(?:access|permissions?|logins?)\b")
_ACCOUNT = re.compile(r"(?i)\b(?:logins?|users?|roles?|schemas?|service" + _H + r"+accounts?|svc\w*|accounts?"
                      r"|groups?|permissions?)\b|\w\\\w")
# Whose access: our staff (staff_access) and our collector (monitoring_tooling) are not client access;
# change-ticket, release and refresh work keeps its own type.
_NOT_CLIENT = re.compile(r"(?i)\b(?:fortified|FD)(?:" + _H + r"+data)?(?:'s)?" + _H
                         + r"+(?:accounts?|logins?|users?|staff|dbas?|team" + _H + r"+members?|employees?|engineers?)\b"
                         r"|\bour" + _H + r"+(?:accounts?|logins?|access|team|dbas?|staff)\b|\bkeeper\b"
                         r"|\bvpn\b|\bmfa\b|\bvdi\b|\bcitrix\b|\bwisdom\b|\bcollector\b|\bexporter\b|\bfddba\b"
                         r"|\bprometheus\b|\bgrafana\b|\bmonitoring" + _H + r"+(?:account|login|user)\b")
# Copying or restoring into another environment is database_refresh; running supplied scripts, change
# tickets and releases is deployment.
_OTHER_WORK = re.compile(r"(?i)\bCHG\d+|\bTCM\d*\b|\brelease\b|\bdeploy\w*|\brefresh\w*|\brestor\w*|\bcop(?:y|ied)\b"
                         r"|\bscripts?\b|\.sql\b|\bdacpac\b")


def access_request(subject: str, text: str) -> str | None:
    """A grant, read, write or access request for a login, user, role, service account or schema,
    unless the account is ours, the access is our collector's, or the work is a change ticket, release,
    supplied script, or a refresh or restore into another environment."""
    both = f"{subject}\n{text}"
    if not (_ACCESS.search(both) and _ACCOUNT.search(both)):
        return None
    if _NOT_CLIENT.search(both) or _OTHER_WORK.search(both):
        return None
    return "access_security"


# --- ha_terms: a subject that names availability groups, replication or failover --------------

# AG and AGs only in upper case, so "ag" inside other words or lower-case text does not count
_HA = re.compile(r"\bAGs?\b|(?i:\bavailability" + _H + r"+groups?\b|\balways" + _H + r"*on\b|\breplicat\w*"
                 r"|\blog" + _H + r"*shipping\b|\bmirroring\b|\bdata" + _H + r"*guard\b|\bdbvisit\b"
                 r"|\bfail(?:ed)?" + _H + r"*-?" + _H + r"*overs?\b)")
# Patching or upgrading AG nodes, often with a failover, is maintenance_patching or an upgrade
_PATCHING = re.compile(r"(?i)\bpatch\w*|\bcumulative" + _H + r"+updates?\b|\bCU\d+\b|\bwindows" + _H
                       + r"+updates?\b|\bupgrad\w*")


def ha_terms(subject: str, text: str) -> str | None:
    """An availability group (AG, AGs, availability group, Always On), replication, log shipping,
    mirroring, Data Guard, Dbvisit or failover subject, unless it is about patching or upgrading. In
    Claude's sample labels these subjects were replication_ha_dr 27 times out of 28; 'replica' (14 of
    23) and 'production' (4 of 37) are left to the model."""
    return "replication_ha_dr" if _HA.search(subject) and not _PATCHING.search(subject) else None


RULES = [("staff_logon", staff_logon), ("validation_results", validation_results), ("sip_review", sip_review),
         ("database_refresh", database_refresh), ("access_request", access_request), ("ha_terms", ha_terms)]


def apply_rules(subject: str, text: str, rules=RULES) -> tuple[str, str] | None:
    """(type, rule name) from the first rule that fires on the cleaned text, or None."""
    for name, rule in rules:
        kind = rule(subject or "", text or "")
        if kind:
            return kind, name
    return None
