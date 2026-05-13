"""
Corre este script UNA VEZ en tu máquina local para obtener el refresh token.
No lo corras en Railway ni en producción.

Prerequisito: pip install google-auth-oauthlib

Cómo usarlo:
1. Ve a https://console.cloud.google.com/apis/credentials
2. Crea credenciales → ID de cliente OAuth 2.0 → Tipo: Aplicación de escritorio
3. Descarga el JSON o copia el Client ID y Client Secret
4. Corre: py scripts/get_refresh_token.py
5. Autoriza en el navegador
6. Copia GOOGLE_REFRESH_TOKEN al .env
"""
import sys


def main():
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print("Instala la dependencia: pip install google-auth-oauthlib")
        sys.exit(1)

    print("=== Obtener Refresh Token para Google Sheets ===\n")
    client_id = input("Client ID (termina en .apps.googleusercontent.com): ").strip()
    client_secret = input("Client Secret: ").strip()

    if not client_id or not client_secret:
        print("Error: Client ID y Client Secret son obligatorios.")
        sys.exit(1)

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

    print("\nAbriendo el navegador para autorizar el acceso...")
    print("Si no abre automáticamente, copia la URL que aparece en la terminal.\n")

    creds = flow.run_local_server(port=0, open_browser=True)

    print("\n" + "=" * 50)
    print("¡Listo! Copia estas líneas en tu .env:\n")
    print(f"GOOGLE_CLIENT_ID={client_id}")
    print(f"GOOGLE_CLIENT_SECRET={client_secret}")
    print(f"GOOGLE_REFRESH_TOKEN={creds.refresh_token}")
    print("=" * 50)


if __name__ == "__main__":
    main()
