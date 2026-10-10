from lxml import etree as ET

from Utils import db_shortest_path_maker
from Utils.route_types import ComplexRoute, SimpleRoute
from Utils.svg_manipulator import modify_svg

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
    floor.write_text(FLOOR_SVG, encoding="utf-8")
    output = tmp_path / "floor 1 shortest path.svg"
    modify_svg(str(floor), str(output), points)
    return ET.parse(str(output)).getroot()


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
