"""Journal for a step that cannot be taken back: write the intent, do it, mark it done."""
import errno
import fcntl
import json
import os
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
_FIELDS = {"intent": ("id", "at", "what"), "done": ("id", "at"), "failed": ("id", "at", "detail"), "resolved": ("id", "at", "happened", "note"), "torn": ("at",)}
EXIT_OK = 0
EXIT_DID = 0
EXIT_FAILED = 1
EXIT_COULD_NOT = 2
EXIT_USAGE = 2
EXIT_STOP = 3
EXIT_STATUS_OPEN = 3
EXIT_RECOVER_OPEN = 3
EXIT_BUSY = 4
_STATUS_COUNTS = "%d open, %d done, %d cleared"
_RECOVER_COUNTS = "recovered %d of %d: %d happened, %d did not happen, %d still open"
_USAGE = 'usage: python3 intent_journal.py run JOURNAL --what TEXT [--key KEY] [--block all|key] [--failure-is-clean] -- COMMAND [ARG...] | status JOURNAL [--json] | resolve JOURNAL ID (--happened | --did-not-happen) --note TEXT | recover JOURNAL --prove COMMAND [--dry-run]'
class Outcome(object):
    def __init__(self, state, intent_id):
        self.state, self.intent_id = state, intent_id
class OpenIntents(Exception):
    def __init__(self, intents):
        self.intents = intents
        Exception.__init__(self, "%d open intent(s)" % len(intents))
class JournalDamaged(Exception):
    def __init__(self, line_number, path):
        self.line_number, self.path = line_number, path
        Exception.__init__(self, "line %s of %s is not a record" % (line_number, path))
class Busy(Exception):
    pass
class _WriteFailed(Exception):
    def __init__(self, stage, err, intent_id=None):
        self.stage, self.err, self.intent_id = stage, err, intent_id
        Exception.__init__(self, str(err))
class _CommandFailed(Exception):
    def __init__(self, code):
        self.code = code
        self.may_have_acted = code < 0 or code > 128
        Exception.__init__(self, "exit %s" % code)
class _CommandNotStarted(Exception):
    def __init__(self, err):
        self.err = err
        self.clean_note = "the command could not be started"
        Exception.__init__(self, str(err))
class _OpenFailed(OSError):
    pass
def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
def _dumps(record):
    return json.dumps(record, separators=(",", ":")).encode("utf-8") + b"\n"
def _parse_line(raw):
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        obj = json.loads(text)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    event = obj.get("event")
    need = _FIELDS.get(event)
    if need is None or any(name not in obj for name in need):
        return None
    if not isinstance(obj.get("at"), str):
        return None
    if event != "torn" and not isinstance(obj.get("id"), str):
        return None
    if event == "intent" and not isinstance(obj.get("what"), str):
        return None
    if event == "resolved" and (not isinstance(obj.get("happened"), bool) or not isinstance(obj.get("note"), str)):
        return None
    return obj
def _full_sync(fd):
    if hasattr(fcntl, "F_FULLFSYNC"):
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
    else:
        os.fsync(fd)
def _write_all(fd, data):
    view = data
    while view:
        wrote = os.write(fd, view)
        if wrote <= 0:
            raise OSError("short write on the journal")
        view = view[wrote:]
def _classify(records):
    order, slots = [], {}
    for rec in records:
        if rec["event"] == "torn":
            continue
        iid = rec["id"]
        if iid not in slots:
            slots[iid] = {"intent": None, "done_at": None, "resolved": None}
            order.append(iid)
        slot = slots[iid]
        if rec["event"] == "intent":
            slot["intent"] = rec
        elif rec["event"] == "done":
            slot["done_at"] = rec["at"]
        elif rec["event"] == "resolved":
            slot["resolved"] = rec
    items = []
    for iid in order:
        slot, resolved = slots[iid], slots[iid]["resolved"]
        state, closed_at = "open", None
        if resolved is not None and resolved["happened"] is False:
            state, closed_at = "cleared", resolved["at"]
        elif slot["done_at"] is not None or (resolved is not None and resolved["happened"] is True):
            state = "done"
            closed_at = slot["done_at"] if slot["done_at"] is not None else resolved["at"]
        items.append({"id": iid, "state": state, "intent": slot["intent"], "closed_at": closed_at})
    return items

class Journal(object):
    def __init__(self, path):
        self.path, self._last_id, self._last_cleared = path, None, False
    @contextmanager
    def _locked(self):
        try:
            fd = os.open(self.path + ".lock", os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as err:
            raise _OpenFailed(err.errno, err.strerror, err.filename)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as err:
                if err.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    raise Busy(self.path + ".lock")
                raise
            yield
        finally:
            os.close(fd)
    def _reject(self, line_no):
        raise JournalDamaged(line_no, self.path)
    def _cut_off(self, records):
        return records, True
    def _load(self):
        if not os.path.exists(self.path):
            return [], False
        try:
            with open(self.path, "rb") as handle:
                data = handle.read()
        except OSError as err:
            raise _OpenFailed(err.errno, err.strerror, err.filename)
        if not data:
            return [], False
        ends_nl = data.endswith(b"\n")
        parts = data.split(b"\n")[:-1] if ends_nl else data.split(b"\n")
        records, idx = [], 0
        while idx < len(parts):
            rec = _parse_line(parts[idx])
            if rec is None:
                if idx == len(parts) - 1 and not ends_nl:
                    return self._cut_off(records)
                nxt = _parse_line(parts[idx + 1]) if idx + 1 < len(parts) else None
                if nxt is not None and nxt.get("event") == "torn":
                    idx += 2
                    continue
                self._reject(idx + 1)
                continue
            records.append(rec)
            idx += 1
        return records, False
    def _repair_prefix(self):
        with open(self.path, "rb") as handle:
            data = handle.read()
        if not data or data.endswith(b"\n"):
            return b""
        prefix = b"\n"
        if _parse_line(data.rsplit(b"\n", 1)[-1]) is None:
            prefix += _dumps({"event": "torn", "at": _now()})
        return prefix
    def _append(self, record):
        created = not os.path.exists(self.path)
        data = (b"" if created else self._repair_prefix()) + _dumps(record)
        fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o644)
        try:
            _write_all(fd, data)
            _full_sync(fd)
        finally:
            os.close(fd)
        if not created:
            return
        dir_fd = os.open(os.path.dirname(os.path.abspath(self.path)), os.O_RDONLY)
        try:
            _full_sync(dir_fd)
        finally:
            os.close(dir_fd)
    def _new_id(self, records):
        used = set(rec["id"] for rec in records if isinstance(rec.get("id"), str))
        for _ignored in range(100):
            iid = uuid.uuid4().hex[:12]
            if iid not in used:
                return iid
        raise RuntimeError("could not allocate an intent id")
    def _commit(self, record, stage, iid=None):
        try:
            self._append(record)
        except Exception as err:
            raise _WriteFailed(stage, err, iid)
    def _record_failure(self, iid, exc, failure_is_clean):
        detail = "%s: %s" % (type(exc).__name__, str(exc)[:200])
        self._commit({"event": "failed", "id": iid, "at": _now(), "detail": detail}, "failed", iid)
        note = getattr(exc, "clean_note", None)
        if note is None and failure_is_clean and not getattr(exc, "may_have_acted", False):
            note = "step failed"
        if note is not None:
            self._commit({"event": "resolved", "id": iid, "at": _now(), "happened": False, "note": note}, "failed", iid)
            self._last_cleared = True
    def _run_step(self, step, iid, failure_is_clean):
        try:
            step()
        except Exception as exc:
            self._record_failure(iid, exc, failure_is_clean)
            raise
    def _run_once(self, step, what, key, block, failure_is_clean):
        try:
            records, _torn = self._load()
        except OSError as err:
            raise _WriteFailed("intent", err)
        items = _classify(records)
        opens = [it["intent"] for it in items if it["state"] == "open" and it["intent"] is not None]
        if block == "all":
            blocking = list(opens)
        elif block == "key":
            blocking = [rec for rec in opens if key is not None and rec.get("key") == key]
        else:
            raise ValueError("block must be all or key")
        if blocking:
            raise OpenIntents(blocking)
        newest = None
        if key is not None:
            for item in items:
                intent = item["intent"]
                if intent is not None and intent.get("key") == key and item["state"] != "open":
                    newest = item
        if newest is not None and newest["state"] == "done":
            return Outcome("nothing_to_do", newest["id"])
        iid = self._new_id(records)
        record = {"event": "intent", "id": iid, "at": _now(), "what": what}
        if key is not None:
            record["key"] = key
        self._last_id = iid
        self._commit(record, "intent")
        self._run_step(step, iid, failure_is_clean)
        self._commit({"event": "done", "id": iid, "at": _now()}, "done", iid)
        return Outcome("did", iid)
    def run_once(self, step, what, key=None, block="all", failure_is_clean=False):
        with self._locked():
            return self._run_once(step, what, key, block, failure_is_clean)
    def open_intents(self, key=None):
        rows = _classify(self._load()[0])
        return [it["intent"] for it in rows if it["state"] == "open" and it["intent"] is not None and (key is None or it["intent"].get("key") == key)]
    def status(self):
        records, torn = self._load()
        counts = {"open": 0, "done": 0, "cleared": 0, "torn_tail": torn}
        for item in _classify(records):
            if item["intent"] is not None:
                counts[item["state"]] += 1
        return counts
    def resolve(self, intent_id, happened, note):
        if not isinstance(note, str) or note.strip() == "":
            raise ValueError("note is empty")
        if not isinstance(happened, bool):
            raise ValueError("happened must be true or false")
        with self._locked():
            item = None
            for candidate in _classify(self._load()[0]):
                if candidate["id"] == intent_id:
                    item = candidate
            if item is None or item["intent"] is None:
                raise ValueError("no such intent: %s" % intent_id)
            if item["state"] != "open":
                raise ValueError("intent %s is not open" % intent_id)
            self._append({"event": "resolved", "id": intent_id, "at": _now(), "happened": happened, "note": note})
    def _settle(self, counts, intent, bucket, happened, dry_run):
        counts[bucket] += 1
        if happened is None or dry_run:
            return
        self._append({"event": "resolved", "id": intent["id"], "at": _now(), "happened": happened, "note": "proved"})
    def recover(self, prove, dry_run=False):
        with self._locked():
            counts = {"happened": 0, "did_not_happen": 0, "unknown": 0}
            for item in _classify(self._load()[0]):
                if item["state"] != "open" or item["intent"] is None:
                    continue
                intent = item["intent"]
                try:
                    answer = prove(intent)
                except Exception:
                    answer = None
                if answer is True:
                    self._settle(counts, intent, "happened", True, dry_run)
                elif answer is False:
                    self._settle(counts, intent, "did_not_happen", False, dry_run)
                else:
                    self._settle(counts, intent, "unknown", None, dry_run)
            return counts

def _out(line):
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
def _usage():
    sys.stderr.write(_USAGE + "\n")
    return EXIT_USAGE
def _closed_at(journal, intent_id):
    rows = _classify(journal._load()[0])
    return next((item["closed_at"] for item in rows if item["id"] == intent_id), "")
def _intent_line(rec):
    return "  %s  %s  %s  %s" % (rec["id"], rec["at"], rec.get("key") or "-", rec["what"])
def _print_stop(path, intents):
    _out("STOP: %d open intent(s). Check each by hand before anything new:" % len(intents))
    for rec in intents:
        _out(_intent_line(rec))
    _out('  then: python3 intent_journal.py resolve %s ID --happened | --did-not-happen --note "who checked, and where"' % path)
def _could_not_damage(err, path):
    _out("COULD NOT: line %s of %s is not a record; nothing was run" % (err.line_number, path))
    return EXIT_COULD_NOT
def _busy(path):
    _out("BUSY: another run holds %s.lock" % path)
    return EXIT_BUSY
def _scan(argv, spec):
    values, positionals, index = {}, [], 0
    while index < len(argv):
        arg, kind = argv[index], spec.get(argv[index])
        if kind == "bool":
            values[arg] = True
            index += 1
        elif kind == "value":
            if index + 1 >= len(argv):
                return None
            values[arg] = argv[index + 1]
            index += 2
        elif arg.startswith("-"):
            return None
        else:
            positionals.append(arg)
            index += 1
    return positionals, values

def _child_env(intent_id, key, what, at=None):
    env = os.environ.copy()
    env["INTENT_ID"] = intent_id
    env["INTENT_KEY"] = "" if key is None else key
    env["INTENT_WHAT"] = what
    if at is not None:
        env["INTENT_AT"] = at
    return env
def _cmd_run(argv):
    if "--" not in argv:
        return _usage()
    split = argv.index("--")
    command = argv[split + 1:]
    scanned = _scan(argv[:split], {"--what": "value", "--key": "value", "--block": "value", "--failure-is-clean": "bool"})
    if scanned is None or not command:
        return _usage()
    positionals, opt = scanned
    what, key = opt.get("--what"), opt.get("--key")
    block, failure_is_clean = opt.get("--block", "all"), opt.get("--failure-is-clean", False)
    if len(positionals) != 1 or what is None or block not in ("all", "key"):
        return _usage()
    path, journal = positionals[0], Journal(positionals[0])
    def step():
        try:
            proc = subprocess.Popen(command, env=_child_env(journal._last_id, key, what))
        except OSError as err:
            raise _CommandNotStarted(err)
        code = proc.wait()
        if code != 0:
            raise _CommandFailed(code)
    try:
        outcome = journal.run_once(step, what, key=key, block=block, failure_is_clean=failure_is_clean)
    except _OpenFailed as err:
        _out("COULD NOT: the journal could not be opened (%s); nothing was run or written" % err)
        return EXIT_COULD_NOT
    except Busy:
        return _busy(path)
    except JournalDamaged as err:
        return _could_not_damage(err, path)
    except OpenIntents as err:
        _print_stop(path, err.intents)
        return EXIT_STOP
    except _CommandNotStarted as err:
        _out("COULD NOT: %s could not be started (%s); recorded as did not happen (%s)" % (command[0], err, journal._last_id))
        return EXIT_COULD_NOT
    except _CommandFailed as err:
        how = "recorded as did not happen" if journal._last_cleared else "the intent stays open"
        if err.code < 0:
            _out("FAILED: %s was killed by signal %d; %s (%s)" % (command[0], -err.code, how, journal._last_id))
        else:
            _out("FAILED: %s exited %s; %s (%s)" % (command[0], err.code, how, journal._last_id))
        return EXIT_FAILED
    except _WriteFailed as err:
        if err.stage == "intent":
            _out("COULD NOT: the intent could not be written (%s); the step was not run" % err.err)
        elif err.stage == "done":
            _out("COULD NOT: the step ran but the done mark could not be written (%s); the intent stays open (%s)" % (err.err, err.intent_id))
        else:
            _out("COULD NOT: the failure could not be written (%s); the intent stays open (%s)" % (err.err, err.intent_id))
        return EXIT_COULD_NOT
    if outcome.state == "nothing_to_do":
        _out("NOTHING TO DO: %s was done at %s (intent %s)" % (key, _closed_at(journal, outcome.intent_id), outcome.intent_id))
        return EXIT_OK
    _out("DID: %s (intent %s)" % (what, outcome.intent_id))
    return EXIT_DID
def _cmd_status(argv):
    scanned = _scan(argv, {"--json": "bool"})
    if scanned is None or len(scanned[0]) != 1:
        return _usage()
    path, opt = scanned[0][0], scanned[1]
    if not os.path.exists(path):
        counts, opens = {"open": 0, "done": 0, "cleared": 0, "torn_tail": False}, []
    else:
        try:
            journal = Journal(path)
            counts, opens = journal.status(), journal.open_intents()
        except _OpenFailed as err:
            _out("COULD NOT: the journal could not be opened (%s); nothing was run or written" % err)
            return EXIT_COULD_NOT
        except JournalDamaged as err:
            return _could_not_damage(err, path)
    if opt.get("--json"):
        _out(json.dumps({"open": opens, "done": counts["done"], "cleared": counts["cleared"], "torn_tail": counts["torn_tail"]}))
    else:
        for rec in opens:
            _out(_intent_line(rec))
        if counts["torn_tail"]:
            _out("note: the last line was cut off mid-write and is ignored")
        _out(_STATUS_COUNTS % (counts["open"], counts["done"], counts["cleared"]))
    return EXIT_STATUS_OPEN if counts["open"] else EXIT_OK
def _cmd_resolve(argv):
    scanned = _scan(argv, {"--happened": "bool", "--did-not-happen": "bool", "--note": "value"})
    if scanned is None or len(scanned[0]) != 2 or "--note" not in scanned[1]:
        return _usage()
    positionals, opt = scanned
    if bool(opt.get("--happened")) == bool(opt.get("--did-not-happen")):
        return _usage()
    path, iid = positionals
    happened = bool(opt.get("--happened"))
    try:
        Journal(path).resolve(iid, happened, opt["--note"])
    except _OpenFailed as err:
        _out("COULD NOT: the journal could not be opened (%s); nothing was run or written" % err)
        return EXIT_COULD_NOT
    except OSError as err:
        _out("COULD NOT: the resolution could not be written (%s); the intent stays open (%s)" % (err, iid))
        return EXIT_COULD_NOT
    except Busy:
        return _busy(path)
    except JournalDamaged as err:
        return _could_not_damage(err, path)
    except ValueError as err:
        _out(str(err))
        return EXIT_COULD_NOT
    _out("RESOLVED: %s %s" % (iid, "happened" if happened else "did not happen"))
    return EXIT_OK
def _cmd_recover(argv):
    scanned = _scan(argv, {"--prove": "value", "--dry-run": "bool"})
    if scanned is None or len(scanned[0]) != 1 or "--prove" not in scanned[1]:
        return _usage()
    path, prove_cmd, dry_run = scanned[0][0], scanned[1]["--prove"], scanned[1].get("--dry-run", False)
    lines = []
    def prove(intent):
        try:
            code = subprocess.run(["sh", "-c", prove_cmd], env=_child_env(intent["id"], intent.get("key"), intent["what"], intent["at"])).returncode
        except OSError:
            lines.append((intent["id"], "unknown", 127))
            return None
        if code == 0:
            lines.append((intent["id"], "happened", code))
            return True
        if code == 1:
            lines.append((intent["id"], "did not happen", code))
            return False
        lines.append((intent["id"], "unknown", code))
        return None
    try:
        counts = Journal(path).recover(prove, dry_run=dry_run)
    except _OpenFailed as err:
        _out("COULD NOT: the journal could not be opened (%s); nothing was run or written" % err)
        return EXIT_COULD_NOT
    except OSError as err:
        prefix = "would: " if dry_run else ""
        for iid, label, code in lines[:-1]:
            if label == "unknown":
                _out("%s%s unknown (exit %s)" % (prefix, iid, code))
            else:
                _out("%s%s %s" % (prefix, iid, label))
        _out("COULD NOT: the resolution could not be written (%s); the intent stays open (%s)" % (err, lines[-1][0]))
        return EXIT_COULD_NOT
    except Busy:
        return _busy(path)
    except JournalDamaged as err:
        return _could_not_damage(err, path)
    prefix = "would: " if dry_run else ""
    for iid, label, code in lines:
        if label == "unknown":
            _out("%s%s unknown (exit %s)" % (prefix, iid, code))
        else:
            _out("%s%s %s" % (prefix, iid, label))
    happened, missed, unknown = counts["happened"], counts["did_not_happen"], counts["unknown"]
    _out(_RECOVER_COUNTS % (happened + missed, happened + missed + unknown, happened, missed, unknown))
    return EXIT_RECOVER_OPEN if unknown else EXIT_OK

def main(argv):
    if not argv:
        return _usage()
    table = {"run": _cmd_run, "status": _cmd_status, "resolve": _cmd_resolve, "recover": _cmd_recover}
    fn = table.get(argv[0])
    return _usage() if fn is None else fn(argv[1:])
if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
