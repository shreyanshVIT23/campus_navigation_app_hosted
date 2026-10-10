import base64
import functools
import itertools
import os
from pathlib import Path

from lxml import etree as ET

from . import db_shortest_path_maker, loader
from .building_handling import get_building_paths
from .route_types import BuildingRoute, FailedRoute, MultiBuildingRoute

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


def modify_svg(svg_file, output_file, path_points):

    break_polyline_to_lines(svg_file, output_file)

    tree = ET.parse(output_file)
    root = tree.getroot()

    create_arrow_with_image(
        root, path_points[-2], path_points[-1], image_href=pin_image_data_uri()
    )

    path_group = root.find(".//svg:g[@id='Path']", namespaces=NS)
    if path_group is not None:
        segments = route_segments(path_points)
        # The hiding class sits on the group and on walkways at every depth
        for element in path_group.iter():
            if isinstance(element.tag, str) and "class" in element.attrib:
                del element.attrib["class"]
        for line in path_group.xpath(".//svg:line", namespaces=NS):
            modify_line(line, segments)
        # Curved/decorative walkway shapes are not part of the routing graph
        for shape in path_group.xpath(".//svg:path", namespaces=NS):
            shape.set("visibility", "hidden")

    with open(output_file, "wb") as f:
        tree.write(f, pretty_print=True, xml_declaration=True, encoding="utf-8")

    return output_file


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


def break_polyline_to_lines(svg_file, output_file):

    parser = ET.XMLParser(remove_blank_text=True)
    tree = ET.parse(svg_file, parser)
    root = tree.getroot()
    namespace = {"svg": "http://www.w3.org/2000/svg"}

    for polyline in root.xpath(".//svg:polyline", namespaces=namespace):
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

    tree.write(output_file, pretty_print=True, xml_declaration=True, encoding="utf-8")


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


def modify_line(line, segments: set[Segment]):
    """Show a walkway line in red if it is one hop of the route; hide it otherwise."""
    try:
        start = point_key(line.get("x1"), line.get("y1"))
        end = point_key(line.get("x2"), line.get("y2"))
    except (ValueError, TypeError) as e:
        raise ValueError(
            "Invalid line coordinates: 'x1', 'y1', 'x2', and 'y2' must be valid numbers."
        ) from e

    if (start, end) in segments:
        line.set("visibility", "visible")
        line.set("stroke", "red")
    else:
        line.set("visibility", "hidden")


def output(val: int | str, output_map: str):
    return os.path.join(output_map, f"floor {val} shortest path.svg")


def floor_svg(val: int | str, floor_map: str):
    return os.path.join(floor_map, f"floor {val} copy path.svg")


def main(
    start: str, end: str, building: str | None, preference: str | None = None
) -> BuildingRoute:

    db_path, floor_path, output_path = get_building_paths(building)

    shortest_path = db_shortest_path_maker.main(
        db_path=db_path,
        start_description=start,
        end_description=end,
        preference=preference,
    )

    result_based_update(floor_path, output_path, shortest_path)
    return shortest_path


def result_based_update(floor_path, output_path, shortest_path: BuildingRoute):
    """Draw the route onto the floor SVGs; returns the written SVG paths."""
    svg_updates: list[str] = []

    if shortest_path["complexity"] == "complex":
        for floor_no, points in shortest_path["path"]:
            svg_updates.append(
                modify_svg(
                    floor_svg(floor_no, floor_path),
                    output(floor_no, output_path),
                    points,
                )
            )
    elif shortest_path["complexity"] == "simple":
        floor_no, points = shortest_path["path"]
        svg_updates.append(
            modify_svg(
                floor_svg(floor_no, floor_path), output(floor_no, output_path), points
            )
        )
    return svg_updates


def multi_building_nav(
    building_name1: str,
    start_1: str,
    building_name2: str,
    end_2: str,
) -> MultiBuildingRoute | FailedRoute:
    db_path_1, floor_path_1, output_path_1 = get_building_paths(
        building_name=building_name1
    )
    db_path_2, floor_path_2, output_path_2 = get_building_paths(
        building_name=building_name2
    )

    shortest_path = db_shortest_path_maker.multi_building_nav(
        db_path_1=db_path_1,
        building_name1=building_name1,
        start_1=start_1,
        db_path_2=db_path_2,
        building_name2=building_name2,
        end_2=end_2,
    )

    if shortest_path["complexity"] == "failed":
        return shortest_path

    result_based_update(
        floor_path=floor_path_1,
        output_path=output_path_1,
        shortest_path=shortest_path["path"][building_name1],
    )
    result_based_update(
        floor_path=floor_path_2,
        output_path=output_path_2,
        shortest_path=shortest_path["path"][building_name2],
    )
    return shortest_path


if __name__ == "__main__":
    print(multi_building_nav("AB-01", "303", "Lab-Complex", "201"))
    print()
