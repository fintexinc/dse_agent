"""Build credentials for the pods the DSE runs the customer's build in.

ONE settings builder, imported by both consumers — the sandbox driver
(`sandbox_runtime.k8s_driver`, which delivers the document over exec stdin) and
the preview recipe (`dse_validation.preview.argocd`, which seeds it as a Secret
in the preview namespace). The layering follows the existing direction:
sandbox-runtime already imports dse_validation; never the reverse.

It moved here on 2026-08-19, the day a preview build died with 401 on the
private Maven feed while the sandbox, running the SAME build for the SAME
work item, had the credential all along — two delivery paths, one of them
forgotten. A single module is what keeps the next credential (an npm registry,
a NuGet feed) from repeating that.
"""
from __future__ import annotations

import urllib.parse
from xml.sax.saxutils import escape as xml_escape


def maven_proxy_settings_xml(
    egress_proxy_url: str | None,
    *,
    feed_id: str = "",
    feed_username: str = "",
    feed_token: str = "",
) -> str:
    """Maven's resolver honors NEITHER the http(s)_proxy env vars NOR the
    -Dhttps.proxyHost system properties — a `<proxies>` block in settings.xml is
    the only channel it respects. Without it every artifact download from a
    sandbox Pod dies with "Network is unreachable" (default-deny NetworkPolicy).
    nonProxyHosts mirrors NO_PROXY.

    `egress_proxy_url=None` (2026-08-19): the PREVIEW pod. Preview namespaces
    have no NetworkPolicy and no proxy env — the build goes straight out — so
    the document for them carries only `<servers>`. Injecting `<proxies>` there
    would reroute the preview build through the egress proxy as a side effect
    of a credential change.

    `feed_*` (2026-08-18): the customer's private repository. `<servers>` is,
    for the same reason as `<proxies>`, the only credential channel the
    resolver reads. `feed_id` MUST equal the `id` of the `<repository>` in the
    customer's POM — Maven matches credentials by id and silently ignores any
    other name, producing the same 403 as no credential at all. Empty = no
    `<servers>`: a tenant without a private feed gets exactly the document it
    always got.
    """
    proxies_block = ""
    if egress_proxy_url:
        parsed = urllib.parse.urlparse(egress_proxy_url)
        host, port = parsed.hostname or "", parsed.port or 8806
        non_proxy = "localhost|127.0.0.1|*.svc|*.cluster.local"
        proxies = "".join(
            f"<proxy><id>dse-egress-{scheme}</id><active>true</active>"
            f"<protocol>{scheme}</protocol><host>{host}</host><port>{port}</port>"
            f"<nonProxyHosts>{non_proxy}</nonProxyHosts></proxy>"
            for scheme in ("https", "http")
        )
        proxies_block = f"<proxies>{proxies}</proxies>"
    servers = ""
    if feed_id and feed_token:
        # `escape`: the PAT is minted by a third party and may contain `&`,
        # `<`, `>`. One of them raw would break the XML and Maven would fail
        # with a parse error — diagnosed as anything but the credential.
        servers = (
            "<servers><server>"
            f"<id>{xml_escape(feed_id)}</id>"
            f"<username>{xml_escape(feed_username or 'dse')}</username>"
            f"<password>{xml_escape(feed_token)}</password>"
            "</server></servers>"
        )
    return (
        '<settings xmlns="http://maven.apache.org/SETTINGS/1.0.0">'
        f"{servers}{proxies_block}</settings>"
    )


#: Where each credential file lands inside the build container. The preview
#: prelude copies whatever keys exist in the Secret to these destinations —
#: adding an ecosystem is adding a row, never another architecture.
CREDENTIAL_DESTINATIONS: dict[str, str] = {
    "maven-settings.xml": '"$HOME/.m2/settings.xml"',
    "npmrc": '"$HOME/.npmrc"',
}


def preview_credential_files(
    *, feed_id: str = "", feed_username: str = "", feed_token: str = ""
) -> dict[str, str]:
    """The credential files a preview namespace's Secret should carry —
    one key per ecosystem file, empty when nothing is configured (and then
    NOTHING changes anywhere: no Secret, no volume, no prelude)."""
    files: dict[str, str] = {}
    if feed_id and feed_token:
        files["maven-settings.xml"] = maven_proxy_settings_xml(
            None, feed_id=feed_id, feed_username=feed_username, feed_token=feed_token,
        )
    return files
