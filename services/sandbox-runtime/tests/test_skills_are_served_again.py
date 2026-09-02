"""As skills do painel voltam a chegar ao Coder (o serving religado).

A rc.125 tirou a materialização do caminho de execução: 25 skills GLOBAIS de
vários clientes entravam em todo run, e o `.claude/.dse-materialized` vazava na
PR. O registro ficou dormente. O operador agora pediu de volta — com as
regras que faltavam: só o que o painel TICOU para o repositório
(`skill_registry.repo_scope`), o SKILL.md escrito VERBATIM quando já traz o
seu frontmatter (é o `description:` que diz ao agente QUANDO usar a skill —
um cabeçalho reconstruído a partir do título perdia exatamente isso), e o
registro fora do ar nunca derruba um provision.

O substrato carrega `.claude/skills/` nativamente (`setting_sources=["project"]`);
o que este arquivo pina é que os arquivos CHEGAM ao Pod no provision.
"""
from __future__ import annotations

import asyncio
import base64
import io
import tarfile

import pytest

import sandbox_runtime.activities as activities
import sandbox_runtime.skill_registry as registry
from sandbox_runtime.activities import ProvisionSandboxInput
from sandbox_runtime.skill_files import plan_materialization
from sandbox_runtime.skill_registry import Skill, SkillRegistryUnavailable

from test_k8s_lifecycle_activities import _StubK8sDriver

_AVISO_MD = """---
name: writing-aviso-platform-code
description: Applies AvisoWealth's engineering standards. Use when writing code for the AvisoWealth tenant.
metadata:
  version: "1.1.0"
---

# Writing Aviso Platform Code

Rules here.
"""


def _skill(key: str, body: str, *, title: str = "Writing Aviso Platform Code", repos=None) -> Skill:
    return Skill(tenant_id="fintex-poc", skill_key=key, title=title, body=body,
                 category="code standards", repo_scope=repos)


# ---------------------------------------------------------------------------
# O arquivo escrito
# ---------------------------------------------------------------------------

def test_a_body_with_its_own_frontmatter_is_written_verbatim_under_its_name():
    files, excludes, keys = plan_materialization(
        [_skill("custom-3", _AVISO_MD)], existing_skill_keys=set(), marker_entries=set(),
    )
    assert files == [(".claude/skills/writing-aviso-platform-code/SKILL.md", _AVISO_MD)], (
        "o painel dá ids custom-N; o nome e o `description:` são os do frontmatter, "
        "e o diretório tem que bater com o `name:` (é assim que o SDK reconhece a skill)"
    )
    assert excludes == [".claude/skills/writing-aviso-platform-code/"]
    assert keys == ["custom-3"]


def test_a_body_without_frontmatter_still_gets_one():
    files, _, _ = plan_materialization(
        [_skill("pci-dss-logging", "# PCI\n\nMask PANs.", title="PCI-DSS logging")],
        existing_skill_keys=set(), marker_entries=set(),
    )
    (path, content), = files
    assert path == ".claude/skills/pci-dss-logging/SKILL.md"
    assert content.startswith("---\nname: pci-dss-logging\ndescription: PCI-DSS logging\n---")


def test_a_skill_committed_in_the_repo_under_the_frontmatter_name_is_sovereign():
    files, _, _ = plan_materialization(
        [_skill("custom-3", _AVISO_MD)],
        existing_skill_keys={"writing-aviso-platform-code"}, marker_entries=set(),
    )
    assert files == [], "o repo commitou a mesma skill: a cópia do painel não a sobrescreve"


# ---------------------------------------------------------------------------
# O provision no K8s
# ---------------------------------------------------------------------------

class _PodRecorder(_StubK8sDriver):
    """O stub do ciclo de vida + o `run_in_pod` que a materialização usa."""

    def __init__(self):
        super().__init__()
        self.execs: list[tuple[list[str], str | None]] = []

    def run_in_pod(self, sandbox_id, argv, input_text=None, *, timeout=120):
        self.execs.append((list(argv), input_text))
        if input_text is None:
            return 0, "--dirs--\n--marker--\n"   # workspace vazio de skills
        return 0, "OK"


def _written_files(driver: _PodRecorder) -> dict[str, str]:
    payloads = [stdin for _argv, stdin in driver.execs if stdin]
    assert payloads, "nenhum payload de skills chegou ao Pod"
    raw = base64.b64decode(payloads[-1])
    out: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for m in tar.getmembers():
            out[m.name] = tar.extractfile(m).read().decode("utf-8")
    return out


@pytest.fixture
def pod(monkeypatch):
    driver = _PodRecorder()
    monkeypatch.setattr(activities, "select_sandbox_driver", lambda: driver)
    return driver


@pytest.fixture
def audits(monkeypatch):
    rows: list[dict] = []
    monkeypatch.setattr(activities, "audit_emit", lambda **kw: rows.append(kw))
    return rows


def _provision(work_item_id: str):
    return asyncio.run(activities.provision_sandbox(ProvisionSandboxInput(
        work_item_id=work_item_id, tenant_id="fintex-poc",
        repo="fintexinc/calculation-engine-service", base_branch="main",
    )))


def test_provision_on_k8s_materializes_the_skills_ticked_for_the_repo(pod, audits, monkeypatch, work_item_id, state_dir):
    seen: dict = {}

    def fake_read(tenant_id, *, repo=None, task_class=None, conn=None):
        seen.update(tenant_id=tenant_id, repo=repo)
        return [_skill("custom-3", _AVISO_MD, repos=["*"])]

    monkeypatch.setattr(registry, "read_approved_skills", fake_read)
    handle = _provision(work_item_id)

    assert handle.container_id == f"dse-sbx-{work_item_id}"
    assert seen == {"tenant_id": "fintex-poc", "repo": "fintexinc/calculation-engine-service"}, (
        "a leitura é do tenant E do repo — o tick por repositório é a regra"
    )
    files = _written_files(pod)
    assert files == {".claude/skills/writing-aviso-platform-code/SKILL.md": _AVISO_MD}
    served = [a for a in audits if a.get("action") == "skills_materialized"]
    assert served and served[0]["details"]["skills"] == ["custom-3"]


def test_nothing_ticked_for_the_repo_means_nothing_written_and_it_is_on_the_ledger(pod, audits, monkeypatch, work_item_id, state_dir):
    monkeypatch.setattr(registry, "read_approved_skills", lambda tenant_id, **kw: [])
    _provision(work_item_id)
    assert not [s for _a, s in pod.execs if s], "sem skill servida, nada é escrito no Pod"
    assert [a for a in audits if a.get("action") == "skills_resolved_empty"]


def test_the_registry_being_down_never_fails_the_provision(pod, audits, monkeypatch, work_item_id, state_dir):
    def boom(tenant_id, **kw):
        raise SkillRegistryUnavailable("skill_registry: Postgres unavailable")

    monkeypatch.setattr(registry, "read_approved_skills", boom)
    handle = _provision(work_item_id)
    assert handle.container_id == f"dse-sbx-{work_item_id}"
    skipped = [a for a in audits if a.get("action") == "skills_materialization_skipped"]
    assert skipped and "Postgres unavailable" in str(skipped[0]["details"])
