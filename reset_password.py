"""Reset a user's password in the Wheelhouse database."""
import os
import sqlite3
from werkzeug.security import generate_password_hash

DB_PATH = os.path.join(os.path.dirname(__file__), 'wheelhouse.db')


def reset_password(username, new_password):
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    user = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
    if not user:
        print(f'User "{username}" not found.')
        db.close()
        return False
    pw_hash = generate_password_hash(new_password)
    db.execute('UPDATE users SET password_hash = ? WHERE username = ?', (pw_hash, username))
    db.commit()
    db.close()
    print(f'Password updated for {username}.')
    return True


if __name__ == '__main__':
    reset_password('mgiorgio@rednun.com', 'Barnie11!!')
