"""Independent checks for the routing engine: map data, route validity, optimality.

The engine routes floor by floor (walk to a lift/stairs, change floor once, walk
on). This module rebuilds that model on its own from the raw tables, as a single
directed graph (start floor -> one connector -> end floor), and solves it with
one Dijkstra. A broken path, a wrong connector or a route that is not the
shortest then shows up as a mismatch with the engine's answer.
"""

import contextlib
import itertools
import math
import sqlite3
from collections import Counter
from pathlib import Path

import networkx as nx
from lxml import etree

from .place_lookup import ToiletKind, serves, toilet_gender
from .route_types import BuildingRoute, FloorPath
from .svg_manipulator import point_key

# Relative tolerance when comparing route lengths (floating point sums)
TOLERANCE = 1e-6
SVG_NS = {"svg": "http://www.w3.org/2000/svg"}
CONNECTOR_KINDS = ("Lift", "Stairs")

Anchor = tuple[int, int]  # (floor, anchor id)


def connector_kinds(preference: str | None) -> tuple[str, ...]:
    return (preference,) if preference in CONNECTOR_KINDS else CONNECTOR_KINDS


def same_length(a: float, b: float) -> bool:
    return math.isclose(a, b, rel_tol=TOLERANCE, abs_tol=TOLERANCE)


class MapData:
    """One building's map, read once from its database (read-only)."""

    def __init__(self, db_path: str | Path):
        uri = f"file:{Path(db_path).as_posix()}?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as conn:
            points = conn.execute(
                "SELECT id, x_coordinate, y_coordinate, floor FROM anchor_point_coordinates"
            ).fetchall()
            descriptions = conn.execute(
                "SELECT anchor_point_id, TRIM(COALESCE(description, '')),"
                " TRIM(COALESCE(extra_info, '')) FROM anchor_point_description"
            ).fetchall()
            connections = conn.execute(
                "SELECT point_a_id, point_b_id, distance FROM connections"
            ).fetchall()

        self.points: dict[int, tuple[float, float, int]] = {
            anchor_id: (x, y, floor) for anchor_id, x, y, floor in points
        }
        self.anchor_at: dict[tuple[int, float, float], int] = {
            (floor, x, y): anchor_id for anchor_id, (x, y, floor) in self.points.items()
        }
        self.descriptions: dict[int, str] = {}
        self.extra_info: dict[int, str] = {}
        for anchor_id, description, extra in descriptions:
            self.descriptions[anchor_id] = description
            self.extra_info[anchor_id] = extra
        self.connections = connections

        self.floor_graphs: dict[int, nx.Graph] = {}
        for anchor_id, (_, _, floor) in self.points.items():
            self.floor_graphs.setdefault(floor, nx.Graph()).add_node(anchor_id)
        for a, b, distance in connections:
            if a not in self.points or b not in self.points:
                continue
            floor_a, floor_b = self.points[a][2], self.points[b][2]
            if floor_a != floor_b or distance is None:
                continue
            graph = self.floor_graphs[floor_a]
            # Keep the shortest of duplicate walkways between the same anchors
            if not graph.has_edge(a, b) or distance < graph[a][b]["weight"]:
                graph.add_edge(a, b, weight=distance)

        # floor -> connector name -> anchor, for names used once on that floor
        self.connectors: dict[int, dict[str, int]] = {}
        names_per_floor: dict[int, Counter[str]] = {}
        for anchor_id, description in self.descriptions.items():
            if anchor_id in self.points and description.startswith(CONNECTOR_KINDS):
                floor = self.points[anchor_id][2]
                names_per_floor.setdefault(floor, Counter())[description] += 1
        for anchor_id, description in self.descriptions.items():
            if anchor_id not in self.points:
                continue
            floor = self.points[anchor_id][2]
            if names_per_floor.get(floor, Counter())[description] == 1:
                self.connectors.setdefault(floor, {})[description] = anchor_id

        self._layered: dict[tuple[int, int, tuple[str, ...]], nx.DiGraph] = {}
        self._lengths: dict[tuple[tuple[int, ...], int, int, str | None], dict] = {}

    # ---- What a typed label means ----

    def anchors_of(self, label: str) -> list[Anchor]:
        """Anchors whose description or extra info is exactly `label`."""
        wanted = label.strip()
        return sorted(
            (self.points[anchor_id][2], anchor_id)
            for anchor_id in self.descriptions
            if anchor_id in self.points
            and wanted in (self.descriptions[anchor_id], self.extra_info[anchor_id])
        )

    # ---- Reference shortest length ----

    def kinds_for(
        self, floor_a: int, floor_b: int, preference: str | None
    ) -> tuple[str, ...]:
        """Connector kinds a route may use between two floors: the preferred kind,
        or the other kind when the preferred one does not link the floors at all."""
        kinds = connector_kinds(preference)
        on_b = self.connectors.get(floor_b, {})
        linked = any(
            name.startswith(kinds) and name in on_b
            for name in self.connectors.get(floor_a, {})
        )
        if kinds == CONNECTOR_KINDS or linked:
            return kinds
        return tuple(kind for kind in CONNECTOR_KINDS if kind not in kinds)

    def layered_graph(self, floor_a: int, floor_b: int, kinds: tuple[str, ...]):
        """Floor A, then exactly one same-named lift/stairs, then floor B."""
        key = (floor_a, floor_b, kinds)
        if key not in self._layered:
            graph = nx.DiGraph()
            for side, floor in (("a", floor_a), ("b", floor_b)):
                for u, v, weight in self.floor_graphs[floor].edges(data="weight"):
                    graph.add_edge((side, u), (side, v), weight=weight)
                    graph.add_edge((side, v), (side, u), weight=weight)
            on_a = self.connectors.get(floor_a, {})
            on_b = self.connectors.get(floor_b, {})
            for name, anchor_a in on_a.items():
                if name.startswith(kinds) and name in on_b:
                    graph.add_edge(("a", anchor_a), ("b", on_b[name]), weight=0.0)
            self._layered[key] = graph
        return self._layered[key]

    def shortest_length(
        self, starts: list[Anchor], ends: list[Anchor], preference: str | None
    ) -> float:
        """Shortest walk from any start anchor to any end anchor (inf if none)."""
        best = math.inf
        for start_floor in sorted({floor for floor, _ in starts}):
            sources = [a for floor, a in starts if floor == start_floor]
            for end_floor in sorted({floor for floor, _ in ends}):
                targets = [a for floor, a in ends if floor == end_floor]
                lengths = self._lengths_from(
                    tuple(sorted(sources)), start_floor, end_floor, preference
                )
                side = "a" if start_floor != end_floor else None
                for anchor in targets:
                    node = ("b", anchor) if side else anchor
                    if node in lengths:
                        best = min(best, lengths[node])
        return best

    def _lengths_from(
        self,
        sources: tuple[int, ...],
        start_floor: int,
        end_floor: int,
        preference: str | None,
    ) -> dict:
        """Distances from the sources to every reachable node (cached per input)."""
        key = (sources, start_floor, end_floor, preference)
        if key not in self._lengths:
            if start_floor == end_floor:
                graph = self.floor_graphs[start_floor]
                source_nodes: list = list(sources)
            else:
                graph = self.layered_graph(
                    start_floor,
                    end_floor,
                    self.kinds_for(start_floor, end_floor, preference),
                )
                source_nodes = [("a", a) for a in sources]
            source_nodes = [n for n in source_nodes if n in graph]
            self._lengths[key] = (
                dict(
                    nx.multi_source_dijkstra_path_length(
                        graph, set(source_nodes), weight="weight"
                    )
                )
                if source_nodes
                else {}
            )
        return self._lengths[key]

    def toilet_anchors(self, kind: ToiletKind) -> list[Anchor]:
        return sorted(
            (self.points[anchor_id][2], anchor_id)
            for anchor_id, description in self.descriptions.items()
            if anchor_id in self.points
            and serves(kind, toilet_gender(description, self.extra_info[anchor_id]))
        )

    def toilet_length(
        self,
        room: list[Anchor],
        kind: ToiletKind,
        preference: str | None,
        room_is_start: bool,
    ) -> float:
        """Shortest walk between a room and a toilet of `kind`.

        Toilets on the room's floor count when there are any; otherwise all floors.
        """
        toilets = self.toilet_anchors(kind)
        room_floors = {floor for floor, _ in room}
        same_floor = [t for t in toilets if t[0] in room_floors]
        options = same_floor or toilets
        if room_is_start:
            return self.shortest_length(room, options, preference)
        return self.shortest_length(options, room, preference)

    # ---- Validity of a route returned by the engine ----

    def route_problems(
        self,
        route: BuildingRoute,
        starts: list[Anchor],
        ends: list[Anchor],
        preference: str | None,
    ) -> list[str]:
        """Everything wrong with a successful route; an empty list means valid."""
        if route["complexity"] == "failed":
            return ["route failed"]
        legs: list[FloorPath] = (
            [route["path"]] if route["complexity"] == "simple" else route["path"]
        )
        if len(legs) not in (1, 2):
            return [f"route has {len(legs)} floor legs"]
        if len(legs) == 2 and legs[0][0] == legs[1][0]:
            return ["both legs are on the same floor"]

        problems: list[str] = []
        walked = 0.0
        leg_nodes: list[list[int]] = []
        for floor, points in legs:
            if not points:
                problems.append(f"empty leg on floor {floor}")
                leg_nodes.append([])
                continue
            nodes = []
            for x, y in points:
                node = self.anchor_at.get((floor, x, y))
                if node is None:
                    problems.append(
                        f"point ({x}, {y}) is not an anchor on floor {floor}"
                    )
                    break
                nodes.append(node)
            leg_nodes.append(nodes)
            graph = self.floor_graphs.get(floor, nx.Graph())
            for u, v in itertools.pairwise(nodes):
                if not graph.has_edge(u, v):
                    problems.append(f"no walkway between anchors {u} and {v}")
                else:
                    walked += graph[u][v]["weight"]
        if problems:
            return problems

        first_floor, last_floor = legs[0][0], legs[-1][0]
        if (first_floor, leg_nodes[0][0]) not in starts:
            problems.append("does not start at the start room")
        if (last_floor, leg_nodes[-1][-1]) not in ends:
            problems.append("does not end at the end room")

        if len(legs) == 2:
            up, down = leg_nodes[0][-1], leg_nodes[1][0]
            name_up = self.descriptions.get(up, "")
            name_down = self.descriptions.get(down, "")
            if self.connectors.get(first_floor, {}).get(name_up) != up:
                problems.append(
                    f"leg 1 does not end at a usable lift/stairs ({name_up!r})"
                )
            elif name_up != name_down:
                problems.append(f"changes floor from {name_up!r} to {name_down!r}")
            elif not name_up.startswith(
                self.kinds_for(first_floor, legs[1][0], preference)
            ):
                problems.append(f"uses {name_up!r} against preference {preference!r}")

        if not same_length(walked, route["total_weight"]):
            problems.append(
                f"reported length {route['total_weight']:.3f} but the path walks {walked:.3f}"
            )
        return problems

    # ---- The route as drawn on the floor maps ----

    @staticmethod
    def drawn_problems(svg_file: str | Path, points: list) -> list[str]:
        """What is wrong with one floor leg as drawn by svg_manipulator.

        Every hop of the route must be a visible red walkway line, nothing else
        may be red, and the destination arrow/pin must be there.
        """
        root = etree.parse(str(svg_file)).getroot()
        group = root.find(".//svg:g[@id='Path']", namespaces=SVG_NS)
        if group is None:
            return ["the floor map has no walkway group"]

        def segment(a, b) -> frozenset:
            return frozenset((point_key(*a), point_key(*b)))

        shown = {
            segment((line.get("x1"), line.get("y1")), (line.get("x2"), line.get("y2")))
            for line in group.xpath(".//svg:line", namespaces=SVG_NS)
            if line.get("visibility") == "visible" and line.get("stroke") == "red"
        }
        wanted = {segment(a, b) for a, b in itertools.pairwise(points)}
        problems = []
        if wanted - shown:
            problems.append(f"{len(wanted - shown)} route hops are not drawn")
        if shown - wanted:
            problems.append(
                f"{len(shown - wanted)} walkways are red but not on the route"
            )
        if not root.xpath("//svg:marker", namespaces=SVG_NS):
            problems.append("no destination arrow")
        return problems

    # ---- Map data ----

    def data_problems(self) -> list[str]:
        """Problems in the map tables that can break routing."""
        problems: list[str] = []
        seen: dict[frozenset[int], float] = {}
        for a, b, distance in self.connections:
            if a not in self.points or b not in self.points:
                problems.append(f"connection {a}-{b} refers to a missing anchor")
                continue
            if a == b:
                problems.append(f"connection {a}-{b} is a loop")
            if self.points[a][2] != self.points[b][2]:
                problems.append(f"connection {a}-{b} joins two floors")
            if distance is None or distance <= 0:
                problems.append(f"connection {a}-{b} has distance {distance}")
                continue
            straight = math.dist(self.points[a][:2], self.points[b][:2])
            if not math.isclose(distance, straight, rel_tol=0.01, abs_tol=0.01):
                problems.append(
                    f"connection {a}-{b} is {distance} long but the anchors are"
                    f" {straight:.2f} apart"
                )
            pair = frozenset((a, b))
            if pair in seen and seen[pair] != distance:
                problems.append(
                    f"connection {a}-{b} is stored twice with other lengths"
                )
            seen[pair] = distance

        if len(self.anchor_at) != len(self.points):
            problems.append("two anchors share the same coordinates on a floor")

        for floor, graph in sorted(self.floor_graphs.items()):
            labelled = [
                n
                for n in graph
                if self.descriptions.get(n)
                and self.descriptions[n] != "Unique anchor point"
            ]
            if not labelled:
                continue
            main = max(nx.connected_components(graph), key=len)
            for node in labelled:
                if node not in main:
                    problems.append(
                        f"floor {floor}: {self.descriptions[node]!r} (anchor {node})"
                        " is cut off from the main walkway"
                    )

        floors_by_name: dict[str, list[int]] = {}
        for floor, names in self.connectors.items():
            for name in names:
                floors_by_name.setdefault(name, []).append(floor)
        for anchor_id, description in sorted(self.descriptions.items()):
            if anchor_id not in self.points or not description.startswith(
                CONNECTOR_KINDS
            ):
                continue
            floor = self.points[anchor_id][2]
            if self.connectors.get(floor, {}).get(description) != anchor_id:
                problems.append(
                    f"floor {floor}: {description!r} is used more than once, so it"
                    " cannot link floors"
                )
            elif len(floors_by_name[description]) == 1:
                problems.append(
                    f"floor {floor}: {description!r} exists on no other floor"
                )
        return problems
