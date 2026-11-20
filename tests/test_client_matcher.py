import json
from pathlib import Path

import pytest

import client_db
import config
import intake_watcher
from client_db import ClientDbNotConfigured, ClientDbUnavailable, clean_folder, client_from_row
from client_matcher import match_batch, match_notice, parse_people
from extractor import extract_batch
from generate_test_batch import build_batch
from ocr import ocr_batch
from pdf_splitter import batch_dir, split_pdf
from processing_history import get_record, initialize_database

REAL_LOAD_CLIENTS = client_db.load_clients  # before conftest replaces it


def notice(name=None, spouse=None, last4=None, tin_type="SSN", spouse_last4=None, entity="individual"):
    return dict(
        taxpayer_name=name, spouse_name=spouse, tin_last4=last4, tin_type=tin_type if last4 else None,
        spouse_tin_last4=spouse_last4, entity_type=entity,
    )


def client(client_id, name, spouse=None, ssn=None, ein=None, folder=r"\\server\clients\x"):
    return client_from_row(dict(client_id=client_id, client_name=name, spouse_name=spouse,
                                ssn=ssn, ein=ein, folder=folder))


# --- name parsing ---------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        ("Smith, John A.", {("JOHN", "SMITH")}),
        ("Smith, John & Jane", {("JOHN", "SMITH"), ("JANE", "SMITH")}),
        ("John A. & Jane B. Smith", {("JOHN", "SMITH"), ("JANE", "SMITH")}),
        ("JOHN SMITH AND JANE DOE", {("JOHN", "SMITH"), ("JANE", "DOE")}),
        ("Mary Smith-Jones", {("MARY", "SMITH-JONES")}),
        ("Smith Jr., John", {("JOHN", "SMITH")}),
        ("SMITH JOHN", {("JOHN", "SMITH")}),  # LAST FIRST without a comma
    ],
)
def test_parse_people(text, expected):
    assert expected <= set(parse_people(text))


# --- single notice decisions ----------------------------------------------

def test_name_and_id_match_one_client(fake_clients):
    result = match_notice(notice("JOHN A SMITH", "JANE B SMITH", "1234"), fake_clients)

    assert result["status"] == "matched"
    assert result["client_id"] == "101"
    assert result["notices_folder"].endswith(r"Smith John and Jane\Notices")


def test_id_picks_the_right_client_when_names_are_shared(fake_clients):
    # two clients are named John Smith
    assert match_notice(notice("JOHN SMITH", last4="9999"), fake_clients)["client_id"] == "107"
    assert match_notice(notice("JOHN SMITH", last4="1234"), fake_clients)["client_id"] == "101"


def test_spouse_ssn_matches_the_joint_client(fake_clients):
    result = match_notice(notice("JANE SMITH", last4="4320"), fake_clients)

    assert (result["status"], result["client_id"]) == ("matched", "101")


def test_shared_name_without_id_is_ambiguous(fake_clients):
    result = match_notice(notice("JOHN SMITH"), fake_clients)

    assert result["status"] == "ambiguous"
    assert {c["client_id"] for c in result["candidates"]} == {"101", "107"}
    assert result["notices_folder"] is None


def test_name_only_unique_is_suggested_not_matched(fake_clients):
    result = match_notice(notice("MARIA GARCIA"), fake_clients)

    assert (result["status"], result["client_id"]) == ("suggested", "102")


def test_id_only_is_suggested(fake_clients):
    result = match_notice(notice(last4="5678"), fake_clients)

    assert (result["status"], result["client_id"]) == ("suggested", "102")


def test_id_belongs_to_someone_else(fake_clients):
    result = match_notice(notice("MARIA GARCIA", last4="2468"), fake_clients)

    assert result["status"] == "conflict"
    assert result["client_id"] is None
    assert "White, Dana" in result["reason"]


def test_name_matches_but_id_differs(fake_clients):
    result = match_notice(notice("MARIA GARCIA", last4="0000"), fake_clients)

    assert result["status"] == "conflict"
    assert "Garcia, Maria" in result["reason"]


def test_unknown_taxpayer(fake_clients):
    assert match_notice(notice("PAT NOBODY", last4="0000"), fake_clients)["status"] == "not_found"


def test_nothing_to_match_on(fake_clients):
    assert match_notice(notice(), fake_clients)["status"] == "no_client_info"


def test_ocr_slip_in_a_name_still_matches(fake_clients):
    result = match_notice(notice("MARIA GARClA", last4="5678"), fake_clients)

    assert result["status"] == "matched"


def test_short_names_need_an_exact_match():
    clients = [client(1, "Lee, Ann", ssn="111-11-1111")]

    assert match_notice(notice("ANN LEE", last4="1111"), clients)["status"] == "matched"
    assert match_notice(notice("ANN LEW", last4="1111"), clients)["status"] == "conflict"


def test_business_name_variants(fake_clients):
    result = match_notice(notice("ACME WIDGETS LLC", last4="6789", tin_type="EIN", entity="business"), fake_clients)

    assert (result["status"], result["client_id"]) == ("matched", "104")


def test_ein_does_not_match_an_ssn_with_the_same_last4():
    clients = [client(1, "Doe, John", ssn="111-22-6789"), client(2, "Doe Holdings LLC", ein="12-3456789")]
    result = match_notice(notice("DOE HOLDINGS LLC", last4="6789", tin_type="EIN", entity="business"), clients)

    assert (result["status"], result["client_id"]) == ("matched", "2")


def test_client_without_folder_is_flagged():
    clients = [client(1, "Lee, Ann", ssn="111-11-1111", folder=None)]
    result = match_notice(notice("ANN LEE", last4="1111"), clients)

    assert result["status"] == "matched"
    assert result["warnings"] == ["client has no folder path in the database"]


# --- database rows ------------------------------------------------------------

def test_only_last4_of_full_numbers_is_kept():
    row = client_from_row(dict(client_id=1, client_name="Lee, Ann", ssn="123-45-6789",
                               spouse_ssn="987654321", ein=None, folder=None))

    assert row.ssn_last4 == {"6789", "4321"}
    assert "12345" not in repr(row) and "98765" not in repr(row)


@pytest.mark.parametrize(
    "value, expected",
    [
        (r"\\server\clients\Lee, Ann", r"\\server\clients\Lee, Ann"),
        (r"Lee folder#\\server\clients\Lee, Ann#", r"\\server\clients\Lee, Ann"),  # Hyperlink field
        (r'  "\\server\clients\Lee"  ', r"\\server\clients\Lee"),
        ("", None),
        (None, None),
    ],
)
def test_folder_cleanup(value, expected):
    assert clean_folder(value) == expected


def test_missing_settings_mean_not_configured(monkeypatch):
    monkeypatch.setattr(client_db, "db_path", lambda: None)

    with pytest.raises(ClientDbNotConfigured):
        REAL_LOAD_CLIENTS()


# --- batches ----------------------------------------------------------------

@pytest.fixture
def variety_batch():
    path = config.INTAKE_DIR / "variety.pdf"
    with build_batch("batch_17_notice_variety") as doc:
        doc.save(path)
    folder = batch_dir(split_pdf(path))
    ocr_batch(folder)
    extract_batch(folder)
    return folder


def test_batch_matching(variety_batch):
    notices = match_batch(variety_batch)["notices"]

    assert [(n["match"]["status"], n["match"]["client_id"]) for n in notices] == [
        ("matched", "101"),
        ("matched", "102"),
        ("matched", "103"),
        ("matched", "104"),
        ("matched", "105"),
        ("matched", "106"),
        ("no_client_info", None),
    ]
    # CP575 still needs review (no tax year) even though the client matched
    assert [n["needs_review"] for n in notices] == [False] * 4 + [True, False, True]
    assert all(Path(n["match"]["notices_folder"]).parent.is_dir() for n in notices[:6])


def test_missing_client_folder_needs_review(variety_batch, fake_clients):
    Path(fake_clients[1].folder).rmdir()  # Maria Garcia's folder

    second = match_batch(variety_batch)["notices"][1]

    assert second["needs_review"]
    assert any("client folder not found" in r for r in second["review_reasons"])


def test_reextracting_clears_a_stale_match(variety_batch):
    match_batch(variety_batch)
    notices = extract_batch(variety_batch)["notices"]

    assert all("match" not in n for n in notices)


# --- watcher ----------------------------------------------------------------

@pytest.fixture
def watcher(monkeypatch):
    initialize_database()
    monkeypatch.setattr(intake_watcher, "STABLE_SECONDS", 0)
    monkeypatch.setattr(intake_watcher, "sleep", lambda _seconds: None)
    monkeypatch.setattr(intake_watcher, "_paused_until", 0.0)
    intake_watcher._known_files.clear()
    intake_watcher._settled.clear()

    path = config.INTAKE_DIR / "variety.pdf"
    with build_batch("batch_17_notice_variety") as doc:
        doc.save(path)
    return path


def _manifest():
    return json.loads(next(config.SPLIT_DIR.glob("*/manifest.json")).read_text())


def test_watcher_matches_after_extraction(watcher):
    intake_watcher.process_file(watcher)

    assert [n["match"]["status"] for n in _manifest()["notices"]].count("matched") == 6


def test_watcher_skips_matching_until_database_is_set_up(watcher, monkeypatch):
    def not_configured():
        raise ClientDbNotConfigured("ACCESS_DB_PATH is not set in .env")

    monkeypatch.setattr(client_db, "load_clients", not_configured)
    intake_watcher.process_file(watcher)

    assert not watcher.exists()  # completed
    assert all("match" not in n for n in _manifest()["notices"])


def test_watcher_pauses_when_database_is_unreachable(watcher, monkeypatch):
    from file_utils import file_hash

    def unreachable():
        raise ClientDbUnavailable("client database not found: \\\\server\\db.accdb")

    monkeypatch.setattr(client_db, "load_clients", unreachable)
    file_id = file_hash(watcher)
    intake_watcher.process_file(watcher)

    assert get_record(file_id) == ("waiting", 0)  # no attempt used up
    assert watcher.exists()
    assert intake_watcher._paused_until > 0


# --- 32-bit helper ------------------------------------------------------------

import subprocess  # noqa: E402

import access_reader  # noqa: E402


@pytest.fixture
def helper(monkeypatch, tmp_path):
    """Force the 32-bit helper route with a fake helper Python."""
    python = tmp_path / "python.exe"
    python.write_text("")
    monkeypatch.setattr(client_db, "_direct_driver_available", lambda: False)
    monkeypatch.setattr(client_db, "helper_python", lambda: python)
    monkeypatch.setattr(client_db, "db_path", lambda: r"\server\db.accdb")
    monkeypatch.setattr(client_db, "CLIENT_QUERY", "SELECT ...")

    def reply(stdout, stderr=""):
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=stdout, stderr=stderr),
        )
    return reply


def test_helper_rows_become_clients(helper):
    row = dict(client_id=7, client_name="Lee, Ann", spouse_name=None, ssn="1111",
               spouse_ssn=None, ein=None, tin=None, folder=r"x#\server\Lee#")
    helper(json.dumps({"rows": [row]}))

    (client,) = REAL_LOAD_CLIENTS()

    assert (client.client_id, client.ssn_last4, client.folder) == ("7", {"1111"}, r"\server\Lee")


def test_helper_errors_map_to_pause_or_skip(helper):
    helper(json.dumps({"error": "database not found", "kind": "unavailable"}))
    with pytest.raises(ClientDbUnavailable):
        REAL_LOAD_CLIENTS()

    helper(json.dumps({"error": "CLIENT_QUERY is missing columns: ein", "kind": "config"}))
    with pytest.raises(ClientDbNotConfigured):
        REAL_LOAD_CLIENTS()


def test_helper_crash_is_unavailable(helper):
    helper("", stderr="Traceback ... boom")

    with pytest.raises(ClientDbUnavailable, match="helper failed"):
        REAL_LOAD_CLIENTS()


def test_missing_helper_python_is_not_configured(helper, monkeypatch, tmp_path):
    monkeypatch.setattr(client_db, "helper_python", lambda: tmp_path / "nope.exe")

    with pytest.raises(ClientDbNotConfigured, match="32-bit helper"):
        REAL_LOAD_CLIENTS()


def test_reader_never_returns_full_ids(monkeypatch):
    class Cursor:
        description = [(name,) for name in ("CLIENT_ID", "CLIENT_NAME", "SSN", "EIN", "FOLDER", "EXTRA")]

        def execute(self, _query):
            pass

        def fetchall(self):
            return [(1, "Lee, Ann", "123-45-6789", "12-3456789", r"\s\Lee", "ignored")]

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(access_reader, "_connect", lambda _path, _password=None: Connection())
    (row,) = access_reader.read_rows("db", "q", ["client_id", "client_name", "ssn", "ein", "folder"])

    assert row == {"client_id": 1, "client_name": "Lee, Ann", "ssn": "6789", "ein": "6789", "folder": r"\s\Lee"}


# --- database password --------------------------------------------------------

@pytest.mark.parametrize(
    "password, expected",
    [
        (None, "DRIVER={X};DBQ=db;ReadOnly=1;"),
        ("Testpw1", "DRIVER={X};DBQ=db;ReadOnly=1;PWD=Testpw1;"),  # Access takes PWD literally
        ("a;b}c", "DRIVER={X};DBQ=db;ReadOnly=1;PWD={a;b}}c};"),  # a ; must be quoted
    ],
)
def test_connection_string_password(password, expected):
    assert access_reader.connection_string("X", "db", password) == expected


def test_password_goes_to_helper_on_stdin_not_command_line(helper, monkeypatch):
    monkeypatch.setattr(client_db, "db_password", lambda: "S3cret!")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"rows": []}), stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    REAL_LOAD_CLIENTS()

    assert calls  # clients and businesses
    for args, kwargs in calls:
        assert "S3cret!" not in " ".join(args)
        assert json.loads(kwargs["input"])["password"] == "S3cret!"


def test_garbled_access_descriptions_are_tolerated():
    # the Access driver returns descriptions with trailing junk bytes
    raw = "Client name".encode("utf-16-le") + b"\x00\x00\x00\xd8\x41"
    assert access_reader._lenient_utf16(raw) == "Client name"
    assert access_reader._lenient_utf16(None) is None


# --- real database layout (LeadsTable + tblBusinesses) ----------------------

from client_matcher import FolderFinder, expected_folder_name, folder_marks  # noqa: E402


def lead(client_id, first, last, spouse_first=None, spouse_last=None, ssn=None, spouse_ssn=None,
         ein=None, bus_name=None, businesses=()):
    return client_from_row(
        dict(client_id=client_id, first_name=first, last_name=last, spouse_first_name=spouse_first,
             spouse_last_name=spouse_last, ssn=ssn, spouse_ssn=spouse_ssn, ein=ein, bus_name=bus_name),
        businesses,
    )


def test_lead_row_becomes_client():
    client = lead(
        5, "Pat", "Rivera", "Sam", None, "123-45-6789", "987654321", ein=123456789, bus_name="Rivera Bakery",
        businesses=[dict(client_id=5, bus_name="Rivera Catering LLC", ein="98-7650001")],
    )

    assert client.client_name == "Rivera, Pat"
    assert client.spouse_name == "Rivera, Sam"  # spouse last name defaults to the client's
    assert client.ssn_last4 == {"6789", "4321"}
    assert client.ein_last4 == {"6789", "0001"}  # EIN stored as a number still works
    assert client.business_names == ("Rivera Bakery", "Rivera Catering LLC")
    assert expected_folder_name(client) == "Rivera, Pat and Sam"


def test_notice_for_a_clients_business_matches_the_client():
    clients = [lead(5, "Pat", "Rivera", ssn="111-22-3333",
                    businesses=[dict(client_id=5, bus_name="Rivera Catering LLC", ein="98-7650001")])]
    result = match_notice(notice("RIVERA CATERING LLC", last4="0001", tin_type="EIN", entity="business"),
                          clients)

    assert (result["status"], result["client_id"]) == ("matched", "5")


@pytest.fixture
def server(tmp_path):
    r"""A fake \\server\TTI Clients\Resolution tree."""
    root = tmp_path / "Resolution"
    for name in ["R/Rivera, Pat and Sam", "R/Rivera, Pat", "R/Reyes, Ana", "R/Ross, Lee - closed 2019"]:
        (root / name).mkdir(parents=True)
    return root


def test_folder_follows_the_firm_pattern(server):
    client = lead(5, "Pat", "Rivera", "Sam", ssn="1111")

    assert FolderFinder(server)(client) == (str(server / "R" / "Rivera, Pat and Sam"), [])


def test_folder_named_slightly_differently_is_found(server):
    client = lead(6, "Lee", "Ross", ssn="2222")

    assert FolderFinder(server)(client) == (str(server / "R" / "Ross, Lee - closed 2019"), [])


def test_folder_search_prefers_the_one_with_the_spouse(server):
    (server / "R" / "Rivera, Pat and Sam").rename(server / "R" / "Rivera, Pat & Sam")
    client = lead(5, "Pat", "Rivera", "Sam", ssn="1111")

    assert FolderFinder(server)(client)[0] == str(server / "R" / "Rivera, Pat & Sam")


def test_two_possible_folders_need_review(server):
    (server / "R" / "Rivera, Pat and Sam").rename(server / "R" / "Rivera, Patricia")
    client = lead(7, "Pat", "Rivera", ssn="3333")  # exact "Rivera, Pat" exists -> fine
    assert FolderFinder(server)(client)[0] == str(server / "R" / "Rivera, Pat")

    (server / "R" / "Rivera, Pat").rmdir()
    (server / "R" / "Rivera, Pat Jr").mkdir()
    folder, warnings = FolderFinder(server)(client)
    assert folder is None and "several possible client folders" in warnings[0]


def test_missing_folder_and_settings_are_reported(server, tmp_path):
    client = lead(8, "Kim", "Reed", ssn="4444")

    folder, warnings = FolderFinder(server)(client)
    assert folder is None and 'expected "Reed, Kim"' in warnings[0]

    assert FolderFinder(None)(client) == (None, ["CLIENT_FOLDERS_ROOT is not set in .env"])
    assert "cannot open" in FolderFinder(str(tmp_path / "offline"))(client)[1][0]


def test_matched_notice_gets_the_notices_subfolder(server):
    clients = [lead(5, "Pat", "Rivera", "Sam", ssn="111-22-3333")]
    result = match_notice(notice("PAT RIVERA", "SAM RIVERA", "3333"), clients, FolderFinder(server))

    assert result["notices_folder"] == str(server / "R" / "Rivera, Pat and Sam" / "Notices")
    assert result["warnings"] == []


def test_folder_under_nickname_is_found(server):
    (server / "R" / "Rivera, Bobby").mkdir()
    client = client_from_row(dict(client_id=9, first_name="Robert", last_name="Rivera", nickname="Bobby", ssn="5555"))

    assert FolderFinder(server)(client)[0] == str(server / "R" / "Rivera, Bobby")


def test_new_client_folder_still_in_intake(server):
    (server / "0 INTAKE New Customer Folders" / "Reed, Kim").mkdir(parents=True)
    client = lead(8, "Kim", "Reed", ssn="4444")

    assert FolderFinder(server)(client)[0] == str(server / "0 INTAKE New Customer Folders" / "Reed, Kim")


def test_copy_folders_are_ignored(server):
    (server / "R" / "Reyes, Ana - Copy").mkdir()
    client = lead(10, "Ana", "Reyes", ssn="6666")
    (server / "R" / "Reyes, Ana").rename(server / "R" / "Reyes, Ana M")

    assert FolderFinder(server)(client)[0] == str(server / "R" / "Reyes, Ana M")


def person(first, last, middle=None, suffix=None, spouse_first=None):
    return client_from_row(dict(client_id=20, first_name=first, middle_initial=middle, last_name=last,
                                suffix=suffix, spouse_first_name=spouse_first, ssn="8888"))


@pytest.mark.parametrize(
    "row, expected",
    [
        (dict(first_name="John", middle_initial="F.", last_name="Smith", suffix="Jr."), ("F", "JR")),
        (dict(first_name="John F.", last_name="Smith Jr"), ("F", "JR")),
        (dict(first_name="Mary Ann", middle_initial="", last_name="O'Brien", suffix="III"), (None, "III")),
        (dict(first_name="John", middle_initial="Francis", last_name="Smith", suffix="CPA"), ("F", None)),
    ],
)
def test_middle_initial_and_suffix_from_the_database(row, expected):
    client = client_from_row(dict(client_id=1, **row))
    assert (client.middle_initial, client.suffix) == expected


@pytest.mark.parametrize(
    "name, marks",
    [
        ("Smith, John F", ({"F"}, None)),
        ("Smith, John F. and Jane M.", ({"F"}, None)),
        ("John F Smith", ({"F"}, None)),
        ("Smith Jr, John", (set(), "JR")),
        ("Smith, John Sr.", (set(), "SR")),
        ("Smith_John_F", ({"F"}, None)),
        ("O'Brien, Kevin", (set(), None)),
        ("O’Brien Kevin", (set(), None)),
    ],
)
def test_folder_marks(name, marks):
    assert folder_marks(name) == marks


@pytest.fixture
def smiths(tmp_path):
    root = tmp_path / "Resolution"
    for name in ["Smith, John", "Smith, John F", "Smith, John Jr", "Smith, John Sr"]:
        (root / "S" / name).mkdir(parents=True)
    return root


@pytest.mark.parametrize(
    "client, folder",
    [
        (person("John", "Smith", middle="F"), "Smith, John F"),
        (person("John", "Smith"), "Smith, John"),
        (person("John", "Smith", suffix="Jr."), "Smith, John Jr"),
        (person("John", "Smith", suffix="Sr"), "Smith, John Sr"),
        (person("John F.", "Smith"), "Smith, John F"),  # initial typed into First Name
    ],
)
def test_middle_initial_and_suffix_pick_the_right_folder(smiths, client, folder):
    assert FolderFinder(smiths)(client) == (str(smiths / "S" / folder), [])


def test_first_last_folder_names_with_initials(tmp_path):
    root = tmp_path / "Resolution"
    for name in ["John Smith", "John F Smith"]:
        (root / "S" / name).mkdir(parents=True)

    assert FolderFinder(root)(person("John", "Smith", middle="F"))[0] == str(root / "S" / "John F Smith")
    assert FolderFinder(root)(person("John", "Smith"))[0] == str(root / "S" / "John Smith")


def test_folder_with_a_different_initial_or_generation_is_not_used(tmp_path):
    root = tmp_path / "Resolution"
    (root / "S" / "Smith, John G").mkdir(parents=True)
    (root / "S" / "Smith, John Sr").mkdir(parents=True)

    folder, warnings = FolderFinder(root)(person("John", "Smith", middle="F", suffix="Jr"))
    assert folder is None
    assert "skipped (different middle initial or Jr./Sr.): Smith, John G; Smith, John Sr" in warnings[0]


def test_only_folder_without_the_initial_is_still_used(tmp_path):
    root = tmp_path / "Resolution"
    (root / "S" / "Smith, John").mkdir(parents=True)

    assert FolderFinder(root)(person("John", "Smith", middle="F"))[0] == str(root / "S" / "Smith, John")


def test_spouse_still_comes_before_initials(tmp_path):
    root = tmp_path / "Resolution"
    (root / "S" / "Smith, John and Jane").mkdir(parents=True)
    (root / "S" / "Smith, John F").mkdir(parents=True)

    client = person("John", "Smith", middle="F", spouse_first="Jane")
    assert FolderFinder(root)(client)[0] == str(root / "S" / "Smith, John and Jane")


def test_tax_prep_only_folders_are_searched_too(server, tmp_path):
    tax_prep = tmp_path / "Tax Prep Only"
    (tax_prep / "W" / "Williams_Toni").mkdir(parents=True)
    (tax_prep / "W" / "Williams_Tony_and_Ann").mkdir(parents=True)
    client = lead(11, "Tony", "Williams", "Ann", ssn="7777")

    finder = FolderFinder(f"{server};{tax_prep}")
    assert finder(client)[0] == str(tax_prep / "W" / "Williams_Tony_and_Ann")


def test_first_root_wins(server, tmp_path):
    tax_prep = tmp_path / "Tax Prep Only"
    (tax_prep / "R" / "Reyes_Ana").mkdir(parents=True)
    client = lead(10, "Ana", "Reyes", ssn="6666")

    assert FolderFinder([server, tax_prep])(client)[0] == str(server / "R" / "Reyes, Ana")
    assert FolderFinder([tax_prep, server])(client)[0] == str(tax_prep / "R" / "Reyes_Ana")


def test_tax_prep_intake_folder(server, tmp_path):
    tax_prep = tmp_path / "Tax Prep Only"
    (tax_prep / "0 Intake Tax Preparation Folders" / "Reed_Kim").mkdir(parents=True)
    client = lead(8, "Kim", "Reed", ssn="4444")

    assert FolderFinder([server, tax_prep])(client)[0] == str(
        tax_prep / "0 Intake Tax Preparation Folders" / "Reed_Kim"
    )
