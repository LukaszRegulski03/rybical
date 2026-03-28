"""
Generate a bcrypt password hash to paste into config/users.toml.

Usage:
    python scripts/hash_password.py
"""
import getpass
import bcrypt


def main():
    password = getpass.getpass("Enter password: ")
    confirm  = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords do not match.")
        return
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12))
    print("\nPaste this as password_hash in config/users.toml:\n")
    print(hashed.decode())


if __name__ == "__main__":
    main()
