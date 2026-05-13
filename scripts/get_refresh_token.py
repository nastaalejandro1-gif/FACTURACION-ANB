"""
Corre este script UNA VEZ en tu máquina local para obtener el refresh token.
Lee GOOGLE_CLIENT_ID y GOOGLE_CLIENT_SECRET del .env automáticamente.

Uso:
    py scripts/get_refresh_token.py
"""
import os
import sys
from pathlib import Path


def load_env():
    env_path = Path(__file__).parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def main():
    load_env()

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Instala la dependencia: pip install google-auth-oauthlib")
        sys.exit(1)

    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()

    if not client_id or not client_secret:
        print("Error: GOOGLE_CLIENT_ID y GOOGLE_CLIENT_SECRET deben estar en el .env")
        sys.exit(1)

    print(f"Usando Client ID: {client_id[:30]}...")
    print("\nAbriendo el navegador para autorizar el acceso a Google Sheets...")
    print("Inicia sesión con la cuenta de Google que tiene acceso al Spreadsheet.\n")

    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uris": ["http://localhost"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )

    creds = flow.run_local_server(port=0, open_browser=True)

    refresh_token = creds.refresh_token
    env_path = Path(__file__).parent.parent / ".env"

    # Write refresh token directly into .env
    content = env_path.read_text()
    if "GOOGLE_REFRESH_TOKEN=" in content:
        lines = content.splitlines()
        new_lines = []
        for line in lines:
            if line.startswith("GOOGLE_REFRESH_TOKEN="):
                new_lines.append(f"GOOGLE_REFRESH_TOKEN={refresh_token}")
            else:
                new_lines.append(line)
        env_path.write_text("\n".join(new_lines) + "\n")
        print(f"\nListo! Refresh token guardado en .env automaticamente.")
    else:
        print(f"\nCopia esto en tu .env:\nGOOGLE_REFRESH_TOKEN={refresh_token}")


if __name__ == "__main__":
    main()
