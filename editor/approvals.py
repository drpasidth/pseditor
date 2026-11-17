"""Human-in-the-loop approvals for agent actions.

The chat request is a long-lived stream. Before an agent action runs, the stream yields an ``approval`` event and the
server thread blocks here until the browser answers through ``/api/approvals/<id>`` (allow / deny), the user presses
Stop, or the wait times out. Nothing runs without an explicit "allow".

This needs a threaded server (``runserver`` is threaded by default; with gunicorn use ``--threads``).
"""
import threading
import uuid

from django.conf import settings

# Tools that only look at the workspace. Everything else (writes, deletes, commands, CLI/webhook agents)
# always needs approval. Set PSEDITOR_APPROVE_READS = True to be asked about these too.
READ_ONLY = frozenset({"workspace_info", "list_files", "read_file", "search"})

ALLOW, DENY, CANCELLED, TIMEOUT = "allow", "deny", "cancelled", "timeout"

_lock = threading.Lock()
_pending = {}


class Approval:
    def __init__(self, session_id, name, arguments):
        self.id = uuid.uuid4().hex
        self.session_id = session_id
        self.name = name
        self.arguments = arguments
        self.event = threading.Event()
        self.decision = None
        self.reason = ""


def required(name):
    if not getattr(settings, "PSEDITOR_REQUIRE_APPROVAL", True):
        return False
    if name in READ_ONLY:
        return bool(getattr(settings, "PSEDITOR_APPROVE_READS", False))
    return True


def create(session_id, name, arguments):
    a = Approval(session_id, name, arguments)
    with _lock:
        _pending[a.id] = a
    return a


def wait(a, timeout=None):
    """Block until the user decides. Returns ALLOW, DENY, CANCELLED or TIMEOUT."""
    if timeout is None:
        timeout = getattr(settings, "PSEDITOR_APPROVAL_TIMEOUT", 600)
    a.event.wait(timeout)
    with _lock:
        if a.decision is None:  # nobody answered in time
            a.decision = TIMEOUT
        _pending.pop(a.id, None)
        return a.decision


def discard(approval_id):
    with _lock:
        _pending.pop(approval_id, None)


def decide(approval_id, decision, reason=""):
    """Record the user's answer. Returns False if the request is not pending any more."""
    if decision not in (ALLOW, DENY):
        raise ValueError("decision must be 'allow' or 'deny'")
    with _lock:
        a = _pending.get(approval_id)
        if a is None or a.decision is not None:
            return False
        a.decision = decision
        a.reason = (reason or "").strip()[:1000]
    a.event.set()
    return True


def cancel_session(session_id):
    """Release every approval a chat session is waiting on (the user pressed Stop). Returns how many."""
    with _lock:
        waiting = [a for a in _pending.values() if a.session_id == session_id and a.decision is None]
        for a in waiting:
            a.decision = CANCELLED
    for a in waiting:
        a.event.set()
    return len(waiting)


def pending_for(session_id):
    with _lock:
        return [a for a in _pending.values() if a.session_id == session_id and a.decision is None]
