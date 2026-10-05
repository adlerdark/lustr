# This file is: ./reset_password.py
"""
Forgot the password? Set a new one from the server and log every browser out:

    docker exec -it lustr python3 reset_password.py

(`lustr` is the container's name.) Works while lustr is running; no restart needed.
"""
import getpass
import sys

import auth_sessions
from config import DATABASE_FILE
from model import UserModel

MIN_PASSWORD = 8


def reset(username: str, password: str, users: UserModel = None) -> int:
    """Set the password and end all sessions; returns how many sessions ended."""
    users = users or UserModel()
    users.set_password(username, password).join()
    auth_sessions.set_db_path(DATABASE_FILE)
    return auth_sessions.end_all()


def main():
    users = UserModel()
    if not users.users:
        print("There is no account yet: open lustr in a browser and create one.")
        return 1
    if len(sys.argv) > 1:
        username = sys.argv[1].lower().strip()
    elif len(users.users) == 1:
        username = next(iter(users.users))
    else:
        username = input(f"Account ({', '.join(users.users)}): ").lower().strip()
    if username not in users.users:
        print(f"No account called '{username}'.")
        return 1
    print(f"New password for '{username}' (at least {MIN_PASSWORD} characters).")
    password = getpass.getpass("New password: ")
    if len(password) < MIN_PASSWORD:
        print("Too short; nothing changed.")
        return 1
    if getpass.getpass("Again: ") != password:
        print("The two passwords differ; nothing changed.")
        return 1
    ended = reset(username, password, users)
    print(f"Password changed. {ended} browser session{'' if ended == 1 else 's'} logged out.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
