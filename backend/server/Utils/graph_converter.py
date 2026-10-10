import logging
import sqlite3

import networkx as nx

from . import file_version
from .building_handling import get_building_paths
from .timing import timed

logger = logging.getLogger(__name__)


def create_graph_from_svg_data(
    anchor_points: list[tuple[float, float]],
    connections: list[tuple[tuple[tuple[float, float], tuple[float, float]], float]],
) -> nx.Graph:

    G = nx.Graph()

    for point in anchor_points:
        G.add_node(point)

    for connection in connections:
        (point1, point2), weight = connection
        G.add_edge(point1, point2, weight=weight)

    return G


def is_graph_connected(db_path, floor):

    G = create_graph_from_db(db_path, floor)

    return nx.is_connected(G)


def check_graph_connectivity(db_path, floor):

    G = create_graph_from_db(db_path, floor)

    if nx.is_connected(G):
        return True, []

    disconnected_components = list(nx.connected_components(G))
    return False, disconnected_components


@timed("graph")
@file_version.cached_by_file("graph", maxsize=64, copy_result=False)
def create_graph_from_db(db_path, floor):
    """The walkway graph of one floor; cached and frozen (nothing may change it)."""

    conn = sqlite3.connect(str(db_path))
    cursor = conn.cursor()

    try:
        cursor.execute(
            """
            SELECT id, x_coordinate, y_coordinate, floor 
            FROM anchor_point_coordinates 
            WHERE floor = ?
        """,
            (floor,),
        )
        nodes = cursor.fetchall()

        cursor.execute(
            """
            SELECT point_a_id, point_b_id, distance 
            FROM connections 
            WHERE point_a_id IN (SELECT id FROM anchor_point_coordinates WHERE floor = ?)
              AND point_b_id IN (SELECT id FROM anchor_point_coordinates WHERE floor = ?)
        """,
            (floor, floor),
        )
        edges = cursor.fetchall()

        G = nx.Graph()

        for node in nodes:
            node_id, x, y, node_floor = node
            G.add_node(node_id, x=x, y=y, floor=node_floor)

        for edge in edges:
            point_a_id, point_b_id, distance = edge
            G.add_edge(point_a_id, point_b_id, weight=distance)

        # Shared by every request: adding or removing nodes/edges now raises
        return nx.freeze(G)
    except sqlite3.Error as e:
        raise RuntimeError(f"Database error: {e}")
    finally:
        conn.close()


def find_shortest_path(
    graph: nx.Graph, start: tuple[float, float], end: tuple[float, float]
) -> tuple[list[tuple[float, float]], float]:

    try:
        path = nx.dijkstra_path(graph, source=start, target=end, weight="weight")

        total_weight = nx.dijkstra_path_length(
            graph, source=start, target=end, weight="weight"
        )
        return path, total_weight
    except nx.NetworkXNoPath:
        logger.debug("No path found between %s and %s", start, end)

        return [], float("inf")


def example():

    db_path = get_building_paths("Lab-Complex")[0]
    floor = "0"
    is_connected, disconnected_components = check_graph_connectivity(db_path, floor)

    if is_connected:
        print("The graph is connected.")
    else:
        print("The graph is not connected.")
        print("Disconnected components:")
        for i, component in enumerate(disconnected_components, start=1):
            print(f"Component {i}: {component}")


if __name__ == "__main__":
    example()
