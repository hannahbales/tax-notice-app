"""
Match each extracted notice to a client in the Access database.

Evidence from the notice: taxpayer / spouse names and the last 4
digits of the SSN/EIN. A notice is "matched" only when BOTH a name and
the ID agree with exactly one client. Everything else goes to review
with a reason:

    matched         name + ID agree with one client
    suggested       only a name, or only an ID, was on the notice, and
                    it fits exactly one client (needs a person to confirm)
    conflict        the ID points to one client but the name doesn't
                    agree (or the name fits but the ID is different)
    ambiguous       more than one client fits
    not_found       no client fits
    no_client_info  the notice had no name or ID to match on

The result is written to manifest.json as notice["match"], including
the client's Notices folder for filing.
"""

import json
import logging
import os
import re
import sys
from functools import lru_cache
from pathlib import Path, PureWindowsPath

from dotenv import load_dotenv

import client_db
from extractor import BUSINESS_RE, problems
from pdf_splitter import MANIFEST_NAME

# notices are filed in this subfolder of the client's main folder
NOTICES_SUBFOLDER = "Notices"

# new clients' folders wait in these (inside each CLIENT_FOLDERS_ROOT)
# before being moved to their letter folder
NEW_CLIENT_FOLDERS = ("0 INTAKE New Customer Folders", "0 Intake Tax Preparation Folders")

logger = logging.getLogger(__name__)


# --- names ------------------------------------------------------------

TITLES = {"MR", "MRS", "MS", "MISS", "DR"}
SUFFIXES = {"JR", "SR", "II", "III", "IV", "MD", "CPA", "ESQ", "DDS", "PHD"}
JOINERS_RE = re.compile(r"\s*(?:&|\+|/|\bAND\b)\s*")

BUSINESS_NOISE = {
    "LLC", "INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY",
    "LTD", "LP", "LLP", "PLLC", "PC", "PA", "THE", "OF", "AND",
}


def _words(text):
    """Upper-case words; hyphenated surnames stay together, initials are dropped."""
    words = re.sub(r"[^A-Z\- ]", " ", text.upper()).split()
    return [w.strip("-") for w in words if w.strip("-") and w not in TITLES | SUFFIXES]


@lru_cache(maxsize=None)
def parse_people(text):
    """
    People named in a name field, as ((first, last), ...).

    Handles "JOHN A SMITH", "Smith, John A.", "John & Jane Smith",
    "Smith, John & Jane", "JOHN SMITH AND JANE DOE". A name with no
    comma might be "LAST FIRST", so it is also tried reversed.
    """
    if not text:
        return ()

    text = text.upper()
    people = []

    if "," in text:
        last, rest = text.split(",", 1)
        last_words = _words(last)
        if last_words:
            for part in JOINERS_RE.split(rest):
                firsts = [w for w in _words(part) if len(w) > 1]
                if firsts:
                    people.append((firsts[0], last_words[-1]))
        return tuple(people)

    parts = [[w for w in _words(p) if len(w) > 1] for p in JOINERS_RE.split(text)]
    parts = [p for p in parts if p]
    shared_last = parts[-1][-1] if parts and len(parts[-1]) >= 2 else None

    for words in parts:
        if len(words) >= 2:
            people.append((words[0], words[-1]))
            people.append((words[-1], words[0]))  # maybe "LAST FIRST"
        elif shared_last:
            people.append((words[0], shared_last))  # "JOHN & JANE SMITH"

    return tuple(people)


def _edit_distance_at_most_one(a, b):
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) > len(b):
        a, b = b, a
    i = j = 0
    edits = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        if len(a) == len(b):
            i += 1
        j += 1
    return edits + (len(b) - j) <= 1


def _same_word(a, b, min_typo_length=5):
    """Equal, or one OCR slip apart for longer words."""
    if a == b:
        return True
    return min(len(a), len(b)) >= min_typo_length and _edit_distance_at_most_one(a, b)


def _same_first(a, b):
    # Rob / Robert, Kathy / Kathryn are different on purpose: only prefixes
    if a.startswith(b) or b.startswith(a):
        return min(len(a), len(b)) >= 3
    return _same_word(a, b)


def _same_last(a, b):
    parts_a, parts_b = set(a.split("-")), set(b.split("-"))  # SMITH-JONES ~ JONES
    return any(_same_word(x, y) for x in parts_a for y in parts_b if len(x) > 1)


def people_match(notice_people, client_people):
    return any(
        _same_last(n_last, c_last) and _same_first(n_first, c_first)
        for n_first, n_last in notice_people
        for c_first, c_last in client_people
    )


@lru_cache(maxsize=None)
def business_words(text):
    words = re.sub(r"[^A-Z0-9 ]", " ", (text or "").upper()).split()
    return frozenset(w for w in words if len(w) > 1 and w not in BUSINESS_NOISE)


def business_match(notice_name, client_name):
    a, b = business_words(notice_name), business_words(client_name)
    if not a or not b:
        return False
    smaller, larger = sorted((a, b), key=len)
    return a == b or (smaller <= larger and len(smaller) >= 2)


# --- matching ---------------------------------------------------------

def _notice_ids(extracted):
    """[(last4, kind)] where kind is SSN, EIN, or None (unknown)."""
    ids = []
    if extracted.get("tin_last4"):
        ids.append((extracted["tin_last4"], extracted.get("tin_type")))
    if extracted.get("spouse_tin_last4"):
        ids.append((extracted["spouse_tin_last4"], "SSN"))
    return ids


def _id_hit(ids, client):
    for digits, kind in ids:
        if kind == "SSN":
            pool = client.ssn_last4 | client.tin_last4
        elif kind == "EIN":
            pool = client.ein_last4 | client.tin_last4
        else:
            pool = client.ssn_last4 | client.ein_last4 | client.tin_last4
        if digits in pool:
            return True
    return False


def _name_hit(extracted, client):
    names = [n for n in (extracted.get("taxpayer_name"), extracted.get("spouse_name")) if n]
    if not names:
        return False

    business_names = list(client.business_names)
    if BUSINESS_RE.search(client.client_name):
        business_names.append(client.client_name)
    if any(business_match(names[0], name) for name in business_names):
        return True

    notice_people = [p for name in names for p in parse_people(name)]
    client_people = parse_people(client.client_name) + parse_people(client.spouse_name)
    return people_match(notice_people, client_people)


def _summary(client):
    return {"client_id": client.client_id, "client_name": client.client_name}


def _database_folder(client):
    """(folder, warnings) when only the database is consulted."""
    if client.folder:
        return client.folder, []
    return None, ["client has no folder path in the database"]


def _result(status, reason, client=None, candidates=(), folder_for=_database_folder):
    result = {
        "status": status,
        "reason": reason,
        "client_id": None,
        "client_name": None,
        "folder": None,
        "notices_folder": None,
        "warnings": [],
        "candidates": [_summary(c) for c in candidates],
    }
    if client is not None:
        result.update(_summary(client))
        folder, warnings = folder_for(client)
        result["folder"] = folder
        result["warnings"] += warnings
        if folder:
            result["notices_folder"] = str(PureWindowsPath(folder) / NOTICES_SUBFOLDER)
    return result


def _names(clients):
    return ", ".join(c.client_name for c in clients[:5])


def match_notice(extracted, clients, folder_for=_database_folder):
    """
    Decide which client (if any) a notice belongs to. folder_for(client)
    returns (folder, warnings) for the chosen client.
    """
    ids = _notice_ids(extracted)
    has_name = bool(extracted.get("taxpayer_name"))
    id_label = " / ".join(f"ending {digits}" for digits, _kind in ids)

    if not ids and not has_name:
        return _result("no_client_info", "no taxpayer name or ID number on the notice")

    by_id = [c for c in clients if ids and _id_hit(ids, c)]
    by_name = [c for c in clients if has_name and _name_hit(extracted, c)]

    if ids and has_name:
        both = [c for c in by_id if c in by_name]
        if len(both) == 1:
            return _result("matched", "name and ID match", both[0], folder_for=folder_for)
        if len(both) > 1:
            return _result("ambiguous", f"name and ID match {len(both)} clients: {_names(both)}", candidates=both)
        if by_id:
            return _result(
                "conflict",
                f"ID {id_label} belongs to {_names(by_id)}, but the name on the notice doesn't match",
                candidates=by_id + by_name,
            )
        if by_name:
            return _result(
                "conflict",
                f"name matches {_names(by_name)}, but the ID {id_label} doesn't",
                candidates=by_name,
            )
        return _result("not_found", "no client with this name or ID")

    found, evidence = (by_id, f"ID {id_label}") if ids else (by_name, "name")
    if len(found) == 1:
        return _result(
            "suggested",
            f"only the {evidence} was on the notice - please confirm",
            found[0],
            found,
            folder_for=folder_for,
        )
    if len(found) > 1:
        return _result("ambiguous", f"{evidence} matches {len(found)} clients: {_names(found)}", candidates=found)
    return _result("not_found", f"no client with this {evidence}")


# --- client folders ---------------------------------------------------

def folders_root():
    r"""
    CLIENT_FOLDERS_ROOT from .env: one or more folders separated by ";",
    searched in order, e.g.
    \server\TTI Clients\Resolution;\server\TTI Clients\Tax Prep Only
    """
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    return os.environ.get("CLIENT_FOLDERS_ROOT") or None


def expected_folder_name(client):
    """The firm's usual pattern: "Last, First" or "Last, First and SpouseFirst"."""
    name = f"{client.last_name}, {client.first_name}"
    if client.spouse_first_name:
        name += f" and {client.spouse_first_name}"
    return name


COPY_RE = re.compile(r"\bcopy\b", re.IGNORECASE)


def _comma_form(name):
    """"Williams_Tony" reads as "Williams, Tony"."""
    if "," not in name and "_" in name:
        last, rest = name.split("_", 1)
        name = f"{last}, {rest.replace('_', ' ')}"
    return name


def folder_people(name):
    """People named by a folder."""
    return parse_people(_comma_form(name))


def folder_marks(name):
    """
    (middle initials, generation) of the first person a folder names:
    "Smith, John F and Jane" -> ({"F"}, None); "Smith Jr_John" -> (set(), "JR").
    """
    first_person = JOINERS_RE.split(_comma_form(name).upper(), maxsplit=1)[0]
    if "," in first_person:
        given = client_db._upper_words(first_person.split(",", 1)[1])
    else:  # "John F Smith": between the first and last word
        given = client_db._upper_words(first_person)[1:-1]
    return {w for w in given if len(w) == 1}, client_db.generation(first_person)


def folder_fit(client, name, primary):
    """
    How well a folder that names the client fits them: None when its
    middle initial or Jr./Sr. contradicts the client's (a different
    person), otherwise the number of near-misses, lower is better: a
    middle initial or suffix on only one side, or a first name that
    only roughly matches (Pat / Patricia).
    """
    initials, folder_generation = folder_marks(name)
    misses = 0

    if client.middle_initial and initials:
        if client.middle_initial not in initials:
            return None
    elif client.middle_initial or initials:
        misses += 1

    if client.suffix and folder_generation:
        if client.suffix != folder_generation:
            return None
    elif client.suffix or folder_generation:
        misses += 1

    firsts = {first for first, _last in primary}
    if not any(first in firsts for first, _last in folder_people(name)):
        misses += 1
    return misses


class FolderFinder:
    r"""
    Find a client's main folder. For each root in turn (in .env order):
    the usual <root>\<letter>\<Last, First[ and SpouseFirst]> folder;
    otherwise the letter folder, then the new-client intake folders, are
    searched for one naming the client ("Last_First" style included).

    Folders whose middle initial or Jr./Sr. differs from the client's are
    skipped. Folders named "... copy" are ignored when another candidate
    exists. If several remain, the one with the spouse's name, then the
    closest fit (same middle initial / suffix, exact first name), wins.
    Listings are cached, so one finder per batch reads each folder once.
    """

    def __init__(self, roots):
        if isinstance(roots, (str, Path)):
            roots = str(roots).split(";")
        self.roots = [Path(str(r).strip()) for r in roots or () if str(r).strip()]
        self._listings = {}

    def _list(self, folder):
        if folder not in self._listings:
            try:
                self._listings[folder] = sorted(p.name for p in folder.iterdir() if p.is_dir())
            except OSError:
                self._listings[folder] = None
        return self._listings[folder]

    def _search(self, folder, client, primary):
        """(found folder names, skipped as a different person, reachable) in one folder."""
        names = self._list(folder)
        if names is None:
            return [], [], False

        found = [name for name in names if people_match(folder_people(name), primary)]
        fit = {name: folder_fit(client, name, primary) for name in found}
        skipped = [name for name in found if fit[name] is None]
        found = [name for name in found if fit[name] is not None]
        found = [name for name in found if not COPY_RE.search(name)] or found

        if len(found) > 1 and client.spouse_name:
            spouse = parse_people(client.spouse_name)
            found = [name for name in found if people_match(folder_people(name), spouse)] or found
        if len(found) > 1:
            best = min(fit[name] for name in found)
            found = [name for name in found if fit[name] == best]
        if len(found) > 1 and expected_folder_name(client) in found:
            found = [expected_folder_name(client)]
        return found, skipped, True

    def __call__(self, client):
        if client.folder:
            return client.folder, []
        if not self.roots:
            return None, ["CLIENT_FOLDERS_ROOT is not set in .env"]
        if not client.last_name or not client.first_name:
            return None, ["client's first or last name is missing in the database"]

        letter = client.last_name.strip()[0].upper()
        primary = parse_people(f"{client.last_name}, {client.first_name}")
        if client.nickname:
            primary += parse_people(f"{client.last_name}, {client.nickname}")

        reachable = False
        skipped = []
        for root in self.roots:
            # shortcut: the usual folder name, when nothing could fit better
            expected = expected_folder_name(client)
            if folder_fit(client, expected, primary) == 0:
                # the folder's real name: Windows ignores case and a trailing "."
                wanted = expected.rstrip(". ").casefold()
                real = [n for n in self._list(root / letter) or () if n.rstrip(". ").casefold() == wanted]
                if real:
                    return str(root / letter / real[0]), []

            for folder in (root / letter, *(root / name for name in NEW_CLIENT_FOLDERS)):
                found, different, ok = self._search(folder, client, primary)
                reachable = reachable or ok
                skipped += different
                if len(found) == 1:
                    return str(folder / found[0]), []
                if found:
                    return None, [f"several possible client folders in {folder}: {'; '.join(found[:5])}"]

        if not reachable:
            return None, [f"client folder not found: cannot open {' or '.join(str(r / letter) for r in self.roots)}"]
        message = (
            f'client folder not found (expected "{expected_folder_name(client)}" in a '
            f'"{letter}" folder under {" or ".join(str(r) for r in self.roots)})'
        )
        if skipped:
            message += f"; skipped (different middle initial or Jr./Sr.): {'; '.join(skipped[:5])}"
        return None, [message]


def check_folder(match):
    """Warn when a matched client's main folder can't be reached."""
    folder = match.get("folder")
    if folder and not Path(folder).is_dir():
        match["warnings"].append(f"client folder not found: {folder}")


# --- review -----------------------------------------------------------

def review_reasons(notice):
    """Every reason this notice needs a person to look at it."""
    reasons = list(notice.get("warnings", []))
    extracted = notice.get("extracted")
    if extracted:
        reasons += problems(extracted)
    match = notice.get("match")
    if match:
        if match["status"] != "matched":
            reasons.append(f"client {match['status'].replace('_', ' ')}: {match['reason']}")
        reasons += match["warnings"]
    return reasons


# --- batch --------------------------------------------------------------

def match_batch(batch_dir, clients=None):
    """
    Match every extracted notice in a split batch. Safe to re-run.

    Raises client_db.ClientDbNotConfigured / ClientDbUnavailable when
    clients is None and the database can't be read.
    """
    if clients is None:
        clients = client_db.load_clients()
    folder_for = FolderFinder(folders_root())

    batch_dir = Path(batch_dir)
    manifest_path = batch_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    counts = {}
    for notice in manifest["notices"]:
        extracted = notice.get("extracted")
        if not extracted:
            continue

        match = match_notice(extracted, clients, folder_for)
        check_folder(match)

        notice["match"] = match
        notice["review_reasons"] = review_reasons(notice)
        notice["needs_review"] = bool(notice["review_reasons"])
        counts[match["status"]] = counts.get(match["status"], 0) + 1

    partial = manifest_path.with_name(manifest_path.name + ".partial")
    partial.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    partial.replace(manifest_path)

    logger.info(
        "Matched %s: %s",
        batch_dir.name,
        ", ".join(f"{n} {status}" for status, n in sorted(counts.items())) or "nothing to match",
    )
    return manifest


def _print_notice(notice):
    match = notice.get("match")
    if not match:
        print(f"== {notice['file']}: not extracted yet")
        return
    print(f"== {notice['file']}: {match['status'].upper()}")
    if match["client_name"]:
        print(f"   client           {match['client_name']} (id {match['client_id']})")
        print(f"   notices folder   {match['notices_folder']}")
    for reason in notice["review_reasons"]:
        print(f"   REVIEW: {reason}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if len(sys.argv) != 2:
        sys.exit("usage: python src/client_matcher.py <split batch folder>")

    try:
        result = match_batch(sys.argv[1])
    except (client_db.ClientDbNotConfigured, client_db.ClientDbUnavailable) as error:
        sys.exit(f"ERROR: {error}")

    for item in result["notices"]:
        _print_notice(item)
