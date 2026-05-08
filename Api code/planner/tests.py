import json
from unittest.mock import patch

from django.test import Client, TestCase, override_settings

from .services import Coordinate, FuelStation, _choose_stops, _haversine_miles


class RoutePlanViewTests(TestCase):
    def test_requires_start_and_finish(self):
        response = Client().post(
            "/api/route-plan/",
            data=json.dumps({"start": "Chicago, IL"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("finish", response.json()["error"])

    @patch("planner.views.build_route_plan")
    def test_returns_route_plan(self, build_route_plan):
        build_route_plan.return_value = {"ok": True}

        response = Client().post(
            "/api/route-plan/",
            data=json.dumps({"start": "Chicago, IL", "finish": "Denver, CO"}),
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True})
        build_route_plan.assert_called_once_with("Chicago, IL", "Denver, CO")


class FuelSelectionTests(TestCase):
    def test_haversine_distance_between_known_points(self):
        chicago = Coordinate(lat=41.8781, lon=-87.6298)
        denver = Coordinate(lat=39.7392, lon=-104.9903)

        self.assertAlmostEqual(_haversine_miles(chicago, denver), 919, delta=10)

    @override_settings(VEHICLE_RANGE_MILES=500)
    def test_choose_stops_adds_stop_for_long_route(self):
        route = [
            Coordinate(lat=41.8781, lon=-87.6298),
            Coordinate(lat=39.7392, lon=-104.9903),
        ]
        candidates = [
            FuelStation(
                opis_id="1",
                name="Cheap Stop",
                address="Example",
                city="Omaha",
                state="NE",
                rack_id="1",
                price=3.10,
                coordinate=Coordinate(lat=41.2565, lon=-95.9345),
            )
        ]

        stops = _choose_stops(candidates, route, distance_miles=920)

        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].name, "Cheap Stop")

# Create your tests here.
