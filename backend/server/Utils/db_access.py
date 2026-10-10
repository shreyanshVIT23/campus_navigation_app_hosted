import sqlite3

from typing_extensions import TypedDict


class TeacherRecord(TypedDict):
    name: str
    cabin_no: str | None
    room_no: str | None
    phone_number: str | None


class NewTeacher(TypedDict):
    name: str
    cabin_no: str
    room_no: str
    phone_number: str | None


def get_coordinates(db_path, description_condition, floor_condition):

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            query = """
                SELECT apc.x_coordinate, apc.y_coordinate
                FROM anchor_point_coordinates apc
                JOIN anchor_point_description apd ON apc.id = apd.anchor_point_id
                WHERE TRIM(apd.description) LIKE TRIM(?||'%') AND apc.floor = ?
            """
            cursor.execute(query, (description_condition, floor_condition))
            return cursor.fetchall()
    except sqlite3.Error as e:
        print(f"Database error: {e}")
    return None


Place = tuple[str, tuple[float, float], int]


def find_places(db_path, label, floor) -> list[Place]:
    """Anchors on a floor labelled `label`, as (label, (x, y), floor).

    An exact label wins; only when there is none are labels starting with
    `label` returned, so "120" never also matches "120A".
    """
    # sqlite3.Error propagates to the routing engine, which reports it
    select = """
        SELECT TRIM(apd.description), apc.x_coordinate, apc.y_coordinate, apc.floor
        FROM anchor_point_coordinates apc
        JOIN anchor_point_description apd ON apc.id = apd.anchor_point_id
        WHERE apc.floor = ? AND
    """
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            select + "TRIM(apd.description) = TRIM(?) COLLATE NOCASE", (floor, label)
        ).fetchall()
        if not rows:
            rows = conn.execute(
                select + "TRIM(apd.description) LIKE TRIM(?) || '%'", (floor, label)
            ).fetchall()
    return [(description, (x, y), row_floor) for description, x, y, row_floor in rows]


def has_exact_label(db_path, label) -> bool:
    """Whether some anchor's description or extra info is exactly `label`."""
    # sqlite3.Error propagates to the routing engine, which reports it
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM anchor_point_description
            WHERE TRIM(description) = TRIM(?) COLLATE NOCASE
               OR TRIM(extra_info) = TRIM(?) COLLATE NOCASE
            LIMIT 1
            """,
            (label, label),
        ).fetchone()
    return row is not None


def find_described(
    db_path, descriptions
) -> list[tuple[int, tuple[float, float], str, str]]:
    """(floor, (x, y), description, extra info) of every anchor whose description
    is one of `descriptions`."""
    wanted = list(descriptions)
    placeholders = ", ".join("?" for _ in wanted)
    # sqlite3.Error propagates to the routing engine, which reports it
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT apc.floor, apc.x_coordinate, apc.y_coordinate,
                   TRIM(apd.description), TRIM(COALESCE(apd.extra_info, ''))
            FROM anchor_point_coordinates apc
            JOIN anchor_point_description apd ON apc.id = apd.anchor_point_id
            WHERE TRIM(apd.description) IN ({placeholders})
            ORDER BY apd.id
            """,
            wanted,
        ).fetchall()
    return [
        (floor, (x, y), description, extra) for floor, x, y, description, extra in rows
    ]


def find_places_anywhere(db_path, label) -> list[Place]:
    """Anchors on any floor whose description or extra info contains `label`.

    The returned label is the extra info when that is what matched, e.g.
    "T202(A)" for a toilet stored as description "T".
    """
    # sqlite3.Error propagates to the routing engine, which reports it
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT TRIM(apd.description), TRIM(COALESCE(apd.extra_info, '')),
                   apc.x_coordinate, apc.y_coordinate, apc.floor
            FROM anchor_point_coordinates apc
            JOIN anchor_point_description apd ON apc.id = apd.anchor_point_id
            WHERE apd.description LIKE '%' || ? || '%'
               OR apd.extra_info LIKE '%' || ? || '%'
            ORDER BY apd.id
            """,
            (label, label),
        ).fetchall()
    needle = label.strip().lower()
    return [
        (description if needle in description.lower() else extra, (x, y), floor)
        for description, extra, x, y, floor in rows
    ]


def get_description(db_path, coordinates, floor_condition):

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            query = """
                SELECT apd.description
                FROM anchor_point_coordinates apc
                JOIN anchor_point_description apd ON apc.id = apd.anchor_point_id
                WHERE apc.x_coordinate = ? AND apc.y_coordinate = ? AND apc.floor = ?
            """
            cursor.execute(query, (*coordinates, floor_condition))
            return [row[0] for row in cursor.fetchall()]
    except sqlite3.Error as e:
        print(f"Database error: {e}")
    return None


def get_teacher_data(
    db_path: str,
    name: str | None = None,
    cabin_no: str | None = None,
    room_no: str | None = None,
) -> list[TeacherRecord]:

    query = "SELECT name, cabin_no, room_no, phone_number FROM teachers WHERE 1=1"
    params = []

    if name:
        query += " AND name LIKE ?"
        params.append(f"%{name}%")
    if cabin_no:
        query += " AND cabin_no = ?"
        params.append(cabin_no)
    if room_no:
        query += " AND room_no = ?"
        params.append(room_no)

    # sqlite3.Error propagates to the caller (route_utilary.retrieve_teachers).
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        result = cursor.fetchall()

    return [
        {
            "name": row[0],
            "cabin_no": row[1],
            "room_no": row[2],
            "phone_number": row[3],
        }
        for row in result
    ]


def add_teacher_to_db(db_path: str, data: NewTeacher):

    # sqlite3.Error propagates to the caller (route_utilary.add_teacher).
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO teachers (name, cabin_no, room_no, phone_number)
            VALUES (?, ?, ?, ?);
        """,
            (data["name"], data["cabin_no"], data["room_no"], data["phone_number"]),
        )
        conn.commit()


def get_password_users(db_path, username):

    try:
        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()

            query = "SELECT password FROM users WHERE username = ?"
            cursor.execute(query, (username,))
            result = cursor.fetchone()

            return result[0] if result else None

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None


def check():
    def fetch_data(db_path, table_name):
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(f"SELECT * FROM {table_name}")
        data = cursor.fetchall()
        conn.close()
        return data

    def update_description(db1_data, db2_path, table_name):
        conn = sqlite3.connect(db2_path)
        cursor = conn.cursor()

        db1_dict = {row[1]: row[2] for row in db1_data}

        cursor.execute(f"SELECT * FROM {table_name}")
        db2_data = cursor.fetchall()

        for row in db2_data:
            _, db2_id, db2_name, db2_description, _ = row
            if db2_name in db1_dict and db2_description != db1_dict[db2_name]:
                cursor.execute(
                    f"UPDATE {table_name} SET description = ? WHERE id = ?",
                    (db1_dict[db2_name], db2_id),
                )

        conn.commit()
        conn.close()

    def find_rows_not_in_db1(db1_data, db2_data):

        db1_set = {tuple(row) for row in db1_data}
        db2_set = {tuple(row) for row in db2_data}
        return list(db2_set - db1_set)

    db1_path = r"DB\latest_db_1.db"
    db2_path = r"DB\latest_db_2.db"
    table_name = "anchor_point_description"

    try:
        db1_data = fetch_data(db1_path, table_name)
        db2_data = fetch_data(db2_path, table_name)

        update_description(db1_data, db2_path, table_name)

        db2_data = fetch_data(db2_path, table_name)

        rows_in_db2_not_in_db1 = find_rows_not_in_db1(db1_data, db2_data)

        print("Rows in DB2 but not in DB1:")
        for row in rows_in_db2_not_in_db1:
            print(row)
    except sqlite3.OperationalError as e:
        print(f"Database error: {e}")


if __name__ == "__main__":
    check()


def get_connectors(db_path, floor) -> list[tuple[str, tuple[float, float]]]:
    """Lifts and stairs on a floor as (trimmed description, (x, y))."""
    # sqlite3.Error propagates to the routing engine, which reports it
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT TRIM(apd.description), apc.x_coordinate, apc.y_coordinate
            FROM anchor_point_coordinates apc
            JOIN anchor_point_description apd ON apc.id = apd.anchor_point_id
            WHERE apc.floor = ?
              AND (TRIM(apd.description) LIKE 'Lift%'
                   OR TRIM(apd.description) LIKE 'Stairs%')
            """,
            (floor,),
        ).fetchall()
    return [(description, (x, y)) for description, x, y in rows]
