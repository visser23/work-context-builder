"""Config validation, front matter, CLI login and doctor checks for mail/calendar/Slack."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from tests.fake_sharepoint import make_source
from tests.fake_web import days_ago, slack_ts
from workctx import doctor
from workctx.cli import main as cli_main
from workctx.config import ProjectConfig, SlackSource, load_config
from workctx.models import FrontMatter, SourceType

BASE = {
    "version": 1,
    "project": {"id": "p", "name": "P", "output_root": "/tmp/o", "state_dir": "/tmp/s"},
}
SP = {
    "name": "sp",
    "site_url": "https://contoso.sharepoint.com/sites/x",
    "mode": "browser",
    "auth": {"mode": "browser", "secret_ref": "sp-cookies"},
}


def cfg(**sources):
    return ProjectConfig.model_validate({**BASE, "sources": sources})


# ------------------------------------------------------------------ config


def test_defaults_and_profile_resolution():
    config = cfg(
        sharepoint=[SP],
        mail=[{"name": "m", "sharepoint_source": "sp"}],
        calendar=[{"name": "c", "profile": "standalone"}],
        slack=[
            {"name": "s", "sharepoint_source": "sp", "client_url": "https://app.slack.com/c/E1/"}
        ],
    )
    sp = {s.name: s for s in config.sources.sharepoint}
    assert config.sources.mail[0].profile_name(sp) == "sp"
    assert config.sources.calendar[0].profile_name(sp) == "standalone"
    assert config.sources.mail[0].folders == ["inbox", "sentitems"]
    assert config.sources.mail[0].mailbox_url == "https://outlook.office.com/mail/"
    assert config.sources.slack[0].conversation_types == [
        "public_channel",
        "private_channel",
        "mpim",
        "im",
    ]
    assert config.sources.slack[0].signin_url is None
    assert config.all_source_names() == ["sp", "m", "c", "s"] or set(config.all_source_names()) == {
        "sp",
        "m",
        "c",
        "s",
    }


def test_source_without_a_login_is_rejected():
    with pytest.raises(ValidationError, match="needs either"):
        cfg(mail=[{"name": "m"}])
    with pytest.raises(ValidationError):
        SlackSource(name="s", profile="p")  # client_url is required


def test_unknown_sharepoint_reference_is_rejected():
    with pytest.raises(ValidationError, match="not a 'mode: browser' SharePoint source"):
        cfg(sharepoint=[SP], calendar=[{"name": "c", "sharepoint_source": "nope"}])


def test_source_names_must_be_unique_across_types():
    with pytest.raises(ValidationError, match=r"[Dd]uplicate|unique"):
        cfg(mail=[{"name": "x", "profile": "p"}], calendar=[{"name": "x", "profile": "p"}])


def test_numeric_bounds():
    with pytest.raises(ValidationError):
        cfg(mail=[{"name": "m", "profile": "p", "since_days": 0}])
    with pytest.raises(ValidationError):
        cfg(mail=[{"name": "m", "profile": "p", "max_body_chars": 10}])


def test_example_config_documents_the_new_sources():
    from pathlib import Path

    text = (Path(__file__).parent.parent / "example-config.yaml").read_text()
    config = ProjectConfig.model_validate(yaml.safe_load(text))
    assert config.project.id
    for marker in ("# mail:", "# calendar:", "# slack:", "workctx auth login-web"):
        assert marker in text


# ---------------------------------------------------------- front matter


def test_front_matter_new_fields_are_escaped():
    fm = FrontMatter(
        source_type=SourceType.SLACK,
        source_name="s",
        source_id="T1:C1:2026-01-01",
        title='He said "hi" \\ bye',
        workspace="Con\ntoso",
        channel="#gen",
        message_count=3,
        sender='Evil: "name"',
        location="Room: 1",
    )
    meta = yaml.safe_load(fm.to_yaml_str().strip("-\n"))
    assert meta["title"] == 'He said "hi" \\ bye'
    assert meta["workspace"] == "Con toso" and meta["message_count"] == 3
    assert meta["sender"] == 'Evil: "name"' and meta["location"] == "Room: 1"


# ------------------------------------------------------------- transcripts


def test_transcript_retention_cutoff_follows_since_days():
    assert make_source().retention_cutoff() is None
    cutoff = make_source(since_days=10).retention_cutoff()
    assert abs(cutoff - (datetime.now(UTC) - timedelta(days=10))) < timedelta(seconds=5)


# --------------------------------------------------------------------- CLI


@pytest.fixture
def config_file(tmp_path):
    data = {
        **BASE,
        "sources": {
            "mail": [{"name": "m", "profile": "pm", "mailbox_url": "https://mail.example.com/"}],
            "slack": [
                {
                    "name": "s",
                    "profile": "ps",
                    "client_url": "https://app.slack.com/client/E1/",
                    "signin_url": "https://corp.enterprise.slack.com/",
                    "sso_button_text": "Sign in with Okta",
                }
            ],
        },
    }
    path = tmp_path / "workctx.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


@pytest.fixture(autouse=True)
def lapsed_sessions(monkeypatch):
    """login-web checks sessions silently first; by default pretend they have lapsed."""
    checked = []

    def fake_check(profile, url, kind, **kw):
        checked.append((profile, url, kind))
        return False

    monkeypatch.setattr("workctx.auth.webtokens.check_web_session", fake_check)
    return checked


def run_login(monkeypatch, config_file, source, result=True):
    calls = []

    def fake_login(profile, url, kind, **kw):
        calls.append((profile, url, kind, kw))
        return result

    monkeypatch.setattr("workctx.auth.webtokens.interactive_web_login", fake_login)
    outcome = CliRunner().invoke(
        cli_main, ["auth", "login-web", "--config", str(config_file), "--source", source]
    )
    return outcome, calls


def test_login_web_outlook(monkeypatch, config_file):
    outcome, calls = run_login(monkeypatch, config_file, "m")
    assert outcome.exit_code == 0, outcome.output
    assert calls[0][:3] == ("pm", "https://mail.example.com/", "outlook")


def test_login_web_slack_uses_signin_url_and_sso_text(monkeypatch, config_file):
    outcome, calls = run_login(monkeypatch, config_file, "s")
    assert outcome.exit_code == 0
    profile, url, kind, kw = calls[0]
    assert (profile, url, kind) == ("ps", "https://app.slack.com/client/E1/", "slack")
    assert kw["signin_url"] == "https://corp.enterprise.slack.com/"
    assert kw["sso_button_text"] == "Sign in with Okta"


def test_login_web_without_source_signs_in_every_distinct_web_session(monkeypatch, config_file):
    # `workctx auth login-web --config X` (no --source) is what users naturally type.
    calls = []

    def fake_login(profile, url, kind, **kw):
        calls.append((profile, url, kind))
        return True

    monkeypatch.setattr("workctx.auth.webtokens.interactive_web_login", fake_login)
    outcome = CliRunner().invoke(cli_main, ["auth", "login-web", "--config", str(config_file)])
    assert outcome.exit_code == 0, outcome.output
    kinds = [c[2] for c in calls]
    assert "outlook" in kinds and "slack" in kinds
    assert len(calls) == len(set(calls))  # mail + calendar on one profile open it once


def test_login_sharepoint_delegates_web_sources_instead_of_failing(monkeypatch, config_file):
    # `workctx-relogin --source <slack source>` used to say "SharePoint source not found".
    calls = []

    def fake_login(profile, url, kind, **kw):
        calls.append((profile, url, kind))
        return True

    monkeypatch.setattr("workctx.auth.webtokens.interactive_web_login", fake_login)
    outcome = CliRunner().invoke(
        cli_main, ["auth", "login-sharepoint", "--config", str(config_file), "--source", "s"]
    )
    assert outcome.exit_code == 0, outcome.output
    assert calls == [("ps", "https://app.slack.com/client/E1/", "slack")]


def test_sources_lists_every_configured_source_without_secrets(config_file):
    outcome = CliRunner().invoke(cli_main, ["sources", "--config", str(config_file)])
    assert outcome.exit_code == 0, outcome.output
    out = outcome.output
    for needle in ("slack", "mail", "pm", "ps", "https://app.slack.com/client/E1/"):
        assert needle in out
    assert "Config file" in out and "Output" in out


def test_login_web_only_opens_windows_for_lapsed_sessions(
    monkeypatch, config_file, lapsed_sessions
):
    opened = []
    monkeypatch.setattr(
        "workctx.auth.webtokens.interactive_web_login",
        lambda profile, url, kind, **kw: opened.append(kind) or True,
    )
    # Outlook is still signed in, Slack has lapsed.
    monkeypatch.setattr(
        "workctx.auth.webtokens.check_web_session",
        lambda profile, url, kind, **kw: kind == "outlook",
    )
    outcome = CliRunner().invoke(cli_main, ["auth", "login-web", "--config", str(config_file)])
    assert outcome.exit_code == 0, outcome.output
    assert opened == ["slack"]
    assert "already signed in" in outcome.output


def test_login_web_force_opens_even_when_signed_in(monkeypatch, config_file):
    opened = []
    monkeypatch.setattr(
        "workctx.auth.webtokens.interactive_web_login",
        lambda profile, url, kind, **kw: opened.append(kind) or True,
    )
    monkeypatch.setattr("workctx.auth.webtokens.check_web_session", lambda *a, **k: True)
    outcome = CliRunner().invoke(
        cli_main, ["auth", "login-web", "--config", str(config_file), "--force"]
    )
    assert outcome.exit_code == 0, outcome.output
    assert sorted(opened) == ["outlook", "slack"]


def test_login_web_failure_and_unknown_source(monkeypatch, config_file):
    outcome, _ = run_login(monkeypatch, config_file, "m", result=False)
    assert outcome.exit_code == 1 and "Timed out" in outcome.output
    outcome, calls = run_login(monkeypatch, config_file, "nope")
    assert outcome.exit_code == 1 and not calls


# ------------------------------------------------------------------ doctor


class Recorder:
    def __init__(self):
        self.ok_, self.fail_, self.warn_ = [], [], []

    def run(self, check, *args):
        check(*args, self.ok_.append, self.fail_.append, self.warn_.append)
        return self


@pytest.fixture
def full_config(tmp_path):
    data = {
        **BASE,
        "sources": {
            "mail": [
                {
                    "name": "m",
                    "profile": "p",
                    "api_base": "https://outlook.example.com/api/v2.0",
                    "folders": ["inbox"],
                }
            ],
            "calendar": [
                {"name": "c", "profile": "p", "api_base": "https://outlook.example.com/api/v2.0"}
            ],
            "slack": [{"name": "s", "profile": "p", "client_url": "https://app.slack.com/c/E1/"}],
        },
    }
    return ProjectConfig.model_validate(data)


def test_doctor_outlook_check_ok(outlook, full_config):
    for kind, src in (
        ("mail", full_config.sources.mail[0]),
        ("calendar", full_config.sources.calendar[0]),
    ):
        rec = Recorder().run(doctor._check_outlook, full_config, src, kind)
        assert not rec.fail_ and any("session valid" in m for m in rec.ok_)


def test_doctor_outlook_check_reports_dead_session(outlook, full_config):
    outlook.always_stale = True
    outlook.provider_token = "stale"
    rec = Recorder().run(doctor._check_outlook, full_config, full_config.sources.mail[0], "mail")
    assert rec.fail_ and "session invalid" in rec.fail_[0]


def test_doctor_outlook_check_flags_bad_config(full_config):
    bad = full_config.sources.mail[0].model_copy(update={"api_base": "http://insecure.example.com"})
    rec = Recorder().run(doctor._check_outlook, full_config, bad, "mail")
    assert rec.fail_


def test_doctor_slack_check(slack_api, full_config):
    slack_api.users = {}
    rec = Recorder().run(doctor._check_slack, full_config, full_config.sources.slack[0])
    assert not rec.fail_ and any("Contoso" in m for m in rec.ok_)

    slack_api.token = "xoxc-rotated"
    rec = Recorder().run(doctor._check_slack, full_config, full_config.sources.slack[0])
    assert rec.fail_ and "Slack check failed" in rec.fail_[0]


def test_doctor_slack_check_no_matching_workspace(slack_api, full_config):
    src = full_config.sources.slack[0].model_copy(update={"workspaces": ["zzz"]})
    rec = Recorder().run(doctor._check_slack, full_config, src)
    assert rec.fail_ and "no Slack workspace matched" in rec.fail_[0]


def test_load_config_roundtrip(tmp_path, config_file):
    assert load_config(config_file).sources.slack[0].sso_button_text == "Sign in with Okta"
    _ = (days_ago, slack_ts)  # fixtures' helpers are imported for reuse in other modules


# ------------------------------------------------------- config discovery / help


def test_workctx_config_env_var_locates_the_config(monkeypatch, config_file, tmp_path):
    monkeypatch.chdir(tmp_path)  # nowhere near a workctx.yaml
    monkeypatch.setenv("WORKCTX_CONFIG", str(config_file))
    outcome = CliRunner().invoke(cli_main, ["sources"])
    assert outcome.exit_code == 0, outcome.output
    assert "Config file" in outcome.output


def test_explicit_config_beats_the_env_var(monkeypatch, config_file, tmp_path):
    monkeypatch.setenv("WORKCTX_CONFIG", str(tmp_path / "does-not-exist.yaml"))
    outcome = CliRunner().invoke(cli_main, ["sources", "--config", str(config_file)])
    assert outcome.exit_code == 0, outcome.output


def test_missing_env_config_is_reported_clearly(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WORKCTX_CONFIG", str(tmp_path / "nope.yaml"))
    outcome = CliRunner().invoke(cli_main, ["sources"])
    assert outcome.exit_code == 1 and "WORKCTX_CONFIG" in outcome.output


def test_top_level_help_is_a_usable_quick_guide():
    out = CliRunner().invoke(cli_main, ["--help"]).output
    for needle in ("sources", "status", "sync", "auth relogin", "WORKCTX_CONFIG", "doctor"):
        assert needle in out


def test_config_option_help_says_where_the_default_comes_from():
    out = CliRunner().invoke(cli_main, ["sources", "--help"]).output
    assert "WORKCTX_CONFIG" in out


# ------------------------------------------------------------ auth relogin


@pytest.fixture
def relogin_env(monkeypatch, tmp_path):
    """A config with a browser SharePoint source, Slack and mail; records what is opened."""
    data = {
        **BASE,
        "sources": {
            "sharepoint": [
                {
                    "name": "sp",
                    "mode": "browser",
                    "site_url": "https://t.sharepoint.example/sites/x",
                    "auth": {"mode": "browser", "secret_ref": "sp-cookies"},
                }
            ],
            "mail": [{"name": "m", "sharepoint_source": "sp"}],
            "slack": [
                {
                    "name": "s",
                    "sharepoint_source": "sp",
                    "client_url": "https://app.slack.com/client/E1/",
                }
            ],
        },
    }
    path = tmp_path / "workctx.yaml"
    path.write_text(yaml.safe_dump(data))
    opened: list[str] = []
    state = {"sp_ok": True, "web_ok": True}

    def fake_sp_cookies(host_url, profile, secret_ref):
        if not state["sp_ok"]:
            raise RuntimeError("expired")
        return {"rtFa": "x"}

    monkeypatch.setattr("workctx.auth.sharepoint.get_valid_cookies", fake_sp_cookies)
    monkeypatch.setattr(
        "workctx.auth.sharepoint.interactive_login",
        lambda *a, **k: opened.append("sharepoint") or {"rtFa": "x"},
    )
    monkeypatch.setattr(
        "workctx.auth.webtokens.check_web_session", lambda p, u, kind, **kw: state["web_ok"]
    )
    monkeypatch.setattr(
        "workctx.auth.webtokens.interactive_web_login",
        lambda p, u, kind, **kw: opened.append(kind) or True,
    )
    return path, opened, state


def relogin(path, *args):
    return CliRunner().invoke(cli_main, ["auth", "relogin", "--config", str(path), *args])


def test_relogin_with_everything_signed_in_opens_nothing(relogin_env):
    path, opened, _ = relogin_env
    outcome = relogin(path)
    assert outcome.exit_code == 0, outcome.output
    assert opened == [] and "already signed in" in outcome.output


def test_relogin_opens_windows_only_where_a_login_lapsed(relogin_env):
    path, opened, state = relogin_env
    state["sp_ok"] = False
    outcome = relogin(path)
    assert outcome.exit_code == 0, outcome.output
    assert opened == ["sharepoint"]  # web sessions were still fine
    state["sp_ok"], state["web_ok"] = True, False
    opened.clear()
    relogin(path)
    assert sorted(opened) == ["outlook", "slack"]


def test_relogin_by_name_routes_to_the_right_login(relogin_env):
    path, opened, state = relogin_env
    state["web_ok"] = False
    assert relogin(path, "--source", "s").exit_code == 0
    assert opened == ["slack"]
    opened.clear()
    state["sp_ok"] = False
    assert relogin(path, "sp").exit_code == 0  # bare name works too
    assert opened == ["sharepoint"]


def test_relogin_force_opens_even_when_signed_in(relogin_env):
    path, opened, _ = relogin_env
    assert relogin(path, "--source", "sp", "--force").exit_code == 0
    assert opened == ["sharepoint"]


def test_relogin_unknown_name_lists_what_exists(relogin_env):
    path, opened, _ = relogin_env
    outcome = relogin(path, "--source", "nope")
    assert outcome.exit_code == 1 and not opened
    for name in ("sp", "m", "s"):
        assert name in outcome.output
