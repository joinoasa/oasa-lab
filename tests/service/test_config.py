"""Config rules: the service never listens on a network address, and a typo cannot open a door."""

from pathlib import Path

import pytest

from oasa_lab.service.config import ConfigError, hash_token, parse_config

USER = {"name": "owner", "role": "admin", "github_id": 1}


def cfg(**kw):
    data = {"users": [USER], "sandbox": {"mode": "none"}, **kw}  # the default is the real sandbox, which needs the policy file
    return parse_config(data, Path("/tmp"), {})


def test_minimal_config_is_loopback_and_unsafe_for_rf():
    c = cfg()
    assert c.host == "127.0.0.1" and c.rf_setup == "unset"


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com"])
def test_non_loopback_host_is_refused(host):
    with pytest.raises(ConfigError, match="loopback"):
        cfg(service={"host": host})


def test_no_users_is_refused():
    with pytest.raises(ConfigError):
        parse_config({"sandbox": {"mode": "none"}}, Path("/tmp"), {})


def test_unknown_role_is_refused():
    with pytest.raises(ConfigError, match="role"):
        parse_config({"sandbox": {"mode": "none"}, "users": [{"name": "x", "role": "root"}]}, Path("/tmp"), {})


def test_token_must_point_at_a_user():
    with pytest.raises(ConfigError, match="unknown user"):
        cfg(tokens=[{"name": "t", "user": "nobody", "sha256": "0" * 64}])


def test_bad_rf_setup_is_refused():
    with pytest.raises(ConfigError, match="rf_setup"):
        cfg(lab={"rf_setup": "anechoic"})


def test_redirect_uri_wildcards_are_refused():
    with pytest.raises(ConfigError):
        cfg(oauth={"redirect_uris": ["https://*.example.com/cb"]})


def test_public_url_must_be_https_unless_local():
    with pytest.raises(ConfigError, match="https"):
        cfg(service={"public_url": "http://lab.example.com"})
    assert cfg(service={"public_url": "https://lab.example.com"}).mcp_url == "https://lab.example.com/mcp"


def test_api_token_lookup_uses_the_hash():
    tok = "oasa_secret"
    c = cfg(tokens=[{"name": "t", "user": "owner", "sha256": hash_token(tok)}])
    assert c.api_token_user(tok)[1].name == "owner"
    assert c.api_token_user("oasa_other") is None


def test_more_users_and_roles_are_only_config():
    c = cfg(users=[USER, {"name": "v", "role": "viewer", "github_id": 77, "github_login": "someone"}])
    assert c.user("v").can("read") and not c.user("v").can("run")
    assert c.user("owner").can("pause")


def test_github_id_is_required_for_every_user():
    for bad in ({"name": "v", "role": "viewer"}, {"name": "v", "role": "viewer", "github_login": "someone"},
                {"name": "v", "role": "viewer", "github_id": "12"}, {"name": "v", "role": "viewer", "github_id": True},
                {"name": "v", "role": "viewer", "github_id": 0}):
        with pytest.raises(ConfigError):
            cfg(users=[USER, bad])
