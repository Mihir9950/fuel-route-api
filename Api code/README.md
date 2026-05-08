# Fuel Route API

Django API for the backend assessment in `Remote Backend Django Engineer - AI & Algorithmic Systems.pdf`.

## What It Does

`POST /api/route-plan/` accepts a USA start and finish location, requests a driving route from OSRM, chooses fuel stops from the supplied OPIS CSV, and returns:

- Route distance, duration, GeoJSON geometry, and an OpenStreetMap URL.
- Suggested fuel stops along the route.
- Estimated gallons and total fuel spend for a 500 mile range vehicle at 10 MPG.

Station geocoding uses Nominatim and is cached in `data/geocode-cache.sqlite3`, so repeated calls avoid unnecessary free API traffic.

## Run Locally

```powershell
py manage.py migrate
py manage.py runserver
```

Then send:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/route-plan/ `
  -ContentType 'application/json' `
  -Body '{"start":"Chicago, IL","finish":"Denver, CO"}'
```

## Example Request

```json
{
  "start": "Chicago, IL",
  "finish": "Denver, CO"
}
```

## Notes

- Free providers used: Nominatim for geocoding and OSRM for routing.
- The supplied fuel CSV does not include latitude/longitude, so station locations are geocoded and cached.
- `GEOCODE_STATION_LIMIT` controls how many low-priced stations are considered per request.
