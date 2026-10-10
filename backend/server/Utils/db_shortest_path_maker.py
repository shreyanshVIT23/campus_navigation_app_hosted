import logging
import re
import sqlite3

import networkx as nx

from . import db_access, file_version, graph_converter, place_lookup
from .building_handling import get_building_paths
from .loader import env_variables
from .route_types import (
    BuildingRoute,
    ComplexRoute,
    Coordinates,
    FailedRoute,
    FailureCause,
    MultiBuildingRoute,
    SimpleRoute,
)
from .timing import timed

logger = logging.getLogger(__name__)


class AmbiguousValueError(ValueError):
    """The places are valid, but the map gives no way between them."""

    cause: FailureCause = "route"


class InputError(AmbiguousValueError):
    """What the user typed can't be routed; the message says how to fix it."""

    cause: FailureCause = "input"


# Most places to list when typed text matches several
MAX_LISTED = 10
WHAT_TO_TYPE = (
    "Type a room number (like 101), a teacher's name or a cabin number (like G-12)."
)

# Everything the routing engine can raise for bad input or data; these are
# reported through the result's "error" field instead of propagating.
ROUTING_ERRORS = (
    ValueError,
    RuntimeError,
    sqlite3.Error,
    nx.NetworkXException,
    LookupError,
    TypeError,
)


def error_result(message: str, cause: FailureCause) -> FailedRoute:
    return {
        "path": None,
        "total_weight": None,
        "complexity": "failed",
        "error": message,
        "cause": cause,
    }


def listing(items: list[str]) -> str:
    """ "a, b, c", or the first MAX_LISTED and how many more."""
    if len(items) <= MAX_LISTED:
        return ", ".join(items)
    return f"{', '.join(items[:MAX_LISTED])} and {len(items) - MAX_LISTED} more"


def is_valid_digit(description: str) -> bool:
    digits = "".join(filter(str.isdigit, description))
    return len(digits) > 0 and len(digits) <= 3


def floor_hint(label: str) -> int | None:
    """The floor a room number implies (its first digit), if the label has one."""
    digits = "".join(filter(str.isdigit, label))
    return int(digits[0]) if is_valid_digit(label) else None


def connector_request(text: str) -> str | None:
    """ "Lift" or "Stairs" when that, rather than a room, was typed."""
    return {"lift": "Lift", "lifts": "Lift", "stairs": "Stairs", "stair": "Stairs"}.get(
        text.strip().lower()
    )


def get_exact_node(graph, coordinates, floor):
    for node, data in graph.nodes(data=True):
        if (
            data["x"] == coordinates[0]
            and data["y"] == coordinates[1]
            and data["floor"] == floor
        ):
            return node
    logger.debug("No graph node at %s on floor %s", coordinates, floor)
    return None


@timed("nodes")
def nodes_at(graph, spots: list[Coordinates], floor) -> list:
    """Graph nodes for the given anchor coordinates (spots not in the graph are skipped)."""
    nodes = (get_exact_node(graph, spot, floor) for spot in spots)
    return [node for node in nodes if node is not None]


def interpret_path(graph, path) -> list[Coordinates]:
    return [(graph.nodes[node]["x"], graph.nodes[node]["y"]) for node in path]


@timed("dijkstra")
def search_from(graph, sources) -> tuple[dict, dict]:
    """Distances and paths from the nearest of `sources` to every reachable node."""
    lengths, paths = nx.multi_source_dijkstra(graph, set(sources), weight="weight")
    # Without a target networkx returns two dicts keyed by node
    if not isinstance(lengths, dict) or not isinstance(paths, dict):
        raise TypeError("multi_source_dijkstra without a target must return dicts")
    return lengths, paths


def shortest_between(graph, sources, targets) -> tuple[list, float]:
    """Shortest path from any source node to any target node; ([], inf) if none."""
    lengths, paths = search_from(graph, sources)
    reachable = [target for target in targets if target in lengths]
    if not reachable:
        return [], float("inf")
    best = min(reachable, key=lambda target: lengths[target])
    return paths[best], lengths[best]


def place_key(label: str) -> str:
    """One key per place: "106" for each door, "T202" for "T202(A)", "G108" for "G108-A"."""
    return re.sub(r"(\([^)]*\)|-[A-Za-z])$", "", label.strip())


@timed("resolve")
def resolve_candidates(
    db_path, description, floor: int | None, role="Location"
) -> tuple[int, list[Coordinates]]:
    """The floor and every anchor of the place the user typed.

    A room can have several doors and a toilet several sections; all of them are
    returned so the route can use the nearest. Labels of different places (e.g.
    "10" matching 101 and 102) are ambiguous.
    """
    places = (
        db_access.find_places(db_path, description, floor) if floor is not None else []
    )
    if not places:
        anywhere = db_access.find_places_anywhere(db_path, description)
        if not anywhere:
            raise InputError(
                f"{role} '{description}' not found on this building's map. "
                + WHAT_TO_TYPE
            )
        # An exact label ("Store") beats labels that contain it ("Store 03")
        wanted = description.strip().casefold()
        anywhere = [p for p in anywhere if p[0].casefold() == wanted] or anywhere
        # Prefer the floor the room number implies, else the first match's floor
        on_floor = [place for place in anywhere if place[2] == floor]
        places = on_floor or [place for place in anywhere if place[2] == anywhere[0][2]]

    labels = sorted({place_key(label) for label, _, _ in places})
    if len(labels) > 1:
        raise InputError(
            f"'{description}' matches several places: {listing(labels)}. "
            "Type the full room number or name."
        )
    spots = list(dict.fromkeys(coords for _, coords, _ in places))
    return places[0][2], spots


@timed("lift_stairs")
def get_lift_stairs(db_path, floor, graph, spots, preference) -> Coordinates:
    """The lift or stairs on `floor` with the shortest walk to any of `spots`."""
    if preference == "Lift":
        lifts_stairs = db_access.get_coordinates(db_path, "Lift %", floor)
    elif preference == "Stairs":
        lifts_stairs = db_access.get_coordinates(db_path, "Stairs %", floor)
    else:
        lifts_stairs = (db_access.get_coordinates(db_path, "Lift %", floor) or []) + (
            db_access.get_coordinates(db_path, "Stairs %", floor) or []
        )

    if not lifts_stairs:
        missing = {"Lift": "is no lift", "Stairs": "are no stairs"}.get(
            preference, "are no lifts or stairs"
        )
        raise InputError(f"There {missing} on floor {floor} of this building.")

    targets = nodes_at(graph, spots, floor)
    if not targets:
        raise AmbiguousValueError(
            f"Start point not found in the graph on floor {floor}."
        )

    # The graph is undirected, so walking from the room finds the nearest one
    path, _ = shortest_between(graph, targets, nodes_at(graph, lifts_stairs, floor))
    if not path:
        raise AmbiguousValueError("Could not find a path to lifts or stairs.")
    return interpret_path(graph, path[-1:])[0]


Connector = tuple[str, Coordinates, Coordinates]


def shared_connectors(db_path, floor_a, floor_b, preference=None) -> list[Connector]:
    """Lifts/stairs that exist under the same name on both floors.

    Floors are linked by name ("Stairs 1" on floor 2 is "Stairs 1" on floor 1). A
    name that is missing on either floor, or used more than once on a floor (e.g.
    several unnumbered "Stairs"), cannot be paired and is skipped.
    """
    kinds = (
        ("Lift",)
        if preference == "Lift"
        else ("Stairs",)
        if preference == "Stairs"
        else ("Lift", "Stairs")
    )

    def unique_by_name(floor) -> dict[str, Coordinates]:
        found: dict[str, list[Coordinates]] = {}
        for name, coords in db_access.get_connectors(db_path, floor):
            if name.startswith(kinds):
                found.setdefault(name, []).append(coords)
        return {name: spots[0] for name, spots in found.items() if len(spots) == 1}

    on_a, on_b = unique_by_name(floor_a), unique_by_name(floor_b)
    return [(name, on_a[name], on_b[name]) for name in sorted(on_a) if name in on_b]


@timed("floors")
def route_across_floors(
    db_path,
    graph_start,
    graph_end,
    start_spots: list[Coordinates],
    end_spots: list[Coordinates],
    floor_start,
    floor_end,
    preference,
) -> ComplexRoute:
    """Route via the lift/stairs giving the shortest total walk across both floors.

    The start and end may each have several anchors (doors); the nearest pair wins.
    """
    start_nodes = nodes_at(graph_start, start_spots, floor_start)
    end_nodes = nodes_at(graph_end, end_spots, floor_end)
    if not start_nodes or not end_nodes:
        raise AmbiguousValueError("Could not determine start or end node in the graph.")

    connectors = shared_connectors(db_path, floor_start, floor_end, preference)
    note = None
    if not connectors and preference:
        # The preferred kind does not link these floors at all: use the other
        other = "Stairs" if preference == "Lift" else "Lift"
        connectors = shared_connectors(db_path, floor_start, floor_end, other)
        if connectors:
            missing = "lift connects" if preference == "Lift" else "stairs connect"
            note = (
                f"No {missing} floor {floor_start} and floor {floor_end},"
                f" so this route uses the {other.lower()}."
            )
    if not connectors:
        raise AmbiguousValueError(
            f"No lifts or stairs connect floor {floor_start} and floor {floor_end}."
        )

    # One search from all start anchors and one from all end anchors covers
    # every connector (the graphs are undirected)
    to_lengths, to_paths = search_from(graph_start, start_nodes)
    from_lengths, from_paths = search_from(graph_end, end_nodes)

    best = None
    for _name, coords_start, coords_end in connectors:
        connector_start = get_exact_node(graph_start, coords_start, floor_start)
        connector_end = get_exact_node(graph_end, coords_end, floor_end)
        if (
            connector_start is None
            or connector_end is None
            or connector_start not in to_lengths
            or connector_end not in from_lengths
        ):
            continue
        total = to_lengths[connector_start] + from_lengths[connector_end]
        if best is None or total < best[0]:
            best = (total, to_paths[connector_start], from_paths[connector_end][::-1])

    if best is None:
        raise AmbiguousValueError(
            f"Could not find a path between floor {floor_start} and floor "
            f"{floor_end} via lifts or stairs."
        )

    total_weight, path_to, path_from = best
    route: ComplexRoute = {
        "path": [
            (floor_start, interpret_path(graph_start, path_to)),
            (floor_end, interpret_path(graph_end, path_from)),
        ],
        "total_weight": total_weight,
        "complexity": "complex",
        "error": None,
    }
    if note:
        route["note"] = note
    return route


def route_between(
    db_path,
    floor_start: int,
    start_spots: list[Coordinates],
    floor_end: int,
    end_spots: list[Coordinates],
    preference: str | None,
    start_name: str,
    end_name: str,
) -> SimpleRoute | ComplexRoute:
    """Shortest route from any start anchor to any end anchor."""
    if floor_start == floor_end and set(start_spots) & set(end_spots):
        raise InputError(
            f"'{start_name}' and '{end_name}' are the same place. "
            "Choose a different start or end."
        )

    graph_start = graph_converter.create_graph_from_db(db_path, floor_start)
    if floor_start != floor_end:
        graph_end = graph_converter.create_graph_from_db(db_path, floor_end)
        return route_across_floors(
            db_path,
            graph_start,
            graph_end,
            start_spots,
            end_spots,
            floor_start,
            floor_end,
            preference,
        )

    start_nodes = nodes_at(graph_start, start_spots, floor_start)
    end_nodes = nodes_at(graph_start, end_spots, floor_start)
    if not start_nodes or not end_nodes:
        raise AmbiguousValueError("Could not determine start or end node in the graph.")

    shortest_path, weight = shortest_between(graph_start, start_nodes, end_nodes)
    if not shortest_path:
        raise AmbiguousValueError(
            f"No walkway connects '{start_name}' and '{end_name}' on floor {floor_start}."
        )
    return {
        "path": (floor_start, interpret_path(graph_start, shortest_path)),
        "total_weight": weight,
        "complexity": "simple",
        "error": None,
    }


def map_label(db_path, room: str) -> str:
    """`room`, or the map's own label for it (T110 is the map's 110)."""
    alias = place_lookup.room_alias(room)
    if alias and not db_access.has_exact_label(db_path, room):
        return alias
    return room


@timed("translate")
def translate_place(db_path, text: str, teachers: list[place_lookup.Teacher]) -> str:
    """The map label for what the user typed: the text itself or a teacher's room.

    An exact map label wins (e.g. the toilet "G223"), then a cabin number ("G-12",
    "G12"), then part of a map label ("202" for "T202(A)"), then a teacher's name.
    """
    if place_lookup.is_dashed_cabin(text) or not db_access.has_exact_label(
        db_path, text
    ):
        room = place_lookup.room_for_cabin(teachers, text)
        if room:
            return map_label(db_path, room)
    if db_access.find_places_anywhere(db_path, text):
        return text

    rooms: dict[str, list[str]] = {}
    for room, names in place_lookup.rooms_for_name(teachers, text).items():
        rooms.setdefault(map_label(db_path, room), []).extend(names)
    if len(rooms) > 1:
        teachers_by_room = [
            f"{', '.join(names)} ({room})" for room, names in sorted(rooms.items())
        ]
        raise InputError(
            f"'{text}' matches teachers in several rooms: "
            f"{'; '.join(teachers_by_room)}. "
            "Type the teacher's full name or cabin number."
        )
    return next(iter(rooms)) if rooms else map_label(db_path, text)


@timed("toilet")
def toilet_route(
    db_path,
    room: str,
    kind: place_lookup.ToiletKind,
    room_is_start: bool,
    preference: str | None,
) -> SimpleRoute | ComplexRoute:
    """Shortest route between a room and the nearest toilet of the given kind.

    Toilets on the room's own floor are used when there are any; otherwise the
    nearest one on another floor, via lift or stairs.
    """
    role = "Start location" if room_is_start else "End location"
    floor, room_spots = resolve_candidates(db_path, room, floor_hint(room), role)

    toilets: dict[int, list[Coordinates]] = {}
    for toilet_floor, spot, description, extra in db_access.find_described(
        db_path, place_lookup.TOILET_MARKS
    ):
        gender = place_lookup.toilet_gender(description, extra)
        if place_lookup.serves(kind, gender):
            toilets.setdefault(toilet_floor, []).append(spot)
    kind_name = "" if kind == "any" else f"{kind} "
    if not toilets:
        raise InputError(f"There are no {kind_name}toilets in this building.")
    if set(room_spots) & set(toilets.get(floor, [])):
        raise InputError(f"'{room}' is itself a {kind_name}toilet.")
    options = {floor: toilets[floor]} if floor in toilets else toilets

    best: SimpleRoute | ComplexRoute | None = None
    last_error = ""
    for toilet_floor, spots in sorted(options.items()):
        try:
            if room_is_start:
                route = route_between(
                    db_path,
                    floor,
                    room_spots,
                    toilet_floor,
                    spots,
                    preference,
                    room,
                    "toilet",
                )
            else:
                route = route_between(
                    db_path,
                    toilet_floor,
                    spots,
                    floor,
                    room_spots,
                    preference,
                    "toilet",
                    room,
                )
        except AmbiguousValueError as e:
            last_error = str(e)
            continue
        if best is None or route["total_weight"] < best["total_weight"]:
            best = route
    if best is None:
        raise AmbiguousValueError(
            f"No {kind_name}toilet can be reached from '{room}'. {last_error}".strip()
        )
    return best


@timed("route")
@file_version.consistent_versions()
def main(
    db_path,
    start_description,
    end_description,
    preference=None,
    teacher_db=None,
) -> BuildingRoute:
    """Shortest route between two places in one building.

    A place is a map label (room "101", "T202(A)", "Store"), a teacher's cabin
    number or name, "Lift"/"Stairs" (the nearest one), or a toilet request such
    as "female toilet" (the nearest toilet of that kind).
    """
    try:
        preference = connector_request(preference) if preference else None
        teachers = place_lookup.load_teachers(
            env_variables["teacher_des"] if teacher_db is None else teacher_db
        )
        start, end = str(start_description).strip(), str(end_description).strip()
        if not start or not end:
            raise InputError("Start and end locations are both required.")

        start_connector, end_connector = (
            connector_request(start),
            connector_request(end),
        )
        start_toilet = None if start_connector else place_lookup.toilet_kind(start)
        end_toilet = None if end_connector else place_lookup.toilet_kind(end)
        if (start_connector or start_toilet) and (end_connector or end_toilet):
            raise InputError(
                f"'{start}' and '{end}' are both general places (a lift, stairs or"
                " toilet), so there is no single route between them."
                " Choose a room as the start or the end."
            )

        # Messages name what the user typed, not the room it was translated to
        start_name, end_name = start, end
        if not (start_connector or start_toilet):
            start = translate_place(db_path, start, teachers)
        if not (end_connector or end_toilet):
            end = translate_place(db_path, end, teachers)

        if start_toilet:
            return toilet_route(db_path, end, start_toilet, False, preference)
        if end_toilet:
            return toilet_route(db_path, start, end_toilet, True, preference)

        if start_connector:
            floor, end_spots = resolve_candidates(
                db_path, end, floor_hint(end), "End location"
            )
            graph = graph_converter.create_graph_from_db(db_path, floor)
            spot = get_lift_stairs(db_path, floor, graph, end_spots, start_connector)
            return route_between(
                db_path,
                floor,
                [spot],
                floor,
                end_spots,
                preference,
                start_name,
                end_name,
            )
        if end_connector:
            floor, start_spots = resolve_candidates(
                db_path, start, floor_hint(start), "Start location"
            )
            graph = graph_converter.create_graph_from_db(db_path, floor)
            spot = get_lift_stairs(db_path, floor, graph, start_spots, end_connector)
            return route_between(
                db_path,
                floor,
                start_spots,
                floor,
                [spot],
                preference,
                start_name,
                end_name,
            )

        # Every anchor of each place: rooms can have several doors
        floor_start, start_spots = resolve_candidates(
            db_path, start, floor_hint(start), "Start location"
        )
        floor_end, end_spots = resolve_candidates(
            db_path, end, floor_hint(end), "End location"
        )
        return route_between(
            db_path,
            floor_start,
            start_spots,
            floor_end,
            end_spots,
            preference,
            start_name,
            end_name,
        )

    except AmbiguousValueError as e:
        return error_result(str(e), e.cause)
    except ROUTING_ERRORS as e:
        return error_result(str(e), "internal")


def describe_entrance(db_path, entrance):
    descriptions = db_access.get_description(db_path, entrance, 0)
    if not descriptions:
        raise AmbiguousValueError(f"No description found for entrance {entrance}.")
    return descriptions[0]


def get_building_entrance_exit(db_path, building_name, reference_point):
    entrances = db_access.get_coordinates(db_path, "%Entry%", 0)
    if not entrances:
        raise AmbiguousValueError(f"No entrance found for building '{building_name}'.")

    min_weight = float("inf")
    best_entrance = None
    best_path_data: BuildingRoute | None = None
    if len(entrances) > 1:
        for entrance in entrances:
            entrance_des = describe_entrance(db_path, entrance)
            path_data = main(db_path, reference_point, entrance_des)
            if (
                path_data["complexity"] != "failed"
                and path_data["total_weight"] < min_weight
            ):
                min_weight = path_data["total_weight"]
                best_entrance = entrance
                best_path_data = path_data
    else:
        entrance_des = describe_entrance(db_path, entrances[0])
        best_path_data = main(db_path, reference_point, entrance_des)
        best_entrance = entrances[0]

    if not best_entrance or best_path_data is None:
        raise AmbiguousValueError(
            f"Could not determine the best entrance for '{building_name}'."
        )

    return best_entrance, best_path_data


def reverse_route(route: SimpleRoute | ComplexRoute) -> SimpleRoute | ComplexRoute:
    """The same walk in the opposite direction (floors and points reversed)."""
    if route["complexity"] == "simple":
        floor, points = route["path"]
        return {**route, "path": (floor, points[::-1])}
    return {
        **route,
        "path": [(floor, points[::-1]) for floor, points in reversed(route["path"])],
    }


@timed("multi_route")
@file_version.consistent_versions()
def multi_building_nav(
    db_path_1,
    building_name1: str,
    start_1: str,
    db_path_2,
    building_name2: str,
    end_2: str,
) -> MultiBuildingRoute | FailedRoute:
    try:
        _, path_building_1 = get_building_entrance_exit(
            db_path_1, building_name1, start_1
        )
        _, path_building_2 = get_building_entrance_exit(
            db_path_2, building_name2, end_2
        )

        if path_building_1["complexity"] == "failed":
            return path_building_1
        if path_building_2["complexity"] == "failed":
            return path_building_2
        # The destination leg is computed from the room to the entrance; the
        # visitor walks it the other way, so the pin must end at their room.
        path_building_2 = reverse_route(path_building_2)

        total_weight = path_building_1["total_weight"] + path_building_2["total_weight"]
        full_path: dict[str, BuildingRoute] = {
            building_name1: path_building_1,
            building_name2: path_building_2,
        }

        return {
            "path": full_path,
            "total_weight": total_weight,
            "complexity": "multi-building",
            "error": None,
        }

    except AmbiguousValueError as e:
        return error_result(str(e), e.cause)
    except ROUTING_ERRORS as e:
        return error_result(str(e), "internal")


def example():
    start_description = "001"
    end_description = "002"

    db_path_1 = get_building_paths("AB-01")[0]
    db_path_2 = get_building_paths("Lab-Complex")[0]
    result = multi_building_nav(
        db_path_1=db_path_1,
        building_name1="AB-01",
        start_1=start_description,
        db_path_2=db_path_2,
        building_name2="Lab-Complex",
        end_2=end_description,
    )
    print("Result:", result)


if __name__ == "__main__":
    example()
