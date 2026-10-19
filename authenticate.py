import getpass
from pyicloud import PyiCloudService
from sync_engine import APPLE_ID, COOKIE_DIR, CONFIG_FILE, save_setting

apple_id = input(f"Apple ID [{APPLE_ID}]: " if APPLE_ID else "Apple ID (email): ").strip() or APPLE_ID
if not apple_id:
    print("An Apple ID is required.")
    exit(1)

password = getpass.getpass("Enter your Apple ID password: ")

print("Connecting to iCloud...")
api = PyiCloudService(apple_id, password, cookie_directory=COOKIE_DIR)

if api.requires_2fa:
    print("Two-factor authentication required.")
    code = input("Enter the 2FA code sent to your device: ")
    result = api.validate_2fa_code(code)
    if result:
        print("2FA authentication successful!")
    else:
        print("Failed to verify 2FA code.")
        exit(1)

    if not api.is_trusted_session:
        print("Trusting this session...")
        api.trust_session()
        print("Session trusted.")

save_setting("ICLOUD_APPLE_ID", apple_id)
print(f"Authentication complete. Session saved; Apple ID stored in {CONFIG_FILE}.")
print(f"iCloud Drive folders: {api.drive.root.dir()}")
