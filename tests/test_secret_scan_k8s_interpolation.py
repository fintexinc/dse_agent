"""O scanner de segredos não pode acusar interpolação k8s de pod-spec.

Regressão do falso positivo que deixou o CI vermelho (mascarado) desde o
commit do gateway-DB: `model-gateway.yaml` monta o DATABASE_URL com
`postgresql://$(LITELLM_DB_USER):$(LITELLM_DB_PASSWORD)@...` — as duas
"credenciais" são referências `$(VAR)` resolvidas pelo kubelet a partir de
secretKeyRef, exatamente o padrão que o scanner quer INCENTIVAR (como já
faz com `${VAR}`, `os.environ`, `secretKeyRef`). Uma senha literal na
mesma forma de DSN continua sendo achado.
"""

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "scan_for_plaintext_secrets.py"
_spec = importlib.util.spec_from_file_location("scan_for_plaintext_secrets", _SCRIPT)
assert _spec is not None and _spec.loader is not None
scanner = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("scan_for_plaintext_secrets", scanner)
_spec.loader.exec_module(scanner)


def test_k8s_env_interpolation_in_dsn_is_a_reference_not_a_secret(tmp_path):
    (tmp_path / "model-gateway.yaml").write_text(
        "env:\n"
        "  - name: DATABASE_URL\n"
        '    value: "postgresql://$(LITELLM_DB_USER):$(LITELLM_DB_PASSWORD)'
        '@dse-postgres:5432/litellm"\n'
    )
    findings = scanner.scan_repo(tmp_path)
    assert findings == [], (
        "referência $(VAR) de pod-spec virou achado — o kubelet injeta o "
        f"valor do Secret em runtime, nada em texto plano: {findings}"
    )


def test_a_literal_password_in_a_dsn_is_still_flagged(tmp_path):
    (tmp_path / "leaked.yaml").write_text(
        "env:\n"
        "  - name: DATABASE_URL\n"
        '    value: "postgresql://litellm:hunter2hunter2@dse-postgres:5432/litellm"\n'
    )
    findings = scanner.scan_repo(tmp_path)
    assert [f[2] for f in findings] == ["postgres_password_in_url"], (
        "a senha literal tem que continuar sendo achada — o fix do $(VAR) "
        f"não pode abrir esse buraco: {findings}"
    )
