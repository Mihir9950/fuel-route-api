import json

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from requests import RequestException

from .services import RoutePlanningError, build_route_plan


@csrf_exempt
@require_POST
def route_plan(request):
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": "Request body must be valid JSON."}, status=400)

    start = str(payload.get("start", "")).strip()
    finish = str(payload.get("finish", "")).strip()
    if not start or not finish:
        return JsonResponse(
            {"error": "Both 'start' and 'finish' are required."}, status=400
        )

    try:
        return JsonResponse(build_route_plan(start, finish), status=200)
    except RoutePlanningError as exc:
        return JsonResponse({"error": str(exc)}, status=422)
    except RequestException as exc:
        return JsonResponse(
            {"error": "Routing or geocoding provider request failed.", "detail": str(exc)},
            status=502,
        )
