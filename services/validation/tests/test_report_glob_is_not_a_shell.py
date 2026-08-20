"""O glob do relatório é interpolado num script de shell que roda no Pod com as
credenciais da plataforma. Ele vem do manifesto do CLIENTE.

Então ele não é uma string qualquer: o parser fecha o alfabeto antes de a
string existir. É a mesma disciplina dos `commands.*` (argv, nunca string de
shell), aplicada ao único campo que precisa ser um padrão e não um argv."""
from __future__ import annotations

import pytest

from dse_contracts import GateStatus
from dse_validation.config import L1Config, L1ManifestError


def _parse(reports):
    return L1Config._from_manifest_payload(
        {"version": 1, "commands": {"test": ["pytest"]}, "reports": reports},
        source="test")


def test_a_plain_glob_is_accepted():
    cfg = _parse({"junit": "target/surefire-reports/*.xml"})
    assert cfg.junit_reports == "target/surefire-reports/*.xml"


def test_no_reports_block_means_no_glob():
    cfg = L1Config._from_manifest_payload(
        {"version": 1, "commands": {"test": ["pytest"]}}, source="test")
    assert cfg.junit_reports is None


@pytest.mark.parametrize("veneno", [
    "a; rm -rf /",
    "$(id)",
    "`id`",
    "a b",
    "'x'",
    '"x"',
    "../../etc/passwd",
    "/absolute/path/*.xml",
    "a\nb",
])
def test_anything_a_shell_could_read_as_more_than_a_path_is_refused(veneno):
    with pytest.raises(L1ManifestError) as err:
        _parse({"junit": veneno})
    assert err.value.status is GateStatus.ERROR


def test_an_unknown_key_in_reports_is_refused():
    with pytest.raises(L1ManifestError):
        _parse({"junit": "x/*.xml", "sarif": "y.json"})
