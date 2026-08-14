#!/usr/bin/env python3
"""Gera o pacote do app do Teams (manifest.json + ícones) pronto para upload.

O pacote é o ÚLTIMO artefato do caminho de registro: ele só funciona depois que
existe um bot registrado (Azure Bot + app do Entra ID), porque o `botId` do
manifesto é o client id desse registro. Ver docs/TEAMS-APP-SETUP.md.

    python scripts/make_teams_app_package.py --bot-id <client-id-do-bot>

Reexecutando para atualizar um app JÁ instalado, passe o mesmo `--app-id` da
primeira vez (o `id` do manifesto é a identidade do app no tenant: mudar o id
instala um app NOVO em vez de atualizar o existente — o script imprime o id
gerado exatamente para que ele seja guardado).
"""
from __future__ import annotations

import argparse
import json
import uuid
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

#: Versão do schema do manifesto (Microsoft 365 app manifest, ago/2026).
MANIFEST_VERSION = "1.30"
SCHEMA_URL = f"https://developer.microsoft.com/json-schemas/teams/v{MANIFEST_VERSION}/MicrosoftTeams.schema.json"

_BG = (13, 27, 42)       # navy do DSE
_FG = (255, 255, 255)


def _font(size: int) -> ImageFont.ImageFont:
    for candidate in ("/System/Library/Fonts/HelveticaNeue.ttc",
                      "/System/Library/Fonts/Helvetica.ttc",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _color_icon(path: Path) -> None:
    """192x192, fundo opaco — o ícone que aparece na lista de apps."""
    img = Image.new("RGBA", (192, 192), (*_BG, 255))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((16, 16, 176, 176), radius=28, outline=(*_FG, 255), width=6)
    font = _font(64)
    draw.text((96, 98), "DSE", font=font, fill=(*_FG, 255), anchor="mm")
    img.save(path, "PNG")


def _outline_icon(path: Path) -> None:
    """32x32, FUNDO TRANSPARENTE e traço branco — exigência do Teams: qualquer
    outra cor aparece errada no rail do cliente."""
    img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((3, 3, 28, 28), radius=6, outline=(*_FG, 255), width=2)
    draw.ellipse((13, 13, 19, 19), fill=(*_FG, 255))
    img.save(path, "PNG")


def build_manifest(*, app_id: str, bot_id: str, name: str, host: str) -> dict:
    return {
        "$schema": SCHEMA_URL,
        "manifestVersion": MANIFEST_VERSION,
        "version": "1.0.0",
        "id": app_id,
        "packageName": "co.fintex.dse",
        "developer": {
            "name": "Fintex",
            "websiteUrl": f"https://{host}",
            "privacyUrl": f"https://{host}/privacy",
            "termsOfUseUrl": f"https://{host}/terms",
        },
        "name": {"short": name, "full": f"{name} — autonomous development engine"},
        "description": {
            "short": "Turns a request into a reviewed pull request.",
            "full": (
                "Mention the DSE in a channel with what you need. It plans the "
                "change, asks for approval when the risk warrants it, implements "
                "and validates it in an isolated sandbox, and opens a pull "
                "request for human review — reporting every step in the thread."
            ),
        },
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": "#0D1B2A",
        "bots": [
            {
                "botId": bot_id,
                # `team`/`groupChat` são o que o DSE usa (thread de canal);
                # `personal` deixa o chat 1:1 funcionar sem custo extra.
                "scopes": ["team", "groupChat", "personal"],
                # false: o bot RECEBE menções (o gatilho do trabalho). Marcar
                # notification-only desligaria a entrada.
                "isNotificationOnly": False,
                "supportsFiles": False,
                "commandLists": [
                    {
                        "scopes": ["team", "groupChat", "personal"],
                        "commands": [
                            {"title": "status", "description": "Show what the DSE is working on"},
                            {"title": "help", "description": "How to ask the DSE for a change"},
                        ],
                    }
                ],
            }
        ],
        "permissions": ["identity", "messageTeamMembers"],
        "validDomains": [host],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bot-id", required=True,
                        help="client id do app do Entra ID usado no Azure Bot")
    parser.add_argument("--app-id", default=None,
                        help="GUID do app no tenant (reuse o da 1a vez para ATUALIZAR)")
    parser.add_argument("--name", default="DSE")
    parser.add_argument("--host", default="dse.notas.api.br",
                        help="host público do DSE (entra em validDomains)")
    parser.add_argument("--out", default="build/teams-app")
    args = parser.parse_args()

    app_id = args.app_id or str(uuid.uuid4())
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    _color_icon(out / "color.png")
    _outline_icon(out / "outline.png")
    manifest = build_manifest(app_id=app_id, bot_id=args.bot_id,
                              name=args.name, host=args.host)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    package = out.parent / "dse-teams-app.zip"
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED) as zf:
        for member in ("manifest.json", "color.png", "outline.png"):
            zf.write(out / member, member)  # na RAIZ do zip, sem diretório

    print(f"package : {package}")
    print(f"app id  : {app_id}   <-- guarde: reusar para ATUALIZAR o app")
    print(f"bot id  : {args.bot_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
