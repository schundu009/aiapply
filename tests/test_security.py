"""Credential removal, factual-answer handling, DB URL rewrite, removed routes."""

import asyncio
import json
from pathlib import Path

from conftest import make_token

ADMIN = {"Authorization": "Bearer " + make_token("admin")}


def test_create_accounts_route_is_gone(client, backend):
    assert client.post("/api/create-accounts", json={}, headers=ADMIN).status_code == 404


def test_account_creation_and_login_code_removed():
    src = (Path(__file__).resolve().parent.parent / "src" / "browser_automation.py").read_text()
    for needle in ("_create_", "create_job_board_accounts", "handle_google_login",
                   "handle_linkedin_login", "_handle_workday_login", "handle_ats_login",
                   "AutomationControlled", "user_agent=", "geolocation=", "slow_mo"):
        assert needle not in src, needle


def test_profile_save_never_persists_passwords(client, backend, flask_app):
    payload = {
        "first_name": "Ada",
        "email": "ada@example.com",
        "google_email": "ada@gmail.com",
        "google_password": "hunter2",
        "linkedin_password": "x",
        "workday_password": "y",
        "default_job_password": "z",
        "api_token": "t",
    }
    resp = client.post("/api/profile", json=payload, headers=ADMIN)
    assert resp.status_code == 200
    stored = json.loads(Path("personal_info.json").read_text())
    assert stored["first_name"] == "Ada"
    assert stored["email"] == "ada@example.com"
    assert not [k for k in stored if "password" in k or k == "google_email" or "token" in k]

    resp = client.post("/api/profile/add-fields", json={"fields": {"lever_password": "p", "city": "Austin"}},
                       headers=ADMIN)
    assert resp.status_code == 200
    stored = json.loads(Path("personal_info.json").read_text())
    assert "lever_password" not in stored and stored["city"] == "Austin"

    page = client.get("/profile", headers=ADMIN)
    assert page.status_code == 200
    assert b'type="password"' not in page.data
    assert b"hunter2" not in page.data


def test_startup_scrub_rewrites_legacy_files(tmp_path):
    from src.profile_store import scrub_profile_files

    legacy = tmp_path / "personal_info.json"
    legacy.write_text(json.dumps({"first_name": "A", "workday_email": "w@x", "workday_password": "p",
                                  "google_password": "g", "default_job_password": "d"}))
    assert scrub_profile_files([legacy, tmp_path / "missing.json"]) == [str(legacy)]
    assert json.loads(legacy.read_text()) == {"first_name": "A"}


def test_default_profile_has_no_factual_defaults(flask_app):
    info = flask_app.load_personal_info()
    for key in ("work_authorization", "requires_sponsorship", "legally_authorized", "willing_to_relocate",
                "background_check_consent", "privacy_agreement", "data_processing_consent", "how_did_you_hear"):
        assert info.get(key, "") == "", key


def test_database_url_rewrite():
    from src.database import normalize_database_url

    assert normalize_database_url("postgresql://u:p@h:5432/db") == "postgresql+psycopg2://u:p@h:5432/db"
    assert normalize_database_url("postgres://u:p@h/db") == "postgresql+psycopg2://u:p@h/db"
    assert normalize_database_url("postgresql+psycopg2://u@h/db") == "postgresql+psycopg2://u@h/db"
    assert normalize_database_url("sqlite:///x.db") == "sqlite:///x.db"
    assert normalize_database_url("") == ""


def _agent():
    from src.form_agent import FormAgent

    agent = FormAgent.__new__(FormAgent)
    agent.calls = []

    def fake_llm(prompt, max_tokens=2000):
        agent.calls.append(prompt)
        return "Drafted answer."

    agent._call_llm = fake_llm
    return agent


def test_factual_fields_only_come_from_saved_profile():
    agent = _agent()
    blank = {"first_name": "Ada"}
    for label in ("Are you legally authorized to work in the US?", "Will you require visa sponsorship?",
                  "Are you willing to relocate?", "How did you hear about us?",
                  "Do you consent to a background check?", "I agree to the privacy policy",
                  "Work authorization status", "Gender", "Veteran status"):
        assert agent.map_field_to_value(label, "select", blank) is None, label
    saved = {"requires_sponsorship": "Yes", "how_did_you_hear": "Referral"}
    assert agent.map_field_to_value("Will you require sponsorship?", "select", saved) == "Yes"
    assert agent.map_field_to_value("How did you hear about this job?", "select", saved) == "Referral"
    assert agent.map_field_to_value("Email address", "text", {"email": "a@b.c"}) == "a@b.c"


def test_llm_never_answers_factual_or_choice_questions():
    agent = _agent()
    info = {"first_name": "Ada", "email": "ada@example.com", "phone": "555", "google_password": "hunter2",
            "work_authorization": "H1B Visa", "current_title": "Engineer"}
    run = lambda *a, **k: asyncio.run(agent.get_field_value_with_llm(*a, **k))
    assert run("Do you require sponsorship?", "text", [], info, "resume") is None
    assert run("Pick one", "select", ["A", "B"], info, "resume") is None
    assert run("Gender", "radio", [], info, "resume") is None
    assert agent.calls == []

    answer = run("Why do you want to work here?", "textarea", [], info, "My resume", job_description="JD")
    assert answer == "Drafted answer."
    prompt = agent.calls[-1]
    for secret in ("hunter2", "ada@example.com", "555", "H1B"):
        assert secret not in prompt
    assert "Engineer" in prompt and "My resume" in prompt


def test_download_rejects_paths_outside_output(client, backend):
    assert client.get("/download/etc/passwd", headers=ADMIN).status_code == 404
    assert client.get("/download/../app.py", headers=ADMIN).status_code == 404
    out = Path("output") / "job1"
    out.mkdir(parents=True, exist_ok=True)
    (out / "resume.pdf").write_bytes(b"%PDF-1.4")
    absolute = (Path.cwd() / out / "resume.pdf").resolve()
    assert client.get(f"/download/{absolute}", headers=ADMIN, follow_redirects=True).status_code == 200
    assert client.get("/download/output/job1/resume.pdf", headers=ADMIN).status_code == 200


def test_auto_submit_defaults_false():
    import inspect
    import app as app_module
    from src.browser_automation import BrowserAutomation

    assert inspect.signature(BrowserAutomation.auto_apply).parameters["auto_submit"].default is False
    assert "data.get('auto_submit', False) is True" in inspect.getsource(app_module.apply_to_job)
