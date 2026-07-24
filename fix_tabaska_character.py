from __future__ import annotations

import os
import sqlite3
import sys


DB_PATH = os.getenv("HC_DB_PATH", "hc_players.db")

WRONG_CHARACTER_NAME = "tabaksa"

CORRECT_DISCORD_USER_ID = "1525735661359861933"
CORRECT_DISPLAY_NAME = "Gestaz/Tabaska"
CORRECT_CHARACTER_NAME = "tabaska"

REALM = "defias-pillager"


def main() -> int:
    if not os.path.exists(DB_PATH):
        print(f"Database not found: {DB_PATH}")
        print("Set HC_DB_PATH to the correct database file and try again.")
        return 1

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        matches = conn.execute(
            """
            SELECT
                id,
                discord_user_id,
                discord_display_name,
                character_name,
                realm,
                active,
                last_level
            FROM hc_players
            WHERE LOWER(character_name) = LOWER(?)
              AND LOWER(realm) = LOWER(?);
            """,
            (
                WRONG_CHARACTER_NAME,
                REALM,
            ),
        ).fetchall()

        if not matches:
            print(
                "No matching row found for:\n"
                f"  Character: {WRONG_CHARACTER_NAME}\n"
                f"  Realm: {REALM}"
            )
            return 1

        if len(matches) > 1:
            print(f"Refusing to update: found {len(matches)} matching rows.")
            for row in matches:
                print(dict(row))
            return 1

        row = matches[0]

        duplicate = conn.execute(
            """
            SELECT
                id,
                discord_user_id,
                discord_display_name,
                character_name,
                realm
            FROM hc_players
            WHERE LOWER(character_name) = LOWER(?)
              AND id != ?
              AND active = 1
            LIMIT 1;
            """,
            (
                CORRECT_CHARACTER_NAME,
                row["id"],
            ),
        ).fetchone()

        if duplicate:
            print("Refusing to update because the corrected character is already active:")
            print(dict(duplicate))
            return 1

        print("Current row:")
        print(dict(row))

        conn.execute(
            """
            UPDATE hc_players
            SET discord_user_id = ?,
                discord_display_name = ?,
                character_name = ?,
                realm = ?,
                updated_at = datetime('now')
            WHERE id = ?;
            """,
            (
                CORRECT_DISCORD_USER_ID,
                CORRECT_DISPLAY_NAME,
                CORRECT_CHARACTER_NAME,
                REALM,
                row["id"],
            ),
        )

        updated = conn.execute(
            """
            SELECT
                id,
                discord_user_id,
                discord_display_name,
                character_name,
                realm,
                active,
                last_level
            FROM hc_players
            WHERE id = ?;
            """,
            (row["id"],),
        ).fetchone()

        print("\nUpdated row:")
        print(dict(updated))

    print("\nCorrection complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
