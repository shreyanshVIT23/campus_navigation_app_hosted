import base64
import functools
import itertools
import os
from pathlib import Path
from typing import NamedTuple

from lxml import etree as ET

from . import db_shortest_path_maker, file_version, loader, route_maps
from .building_handling import get_building_paths
from .route_types import BuildingRoute, FailedRoute, FloorPath, MultiBuildingRoute
from .timing import timed

SVG_NAMESPACE = "http://www.w3.org/2000/svg"
NS = {"svg": SVG_NAMESPACE}
PIN_IMAGE = "location_symbol-removebg.png"

Point = tuple[float, float]
Segment = tuple[Point, Point]


@functools.cache
def pin_image_data_uri() -> str | None:
    """The destination pin as a data URI, so it renders wherever the SVG is shown.

    A filesystem path would not load in the browser (iframe or inline SVG).
    """
    pin = Path(loader.env_variables["image_assets"]) / PIN_IMAGE
    try:
        data = pin.read_bytes()
    except OSError:
        return None
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


def point_key(x, y) -> Point:
    # SVG attributes and DB coordinates round-trip through text; compare at 1e-3
    return (round(float(x), 3), round(float(y), 3))


def route_segments(path_points) -> set[Segment]:
    """Walkway segments between consecutive route points, in both directions."""
    segments: set[Segment] = set()
    for (x1, y1), (x2, y2) in itertools.pairwise(path_points):
        a, b = point_key(x1, y1), point_key(x2, y2)
        segments.add((a, b))
        segments.add((b, a))
    return segments


class FloorTemplate(NamedTuple):
    # The floor map with walkway polylines split into lines, and every walkway
    # already hidden and unstyled: a request only has to show its route's lines
    svg: bytes
    # Walkway line -> its position among the walkway <line>s, in document order.
    # Shared by all requests: never modified.
    lines_by_segment: dict[Segment, list[int]]


def walkway_group(root):
    return root.find(".//svg:g[@id='Path']", namespaces=NS)


def walkway_lines(path_group) -> list:
    return path_group.xpath(".//svg:line", namespaces=NS)


def line_segment(line) -> Segment:
    try:
        return (
            point_key(line.get("x1"), line.get("y1")),
            point_key(line.get("x2"), line.get("y2")),
        )
    except (ValueError, TypeError) as e:
        raise ValueError(
            "Invalid line coordinates: 'x1', 'y1', 'x2', and 'y2' must be valid numbers."
        ) from e


@file_version.cached_by_file("svg_template", maxsize=32, copy_result=False)
def floor_template(svg_file: str) -> FloorTemplate:
    """The floor map, ready to draw a route on; built once per file version.

    Kept as bytes, not a tree: each request parses its own copy (lxml frees the
    GIL while parsing, and trees are never shared between threads, as the lxml
    FAQ advises).
    """
    root = ET.parse(svg_file, ET.XMLParser(remove_blank_text=True)).getroot()
    split_polylines(root)
    lines_by_segment: dict[Segment, list[int]] = {}
    path_group = walkway_group(root)
    if path_group is not None:
        # The hiding class sits on the group and on walkways at every depth
        for element in path_group.iter():
            if isinstance(element.tag, str) and "class" in element.attrib:
                del element.attrib["class"]
        for position, line in enumerate(walkway_lines(path_group)):
            line.set("visibility", "hidden")
            lines_by_segment.setdefault(line_segment(line), []).append(position)
        # Curved/decorative walkway shapes are not part of the routing graph
        for shape in path_group.xpath(".//svg:path", namespaces=NS):
            shape.set("visibility", "hidden")
    return FloorTemplate(ET.tostring(root), lines_by_segment)


@timed("svg")
def render_floor(svg_file: str, path_points) -> bytes:
    """The floor map with the route drawn on it, as SVG bytes (no file is written)."""
    with timed("svg.template"):
        template = floor_template(svg_file)
    with timed("svg.parse"):
        root = ET.fromstring(template.svg, ET.XMLParser(remove_blank_text=True))
    with timed("svg.draw"):
        draw_route(root, path_points, template.lines_by_segment)
    with timed("svg.serialize"):
        return ET.tostring(
            root.getroottree(),
            pretty_print=True,
            xml_declaration=True,
            encoding="utf-8",
        )


def draw_route(root, path_points, lines_by_segment: dict[Segment, list[int]]):
    """Show the route's walkway lines in red, and the pin at its end.

    Every other walkway is already hidden in the template, so only the route's
    own lines are touched.
    """
    create_arrow_with_image(
        root, path_points[-2], path_points[-1], image_href=pin_image_data_uri()
    )
    path_group = walkway_group(root)
    if path_group is None:
        return
    lines = walkway_lines(path_group)
    for segment in route_segments(path_points):
        for position in lines_by_segment.get(segment, ()):
            lines[position].set("visibility", "visible")
            lines[position].set("stroke", "red")


def parse_svg_text(svg_file):

    tree = ET.parse(svg_file)
    root = tree.getroot()
    nsmap = {"svg": "http://www.w3.org/2000/svg"}

    text_data = []
    for text_element in root.findall(".//svg:text", namespaces=nsmap):
        transform = text_element.get("transform", "")
        if "translate" in transform:
            coords = transform.split("translate(")[1].split(")")[0].split()
            x1, y1 = float(coords[0]), float(coords[1])

            tspan = text_element.find("svg:tspan", namespaces=nsmap)
            if tspan is not None:
                text_content = tspan.text
                text_data.append((text_content, x1, y1))
    return text_data


def split_polylines(root):
    """Replace each walkway polyline by one <line> per segment, in place."""
    for polyline in root.xpath(".//svg:polyline", namespaces=NS):
        points = polyline.attrib.get("points", "").strip()
        if points:
            coords = [float(c) for c in points.replace(",", " ").split()]
            point_pairs = [(coords[i], coords[i + 1]) for i in range(0, len(coords), 2)]

            parent = polyline.getparent()
            if parent is None:
                raise ValueError("Parent element not found for the polyline")

            for i in range(len(point_pairs) - 1):
                x1, y1 = point_pairs[i]
                x2, y2 = point_pairs[i + 1]
                line = ET.Element(
                    f"{{{SVG_NAMESPACE}}}line",
                    {
                        "x1": str(x1),
                        "y1": str(y1),
                        "x2": str(x2),
                        "y2": str(y2),
                        "stroke": "black",
                        "stroke-width": "1",
                    },
                )
                parent.insert(parent.index(polyline), line)

            parent.remove(polyline)


def create_arrow_with_image(
    svg_root, start_point, end_point, arrow_id="dynamic-arrow", image_href=None
):

    nsmap = svg_root.nsmap
    svg_ns = nsmap.get(None, "http://www.w3.org/2000/svg")
    xlink_ns = nsmap.get("xlink", "http://www.w3.org/1999/xlink")

    defs = svg_root.find(f"{{{svg_ns}}}defs")
    if defs is None:
        defs = ET.SubElement(svg_root, f"{{{svg_ns}}}defs")

    def create_marker(
        defs, arrow_id, image_href=None, marker_width=10, marker_height=10
    ):

        marker = defs.find(f".//{{{svg_ns}}}marker[@id='{arrow_id}']")
        if marker is None:
            marker = ET.SubElement(
                defs,
                f"{{{svg_ns}}}marker",
                {
                    "id": arrow_id,
                    "markerWidth": str(marker_width),
                    "markerHeight": str(marker_height),
                    "refX": str(marker_width / 2),
                    "refY": str(marker_height),
                    "markerUnits": "strokeWidth",
                },
            )

            if image_href:
                ET.SubElement(
                    marker,
                    f"{{{svg_ns}}}image",
                    {
                        f"{{{xlink_ns}}}href": image_href,
                        "x": "0",
                        "y": "0",
                        "width": str(marker_width),
                        "height": str(marker_height),
                        "preserveAspectRatio": "xMidYMid meet",
                    },
                )
            else:
                ET.SubElement(
                    marker,
                    f"{{{svg_ns}}}path",
                    {"d": "M0,0 L0,10 L10,5 z", "fill": "red", "orient": "auto"},
                )

        return marker

    create_marker(
        defs,
        arrow_id,
        image_href=image_href,
        marker_width=20,
        marker_height=20,
    )

    x1, y1 = start_point
    x2, y2 = end_point

    arrow_line = ET.Element(
        f"{{{svg_ns}}}line",
        {
            "x1": str(x1),
            "y1": str(y1),
            "x2": str(x2),
            "y2": str(y2),
            "stroke": "red",
            "stroke-width": "0.8",
            "marker-end": f"url(#{arrow_id})",
        },
    )

    svg_root.append(arrow_line)


def floor_svg(val: int | str, floor_map: str | Path):
    return os.path.join(floor_map, f"floor {val} copy path.svg")


def route_legs(route: BuildingRoute) -> list[FloorPath]:
    if route["complexity"] == "simple":
        return [route["path"]]
    if route["complexity"] == "complex":
        return route["path"]
    return []


def render_route(
    floor_path: str | Path, route: BuildingRoute
) -> list[tuple[int, bytes]]:
    """Each floor of the route, drawn: [(floor, SVG bytes)] in walking order."""
    return [
        (floor, render_floor(floor_svg(floor, floor_path), points))
        for floor, points in route_legs(route)
    ]


def main(
    start: str, end: str, building: str | None, preference: str | None = None
) -> BuildingRoute:
    """Route within a building, with its maps drawn and stored (route["map_id"])."""
    db_path, floor_path = get_building_paths(building)

    route = db_shortest_path_maker.main(
        db_path=db_path,
        start_description=start,
        end_description=end,
        preference=preference,
    )
    # get_building_paths has refused a missing or unknown building already
    if route["complexity"] != "failed" and building is not None:
        maps = {
            (building, floor): svg for floor, svg in render_route(floor_path, route)
        }
        route["map_id"] = route_maps.store.save(maps)
    return route


def multi_building_nav(
    building_name1: str,
    start_1: str,
    building_name2: str,
    end_2: str,
) -> MultiBuildingRoute | FailedRoute:
    """Route between buildings; both buildings' maps are stored under one id."""
    db_path_1, floor_path_1 = get_building_paths(building_name=building_name1)
    db_path_2, floor_path_2 = get_building_paths(building_name=building_name2)

    route = db_shortest_path_maker.multi_building_nav(
        db_path_1=db_path_1,
        building_name1=building_name1,
        start_1=start_1,
        db_path_2=db_path_2,
        building_name2=building_name2,
        end_2=end_2,
    )
    if route["complexity"] == "failed":
        return route

    maps: route_maps.FloorMaps = {}
    for building, floor_path in (
        (building_name1, floor_path_1),
        (building_name2, floor_path_2),
    ):
        for floor, svg in render_route(floor_path, route["path"][building]):
            maps[(building, floor)] = svg
    route_id = route_maps.store.save(maps)
    for leg in route["path"].values():
        if leg["complexity"] != "failed":
            leg["map_id"] = route_id
    return route


if __name__ == "__main__":
    print(multi_building_nav("AB-01", "303", "Lab-Complex", "201"))
    print()
