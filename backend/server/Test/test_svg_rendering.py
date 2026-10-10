import itertools

import pytest
from lxml import etree as ET

from Utils import db_shortest_path_maker
from Utils import svg_manipulator as svg
from Utils.building_handling import get_building_paths
from Utils.route_types import ComplexRoute, SimpleRoute
from Utils.svg_manipulator import render_floor

SVG_NS = {"svg": "http://www.w3.org/2000/svg"}

# Mirrors the real floor maps: walkway polylines directly in <g id="Path">, more
# inside a nested <g>, and a decorative <path>, all styled by a hiding class.
FLOOR_SVG = """<?xml version="1.0" encoding="utf-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     viewBox="0 0 500 500">
  <defs><style>.cls-11{fill:none;stroke:#000;}</style></defs>
  <g id="Rooms"><rect x="0" y="0" width="10" height="10"/></g>
  <g id="Path" class="cls-11">
    <polyline class="cls-11" points="0 0 10 0 20 0"/>
    <polyline class="cls-11" points="20 0 20 10"/>
    <polyline class="cls-11" points="0 0 20 10"/>
    <polyline class="cls-11" points="50 50 60 50"/>
    <g>
      <polyline class="cls-11" points="20 10 30 10"/>
      <polyline class="cls-11" points="70 70 80 80"/>
    </g>
    <path class="cls-11" d="M100,100 L120,120"/>
  </g>
</svg>
"""

# Walks 0,0 -> 10,0 -> 20,0 -> 20,10 -> 30,10 (the last hop is in the nested <g>)
ROUTE_POINTS = [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0), (20.0, 10.0), (30.0, 10.0)]


def render(tmp_path, points=ROUTE_POINTS):
    floor = tmp_path / "floor 1 copy path.svg"
    if not floor.exists():
        floor.write_text(FLOOR_SVG, encoding="utf-8")
    # Drawn in memory: no output SVG file
    return ET.fromstring(render_floor(str(floor), points))


def path_lines(root):
    return root.xpath("//svg:g[@id='Path']//svg:line", namespaces=SVG_NS)


def segment(line):
    return (
        (float(line.get("x1")), float(line.get("y1"))),
        (float(line.get("x2")), float(line.get("y2"))),
    )


def test_route_segments_are_red_and_everything_else_hidden(tmp_path):
    root = render(tmp_path)
    visible = {
        segment(line)
        for line in path_lines(root)
        if line.get("visibility") == "visible"
    }
    assert visible == {
        ((0.0, 0.0), (10.0, 0.0)),
        ((10.0, 0.0), (20.0, 0.0)),
        ((20.0, 0.0), (20.0, 10.0)),
        ((20.0, 10.0), (30.0, 10.0)),
    }
    for line in path_lines(root):
        if line.get("visibility") == "visible":
            assert line.get("stroke") == "red"


def test_nested_group_lines_are_processed(tmp_path):
    root = render(tmp_path)
    nested = root.xpath("//svg:g[@id='Path']/svg:g//svg:line", namespaces=SVG_NS)
    assert nested, "nested polylines should be converted to lines"
    # Every walkway line gets an explicit visibility; none is left black and visible
    assert all(line.get("visibility") in ("visible", "hidden") for line in nested)
    off_route = [
        line for line in nested if segment(line) == ((70.0, 70.0), (80.0, 80.0))
    ]
    assert off_route[0].get("visibility") == "hidden"


def test_shortcut_between_non_consecutive_route_points_is_not_drawn(tmp_path):
    root = render(tmp_path)
    # (0,0) and (20,10) are both on the route, but not one after the other
    shortcut = [
        line for line in path_lines(root) if segment(line) == ((0.0, 0.0), (20.0, 10.0))
    ]
    assert shortcut[0].get("visibility") == "hidden"


def test_decorative_paths_and_classes_are_hidden(tmp_path):
    root = render(tmp_path)
    for element in root.xpath("//svg:g[@id='Path']//svg:path", namespaces=SVG_NS):
        assert element.get("visibility") == "hidden"
    assert root.xpath("//svg:g[@id='Path']/@class", namespaces=SVG_NS) == []


def test_destination_pin_is_embedded_not_a_file_path(tmp_path):
    root = render(tmp_path)
    images = root.xpath(
        "//svg:marker[@id='dynamic-arrow']/svg:image", namespaces=SVG_NS
    )
    href = images[0].get("{http://www.w3.org/1999/xlink}href")
    assert href.startswith("data:image/png;base64,")
    # No marker that does not exist in the document is referenced
    referenced = {
        line.get("marker-end") for line in root.iter() if line.get("marker-end")
    }
    assert referenced == {"url(#dynamic-arrow)"}


def test_rendering_twice_starts_from_the_clean_floor_map(tmp_path):
    render(tmp_path)
    root = render(tmp_path, points=[(50.0, 50.0), (60.0, 50.0)])
    visible = {
        segment(line)
        for line in path_lines(root)
        if line.get("visibility") == "visible"
    }
    assert visible == {((50.0, 50.0), (60.0, 50.0))}


# ============================================================
# MULTI-BUILDING DIRECTION
# ============================================================


def test_destination_building_leg_runs_from_entrance_to_room(monkeypatch):
    start_leg: SimpleRoute = {
        "path": (1, [(1.0, 1.0), (2.0, 2.0)]),  # room -> exit
        "total_weight": 1.0,
        "complexity": "simple",
        "error": None,
    }
    # The engine computes the destination leg from the room to the entrance
    end_leg: ComplexRoute = {
        "path": [(2, [(9.0, 9.0), (8.0, 8.0)]), (0, [(7.0, 7.0), (6.0, 6.0)])],
        "total_weight": 2.0,
        "complexity": "complex",
        "error": None,
    }
    legs = {"db1": start_leg, "db2": end_leg}
    monkeypatch.setattr(
        db_shortest_path_maker,
        "get_building_entrance_exit",
        lambda db_path, building, reference: ((0.0, 0.0), legs[db_path]),
    )

    route = db_shortest_path_maker.multi_building_nav(
        "db1", "AB-01", "101", "db2", "Lab-Complex", "205"
    )
    assert route["complexity"] == "multi-building"
    # Start building unchanged: room -> exit
    assert route["path"]["AB-01"] == start_leg
    # Destination building reversed: entrance (floor 0) -> room (floor 2)
    assert route["path"]["Lab-Complex"]["path"] == [
        (0, [(6.0, 6.0), (7.0, 7.0)]),
        (2, [(8.0, 8.0), (9.0, 9.0)]),
    ]


# ============================================================
# SAME MAPS AS THE ORIGINAL PER-LINE DRAWING
# ============================================================


def reference_drawing(svg_file, points) -> bytes:
    """The original algorithm: style every walkway line on every request."""
    root = ET.parse(svg_file, ET.XMLParser(remove_blank_text=True)).getroot()
    svg.split_polylines(root)
    svg.create_arrow_with_image(
        root, points[-2], points[-1], image_href=svg.pin_image_data_uri()
    )
    group = root.find(".//svg:g[@id='Path']", namespaces=SVG_NS)
    if group is not None:
        segments = svg.route_segments(points)
        for element in group.iter():
            if isinstance(element.tag, str) and "class" in element.attrib:
                del element.attrib["class"]
        for line in group.xpath(".//svg:line", namespaces=SVG_NS):
            start = svg.point_key(line.get("x1"), line.get("y1"))
            end = svg.point_key(line.get("x2"), line.get("y2"))
            if (start, end) in segments:
                line.set("visibility", "visible")
                line.set("stroke", "red")
            else:
                line.set("visibility", "hidden")
        for shape in group.xpath(".//svg:path", namespaces=SVG_NS):
            shape.set("visibility", "hidden")
    # Serialised exactly as render_floor does, so only the drawing is compared
    return ET.tostring(
        root.getroottree(), pretty_print=True, xml_declaration=True, encoding="utf-8"
    )


def canonical(svg: bytes) -> bytes:
    root = ET.fromstring(svg, ET.XMLParser(remove_blank_text=True))
    return ET.tostring(root, method="c14n")


def first_difference(drawn: bytes, expected: bytes) -> str | None:
    """Where two SVGs first differ, or None. Cheap: comparing the raw bytes would
    make pytest diff two ~200 KB lines, which can run for a very long time."""
    if drawn == expected:
        return None
    a, b = ET.fromstring(drawn), ET.fromstring(expected)
    tree = a.getroottree()
    for left, right in itertools.zip_longest(a.iter(), b.iter()):
        if left is None or right is None:
            return f"one SVG has extra elements (drawn ended: {left is None})"
        where = f"at {tree.getpath(left)}"
        if left.tag != right.tag or dict(left.attrib) != dict(right.attrib):
            return (
                f"{where}: drawn {left.tag} {dict(left.attrib)} "
                f"!= expected {right.tag} {dict(right.attrib)}"
            )
        for part in ("text", "tail"):
            if getattr(left, part) != getattr(right, part):
                return (
                    f"{where}: {part} {getattr(left, part)!r} "
                    f"!= expected {getattr(right, part)!r}"
                )
        # Prefix names don't matter: lxml invents one (ns0, ns40, ...) when the
        # pin's xlink:href is added to a map that doesn't declare xlink
        if set(left.nsmap.values()) != set(right.nsmap.values()):
            return f"{where}: namespaces {left.nsmap} != expected {right.nsmap}"
    # Same elements, attributes (compared by namespace URI), text and namespaces
    return None


@pytest.mark.parametrize(
    ("building", "start", "end"),
    [
        ("AB-01", "101", "105"),
        ("AB-01", "105", "201"),
        ("AB-01", "501", "515"),
        ("AB-01", "301", "female toilet"),
        ("Lab-Complex", "101", "201"),
    ],
)
def test_drawing_matches_the_original_algorithm(building, start, end):
    db_path, floor_path = get_building_paths(building)
    route = db_shortest_path_maker.main(db_path, start, end)
    assert route["complexity"] != "failed", route
    for (floor, points), (_, drawn) in zip(
        svg.route_legs(route),
        svg.render_route(floor_path, route),
        strict=True,
    ):
        source = svg.floor_svg(floor, floor_path)
        # Both through the same canonical form: C14N sorts attributes
        difference = first_difference(
            canonical(drawn), canonical(reference_drawing(source, points))
        )
        assert difference is None, f"floor {floor}: {difference}"
