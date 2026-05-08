from django.urls import path

from .views import route_plan

urlpatterns = [
    path('route-plan/', route_plan, name='route-plan'),
]
