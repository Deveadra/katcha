from pathlib import Path


def test_production_supervisor_requires_renderer_readable_roles_anywhere_bundle(
) -> None:
    script = Path("deploy/scripts/production-supervisor.sh").read_text(encoding="utf-8")

    assert "renderer_uid=10001" in script
    assert "/etc/katcha/aws/runtime/client-key.pem" in script
    assert "/etc/katcha/aws/aws_signing_helper" in script
    assert '"$renderer_uid:600"' in script
    assert '"$renderer_uid:755"' in script


def test_oci_recovery_normalizes_roles_anywhere_bundle_for_renderer() -> None:
    template = Path("deploy/cloud-init/oci-recovery-candidate.sh.tmpl").read_text(
        encoding="utf-8"
    )

    assert "chown -R 10001:10001 /etc/katcha/aws" in template
    assert "find /etc/katcha/aws -type d -exec chmod 0700 {} +" in template
    assert "chmod 0600" in template
    assert "/etc/katcha/aws/runtime/client-key.pem" in template
    assert "chmod 0755 /etc/katcha/aws/aws_signing_helper" in template
    assert 'if [[ ! -f "$required_path" || -L "$required_path" ]]' in template
