from django.urls import include, path

from . import views

app_name = "ephios_shift_coordination"
planning_patterns = [
    path("planning/", views.period_list, name="period_list"),
    path("planning/new/", views.period_create, name="period_create"),
    path("planning/<int:pk>/", views.period_detail, name="period_detail"),
    path("planning/<int:pk>/open/", views.survey_open, name="survey_open"),
    path("surveys/<int:pk>/", views.survey_detail, name="survey_detail"),
    path("surveys/", views.survey_list, name="survey_list"),
    path("settings/", views.configuration, name="settings"),
    path("templates/", views.template_list, name="template_list"),
    path("templates/new/", views.template_edit, name="template_create"),
    path("templates/<int:pk>/", views.template_edit, name="template_edit"),
]

urlpatterns = [path("shift-coordination/", include(planning_patterns))]
